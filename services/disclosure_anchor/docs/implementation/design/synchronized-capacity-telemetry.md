# Synchronized Capacity Telemetry v1

## 决议与边界

本契约为 MinerU 容量试验提供 content-free、跨采样源可对齐、可重放的证据。它默认不启用，
不读取 PDF 内容、不写数据库、不调 worker、不激活 profile，也不替代既有 Observation v1。
本轮只提供 schema、纯验证器和 host/phase 投影接口；Windows collector、采样循环和 DB commit
事件接线必须在后续受控 runtime 变更中单独完成。

目标指标是 `unique correct durably published source pages / full GPU-host-hour`。GPU 利用率、CPU、
队列和内存是解释吞吐损失的信号，不是独立优化目标。缺失采样必须写成 `unsupported + reason`，
不得补零、沿用旧值或伪造支持。

## 同步采样合同

| lane | cadence | 必需字段 |
|---|---:|---|
| `gpu_fast` | 250–500 ms | GPU util、VRAM used/free/total、power |
| `host_slow` | 1 s | API process CPU/RSS/HWM、Docker VM、父 cgroup memory/stat/events/PSI、cpu.stat/throttle、API queue/HTTP、vLLM exact running/waiting/KV/preemption |

每帧同时记录 UTC wall clock、scheduled/started/finished monotonic clock、实际间隔、采集耗时、
missed deadline、runtime/profile/observer identity 和 clock-domain identity。一个 monotonic 数值只有在
clock-domain identity 相同且 phase process epoch 有单独的 binding artifact 时才能与 phase trace 比较；
Mac `time.monotonic_ns()`、PowerShell Stopwatch 和容器 monotonic 不得仅因都是整数就直接比较。

receipt 记录 observer CPU 开销，超过 2% 即 `unsafe`。wall 与 monotonic 的允许误差不是固定 5 ms，
而是 `50 ms + elapsed × 50 ppm`；实测 divergence 必须原样写入并机械复算。任一 epoch drift、
identity drift、超限或缺少必需 lane 都 fail closed。

## 参数生命周期

| 边界 | 参数 | 规则 |
|---|---|---|
| process startup-only | hybrid batch ratio、API task/pending slots、inner inference concurrency、processing window、vLLM max sequences | 进程启动后冻结；变更必须 drain 后重启并生成新 epoch/profile identity |
| document-frozen | window、pipeline depth、resident pages、document credit envelope | 文档 admission 时冻结；终态前不得改变 |
| online scheduler | 哪个已 admission 的 ready stage 获得已存在 credit | 只能在守恒、不越界且不改变冻结 profile 的前提下快环调度 |

## 容量向量与安全余量

容量不是一个固定 `7 GiB` 标量。信用向量分别覆盖 source disk、raster CPU、tensor CPU/GPU、
model CPU/GPU、document owner bytes、task/native owner/vLLM sequence slots。任一 snapshot 必须满足：

```text
capacity = model_baseline + measured_safety_margin + active_reserved + available
```

reserve/borrow/return 对每一维原子守恒，attempt 结束必须归还全部 lease。安全余量使用有样本数的
`p99-mad-positive-jump.v1` 证据，且每维不得低于实测 uncertainty；宿主 61 GiB、WSL/Docker cgroup
上限和容器临时张量不能混作同一可用量。没有足够样本时 `safety_margin=null`，不能回退到常数。

## 进度与 phase 闭合

progress contract 只暴露两类事件：带 `blocked_reason` 的阻塞区间，以及带 source identity hash、
增量/累计页数和 commit latency 的 unique durable page commit。它不携带公司、文档、URL、路径或
task ID。每个 progress event 显式绑定自身 clock domain；Mac worker 发出的事件不能直接与 Windows
phase monotonic 比较。phase summary 只有在完整 trace、同 clock domain 的绑定、两条 lane 覆盖、严格递增事件和
receipt identity 全部闭合时生成；否则独立保存 trace 与 telemetry，禁止声称 synchronized coverage。

receipt 的 lane sample count、边界/相邻最大 gap、late、missed deadline、supported frame 和 required
unsupported observation 数必须从 frames 机械重算。`gpu_fast` 只以 GPU observation 为 required；
`host_slow` 只以 API process、host cgroup 和 queue/vLLM 为 required。其他 lane 上的 `not_due_at_this_tick`
不计为缺失，避免合法的分频采样被误判为永远 incomplete。

正式 schema 位于 `contracts/operational/synchronized-*.v1.schema.json`。所有对象 `extra=forbid`，
新增字段或语义必须发布新版本。
