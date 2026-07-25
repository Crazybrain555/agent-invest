---
id: disclosure_anchor_worker_dynamic_scheduling
title: Worker 动态调度、GPU 锯齿根因与发布验收
date: 2026-07-09
updated_at: 2026-07-25
status: deployed-ab-in-progress
authority: tracked implementation design; live runtime values must be re-verified
---

> 本文记录可复核的事故证据、版本匹配的外部机制和当前实现边界。基础修复 commit
> `dcf7014` 已于 2026-07-25 重启上线，生产 A/B 仍在进行；A/B 又发现单次 readiness
> 抖动仍可结束 parse round，后续修正尚未发布。因此“已覆盖”不等于发布验收完成。
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

## 3. 成熟项目采用的共同原则

本次不是复制某个框架，而是把版本匹配、可证实的调度不变量落到现有 PostgreSQL 队列和
单机 resident worker。

| 成熟实现 | 官方机制 | 本系统采用的原则 | 没有照搬的部分 |
|---|---|---|---|
| [MinerU](https://github.com/opendatalab/MinerU/blob/mineru-3.4.0-released/docs/en/usage/quick_usage.md) | 临时 API、显式 `max_concurrency`、整本文档输出 | 在官方请求入口设硬上限；保留 whole-PDF 语义 | 不 patch MinerU 内部，不把 processing window 当可恢复外部分片 |
| [vLLM](https://docs.vllm.ai/en/stable/) / [SchedulerConfig](https://docs.vllm.ai/en/stable/api/vllm/config/scheduler/) | continuous batching；`max_num_seqs` 限每轮处理 sequence 数 | 客户端平滑、有界地供给，动态合批交给 vLLM | 不把 waiting queue 当 admission control，不靠提交海量请求“喂满”GPU |
| [Celery optimization](https://docs.celeryq.dev/en/latest/userguide/optimizing.html) | 长短任务使用分别配置的 worker/queue；长任务 prefetch multiplier 取 1 | 大小 lane；执行槽空一个才取一个，避免长任务预取占住短任务 | 不引入 Redis/RabbitMQ 或第二份任务真相 |
| [Kueue quota borrowing](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/) | nominal quota 保证份额，空闲 quota 可由 cohort 借用 | regular/heavy/huge 各有名义份额，lane 空时 work-conserving 借满 | 不引入 Kubernetes 控制面 |
| [Ray Serve](https://docs.ray.io/en/latest/serve/advanced-guides/asyncio-best-practices.html) | `max_ongoing_requests` 以 replica 为静态 in-flight cap，超出后排队/背压 | GPU 请求预算与文档槽分开；finalize 不继续占 GPU 槽 | 不为单机 worker 引入 Ray replica/router/autoscaler |
| [Envoy circuit breaker](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking) | 分别限制 active、pending、retry，过载快速背压 | 只把明确 429/`RESOURCE_EXHAUSTED` 当全局 overload；未来多生产者时在统一入口设静态 breaker | 当前单生产者已有数据库排他锁，不新增网络 gateway |
| [Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) | readiness 默认连续失败 3 次才转 not-ready；失败后继续探测，恢复后重新准入 | 探测不确定时暂停新 admission；单次失败不结束整轮，连续失败达到阈值才进入现有 cooldown | 不把 readiness 当 liveness，不因一次网络抖动重启进程 |

共同结论是：

1. **安全容量用静态 admission/circuit breaker 表达**，不由高方差任务的完成时延猜测；
2. **异构公平用 lane/queue 表达**，长任务有份额但不能霸占全部执行槽；
3. **借用必须 work-conserving**，某 lane 空闲时不能为“预留”而让 GPU 空着；
4. **下游必须有界**，否则只把 GPU 队列搬到内存或本地 finalize 队列；
5. **服务端 continuous batching 负责动态组批**，客户端负责有界、平滑供给。

## 4. 针对本系统的动态调度闭环

### 4.1 GPU 请求层：112/128 静态安全包络

生产配置的文档槽仍为 16，但请求安全边界独立配置：

```text
WORKER_GPU_MAX_SEQUENCES=128
WORKER_GPU_REQUEST_BUDGET=112
WORKER_PARSE_CONCURRENCY=16
每份临时 MinerU API: --max-concurrency = floor(112 / 16) = 7
worker 正常稳态包络: 16 × 7 = 112 < 128
```

保留的 16 个 sequence 不是吞吐浪费，而是给取消传播、重试的短暂交叠和服务端内部调度留下
12.5% 余量。该式只证明**单 resident worker 的正常稳态上界**：

- `max_num_seqs=128` 仍须在发布时从远端启动配置或等价运营证据复核；当前 metrics
  端点不直接暴露它；
- 外部绕过本服务直连 GPU 的客户端不受此预算控制；
- shutdown/retry 传播期间允许短暂重叠，因此它不是分布式全局令牌的数学证明。

所有仓内入口使用同一个 `mineru_http_request_concurrency`：

- resident worker；
- admin parse；
- pipeline `parse` / `process`。

admin/pipeline 在执行前还必须取得与 resident worker 相同的 PostgreSQL session advisory
lock；拿不到就 fail closed（admin 409 / CLI busy），从而当前单机部署不会同时出现两个仓内
生产者。resident 在每次补槽和最长 30 秒的等待边界复核同一 lock session；失锁先终止本进程
MinerU，再禁止新准入。它仍不是分布式 fencing。若未来需要多 worker 或允许其他服务直连
GPU，届时应把 112 预算上移到统一 gateway/Envoy/Ray 类 admission 层，而不是继续加本地
semaphore。

### 4.2 文档层：三 lane、名义份额与借用

物理页数只作成本代理，不改 PDF 内容：

| lane | 默认范围 | 三类都饱和、16 槽时的名义份额 |
|---|---:|---:|
| regular | `<80` 页 | 11 |
| heavy | `80–499` 页，或页数探测失败 | 4 |
| huge | `>=500` 页 | 1 |

候选按 `document_id` 取前 1,000。实盘快照中该前缀同时含 regular/heavy，但这是观测，
不是全 backlog 的保证；名义份额与借用只在当前候选窗口内成立。每次选取遵守：

- lane 均有任务时，regular/heavy/huge 保留名义份额；
- 任一 lane 为空，其他 lane 立即借用其空槽；
- 同一 lane 内保留 DB 候选顺序；
- unknown 进入 heavy，避免未知成本冒充短公告；
- 不按 document id、发行人、标题或失败样本特判。

若 A/B 观测到候选窗口长期 `regular=0`，再把页数/成本持久化并改为 per-lane keyset
取数；当前真实前缀没有该问题，不为假设场景增加第二套查询控制面。

这就是本系统的“动态”：动态选择下一份 whole PDF，并在 lane 之间借用空闲文档槽；
GPU 请求总预算本身保持静态。

### 4.3 滚动补槽与 parse/finalize 解耦

旧执行链把 `parse→build→publish` 全部算作一个 GPU 文档槽，并在固定 200 份候选耗尽后
等整批排空。当前实现改为：

```text
DB pending_parse（最多看 1000 个候选）
          │
          ▼
三 lane 选择 ──> parse pool（16；完成一个立即补一个）
                         │ parse 成功即释放 GPU 文档槽
                         ▼
                 finalize pool（build + publish，默认 2）
```

- 在明确的 acquisition/refill window 内，队列暂空或候选耗尽会重新读 DB，而不是等整批
  16 个 future 全部结束再换波；
- window 到期后停止新 admission，让 round 有界地收尾和写报告，不做无限热循环；
- parse 成功即释放 lane/GPU 槽，build/publish 在独立 pool 执行；
- `parse_futures + finalize_futures` 默认最多 `2 × parse_concurrency`，避免把瓶颈搬成
  无界 finalize 内存队列；
- build/publish 的共享基础设施故障仍停止 refill；item-local 失败只隔离该文档。

固定 `WORKER_BATCH_PARSE` 仍是直接 `run_once` 的单轮上界和报告边界；生产 resident 模式
只有在显式 refill window 打开时才可滚动超过它。

### 4.4 超大文档 deadline 与失败域

真实待处理样本曾出现 500–994 页 PDF；统一一小时 timeout 会把正常超大任务反复杀死，
统一四小时又会让短公告挂死太久。当前 deadline 为：

```text
document_deadline =
    min(max_seconds, max(base_seconds, physical_pages × per_page_seconds))

默认：base=3600s，per_page=12s，max=14400s
```

同时把同一 outer deadline 传到临时 API 启动、任务提交/等待、结果下载和进程清理：

- startup timeout 最多 120 秒；
- 内层 task-result wait 从 outer deadline 扣除有界 phase reserve；
- ZIP read-inactivity timeout 为 120 秒；
- dispatcher 每 30 秒只为仍在合法 page-aware deadline 内的 parse 续 heartbeat；
- deadline 全部过期后不再“保活”，真正 wedge 仍由 watchdog fail loudly；
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
19,782 份 active succeeded IR：

- 最新 19,782/19,782 都是 `parsed_pages.full_pdf=true`，`false=0`，不可读 0；
- resident 调度路径没有自动 range parse 或把多个页段结果拼成 active IR；admin 契约仍允许
  操作者显式指定 `start_page` / `end_page`；
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
| resident worker 内层请求扇出 | 已用官方 `--max-concurrency=7`，稳态 `16×7=112` | 重启后从真实命令行/health 核对，不只信配置文件 |
| admin 与 pipeline 并发绕行 | 已统一 cap，并用同一 PostgreSQL advisory lock 排他 | 仓外直连 GPU 的客户端不受控 |
| 大小不一导致短任务饥饿 | 已有 regular/heavy/huge 名义份额和借用 | 页数只是初始代理；历史 GPU 秒 EWMA 尚未引入 |
| 固定 200 批尾 | 已有 1,000 候选窗口和有界滚动补槽 | 仅在 refill window 内；需 A/B 验证 3–10 分钟尾部是否消失 |
| build/publish 占 GPU 槽 | 已拆分 parse/finalize pool，并限制下游 backlog | finalize=2 是否足够要看 A/B，不能预先调大 |
| 超大任务 timeout | 已按页数给 1–4 小时 deadline，统一内外层 SLA | 500+ 页成功率和 p95 需线上复核 |
| health 抖动毁掉成功产物 | readiness 前置，成功后不再探活判废 | 已上线验证产物不被判废 |
| 单次 readiness 假阴性停止整轮 | 连续失败阈值 3；阈值前暂停 5 秒并在原 dispatcher 恢复 | A/B 发现后修正，仍待下一次重启验证 |
| 文档级 AIMD 锯齿 | 已删除容量 AIMD；只有明确 overload 才停 refill/cooldown | 静态 112 是否最优需用吞吐、queue/KV 数据判断 |
| 重启孤儿与新临时目录 | 已 SIGINT cleanup、35s grace、90s launchd exit window | 历史 131 目录不会自动清除 |
| 外部分片语义损坏 | resident 不自动分页/拼接；active IR 审计 `full_pdf=false=0` | admin 仍支持显式页段；不外推成 MinerU 全部语义绝对正确 |

这说明根因链的仓内入口已经闭合，但还不能宣称“生产根治完成”。最后一段证据只能由真实
重启和 A/B 给出。

## 7. 明确拒绝的替代方案

### 7.1 文档完成级 AIMD / minRTT

拒绝。PDF 从 1 页到近 1,000 页，OCR、图表和表格密度还会继续放大方差。用 20–60 分钟后
才完成的文档时延控制秒级 GPU 请求，会把 document complexity 当 congestion。

### 7.2 持久 MinerU API 或自研 GPU gateway

本轮拒绝。当前仓内只有一个合法生产者，统一 cap + PostgreSQL 排他锁已能形成静态包络；
引入常驻 API/gateway 会增加新的生命周期、队列和故障面。只有出现多 worker、跨服务直连或
需要全局 active/pending/retry breaker 时，才有充分理由把 admission 上移。

### 7.3 Celery、Ray、Kueue 等新框架

拒绝引入，保留其不变量。PostgreSQL public ops view 已是唯一 durable queue；新增 broker/
control plane 会制造第二任务真相，而不能自动解决错误的请求层级。

### 7.4 任意 PDF 外部分片

拒绝。当前没有安全 merge 协议，性能优化不能以跨页表和阅读顺序的不可验证损坏为代价。

### 7.5 盲目继续提高文档并发

拒绝。现场高峰已是 `running≈128 + waiting 数百`；提高 16 只会扩大临时进程、内存和请求
洪峰。先验证 112 静态包络下的稳定吞吐，再根据 A/B 决定是否调整。

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

- startup banner 必须打印 `gpu_request_cap=16x7<=128`；
- 实际 MinerU 子进程命令必须出现 `--max-concurrency 7`；
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

1. 实际命令和单生产者边界证明 worker 稳态请求 cap 不超过 112；
2. 有大量 eligible backlog 时，不再重复出现旧基线的 3–10 分钟固定 200 批尾低谷；
3. vLLM waiting、queue time、KV/preemption 相比旧基线实质下降，且没有新 OOM；
4. 普通公告不再被 500+ 页车队长期饿死，huge 仍能借空槽持续前进；
5. parser/build/publish 正确性和成功率不退化；
6. SIGTERM 重启无孤儿 MinerU、无新增同类临时目录泄漏；
7. 若任一项缺证据，报告为“部署完成、A/B 未通过/未完成”，不能写“根治完成”。

参数调整顺序固定为：先查真实命令与第二生产者，再查 waiting/KV/preemption，再查 lane/
finalize backlog；只有证据指向容量边界时才改 112、lane 份额或 finalize=2。不得回到
“看到锯齿就加并发/加 timeout/恢复 AIMD”的补丁循环。

## 9. 保留的历史事故证据

2026-07-12 首个排空轮曾有 44 篇在两分钟内同时 connection reset。Windows 端日志给出的
因果链是：当时文档并发 8 × MinerU 页面窗口 64，服务端约 255 个并发 sequence，
KV cache 97.7%，叠加其他 GPU 负载后 CUDA OOM，vLLM EngineCore 死亡；connection reset
只是结果。该事故已经证明“文档槽”与“GPU 请求”必须分层控制。

2026-07-13 用 16 份真实小 PDF 的 scratch 验收曾得到 16/16 parse/build/publish、
零失败和 1,585 docs/h，下一轮 0.017 秒确认空队列并进入 idle sleep。它只证明 resident
 loop 不再受固定 2 小时节拍限制和 idle CPU 接近零，不能外推到当前异构大 PDF 吞吐。
