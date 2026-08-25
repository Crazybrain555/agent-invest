---
id: disclosure_anchor_worker_dynamic_scheduling
title: Worker 动态调度、GPU 锯齿根因与发布验收
date: 2026-07-09
updated_at: 2026-07-26
status: continuous-resident-candidate-pending-cutover
authority: tracked implementation design; live runtime values must be re-verified
---

> 本文记录可复核的事故证据、版本匹配的外部机制和当前实现边界。基础修复 commit
> `dcf7014` 与 readiness/cutover 修正 `15994df` 已于 2026-07-25 重启上线，生产 A/B
> 证实单次 readiness 失败可在原 dispatcher 内恢复；2026-07-26 的后续 A/B 又确认，
> parse admission 仍被一小时 acquisition/report round 截断。当前候选把生产 parse
> 改为生命周期常驻、把报告改为只读快照；尚未完成本次受控重启和新 A/B。因此“代码
> 覆盖”不等于发布验收完成。
> GPU、backlog、版本和并发数字都是带日期的运维证据，每次发布都必须重新核验。

# Worker 动态调度与 GPU 锯齿根因报告

## 1. 结论先行

本次归因与“GPU 任务不够”不同，也与“每次 MinerU 失败都应该砍文档并发”不同。

| 层级 | 结论 | 证据与影响 |
|---|---|---|
| **第一根因** | 控制单位错位：外层限制 16 份文档，内层每份临时 MinerU API 原可并发 100 个 page/block 请求 | 旧线上命令没有 `--max-concurrency`；理论上一个 worker 可放大到约 1,600 个请求，现场 vLLM running 达 127 时仍有 waiting |
| **直接波形机制** | 请求洪峰把 vLLM running 推到调度平台并积出 waiting；文档随后成组进入本地 finalize，GPU 供给骤降 | 83 秒短窗内从 `1/0 → 127/151 → 回落`；16:34 的独立横截面仍为 `121/40` |
| **放大器** | 文档完成级 AIMD 的反馈延迟为分钟到小时，失败砍半、成功慢升，把少量 item/健康抖动放大成低并发长谷 | 历史报告曾降到 `parse_concurrency_limit=1`；而普通大文档本身需 20–60 分钟，反馈控制不到秒级请求洪峰 |
| **第二根因** | 固定 200 份候选的批尾排空，以及 parse 与 build/publish 共占同一文档槽 | 连续四波都恰好 200 starts，批尾分别有 6/3/7/10 分钟低于 8 个在途且没有新 start |
| **2026-07-26 残余根因** | resident parse 仍把 `WORKER_ACQUISITION_SECONDS=3600` 同时当成 admission deadline 和 report/round 边界；到期后只允许 regular filler | 最近 245 个分钟边界有 69 个低于 16；同时 backlog 约 20,538，候选前 1,000 为 `regular=0/heavy=460/huge=540`，所以时窗一关只能排空长尾 |
| **不是根因** | 不是 backlog 不足，也不能仅凭 GPU 图的空谷判定服务宕机 | 11:53 仍有 19,878 个 eligible pending；同一观测窗 `/health` 20/20 为 200 |

一句话因果链：

```text
16 个文档槽
  × 每份 MinerU 默认 100 个远端请求
  → vLLM running 达 127 时仍积出 waiting
  → 一批文档同步转入本地 finalize / 超大任务长占槽
  → GPU 请求供给下降
  → 文档级 AIMD 在很久以后才误判并砍槽
  → 固定 200 批尾继续排空
  → “过排队—空谷—再突发”的锯齿
```

前两轮修复已收住请求洪峰、拆开 finalize 并删除文档级 AIMD，但旧 round 生命周期仍留下
一条独立锯齿链：

```text
一小时 acquisition/report deadline 到期
  → resident 停止 heavy/huge admission
  → 当前候选恰好没有 regular
  → 16 个不可抢占的 whole-PDF future 逐个完成而不补槽
  → 报告写完、下一 round 才重新准入
  → GPU 到达流量形成长尾空谷
```

目标不是把瞬时 GPU 利用率强行画成 100% 直线，而是在结果语义不变的前提下同时降低：

- vLLM waiting、queue time、KV/preemption 和 overload；
- 有 eligible backlog 时的 parse 空槽和批尾时间；
- 500+ 页任务对普通公告的队头阻塞；
- 重启遗留子进程、临时目录和无意义重试。

## 2. 可复核现场证据

时间窗、source identity、`finished_at` SQL、vLLM 采集和 active IR 扫描命令见
[最小证据包](../audits/gpu-scheduling-closed-loop-2026-07-25/README.md)。

### 2.1 最近数小时的生产波形

2026-07-25 05:45–11:45 的只读采样：

- 959 次 parse start、946 次完成；
- 末三小时文档在途均值 7.74；10:05–11:36 连续 92 分钟低于 8；
- 四个连续波均恰好 200 个 start；每波尾部低于 8 个在途且零新 start，持续
  6、3、7、10 分钟；
- 11:53 仍有 19,878 个 eligible pending，排除“没有任务”；
- 500+ 页完成尝试 16 次、失败 9 次；10 次 invocation failure 合计浪费
  359.3 个文档槽分钟。

2026-07-25 的 vLLM 采样：

- 11:49:27–11:50:50 的约 83 秒短窗从 `running=1, waiting=0` 升至
  `running=127, waiting=151` 后回落；
- 同窗 20 次 health 全部 HTTP 200。health 健康只说明服务能应答，不说明队列没有过载。
- 16:34:20 的独立横截面为 `running=121, waiting=40`、preemptions 0；
  `/version` 返回 vLLM 0.21.0。

