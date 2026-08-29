---
id: disclosure_anchor_mineru_throughput_scheduler
title: MinerU 端到端工作守恒调度与容量契约
date: 2026-08-29
status: implementation-contract
authority: subordinate to service-purpose and repository protocol
runtime_baseline: MinerU 3.4.4 / mineru-vl-utils 1.0.5 / vLLM 0.21.0
---

# MinerU 端到端工作守恒调度与容量契约

## 1. 第一性原则与边界

唯一主指标是：

```text
同一完整 GPU 主机 wall span 内
通过 source/page/structure closure 且整文档原子发布到 PostgreSQL 的
distinct(source_sha256, parser_profile_sha256, page_index) / host_hour
```

重试、重复 build/publish、临时页结果和失败文档不增加分子；readiness、故障、恢复、排空和空闲均计入
分母。GPU/CPU utilization、VLM duty、queue depth 和 phase wall time 只用于解释瓶颈，不能替代主指标。

以下边界不可因吞吐目标放宽：

- 原 PDF、来源、hash、parser/runtime/profile/attempt lineage 完整；
- 一份 PDF 是一个 attempt；不得外部分页、页级 durable publish 或多引擎复制重模型；
- 页、window、block 和跨页结构按源顺序闭合；乱序执行只能进入 attempt-local reorder；
- 整文档 closure 通过前无外部可见输出；发布是单一原子、幂等事务；
- 已 append 后不得混用另一个 profile 或 legacy fallback；重跑必须是从 page 0 开始的新 attempt；
- 异常、取消和 epoch drift 均 fail-visible；不得以健康端点或默认值伪造成功。

## 2. 已证事实与停止沿用的旧假设

2026-08-29 的 legacy trace 证明 348 个 window 的相邻 phase overlap 为 0，VLM interval 仅占 document
wall 约 15.1%；它没有同步 CPU time 或 GPU kernel-busy，因此只能证明串行和供给间隔，不能单独证明
GPU 饱和度。固定轮次、固定 corpus 与强制交叉重复已退出当前执行面。

当前 C/S API 容器不挂 GPU，MinerU 的 `get_vram(cpu)` Auto 回退为 1 GiB，因此 hybrid batch ratio
实际为 1；这不是 RTX 5080 容量结论。requested/effective ratio、OCR override 和各 CPU stage batch cap
必须分别进入 trace 与 runtime identity。

7 GiB 只是旧 campaign guard，不是产品约束。Windows 物理内存、WSL/Docker cgroup limit、进程
working set、GPU VRAM 和临时 tensor 是不同资源域；不能用宿主总内存减几个 RSS 得出并发数。

## 3. 目标数据流与唯一 durable truth

PostgreSQL 仍是唯一 durable backlog；Windows 不新增第二个 durable 队列，Ray/Kafka/Triton/DALI
也不成为运行依赖。

```text
PostgreSQL pending_parse
  -> Mac ParsePump claim/fence
  -> Windows bounded ingress
  -> DocumentOwner
       -> A_READY: render/layout/CPU models
       -> B_READY: remote VLM request
       -> C_READY: postprocess/ordered append
       -> document closure/result artifact
  -> Mac verify/extract/artifact commit: PARSE_SUCCEEDED
  -> PostgreSQL pending_build / pending_publish
  -> Mac FinalizePump
  -> atomic PUBLISHED + outbox commit
  -> durable-page KPI increment
```

Acquisition、Parse 和 Finalize 是独立常驻 pump。parse success 已形成可恢复的 DB 状态后立即归还远端
admission；build/publish 通过既有 durable views 恢复，不依赖同一轮内存 Future 存活。

## 4. Windows 进程级所有权与状态机

### 4.1 ProcessCoordinator

一个 API 进程只能有一个 `ProcessCoordinator`，独占：

- runtime/control epoch、ProcessProfile 和 admission open/closed 状态；
- 非终态 document 上限与有界 A/B/C ready queues；
- process-global vector credits、A/C stage gates、model gates 与最终 HTTP request limiter；
- quiesce、incident circuit、resource counters 和 content-free progress。

FastAPI registry 不能先注册任意 task 再在 processor semaphore 等待。admission 在 upload/task 注册前
原子取得 `source + first full-lifecycle window + document owner`；饱和返回明确 429，quiescing 返回 503。
queue 的 `task_done` 只在该 accepted owner 进入 terminal 后发生。

### 4.2 DocumentOwner

```text
INGRESS -> ADMITTED -> RUNNING -> DRAINING
        -> TERMINAL_SUCCESS | TERMINAL_FAILED
```

每个 owner 唯一拥有 source、attempt/fence、DocumentProfile、私有输出目录、failure latch、append cursor
和 closure manifest。失败后所有未开始 window 取消；已进入 thread/HTTP 的 owner 自然 drain，不能把
`to_thread` cancellation 当作实际完成。

### 4.3 WindowOwner

```text
RESERVED -> A_READY/RUNNING -> B_READY/RUNNING -> C_READY/RUNNING
         -> COMMIT_READY -> APPENDED -> RELEASED
```

