# Synchronized Capacity Telemetry v1

## 决议与边界

本契约为 MinerU 容量试验提供 content-free、跨采样源可对齐、可重放的证据。它默认不启用，
不读取 PDF 内容、不写数据库、不调 worker、不激活 profile，也不替代既有 Observation v1。
当前实现还提供默认关闭、collector-injected 的 resident core：两个独立线程以绝对 deadline 运行，
`gpu_fast` 的慢/失败不会阻塞 `host_slow`，反之亦然；第三个单写者按 sample start 做有界归并。
本 core 不包含 Windows collector、CLI/worker 接线、DB commit 或 profile 激活，这些仍须在后续受控
runtime 变更中单独完成。仓库同时提供一个未激活的 Windows resident wire/持久 HTTP adapter、PS5.1
exporter/supervisor 源码和纯 replay 合同；它们没有 installer/worker/Auto 接线，尚未经过 Windows Job
Object、真实 4 Hz GPU backend、WSL/Docker 1 Hz backend、observer+exporter 总开销或完整 3600 秒实机门禁。
源码存在不等于生产支持。

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

## Resident core 与证据格式

runner 状态机是 `INIT -> PREFLIGHT -> RUNNING -> DRAINING -> SEALED`；文件创建、写入、fsync、
canonical replay 或 receipt sealing 失败进入 `FAILED_EVIDENCE` 并抛错，绝不留下可冒充成功的 receipt。
cancel、sampler/transport shutdown、mailbox overflow 和 artifact bound 是闭集 termination reason，均为
`incomplete`；runtime/profile/clock/process/GPU/cgroup identity drift 为 `unsafe`。正常结束只有
`duration_elapsed`。

每个 run 使用 new-only `0700` 目录；frames/receipt/seal 使用 new-only、`0600`、single-link、`O_NOFOLLOW`
文件，并分别 fsync 文件、run 目录和父目录。既有 public v1 合同及其 JSON-array artifact 语义保持不变；
resident runner 发布独立的 v2：`frames.v2.jsonl` 只接受 LF 结尾的逐行 canonical UTF-8 JSONL，
`receipt.v2.json` 与 `seal.v2.json` 是 canonical JSON object。receipt 写入并 fsync 后，runner 必须从仍锚定的
私有目录 descriptor 完整 replay；只有 replay 成功才写 non-self-referential seal，随后保持 parent/root/run
三个 descriptor 打开并再次 replay，逐文件核对 exact `dev/ino/mode/nlink/size/mtime/ctime` 和目录清单，
同时确认 parent→root 与 root→run 名称仍指向原 inode。不得按 pathname 重新打开，
并在 replay 后再次核验目录锚点，且只有最终 replay 对象能够以 `SEALED` 返回。返回值中的
`run_directory` 仅是事后定位器，不是已验证证据来源；证据完全来自仍打开的原始 write/directory FD。
任何篡改、缺失或磁盘失败都进入 `FAILED_EVIDENCE`。

seal 中的 CPU 字段明确命名为 `preseal_observer_*`：区间从 sampling 前开始，累计 observer parent 与已回收
resident collector child 的 process CPU，覆盖 collector shutdown、frame close、quality derive、receipt
validate/write 以及 mandatory pre-seal replay，但不声称覆盖 seal write 或最终 anchored replay。
2% 比例以该 pre-seal CPU delta 除以 sampling elapsed denominator 机械重算；它不是 full-run CPU 指标。
seal 本身不把自己的 bytes 纳入自引用 attestation。receipt 的 closed safety drift 分别记录
`epoch_drift`、其他 identity drift、累计计数回退和 OOM/OOM-kill 增量；`epoch_changed` 只代表第一类。

父进程只接受 `ResidentTelemetryCollectorSpec`：top-level factory 的 module/qualname、bounded canonical JSON
config、expected collector identity 和显式 `descendants_capability=forbidden`。live sampler object 绝不跨 spawn；
child import factory 并在自身构造 sampler，完成 READY/identity/no-descendants handshake 后，父进程才冻结
start wall/monotonic/end deadline。任一 pickle、spawn、pipe、factory 或 READY 失败均关闭两端并 terminate+join。

每 lane 由一个单 owner、跨 tick 常驻的 collector subprocess 和一条 duplex transport 承载；外层携带
绝对 monotonic deadline 并可关闭 transport、terminate
并 join 整个 collector。忽略 deadline、永久 hang、late return 或 cancel 都必须 bounded return，且不得遗留
每 tick thread/process。只有 typed deadline/transport failure 可投影成 unsupported；assertion、类型错
误和其他程序缺陷必须传播到 `FAILED_EVIDENCE`。每帧 lane ownership 也是闭合的：GPU lane 的 host/queue
观测，以及 host lane 的 GPU 观测，必须严格为 `unsupported/not_due_at_this_tick`。