因此 GPU 图中的低谷和高峰是**到达流量不平滑**；高峰期并非“不饱和”，而是 worker 自己
过度排队。`max_num_seqs` 是 vLLM 每个 scheduler iteration 可处理的最大 sequence 数，不是
HTTP admission 或 waiting queue 上限。该端点没有直接暴露 `max_num_seqs`；128 是本系统
待发布复核的运营配置，不能写成这段短窗直接证明的事实。

### 2.2 统计口径修正

历史第一次诊断曾用 `processing_run.created_at` 近似完成时间，从而夸大 22:27–22:39 的
“零完成”谷。完成时间必须用 `finished_at`。修正后，当时仍有约 12–16 个文档 run；
但 2026-07-25 新采样又确认固定 200 波的批尾确实可持续 3–10 分钟。

这两个结论并不冲突：

- “旧 12 分钟全空”是错误时间字段造成的夸大；
- “固定候选批尾与 finalize 占槽”是后来用 start、真实在途和 backlog 共同确认的次级根因。

### 2.3 版本匹配的 MinerU 请求模型

本机实际 MinerU 3.4.0、`mineru-vl-utils` 1.0.5；远端 OpenAI-compatible 端点在
2026-07-25 报告 vLLM 0.21.0。对已安装源码和官方 release 源码逐项核对后：

- 没有 `--api-url` 时，每份 PDF 的 CLI 会启动一个临时 local `mineru-api`；
- MinerU 3.4 的 `--max-concurrency=N` 会传入 `vlm-http-client`；
- 顶层 `mineru --help` 不列这个动态 backend 参数，但 3.4.0 的
  `parse_unknown_args()` 会把它规范化为 `{"max_concurrency": N}`，随后
  `split_service_and_model_config()` 将它保留在 model config；
- 对正常单 PDF，layout、block/content 和跨页表格 merge 阶段的远端 VLM chat 都受该
  临时 API 的同尺寸 semaphore 约束；阶段并不重叠；
- macOS 日志中的 `Request concurrency limited to 1` 是临时 API 的**文档任务入口**
  semaphore；本系统每个临时 API 只提交一份 PDF。它不覆盖 VLM HTTP client 的页/block
  semaphore。线上异常栈直接显示后者 `value:7`，两者不能混为同一个并发层；
- 每个临时 API 的 semaphore 互相不可见，所以 16 份文档的默认 100 不能形成全局 100，
  而会形成约 `16 × 100` 的外层放大；
- processing window 是 MinerU 内部执行窗口，不是 durable checkpoint，也不能证明任意
  页段可独立解析后无损拼回。

