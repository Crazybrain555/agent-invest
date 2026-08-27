# MinerU Capacity Observation v1

## 1. 决议

当前只实现 **Observation v1**：在已经 commissioned 的 MinerU `1 task × inner 7` 外侧，旁路、
只读地观察 API、vLLM、GPU exporter 和 Windows/Docker host。它不接数据库、不读取文档内容、
不改变 worker admission，也没有选择、推荐或激活配置的权限。

当前明确不做：DB backlog/page sampler、业务 pages/hour、phase/overlap 归因、Advisor、capacity
profile、selector、actuator、AIMD/PID 或任何 live knob mutation。现有 `1×7`、window 16、
`max_num_seqs=128` 和 runtime bundle v5 身份不因本功能改变。

这样收窄有五个事实依据：

1. MinerU `completed_tasks` / `failed_tasks` 是 600 秒 retention 内 terminal registry population
   **gauge**，可因 30 秒 cleanup 合法下降，不能计算 delta；
2. reader role 无权读取 eligible parse 所需的 private ops/core 表，document/run 也没有完整源页数；
3. staged 4/8/16 的 4,943 页是重复 stage page-attempt，不是 unique durable pages；
4. 没有 raw→interval→run 的可重放输入，coverage 与分位数结论不能独立复算；
5. API active 且未观察到 vLLM activity 不能证明 CPU phase，更不能证明 overlap 安全。

## 2. 采样面与语义

| source | cadence | required coverage | max valid-sample gap | 字段语义 |
|---|---:|---:|---:|---|
| MinerU API `/health` | 1 s | 99% | 5 s | queued/processing 与 task/window identity；completed/failed 只作 gauge 的 current/min/max/weighted distribution |
| vLLM `/metrics` | 1 s | 99% | 5 s | running/waiting/KV gauges；preemptions 是同 epoch nondecreasing counter |
| pinned nvidia-smi exporter | 1 s | 99% | 5 s | commissioned 单卡的 kernel-busy utilization、显存、功耗、温度；GPU UUID 只保存 SHA-256 |
| pinned Windows collector | 5 s | 100% | 15 s | container epoch digest、restart/OOM/cgroup events、API RSS/HWM、Docker VM memory |

Windows exporter 的 `last_collect_success_timestamp_seconds` 来自远端整数 Unix 秒。freshness 允许最多
1 秒跨机未来时钟偏差并把显示 age 钳为 0；超过 1 秒的未来时间、超过 30 秒的陈旧 sample 或
`last_collect_success != 1` 仍然 fail-closed。该容差是采样契约，不是性能调参旋钮。

采样调度使用 monotonic clock。延迟后从“当前时点 + cadence”继续，不补发 catch-up burst。
run 边界前先取一次 host sample，再并发取 API/vLLM/GPU boundary sample，随后才启动 monotonic
denominator；这样首段有左边界证据，又不会让慢 SSH 把快速指标伪装成刚采到。运行中每个 source
使用自己的实际 completion offset；同批慢 source 只会形成可见 gap，不能刷新其他 source 的时间。
每个 source 在距 run deadline 不足自己的一个 cadence 时停止发起新请求，已有的最后一次有效 sample
仍只按 max-gap 规则覆盖尾段；这避免资格结果取决于边界前几毫秒的正常调度抖动。更早发起但直到
在 run deadline 后才返回的 sample 不把 values 钳成边界 observation，而在右边界写成 closed
`sample_completed_after_deadline` unavailable evidence；若 sampler 本身也已失败，底层 closed reason
另存为 `underlying_reason_code`，不因 deadline 分类而丢失。
一次 available gauge 只持有到下一次 observation、interval end 或 source max gap 中最早者；
unavailable sample 立即截断上一段，不用零或上一值填补。左边界可以使用仍在 max-gap 内的上一
sample；右边界后的首个 sample 只用于关闭前一 hold，不把自己的值倒灌进 interval。

gauge 积分使用上述 half-open hold；epoch、真 counter 与 safety transition 则严格使用“左边界
baseline + `(start, end]` 内 observation”。因此恰落在 60 秒边界的改变只归入前一个 interval
一次，不会在两个 interval 间漏掉或重复；右边界 unavailable 会使该 source coverage incomplete，
右边界 unsafe observation 仍必须进入 safety verdict。

time-weighted p50/p95 使用按“值升序、累计 covered seconds 首次达到 50%/95%”的 nearest-rank
定义。任一 source coverage 未过阈值，interval 必须是 `incomplete`；container epoch、restart/OOM、
reserve crossing 或真 counter reset 会标为 `unsafe`。缺失值不会缩短运行 denominator，也不会变成零。

## 3. 隐私和身份

Observation contract 是 operational contract，位于 `contracts/operational/`，不进入 Filing API。
raw/interval 只允许固定字段；禁止 document ID、证券代码、公司名、标题、路径、URL、API task ID、
PDF hash、Unit 内容、原始 hostname/username 和任意 Prometheus labels。

每个 raw sample 和 60 秒 interval 都包含：随机 run UUID、连续 sequence、previous-record hash、
self hash、runtime bundle SHA-256 和 observer source SHA-256。host 原始 container ID 只在内存中用于
生成 `container_epoch_sha256`，写盘前删除；GPU UUID 同样只保留 hash。
所有 `*_at_utc` 必须使用 `+00:00`；timezone-aware 但非零 offset 仍不属于本契约。

输出位置只能由 `FileStorePathBuilder` 生成：

```text
$DISCLOSURE_RUNTIME_ROOT/reports/capacity/<run-id>/
  raw-samples.v1.jsonl
  intervals.v1.jsonl
  run.v1.json
```

run 目录 new-only、0700；三个文件 new-only、0600、单 hardlink、拒绝 symlink，单记录、文件总字节和
记录数都有上限。raw 与 interval 按 canonical JSONL 写入并 fsync；final run receipt 只有两条 stream
关闭并 fsync 后才创建。中断留下的 partial stream 不会伪装成完整 run。

## 4. 重放和资格边界

`capacity verify` 默认只接受 configured runtime bundle 和 exact-current observer/CLI source；旧 runtime
或旧代码 evidence 不能冒充当前资格证据。它重新检查 owner/mode/link/size、JSON shape、sequence、
两条 hash chain 和 artifact SHA，并从 run duration/interval 机械推导 interval 数量、索引、边界与 UTC，
同时核对每个 raw offset 的 run boundary/UTC。随后从 raw sample 纯函数重建每个 interval，要求
canonical bytes 与记录逐字相等，并要求每个 available GPU sample 的 identity digest 等于当前
configured expected UUID，最后重建 run receipt。
这是 Observation v1 的唯一机械验收；图表或 terminal 单点截图不是资格证据。

Observer failure 只拒绝 observation evidence，不能停止 worker、MinerU 或 GPU。run receipt 固定
`activation_authorized=false`；即使 status=complete，也只说明采样闭合，不代表某个新 profile 可用。

首个真实资格顺序：

1. 当前 v5 `1×7` 不变，observer 旁路运行；
2. 完成 resident baseline，`make capacity-verify ... REQUIRE_COMPLETE=YES`；
3. 本地与独立 reviewer 从 raw 重算 interval/run 一致；
4. 再依据 baseline variance 决定是否值得新增 phase event 或离线 Advisor。

24h、候选 token budget、1×8、双 task/global 7 与收益阈值都不能在 baseline 之前写成假精确门槛。
raw evidence 默认至少保留到独立复算结束；baseline/experiment 建议保留 30 天。清理必须是显式 operator
动作，Observation/worker 不自动 prune。