collector factory 合同禁止创建任何 descendant。core 在 READY 检查 Python child 集合与 POSIX 独立
process group 的 OS 进程成员；采样快路径绝不启动 `ps`/helper，close、失败、cancel、deadline 在返回前验证整组
quiescent，必要时依次 TERM/KILL，以覆盖 `subprocess`/native descendant。该检测不是
Windows Job Object 的替代：真实 Windows/PowerShell 5.1 collector 接入前必须有 Job Object 硬门禁和真实
spawn smoke；本地纯 Python spawn smoke 只证明当前 core/factory wire contract 可重建。

每 lane mailbox 有独立固定上限且 producer 永不因另一 lane 或 writer backpressure 阻塞；显式 start token
和 monotonic watermark 决定 merge 是否可安全前进，不等待一个尚不存在的未来 head。overflow 丢弃
该 observation 并令 receipt incomplete，后续 written frame/boundary 会机械暴露缺失 deadline。scheduler
从前一绝对 deadline 推进，采集过慢时跳过已过期 slot，不补发 catch-up burst。

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
progress 还必须绑定 receipt 的 process epoch/profile。durable commit 的 source identity 在同一 run
只能出现一次，累计页数必须严格等于前累计加本次 delta；blocked event 携带闭合的起止 monotonic
区间，重叠区间或跨 phase 边界区间不得相加，避免把同时发生的多个阻塞原因重复计时。

`full-gpu-host-hour-kpi.v1` 只接受已验证的 v2 observer coverage 摘要和带原 publish commit UTC、source
identity、profile identity、闭合页数的 durable evidence。bucket 固定为 UTC `[hour, hour+3600s)`；idle、
readiness、restart、故障和恢复均留在分母。coverage gap/overlap、identity drift、unsafe observer、页数冲突
或旧 outbox 缺 profile/runtime 都令结果 incomplete，既不缩短分母，也不补零或外推。late supplement 仍按
原 publish commit hour 回填。host goodput 可跨 profile 汇总；profile-eligible goodput 只在整小时 profile
单一且所有 publish evidence 同 profile 时给出。

当前 relay checkpoint 只是 owner-only、canonical 的本地 cache；它没有独立 anchored head，不能单独证明
restart continuity。生产接线前必须用 append-only evidence ledger/DB cursor 固化最新 head，并从同一事务的
publish evidence replay 推导 first durable publish；调用者布尔值或事后补写文件都不能使 host-hour complete。
同理，当前 artifact adapter 把 resident exporter overhead 固定为 unverified，因此只能生成 incomplete KPI。

receipt 的 lane sample count、边界/相邻最大 gap、late、missed deadline、supported frame 和 required
unsupported observation 数必须从 frames 机械重算。`gpu_fast` 只以 GPU observation 为 required；
`host_slow` 只以 API process、host cgroup 和 queue/vLLM 为 required。其他 lane 上的 `not_due_at_this_tick`
不计为缺失，避免合法的分频采样被误判为永远 incomplete。
每帧 `observed_interval_ns`、`deadline_status` 和 `missed_deadline_count` 必须从相邻 monotonic 时间及
nominal cadence 重算；首帧和末帧还必须分别覆盖 receipt 起止边界。任意一个 nominal deadline 未被
覆盖都令 receipt `incomplete`。每帧 UTC wall time 也必须与 receipt 起点加同一 monotonic delta 在
既定 fixed+ppm 容差内相符，调用者不能用内部自洽但与 receipt 时轴无关的 wall clock 冒充同步证据。

frames、progress、vector、phase capture 与 phase-clock binding 的摘要不得由调用者声明后直接信任。
验证器只接受 canonical UTF-8 JSON bytes，拒绝重复字段、非 canonical 编码和缺失 artifact，并从实际
bytes 重算 SHA-256 与 receipt 逐项核对。`phase-clock-binding.v1` 绑定 phase process epoch、runtime
bundle、container id/start epoch、Windows node、boot identity、observer process epoch、clock domain、
双方 clock source 与 attestor source。只有同一 Linux boot 下双方明确使用
`clock_gettime(CLOCK_MONOTONIC)` 且 capture/receipt identity 全部一致时才允许比较 monotonic 数值；
否则 trace 与 telemetry 只能独立保存和汇总。

API health 的 closed projection 同时要求 `max_pending_tasks_requested` 与
`max_pending_tasks_effective` 为正整数；effective 必须不小于 active slots 和 requested，且
`queued+processing` 不得超过 effective。这样部署带 bounded pending queue 的新 API 不会被旧 parser
误判为字段漂移，同时任何容量降级或伪造零值仍 fail closed。

正式 schema 位于 `contracts/operational/synchronized-*.v1.schema.json`。所有对象 `extra=forbid`，
新增字段或语义必须发布新版本。