window 可以乱序完成，但只有 DocumentOwner 按连续 index append。window lease 从 A 前覆盖至 C、append
和 release，保证 B 完成后不会因重新申请内存而死锁。初始 `per_document_gpu_inflight=1`；跨 window
语义独立和跨页表格 closure 未证明前不得提高。

### 4.4 锁与 limiter

- scheduler mutex 只包无 `await` 的原子 grant/return；执行 stage 时不持有；
- A gate、C gate 分离，不用一把 native lock 串行所有 CPU 工作；
- A/C 内最多持有一把 model-specific lock，真实 model call 结束立即释放；不得持锁等待 c7、另一模型或 DB；
- B 在 `mineru_vl_utils.vlm_client.http_client.HttpVlmClient.aio_predict()` 的最终 async POST 前取得
  process-global request token，在完整响应/失败/cancel drain 后 `finally` 归还；
- 外层 batch semaphore 与最终 POST limiter 必须是不同对象，否则会形成 permit 自锁；
- cross-page main/merge 请求无旁路；transport/inference/cardinality 错误上抛，无候选或语义不确定仍 no-merge。

## 5. 工作守恒与公平

只要某 ready queue 非空、对应 gate/token/credits 可授予且 circuit 为 READY，下一 scheduler tick 必须
dispatch；blocked 必须给出 closed `blocked_reason`。优先级为：

1. C：释放 resident memory 并推进整文档 closure；
2. B：立即填充单例 vLLM 的 continuous batching；
3. A：在 B supply 不足时准备新 window。

不同 gate 可同时运行。文档间用 cost-weighted deficit round-robin + aging；接近完成的文档可获得适度
completion bias，限制 active unpublished pages。regular/heavy/huge/unknown 只是 envelope/cost class，
不得成为全局互斥锁。单个工作若实际消耗接近全部某维资源，可由 credits 自然独占。

## 6. 多维容量与安全余量

每个 lease 是不可跨维兑换的向量：

```text
document_slots
source_buffer_bytes
resident_pages
decoded_payload_bytes
cpu_temporary_bytes
gpu_cost_units / gpu_request_slots
reorder_bytes
terminal_output_bytes
temp_disk_bytes
db_stage_bytes
unpublished_pages
```

model CPU/GPU baseline 每个 process epoch 只永久计一次；不得按 document 重复收费。source PDF 的磁盘
字节默认属于 disk/IO credit，只有实际 resident buffer 计 RAM。decoded bytes 是 payload surrogate，
外部 cgroup/PSI/RSS/VRAM 是 hard safety truth。

每维均须满足：

```text
limit - model_baseline - dynamic_guard - sum(active_leases) >= 0
```

guard 由 warmed idle p99、stage/class/profile 增量 p99 或样本不足时 historical maximum、未解释残差
和最大 1 秒跳变共同估计；epoch/model/profile 变化重置 bootstrap。任何 underestimate 先关闭新 admission，
不得形成负信用。actual 小于 reserve 可在 materialize 后归还差额；引用计数到最后一个 consumer 后归还。

WSL/Docker memory 只在证据表明“增加一个已测 owner 能改善供给且宿主 Available/Commit 仍安全”时，
于全 quiescent 状态调整；每次调整产生新 runtime fingerprint。不得先为 ratio sweep 改内存，也不得
保留固定 7 GiB。OOM、memory.events、PSI、swap/reclaim、CPU throttle 和 VRAM guard 是独立 stop signals。

## 7. 配置生命周期

### 7.1 ProcessProfile：仅 QUIESCENT 切换

- API document hard cap、nonterminal/terminal registry cap；
- final POST c7 hard cap、A/C gate hard cap、model-lock policy；
- `MINERU_HYBRID_BATCH_RATIO`、OMP/CPU thread policy；
- vector hard caps、processing-window upper envelope；
- vLLM engine args、Docker/WSL memory、image/model/runtime epoch。

任何变化必须新 identity、collector、attestation 和 canary，不得运行中改 env 或重新解释已接纳工作。

### 7.2 DocumentProfile：admission 冻结

- source/attempt、window size/depth、render/semantic/parser options；
- class/envelope、timeout、soft token share、fallback policy；
- page/window partition 和 publication/lineage schema。

只可选择与当前 ProcessProfile 精确兼容且有当前验证 receipt 的 profile。失败后换 profile 是新 attempt，
先 drain 旧 owner，再从 page 0 开始。

### 7.3 运行时快环

只允许在 attested hard ceiling 内调整未开始工作：ready queue 选择、DRR/aging、soft token grant、
backpressure 和 circuit。降低 budget 不撤销在途 lease；提高只一步邻居、需高低水位 hysteresis、最小
residence、cooldown 和连续有效样本。observer incomplete 时冻结 actuator并输出原因。

当前没有在线自调执行面。机器/runtime/model/config/epoch 漂移后回到显式 static 或 fail-closed；
不得 warning 后静默选择另一个 profile。

