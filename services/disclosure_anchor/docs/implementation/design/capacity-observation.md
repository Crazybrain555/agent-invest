# MinerU Capacity Observation v1

## 1. 决议

**Observation v1 本身仍是纯旁路**：在 MinerU 外侧只读观察 API、vLLM、GPU exporter 和
Windows/Docker host。它不接数据库、不读取文档内容、不改变 worker admission，也没有选择、推荐
或激活配置的权限。

另有一条与 Observation 严格分离、默认关闭的 **Capacity Pipeline / Commissioning** 路径，见
§5。它只在受控试验中打开 content-free phase trace 和候选 profile；不会把 Observation receipt
变成 actuator，也不做 AIMD/PID 或运行中文件/环境变量 mutation。生产当前 `1×7`、window 16、
`max_num_seqs=128` 仍须由 exact runtime bundle 身份证明；候选没有通过 A-B-B-A 就不能进入 Auto。

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

1. 当前 v6 `1×7` 不变，observer 旁路运行；
2. 完成 resident baseline，`make capacity-verify ... REQUIRE_COMPLETE=YES`；
3. 本地与独立 reviewer 从 raw 重算 interval/run 一致；
4. 再依据 baseline variance 决定是否值得新增 phase event 或离线 Advisor。

24h、候选 token budget、1×8、双 task/global 7 与收益阈值都不能在 baseline 之前写成假精确门槛。
raw evidence 默认至少保留到独立复算结束；baseline/experiment 建议保留 30 天。清理必须是显式 operator
动作，Observation/worker 不自动 prune。

## 5. Capacity Pipeline 与离线 Auto commissioning

### 5.1 借鉴边界

本实现复用了成熟系统的机制，而不引入它们的 control plane：