官方入口说明见 [MinerU quick usage](https://github.com/opendatalab/MinerU/blob/mineru-3.4.0-released/docs/en/usage/quick_usage.md)；
历史上 `max_concurrency` 没有正确透传也确实是社区遇到过并由维护者修复的问题，见
[MinerU #3654](https://github.com/opendatalab/MinerU/issues/3654)。本系统使用 3.4
官方参数透传，不维护 MinerU 私有 fork。

截至 2026-07-25，最新稳定版为
[MinerU 3.4.4](https://github.com/opendatalab/MinerU/releases/tag/mineru-3.4.4-released)。
3.4.0→3.4.4 的 `vlm_analyze.py`、`fast_api.py`、`router.py` 三个调度关键 blob 相同；
升级可获得 PDF/DOCX 等正确性修复，但不会改变上述 semaphore/router 粒度，不能冒充锯齿修复。
最新 4.0.0 alpha 虽增加页范围缓存和任务队列，仍在整 job 完成后写结果，没有跨文档 GPU
request broker 或内部 window 失败续跑 checkpoint；alpha 不进入本轮生产 cutover。

### 2.4 2026-07-26：为什么前一轮修复后仍有锯齿

17:30–21:35 对 `processing_run.started_at/finished_at` 做分钟边界重放：

- 245 个分钟边界中，176 个达到 16 份文档在途，69 个低于 16（28.16%）；
- 46 个边界不高于 8、40 个不高于 4、30 个不高于 1；
- 平均文档在途 12.861；按当前 `16×7` 请求包络粗算，理论供给利用率只有 80.38%；
- 同窗 169 成功、1 失败，不能用失败潮解释 17:38–17:50、19:30–20:04、
  21:15 之后的长尾；
- admission 打开时，文档完成到下一份 start 的 p50 仅 0.77 秒，排除 PostgreSQL dequeue
  或普通补槽本身过慢。

代码与实时队列的交叉证据给出唯一能同时解释这些事实的机制：

1. `run_once()` 把生产默认 3,600 秒的 `limits.acquisition_seconds` 复用成 parse
   `keep_refilling` deadline；
2. deadline 之后只允许 regular filler，等待已有 heavy/huge future 排空后才能返回、写报告；
3. 同时刻 eligible backlog 约 20,538，`pending_parse` 前 1,000 份成本重放为
   `regular=0 / heavy=460 / huge=540`。

因此这是**有任务但调度器主动关闭准入**，不是 GPU 必须 100% 才算健康，也不是提高并发能
解决的问题。固定时窗对不可抢占、耗时高方差的 whole-PDF future 必然制造 drain barrier；
只要报告仍要求先排空，窗口改成两小时或“到期后多放几个 heavy”都只会改变锯齿周期。

## 3. 成熟项目采用的共同原则

本次不是复制某个框架，而是把版本匹配、可证实的调度不变量落到现有 PostgreSQL 队列和
单机 resident worker。

| 成熟实现 | 官方机制 | 本系统采用的原则 | 没有照搬的部分 |
|---|---|---|---|
| [MinerU](https://github.com/opendatalab/MinerU/blob/mineru-3.4.0-released/docs/en/usage/quick_usage.md) | 临时 API、显式 `max_concurrency`、整本文档输出 | 在官方请求入口设硬上限；保留 whole-PDF 语义 | 不 patch MinerU 内部，不把 processing window 当可恢复外部分片 |
| [vLLM](https://docs.vllm.ai/en/stable/) / [SchedulerConfig](https://docs.vllm.ai/en/stable/api/vllm/config/scheduler/) | continuous batching；`max_num_seqs` 限每轮处理 sequence 数 | 客户端平滑、有界地供给，动态合批交给 vLLM | 不把 waiting queue 当 admission control，不靠提交海量请求“喂满”GPU |
| [Docling pipeline](https://docling-project.github.io/docling/reference/pipeline_options/) | OCR/layout/table 分阶段 batch；bounded queue 满时上游阻塞；`document_timeout=None` 默认不强杀 | 页/阶段窗口限制工作集，总时长只是可选业务策略 | 不把 Docling 的 partial-success timeout 套到 MinerU 原子整本产物 |
| [Celery optimization](https://docs.celeryq.dev/en/latest/userguide/optimizing.html) | 长短任务使用分别配置的 worker/queue；长任务 prefetch multiplier 取 1；resident consumer 持续取任务 | 大小 lane；执行槽空一个才取一个；监控/事件周期不结束 consumer 生命周期 | 不引入 Redis/RabbitMQ 或第二份任务真相 |
| [Kueue quota borrowing](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/) | nominal quota 保证份额，空闲 quota 可由 cohort 借用 | regular/heavy/huge 各有名义份额，lane 空时 work-conserving 借满 | 不引入 Kubernetes 控制面 |
| [Temporal heartbeat/fairness](https://docs.temporal.io/encyclopedia/detecting-activity-failures) | 长 Activity 用短 heartbeat timeout 证明存活；Start-To-Close 必须长于最大可能耗时；weighted band 空时可借满 | 正常长任务持续续 heartbeat；耗时预算与 liveness 分开 | MinerU 无页级 checkpoint，不能伪造可恢复进度 payload |
| [NVIDIA Triton](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html) | 单一服务端队列、dynamic batching、queue policy 与 priority | 真正动态容量应位于共享 GPU 请求入口 | 当前 vLLM/MinerU 模型协议不是 Triton drop-in，不替换生产推理栈 |
| [Ray Serve](https://docs.ray.io/en/latest/serve/advanced-guides/asyncio-best-practices.html) | `max_ongoing_requests` 以 replica 为静态 in-flight cap，超出后排队/背压；周期指标不要求 replica 排空 | GPU 请求预算与文档槽分开；报告是所有权转移的周期快照，不是 drain barrier | 不为单机 worker 引入 Ray replica/router/autoscaler |
| [Envoy circuit breaker](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking) | 分别限制 active、pending、retry，过载快速背压 | 只把明确 429/`RESOURCE_EXHAUSTED` 当全局 overload；未来多生产者时在统一入口设静态 breaker | 当前单生产者已有数据库排他锁，不新增网络 gateway |
| [Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) | readiness 默认连续失败 3 次才转 not-ready；失败后继续探测，恢复后重新准入 | 探测不确定时暂停新 admission；单次失败不结束整轮，连续失败达到阈值才进入现有 cooldown | 不把 readiness 当 liveness，不因一次网络抖动重启进程 |

共同结论是：

1. **安全容量用静态 admission/circuit breaker 表达**，不由高方差任务的完成时延猜测；
2. **异构公平用 lane/queue 表达**，长任务有份额但不能霸占全部执行槽；
3. **借用必须 work-conserving**，某 lane 空闲时不能为“预留”而让 GPU 空着；
4. **下游必须有界**，否则只把 GPU 队列搬到内存或本地 finalize 队列；
5. **服务端 continuous batching 负责动态组批**，客户端负责有界、平滑供给。
6. **总耗时与失活必须分开**：成本模型只告警/排队，heartbeat/lease 证明所有权，
   只有远高于已知正常包络的 runaway guard 才能终止。
7. **观测周期与执行生命周期必须分开**：resident consumer/replica 持续工作，报告通过
   快照或事件流产生；为了写一份报告而排空不可抢占任务，会把观测机制变成流量整形器。

## 4. 针对本系统的动态调度闭环

### 4.1 GPU 请求层：固定 API 的 21/128 静态安全包络

生产配置的文档槽仍为 16，但请求安全边界独立配置：

```text
WORKER_GPU_MAX_SEQUENCES=128
WORKER_GPU_REQUEST_BUDGET=21
WORKER_PARSE_CONCURRENCY=16
固定 MinerU API 文档 task slots = 3
每个 active task 的 service-side VLM max-concurrency = 7
worker 正常稳态 active upper bound: 3 × 7 = 21 < 128
```

16 是 client 可同时提交的文档数，不是 active GPU 请求数；超过 3 个的文档在固定 API 队列等待。
该式只证明**单 persistent API identity 的配置上界**：

- `max_num_seqs=128` 仍须在发布时从远端启动配置或等价运营证据复核；当前 metrics
  端点不直接暴露它；
- 外部绕过本服务直连 GPU 的客户端不受此预算控制；
- API 没有 cancel endpoint；shutdown/retry 只能关闭新 admission、终止本地 CLI 后自然 drain，
  因此无法证明 queued=processing=0 时必须 fail closed，而不是重启制造状态丢失。

所有仓内入口使用同一个固定 API：

- resident worker；
- admin parse；
- pipeline `parse` / `process`。

admin/pipeline 在执行前还必须取得与 resident worker 相同的 PostgreSQL session advisory
lock；拿不到就 fail closed（admin 409 / CLI busy），从而当前单机部署不会同时出现两个仓内
生产者。resident 在每次补槽和最长 30 秒的等待边界复核同一 lock session；失锁先终止本进程
MinerU，再禁止新准入。它仍不是分布式 fencing。若未来需要多 worker 或允许其他服务直连
GPU，届时应把 21 的 active budget 上移到统一 gateway/Envoy/Ray 类 admission 层，而不是继续加本地
semaphore。

### 4.2 文档层：三 lane、名义份额与借用

物理页数只作成本代理，不改 PDF 内容：

| lane | 默认范围 | 三类都饱和、16 槽时的名义份额 |
|---|---:|---:|
| regular | `<80` 页 | 11 |
| heavy | `80–499` 页，或页数探测失败 | 4 |
| huge | `>=500` 页，或归档实测字节超过兼容阈值 | 1 |

候选按 `document_id` 取前 1,000。实盘快照中该前缀同时含 regular/heavy，但这是观测，
不是全 backlog 的保证；名义份额与借用只在当前候选窗口内成立。每次选取遵守：

- CNInfo `F005N`/`adjunctSize` 混合单位，只保留为不透明 provider 签名提示；成本读取下载
  `source_access.result_snapshot.byte_count`，旧 `oversized` 键不再影响准入；
- lane 均有任务时，regular/heavy/huge 保留名义份额；
- 任一 lane 为空，其他 lane 立即借用其空槽；
- 同一 lane 内保留 DB 候选顺序；
- unknown 进入 heavy，避免未知成本冒充短公告；
- 不按 document id、发行人、标题或失败样本特判。

若 A/B 观测到候选窗口长期 `regular=0`，再把页数/成本持久化并改为 per-lane keyset
取数；当前真实前缀没有该问题，不为假设场景增加第二套查询控制面。

这就是本系统的“动态”：动态选择下一份 whole PDF，并在 lane 之间借用空闲文档槽；
GPU 请求总预算本身保持静态。必须诚实记录一个当前 MinerU 3.4 的能力缺口：

- 21 由固定 API 的 `3 task slots × 7 inference concurrency` 形成保守配置上界；
- 固定 API 共享文档任务 semaphore，但内层 `aio_batch_two_step_extract()` 仍为每个 active task
  创建私有 semaphore，因此它不是 GPU 请求级的全局 work-conserving token pool；
- 这正是 processing 必须固定不超过 3、8/16 阶段必须观察到 queue、vLLM 与 API 指标必须分开
  记录的原因；
- `mineru-router` 只按整任务负载在多个 API/GPU 间选 upstream，单 GPU 不提供内层请求借用。

所以静态 21 是当前待 commissioning 的保守边界，不应冒充最终共享 broker。真正尾部借满需要 MinerU
上游把 app-scoped semaphore 传到所有 VLM client 调用，或在统一 GPU 请求入口使用成熟的
有界、公平 admission gateway；不能靠再提高每文档并发或手写裸 semaphore 代理替代。

### 4.3 常驻、工作守恒的 parse coordinator

旧执行链把 `parse→build→publish` 全部算作一个 GPU 文档槽；第一轮修复虽拆开了 finalize，
resident 仍在 acquisition/report deadline 到期时停止 admission 并排空。当前候选把执行
生命周期与维护、报告生命周期彻底分开：

```text
主线程（唯一 singleton / admission owner）
  PostgreSQL pending_parse
        │  1000 候选、三 lane 名义份额 + 空槽借用
        ▼
  parse pool（16，完成一个立即补一个）
        │ parse 成功即释放 GPU 文档槽
        ▼
  同一 finalize coordinator / bounded pool
        ├─ 新 run: build → publish
        └─ 周期 seed: pending_build / pending_publish crash leftovers

maintenance thread                 report writer thread
  sync + download + projection       mailbox 接收已封存 WorkerReport
  下载成功只发 work-available Event  写日志/告警失败不停止数据面
```

核心不变量：

- production resident 的 admission 只因 shutdown、singleton 丢失、连续 readiness 失败达到
  阈值，或有证据的共享基础设施故障而关闭；acquisition 结束、报告到点和正常长文档都不是
  关闭理由；
- `WORKER_REPORT_INTERVAL_SECONDS` 默认 300 秒，只轮换 coordinator 私有 report 对象；
  旧对象经 mailbox 转移所有权后永不再修改，parse/finalize future 不排空；
- `WORKER_BATCH_PARSE` 只约束显式 `worker once`；resident 持续从 PostgreSQL 重新取数，
  队列暂空时由下载 Event 立即唤醒，并保留 5 秒 fail-safe poll；
- production stale reclaim 只在取得 singleton 后、第一次 parse admission 前以 cutoff=0
  执行一次：此时本进程尚未创建 running row，现存行只能属于已退出的 prior owner。
  steady maintenance 不按年龄 reclaim，避免误杀合法超过一小时的 whole-PDF run；
- 同一 startup recovery 在首次 parse admission 前强制完成一次 prune-capable search
  projection；失败则保持 finalize/projection-only backoff。这样即使上一个进程在维护线程停止
  后才完成 deactivation，或提交后立刻崩溃，易失的进程内 prune 信号也不会跨重启丢失；
- parse 成功即释放 lane/GPU 槽；`parse_futures + finalize_futures` 最多
  `2 × parse_concurrency`，避免把瓶颈搬成无界 finalize 内存队列；
- build/publish 只有**同一个 coordinator**准入：正常 run 直接进入 finalize pool；
  周期报告安全点从 DB 有界 seed crash/瞬时失败 leftovers，并按 run id 排除 active future；
  队列尾部则在没有 parse pool 活跃时做一轮有界补漏。不会并行启动第二个 BuildUnits consumer；
- 成功/终止失败 ID 只保留一个候选窗口大小的进程内防重复历史；可重试失败先让一个固定
  候选窗口的其他 admission 通过，到期后经同一 PostgreSQL 资格/次数谓词按 exact ID
  优先重新准入，因此即使 ULID 尾部以不低于消费速度持续增长也不会饿死。普通候选查询使用
  固定页大小的 `document_id` keyset 游标，且只推进到实际检查过的最后一行；绝不按已见
  集合大小扩大 SQL `LIMIT`；
  PostgreSQL 状态和尝试次数仍是唯一 durable truth，进程内延后只负责防热重试与公平性；
- build/publish 的共享基础设施故障停止 admission 并进入可中断 backoff；item-local 失败
  只隔离该 run。两次未知下游失败须在独立于报告周期的 300 秒控制窗口内共同出现才视为
  共享故障，报告轮换不会清掉或伪造控制证据。shared outage 后只运行 finalize-only
  恢复探针；build/publish 成功前不得重新放入 parse 流量，避免每个故障 epoch 再累积一池
  leftovers。resident parse 启用时 build/publish recovery limit 必须同时为正，否则启动
  fail fast；零 limit 不能被解释成“无需探针、已经恢复”；
- IR 读取先确认 data root 是否在线：单文件缺失/哈希或合同/单位不变量错误记录为
  `stage=publish,retryable=false` 的终止隔离；挂载、权限或 I/O 故障记录为可重试
  `IR_READ_FAILED` 并触发共享基础设施降载。仍保留 `5 × max_build_retries` 极端安全阀。

`worker once` 保持可预测的“最多 N 份”运维语义；production resident 则采用成熟 consumer
的生命周期语义：持续消费、独立快照、显式背压。这样既不会为了“100% GPU”盲目过载，也
不会因为写报告而主动制造空谷。

### 4.4 正常长任务续租、软耗时包络与失败域

真实待处理样本曾出现 500–994 页 PDF；统一一小时 timeout 会把正常超大任务反复杀死，
而页数、扫描质量、表格和图片密度也使 wall-clock 无法可靠预测正确性。2026-07-25 对
13,896 次 MinerU 3.4 成功 run 的只读统计为：

```text
p50=1.35m, p90=19.34m, p95=27.98m, p99=41.60m,
p99.9=55.19m, observed max=62.74m
```

原 1–4 小时强制 deadline 虽尚未命中这些成功样本，语义仍然错误：未来合法的更长 PDF
会因“慢”被判失败。修正后把两个概念分开：

```text
soft_expected =
    min(max_seconds, max(base_seconds, physical_pages × per_page_seconds))

默认：base=3600s，per_page=12s，max=14400s
runaway_guard=86400s（24h；约为当前 observed max 的 23 倍）
```

`soft_expected` 只发一次结构化 warning，不取消任务，也不停止 worker heartbeat。页数继续
用于 lane、预计成本和告警，不再作为 correctness deadline。正常长任务在以下条件同时成立时
持续拥有执行权：

- worker 的 singleton/advisory-lock owner 仍有效；
- MinerU 子进程仍由本 worker 注册并运行；
- 操作者没有请求 shutdown/cancel；
- 任务尚未返回明确 failed/completed。

MinerU 3.4 的异步状态只有 pending/processing/completed/failed、时间戳和 `queued_ahead`，
没有 completed-pages、checkpoint、resume 或 cancel；因此不能诚实地把日志行、`/health`
或队列长度伪造成“页级进度 lease”。当前 24 小时 runaway guard 只是 live-but-never-return
灾难保险，不是业务 SLA：

- startup timeout 最多 120 秒；
- 内层 task-result wait 从 runaway guard 扣除有界 phase reserve；
- ZIP read-inactivity timeout 为 120 秒；
- dispatcher 每 30 秒为仍由本 worker 持有、且未越过极远 lease 的 parse 续 heartbeat；
- 同一极远 lease 覆盖整个 parse future：MinerU 子进程、产物定位/读取、IR 映射、归档写入
  和 DB finish；Python 线程无法安全强杀，越界后先终止已登记 MinerU process group，再由
  launchd 替换整个 worker，不能继续用一个已污染的进程；
- startup/resident parse 与 maintenance 各自维护独立 heartbeat/watchdog；一个平面的正常
  进展不能替另一个死锁平面续租，任一 active owner 超过 wedge threshold 都触发整进程替换；
- 只有 runaway guard 到期、明确 task failure、子进程退出或操作员取消才结束任务；
- task deadline、generic task failure、输出契约错误都是 item-local，不改变全局容量；
- 只有明确 HTTP 429 或 `RESOURCE_EXHAUSTED` 记为 backend overload，停止新 refill 并进入
  parse cooldown；不再用文档完成级 AIMD 砍 `16→8→4→2→1`。

readiness 只在新 admission 前执行。已成功产生的 artifact 不会再因事后 `/health` 抖动被
改判失败并整份重跑。

#### 4.4.1 上线 A/B 发现的 readiness 控制缺口

`dcf7014` 虽已把 readiness 移到 admission 前，却仍把**一次**
`ParserVersionProbeError` 直接设为 `halt_refill`。2026-07-25 19:43:53 开始的首个正式
安装波次提供了反例：

- 55 份 parse/build/publish 全部完成，另 1 份 item-local `parser_task_failed`；
- 20:20:35 最后一份 parse 完成后，vLLM 从 20:21 至至少 20:24 持续
  `running=0, waiting=0`；
- 同一空谷的 50 秒采样中 `/health` 既有 200，也有 3 秒 connect timeout；
- round 最终报告 `parser_readiness_failed`，证明不是 backlog、finalize 或 lane 耗尽，
  而是单次 readiness 抖动结束了 parse stage。

预设计问题是：如何在不把真实 GPU outage 转成 16 份文档失败的前提下，又不让一次探测
抖动结束整轮。采用的不变量是：

1. readiness 未确认期间**不准入新文档**；
2. 参考 Kubernetes `failureThreshold=3`，连续三次失败才判共享后端不可用；任一成功立即
   清零连续失败计数；
3. 阈值前在原 dispatcher 内每 5 秒重试，已有 parse/finalize 继续运行，不退出 round；
4. 达到阈值后才记录结构化 `parser_readiness_failed`，停止 refill 并交给既有 stage-local
   cooldown；
5. 拒绝“完全取消探活并继续提交”，因为真实 outage 会同时烧掉多份文档重试预算；也拒绝
   “单次失败立即 halt”，因为线上已证明它会放大成数分钟空谷。

验证计划：单测分别覆盖“失败一次后恢复并继续准入”和“连续三次失败才 halt”；完整静态/
回归门通过后受控重启；线上同时采集 parse start、readiness 日志与 vLLM
running/waiting，确认瞬时失败不再形成 round 边界空谷。

### 4.5 重启、取消与官方 cleanup

MinerU CLI 的临时 API 可能处于独立 session。直接 `SIGKILL` 外层进程会跳过其正常 shutdown
和临时目录清理。当前边界是：

1. worker 收到 SIGTERM/SIGINT 后停止补槽，并把所有已登记 MinerU process group 标成
   `parser_cancelled`；
2. 先向 process group 发送 **SIGINT**，走 MinerU 客户端的官方清理路径；
3. 最多等待 35 秒，只对仍存活的 straggler 使用 SIGKILL；
4. launchd `ExitTimeOut=90`，给 wrapper 转发信号、MinerU cleanup 和 Python reap 留出空间；
5. `parser_cancelled` 是 retry-neutral，部署重启不消耗 PDF 的业务重试预算；
6. 进程生命周期 shutdown latch 关闭“已截快照后又注册新子进程”的竞态。

wrapper 只 reap 一次真实 child exit，修复旧双 `wait` 在 launchd 中出现 exit 127 的问题。
watchdog 走 `os._exit()` 前也先执行同一 MinerU shutdown 路径，避免旧临时 API 越过 singleton
worker 生命周期继续向 GPU 发请求。

## 5. 超大 PDF 是否应该外部分片

### 5.1 当前没有“外部分片拼坏”的证据

上线前审计从 8,723 份扩到 12:02 的 19,369 份；16:38 又按同一口径重跑到
19,782 份 active succeeded IR。23:15 为封闭历史 built-run 绕行，又把范围扩到所有
`status=succeeded AND unit_build_status=succeeded` 的 29,777 份 IR：

- 29,777/29,777 都是 `parsed_pages.full_pdf=true`，其中 active 21,500/21,500；
  `false=0`、missing=0、不可读 0；
- 当前 Provider writer 与 admin/CLI/worker 均只接受 full-PDF MinerU 3.4.4
  Hybrid-medium；page-window 只存在于 DB-free review 工具；
- Build/Publish 只接 `provider_document.v1`，并独立核 source PDF hash/page count、canonical
  record hash 和完整 provider bundle 重读；历史 NormalizedIR run 不能重新进入 writer；
- 较早的 PDF/IR 尾页核对中，8,711 份页数一致；另 12 份少一个尾页，均为纯白页或单字符
  噪点，没有跨页表或正文被外部分片丢失的证据。

因此目前**不需要因为“外部分片”重解析**。这只排除“本服务切片拼坏”这一类风险，不等于
MinerU 对每张表、每个阅读顺序都绝对正确；后者属于 parser quality/source-identity 审计，
不能混成切片结论。

### 5.2 131 个临时目录 / 约 1.3 GB 是磁盘泄漏

历史现场发现 131 个 MinerU 临时 API 残留目录，合计约 1.3 GB。它们是异常中断/强杀后没有
执行 cleanup 留下的**磁盘生命周期泄漏**：

- 不代表 PDF 被外部分片；
- 不代表这些目录对应的已发布 IR 被拼接；
- 单凭目录残留不能推导结果语义损坏，也不触发重解析。

当前 SIGINT→grace→SIGKILL 的目标是**阻断新增同类泄漏**；是否达成仍须重启 A/B。它不会
自动删除历史 131 个目录。历史残留应在重启后先核对无 live PID/打开文件，再单独做可恢复
清理，不能用宽泛 `rm`。磁盘清理和语义重解析是两件独立工作。

### 5.3 为什么仍不采用外部 PDF slicing

MinerU 3.4 的 start/end page 和内部 processing window 都不是 durable checkpoint；最终
reading order、连续 page bbox 和跨页表 carrier 仍在整本结果上收口。任意切成 N 个 PDF、
各自转换后拼 JSON，会丢掉边界上下文。

成熟文档系统也区分“整本转换”和“转换后语义 chunk”：

- [Docling chunking](https://docling-project.github.io/docling/concepts/chunking/)
  从完成转换的 `DoclingDocument` 开始，利用 hierarchy、caption 和 table structure
  做后置 chunk；
- [Unstructured PDF splitting](https://docs.unstructured.io/platform-api/partition-api/sdk-python)
  支持有界页批次，但官方提醒服务端看不到整本文档时可能产生 unexpected results，并建议
  先 partition、合并 elements，再单独 chunk。

未来若要做安全的可恢复分片，必须先建立 chunk identity、lease/heartbeat、逐窗口 durable
artifact、overlap/context、deterministic merge、exactly-once commit，以及跨页表/标题/TOC
的 source-identity 正负例。在这些合同存在前，whole PDF 是语义上更安全的边界。

## 6. 当前修复覆盖核对

| 风险/入口 | 当前任务分支 | 边界或待验证项 |
|---|---|---|
| resident worker 内层请求扇出 | 固定 API `3 task slots × 7 inference=21`，16 只是 client submit slots | commissioning 必须从 runtime v3 manifest、API health 与两轮 4/8/16 receipt 核对 |
| admin 与 pipeline 并发绕行 | 已统一 cap，并用同一 PostgreSQL advisory lock 排他 | 仓外直连 GPU 的客户端不受控 |
| 大小不一导致短任务饥饿 | 已有 regular/heavy/huge 名义份额和借用 | 页数只是初始代理；历史 GPU 秒 EWMA 尚未引入 |
| 固定 200 / 一小时 round 批尾 | resident 常驻补槽；`WORKER_BATCH_PARSE` 仅约束 once；报告快照不排空 | 需 A/B 验证 3–10 分钟尾部及 2026-07-26 的 deadline 长谷是否消失 |
| build/publish 占 GPU 槽 | 已拆分 parse/finalize pool，并限制下游 backlog | finalize=2 是否足够要看 A/B，不能预先调大 |
| resident 期间 finalize 瞬时失败只能等重启 | 同一 finalize owner 周期 seed DB leftovers；active run id 排重；尾部有界补漏 | 不允许 maintenance 再启动第二个 BuildUnits consumer |
| 报告/采集周期关闭 admission | 300 秒 report 只做对象所有权转移；acquisition/project 独立维护线程 | 报告 I/O 故障只告警，不能终止健康 parse 数据面 |
| 周期 stale reclaim 误杀合法长文档 | singleton 取得后、首个 admission 前只 reclaim 一次 | 仍由 24h runaway + 进程所有权处理真正 stuck |
| 确定性坏产物反复占 publish 队首 | 所有 hash/contract/full-PDF/unit 不变量错误统一持久隔离；队列按 `stage=publish,retryable=false` 排除 | 共享存储不可读必须保持可重试，不能误隔离 |
| 正常长任务被 wall-clock 误杀 | 本地修正为页数感知软告警 + 整个 parse future 的 24h runaway lease；worker/admin/pipeline 同一默认 | 尚未提交/重启；24h 只可在真实更长样本出现后按证据上调，不得缩成普通 SLA |
| provider 大小提示混合单位 | 不再写/读取 `oversized` 准入键；使用归档实测 `byte_count` 只决定 HUGE lane | 旧 89 份全部恢复准入；无需重新下载 |
| health 抖动毁掉成功产物 | readiness 前置，成功后不再探活判废 | 已上线验证产物不被判废 |
| 单次 readiness 假阴性停止整轮 | 连续失败阈值 3；阈值前暂停 5 秒并在原 dispatcher 恢复 | 21:08 重启后已观测一次失败、随后继续 refill；仍需完整 A/B 窗 |
| 文档级 AIMD 锯齿 | 已删除容量 AIMD；只有明确 overload 才停 refill/cooldown | 静态 21 是否最优需用 API queue、vLLM queue/KV 与吞吐数据判断 |
| 重启孤儿与新临时目录 | 已 SIGINT cleanup、35s grace、90s launchd exit window | 历史 131 目录不会自动清除 |
| 外部分片语义损坏 | resident 不自动分页/拼接；全部 29,777 份 built IR 审计 `full_pdf=false=0`；显式页段在 build 与独立 publish provenance 两处 fail closed | 不外推成 MinerU 全部语义绝对正确 |

这说明根因链的仓内入口已经闭合，但还不能宣称“生产根治完成”。最后一段证据只能由真实
重启和 A/B 给出。

## 7. 明确拒绝的替代方案

### 7.1 文档完成级 AIMD / minRTT

拒绝。PDF 从 1 页到近 1,000 页，OCR、图表和表格密度还会继续放大方差。用 20–60 分钟后
才完成的文档时延控制秒级 GPU 请求，会把 document complexity 当 congestion。

### 7.2 自研 GPU 请求 gateway

本轮采用官方 persistent MinerU API 作为文档级排队/orchestration 层，但拒绝再叠加仓内自研
GPU 请求 gateway。已确认真正 request-level work-conserving 的尾部借用需要统一请求入口；
MinerU 3.4 的 API 与 router 仍没有提供该原语：

- Windows/Linux API 的文档 semaphore 不会传给内层 VLM 调用；
- router 只解决多 upstream/GPU；
- 裸 HTTP semaphore 代理若没有取消释放、有界队列、按文档公平、指标和故障隔离，会新增
  更危险的死锁/泄漏面。

因此当前固定 `3×7=21` 保守 active 包络，并把“MinerU app-scoped shared VLM semaphore 或成熟
admission gateway”记录为明确结构升级，而不是继续堆本地 timeout/并发补丁。优先推动
上游透传现有 `MinerUClient` semaphore 参数；只有无法采用上游且 A/B 证明尾部空谷在大量
backlog 下仍有物质影响时，才实现独立、可测试的 gateway。

21:10–23:06 的延长复核尚未达到该升级门槛：有约 19,525 backlog 和 16 个 parse 在途时，
每个 5 分钟桶都有 start/finish，vLLM waiting 通常 0、峰 10、preemptions 0；残余波形可由
MinerU 远端请求阶段与本地后处理阶段交替解释。后续只有在 backlog>0 且页面请求已 ready，
同时反复出现 `running<16 AND waiting=0` 持续 30 秒，或相反
`running>=120 AND waiting>=64`/明确 429、`RESOURCE_EXHAUSTED`、preemption 增长时，才
重开共享 permit/gateway 设计；优先上游 app-scoped semaphore，不做透明代理。

### 7.3 Celery、Ray、Kueue 等新框架

拒绝引入，保留其不变量。PostgreSQL public ops view 已是唯一 durable queue；新增 broker/
control plane 会制造第二任务真相，而不能自动解决错误的请求层级。

### 7.4 任意 PDF 外部分片

拒绝。当前没有安全 merge 协议，性能优化不能以跨页表和阅读顺序的不可验证损坏为代价。

### 7.5 盲目继续提高 API task slots 或 inference concurrency

拒绝。现场高峰已是 `running≈128 + waiting 数百`；提高 16 只会扩大临时进程、内存和请求
洪峰。先验证 21 静态 active 包络下的稳定吞吐，再根据 A/B 决定是否调整。

## 8. 重启与生产 A/B 验收

### 8.1 重启前硬门

- 相关 focused tests、ruff、strict mypy、composition ledger、`git diff --check` 通过；
- 独立 reviewer 没有未解决的 P1/P2；
- PostgreSQL、AgentSSD 写权限、GPU `/health` 可达；
- 取得 primary worktree 写门和 `worker-launchd` runtime claim；
- 确认没有第二个 worker/manual producer；
- 记录旧 worker、vLLM、MinerU 版本和当前 command line；
- 首次从旧代码切换时，旧在途必须自然排空，不能用新代码的 retry-neutral 语义倒推旧 worker；
- 按 production runbook 冻结 Python 子进程，并复核 PG running、MinerU/API、vLLM waiting
  三个零条件后 bootout；安装脚本对 loaded job 或残留 MinerU/API 必须 fail closed；
- 新 plist 请求 `ExitTimeOut=90`；loaded job 实测有效值至少 60 秒、state running。
  当前 macOS user LaunchAgent 会把 90 秒请求报告为 60 秒，而 worker 自身 graceful
  window 为 35 秒。只有此后的新代码重启才允许 retry-neutral cancellation 接管。

### 8.2 启动即验

- startup banner 必须同时打印
  `resident_dispatch=continuous report_interval=300s` 与
  `parse_submit_slots=16 gpu_request_cap=3x7=21<=128`；
- runtime v3 manifest 与 API `/health` 必须同时证明 task slots=3、inference concurrency=7、
  window=16、retention=600、cleanup=30；client `--api-url` 模式不得再声称本地
  `--max-concurrency` 控制远端；
- singleton advisory lock 只有一个持有者；
- doctor、worker report 和 PG processing_run 能正常推进；
- 重启前后的临时 API/目录数量有基线，确认没有孤儿继续请求 GPU。

### 8.3 至少两小时 A/B

同口径并行采集：

- vLLM：running、waiting、queue time、KV cache usage、preemption、429/
  `RESOURCE_EXHAUSTED`；
- GPU：利用率、显存和 OOM；不把单个瞬时点当结论；
- worker：parse in-flight、regular/heavy/huge dispatched、候选 refill、finalize backlog、
  starts/finished、页数分桶耗时和失败码；
- 业务：docs/hour、500+ 页成功率、重试浪费、build/publish 延迟；
- 生命周期：新临时目录数量/大小、孤儿进程和 launchd exit status。

发布成立需要同时满足：

1. runtime identity、API health 与单生产者边界证明 worker active 配置上界不超过 21；
2. 有大量 eligible backlog 时，不再重复出现旧基线的 3–10 分钟固定 200 批尾低谷；
3. vLLM waiting、queue time、KV/preemption 相比旧基线实质下降，且没有新 OOM；
4. 普通公告不再被 500+ 页车队长期饿死，huge 仍能借空槽持续前进；
5. parser/build/publish 正确性和成功率不退化；
6. SIGTERM 重启无孤儿 MinerU、无新增同类临时目录泄漏；
7. 若任一项缺证据，报告为“部署完成、A/B 未通过/未完成”，不能写“根治完成”。

参数调整顺序固定为：先查 API identity/queue 与第二生产者，再查 vLLM waiting/KV/preemption，
再查 lane/finalize backlog；只有证据指向容量边界时才改 3×7、lane 份额或 finalize=2。不得回到
“看到锯齿就加并发/加 timeout/恢复 AIMD”的补丁循环。

## 9. 保留的历史事故证据

2026-07-12 首个排空轮曾有 44 篇在两分钟内同时 connection reset。Windows 端日志给出的
因果链是：当时文档并发 8 × MinerU 页面窗口 64，服务端约 255 个并发 sequence，
KV cache 97.7%，叠加其他 GPU 负载后 CUDA OOM，vLLM EngineCore 死亡；connection reset
只是结果。该事故已经证明“文档槽”与“GPU 请求”必须分层控制。

2026-07-13 用 16 份真实小 PDF 的 scratch 验收曾得到 16/16 parse/build/publish、
零失败和 1,585 docs/h，下一轮 0.017 秒确认空队列并进入 idle sleep。它只证明 resident
 loop 不再受固定 2 小时节拍限制和 idle CPU 接近零，不能外推到当前异构大 PDF 吞吐。