## 8. Readiness、故障与 quiesce

```text
STARTING -> WARMING_GPU -> CONTROL_HEALTHY -> INFERENCE_PROBING -> READY
READY -> DEGRADED -> CIRCUIT_OPEN -> HALF_OPEN -> READY | CIRCUIT_OPEN
```

`/health`、`/models`、socket 和空 scheduler gauges 只证明 control plane。READY 还要求 exact epoch、同
production vision path 的 deterministic multimodal canary、总 wall deadline、bounded response 和 engine
progress。backlog 下真实 B completion 是 liveness；`B_READY>0` 且长期无 completion 打开 circuit并留现场。

Quiesce：`OPEN -> ADMISSION_CLOSED -> DRAINING -> QUIESCENT`。只有 ready queues、documents、threads、
HTTP owners、DB tx 均为 0，c7/vector/model gates 全归还才可报告 QUIESCENT。deadline 超时为
`STUCK_OPEN_CIRCUIT`，不能切 profile、假 terminal 或盲目 restart。

## 9. 进度与证据

所有 event 同时有 UTC wall、monotonic、runtime epoch、Process/Document profile hash、attempt/fence 和
closed state/phase/blocked_reason；禁止文档内容、文件名、prompt、task ID 或原始主机标识。

采样与公开节奏：phase transition immediate；GPU 250–500 ms；CPU/cgroup/PSI/vLLM/queue 1 s；terminal/
frontend snapshot 2 s；durable heartbeat 10–15 s。缺失/不支持显式 unavailable，不能补 0 或沿用过期值。

关键字段：

- document/window/page 的 preflight/prepared/B/C/ordered/validated/published；
- backlog documents/pages/cost、active owners、unpublished pages；
- 每维 credit limit/reserved/in-use/available、wait seconds；
- application HTTP active/pending/peak、vLLM running/waiting/KV/preemption；
- GPU util/VRAM/power/sample quality，CPU/process busy/RSS，cgroup memory/current/events/PSI/cpu.stat；
- build/publish backlog、commit latency、unique durable pages、1/5/15min 和 full-host rate；
- readiness/circuit/restart/retry、last stable failure code 和 ETA range/basis。

`pages_published` 只在整文档事务 commit 后增加；不能用“GPU 已完成 87%”冒充持久化进度。

## 10. 实施与验证顺序

1. 先落 closed schema、同步 telemetry、真实 durable-page KPI、ratio requested/effective identity；
2. 落 final-POST global c7、process credits/A/C gates/model locks、bounded FastAPI admission 和 strict drain；
3. 落 Mac parse/finalize 解耦，移除 huge 与 finalize Future 人为屏障；
4. 所有确定性成功/异常/cancel/乱序/epoch 测试通过后，才允许 feature-flagged `task_slots=2`；
5. 用独立 held-out 文档做 10–20 分钟验证：先 ratio 1/2/4/8 单槽隔离，再 slots/window/C 一次一维粗到细；
6. correctness、memory、PSI、OOM、restart、preemption、drain 任一失败立即停止该 setting；
7. 两次相邻提高的 goodput 改善均小于 `max(5%, 2×CV)`，或 GPU 在正 backlog 下持续忙且 ready queue
   为正，即到平台期；
8. 最终 static winner 才进入真实 backlog + PostgreSQL publication soak。

探索只在新 telemetry 下做一个短 anchor，除非 epoch/measurement path 漂移
或噪声超阈值，否则不重复。验证集必须含 regular/heavy/huge、OCR、table/formula、跨 page/window 表格、
reading-order edge 和 controlled failure，且全部使用原始完整 PDF。

## 11. 外部机制取舍

- MinerU 3.4.4 C/S 明确支持显式 hybrid batch ratio；本系统必须绑定实际 effective 值；
- vLLM 0.21.0 保留 continuous batching；application 只提供 bounded request supply，不造第二 batcher；
- Ray Data 的 streaming/backpressure、DALI 的 shallow prefetch、Triton 的 central bounded admission 是可迁移
  机制，不引入其完整 runtime；
- Direct WSL2 可能简化层级但仍使用 GPU-PV；native Linux 才消除该 cold-start family。两者不阻塞当前
  scheduler 修复，待 warm steady-state 瓶颈证据出现后再比较。

官方参考：

- [MinerU 3.4.4 source](https://github.com/opendatalab/MinerU/tree/mineru-3.4.4-released)
- [vLLM scheduler configuration](https://docs.vllm.ai/en/v0.21.0/api/vllm/config/scheduler/)
- [vLLM optimization and preemption](https://docs.vllm.ai/en/v0.21.0/configuration/optimization/)
- [Ray Data streaming execution](https://docs.ray.io/en/latest/data/data-internals.html)
- [NVIDIA DALI performance tuning](https://docs.nvidia.com/deeplearning/dali/archives/dali_1_26_0/user-guide/docs/advanced_topics_performance_tuning.html)
- [WSL resource controls](https://learn.microsoft.com/windows/wsl/wsl-config)