- [PaddleOCR-VL/PaddleX](https://paddlepaddle.github.io/PaddleX/3.5/en/pipeline_usage/tutorials/ocr_pipelines/PaddleOCR-VL.html)
  的 `use_queues` 将 PDF 渲染、layout 与 VLM 分阶段异步执行；本地对应 A(render/layout) →
  B(vLLM) → C(postprocess/append)，但仍是单进程、同文档、有序提交；
- [NVIDIA DALI](https://docs.nvidia.com/deeplearning/dali/main-user-guide/docs/advanced_topics_performance_tuning.html)
  默认 shallow prefetch depth=2，并明确指出加深队列会增加内存；本地固定 depth=1、最多两个
  resident window，只用实际可测的 decoded bytes 和 page credits，不伪造 working-memory 精度；
- [Ray Data](https://docs.ray.io/en/latest/data/data-internals.html) 的就绪/背压思想映射为
  C→B→A 的释放优先级和原子 credits；不新增第二 durable queue；
- [Triton Model Analyzer](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/model_analyzer/README.html)
  的 constrained configuration search 映射为冻结语料 A-B-B-A；本地不做 live hill-climb；
- vLLM 继续拥有 GPU request continuous batching，文档 pipeline 不实现第二个 GPU batcher。

因此优化目标不是瞬时 GPU 100%，而是安全条件下的 verified pages/host-hour；VLM supply gap、GPU
duty、waiting/KV 只用于定位瓶颈。任何 OOM、restart、preemption、host reserve crossing、结果 hash
变化、trace/fallback 缺口都会先否决候选。

### 5.2 运行时不变量

- `MINERU_CAPACITY_MODE` 只接受 `legacy|candidate|auto`；正常构建默认 `legacy`；
- profile 是 canonical `mineru-execution-profile.v2`，hash 绑定 window、depth、resident pages、
  source/document envelope、inner concurrency 与 vLLM capacity；文档 admission 后不可变；
- `candidate` 只用于受控试验；profile 本身不携带自声明授权位。`auto` 只接纳 canonical
  `mineru-capacity-catalog.v1` 精确绑定 profile、COMMISSION receipt/evaluator 与当前 runtime
  compatibility fingerprint 的文档，其余配置缺失或漂移一律 fail closed；
- commissioning receipt 闭合 exact evaluation、8 份 arm 输入 hash、collector identity 与当前 evaluator
  bundle；bundle 机械哈希 CLI、staged-load、phase capture、commissioning、deployment gate 和 identity
  的 9 个源文件。catalog builder 会从当前字节重算 bundle 并要求完全相等，防止旧 evaluator receipt
  在代码漂移后继续授权；这是一条 trusted-operator 的 reproducibility gate，不冒充对有本机写权限者的
  密码学签名；
- Auto 候选只有在首个可观察 append 之前、所有 task/owner/credits 已 drain 时才可整文档回退一次；
  append boundary 原子关闭后任何错误都 fail-visible，绝不混合 candidate/legacy 输出；
- phase trace 禁止路径、PDF hash、文档 ID 和内容，只记录 opaque process/trace identity、页面范围、
  profile hash、单调区间与 decoded-credit 证据；完整 DAG 必须绑定外部 attested profile hash；
- host memory 安全由外部 5 秒 Docker/cgroup observer 和 reserve 硬门证明。内部没有可验证的实际
  working-set 数值，所以不输出一个由 page 数估算的“observed working bytes”。

### 5.3 A-B-B-A 机械晋升

四个 arm 必须使用同一 frozen heterogeneous corpus、同一顺序、同一 node/model/client/writer、
同一 API image 和稳定 proxy/vLLM epoch：A1 legacy → B1 candidate → B2 candidate → A2 legacy。
四份 staged receipt 还必须绑定同一 `whole-document-runaway-and-drain.v1` safety limits；document
runaway 与 API drain 均不得低于 86400s。它们只防 live-but-never-return/无法证明 drain，不参与候选
吞吐调参；任何缺失、缩短或 arm 间漂移都在算 throughput 前 fail closed。
兼容镜像的重复本机构建显式关闭 BuildKit provenance attestation；源码、base digest、Dockerfile/patcher
hash、镜像 labels/marker 和 live runtime attestation 仍逐次校验。原因是 provenance manifest 带执行期
元数据，会让相同业务 manifest/config 的外层 OCI index digest 跨构建漂移。该项只硬化首次构建，
不替代 campaign identity：A1 前必须外部冻结完整 API image ID；所有 arm 切换显式传入该 ID，安装器
复用正式 tag、禁止 build/tag/image-rm，只以 `--no-build --no-deps --force-recreate mineru-api` 定向重建
API，并在前后机械证明 proxy/vLLM 的 container ID、StartedAt 和 image ID 完全不变。
每个 arm 运行既有 `make mineru-staged-load`，随后用对应 receipt 的 UTC/host epoch 只采集严格
`MINERU_PHASE_TRACE ` 行：

```bash
make mineru-phase-trace-capture \
  STAGED_RECEIPT=/private/arm.json CAPACITY_MODE=candidate \
  PROFILE_SHA256=sha256:<profile> CAPTURE=/private/arm.trace.json \
  SSH_HOST=<pinned-host> SSH_USER=<operator-user> \
  SSH_IDENTITY=/private/key SSH_KNOWN_HOSTS=/private/known_hosts
```

采集器在 Windows 先过滤日志，再传回 Mac；非 trace 日志不会进入 evidence。它限制 6 小时、
100,000 行、64 MiB，绑定 collector hash、node、container ID/start/restart/OOM、capacity mode 和
profile hash，并在落盘前复算每个 document DAG、overlap、document/page conservation。

四组完成后运行：

```bash
make capacity-commission \
  A1_RECEIPT=/private/a1.json A1_CAPTURE=/private/a1.trace.json \
  B1_RECEIPT=/private/b1.json B1_CAPTURE=/private/b1.trace.json \
  B2_RECEIPT=/private/b2.json B2_CAPTURE=/private/b2.trace.json \
  A2_RECEIPT=/private/a2.json A2_CAPTURE=/private/a2.trace.json \
  LEGACY_PROFILE_SHA256=sha256:<legacy> \
  CANDIDATE_PROFILE_SHA256=sha256:<candidate> \
  WINDOWS_NODE_IDENTITY_SHA256=sha256:<node> \
  DOCKER_MEMORY_RESERVE_BYTES=7516192768 \
  MINIMUM_IMPROVEMENT_BASIS_POINTS=500 \
  MAXIMUM_REPEAT_SPREAD_BASIS_POINTS=300 \
  RECEIPT=/private/new-capacity-commissioning.json
```

本机本轮在 A1 前预声明 `minimum improvement=500 bps`、`maximum within-mode repeat spread=300 bps`；
值进入 v2 receipt，不能看完结果后降低。机械式同时要求 `min(B1,B2)>max(A1,A2)`、候选相对收益
至少达到 500 bps、A 与 B 各自重复离散都不超过 300 bps，而且候选绝对收益必须大于 A/B 两组中
较大的实测重复噪声。分母直接使用已验证的 UTC span；receipt 的 monotonic elapsed 与该 span 必须在
50 ms 内一致，否则 STOP。速率用 decimal/fixed-point 计算，避免容差制造假收益或跨运行时浮点漂移。

四 arm 必须从 UTC 证明真实、非重叠的 A1→B1→B2→A2 顺序；source identity、page count、block
count 逐项相等，28 个 staged documents 与 trace 页数守恒。每次 provider bundle SHA 必须自身合法并
留证，但 bundle 容器可含执行期 provenance，不能要求跨执行字节相等。本 RTX 5080 policy 还显式
绑定 7 GiB Docker reserve 与 versioned collector path/hash；commissioning 复用完整 host verifier，
重算 5 s cadence、15 s max gap、UTC/observed seconds、VM total、summary、epoch、OOM/restart 与 reserve。
metrics 必须没有 sampling gap/preemption，并满足 waiting peak<32、waiting p95≤10、KV peak<0.90、
terminal drain≤120 s。API completed/failed 是 600 s retained terminal gauges，只验证 exact range、两端
idle、真实 processing activity 与 drain，绝不把它们误作累计任务 delta；逐文档 PASS 才是完成证明。

输出 `profile_commissioning_authorized=true` 只允许 operator 用 `make capacity-catalog` 生成一个
new-only、canonical、hash-bound catalog；它不自动改 compose、不重启服务、不启动 worker。Auto
仍需把 catalog 从版本化 Windows runtime 路径只读挂载、重新 exact runtime v6 attestation，并通过
fallback/cancellation smoke。
