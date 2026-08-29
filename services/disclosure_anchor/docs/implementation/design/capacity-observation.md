# MinerU Capacity Observation v1

## 1. 决议

Observation v1 是 MinerU 数据面外侧的纯旁路：只读观察 API、vLLM、GPU exporter 与
Windows/Docker host，不连接数据库、不读取文档内容、不改变 worker admission，也不选择、推荐或激活
配置。它的 receipt 固定 `activation_authorized=false`。

旧的固定 A-B-B-A commissioning、固定 4/8/16 重放、固定 7 GiB reserve、旧 evaluator 与 catalog
builder 已删除。当前调度、容量搜索和 Auto 生命周期只服从
`mineru-throughput-scheduler.md`；Observation receipt 不能被转换成 profile 授权。

## 2. 采样面与语义

| source | cadence | required coverage | max valid-sample gap | 字段语义 |
|---|---:|---:|---:|---|
| MinerU API `/health` | 1 s | 99% | 5 s | queued/processing 与 task/window identity；completed/failed 只是 retention gauge |
| vLLM `/metrics` | 1 s | 99% | 5 s | running/waiting/KV gauge；preemptions 是同 epoch nondecreasing counter |
| pinned nvidia-smi exporter | 1 s | 99% | 5 s | 单卡 kernel-busy、显存、功耗、温度；GPU UUID 只保存 SHA-256 |
| pinned Windows collector | 5 s | 100% | 15 s | epoch、restart/OOM/cgroup、API RSS/HWM 与 Docker VM memory |

Windows exporter freshness 使用远端整数 Unix 秒。只容许最多 1 秒跨机未来偏差；超过 1 秒的未来时间、
超过 30 秒的旧 sample 或 collection failure 都必须 unavailable，不得补零或沿用最近值。

采样以 monotonic clock 调度；延迟后从当前时点继续，不补发 catch-up burst。每个 source 使用自己的实际
completion offset。available gauge 只持有到下一 observation、interval end 或 source max gap 中最早者；
unavailable 立即截断上一段。run 边界、counter、epoch 和 safety transition 使用左边界 baseline 加
`(start, end]` observation，避免相邻 interval 重复记账。

## 3. 隐私、身份与安全

Raw sample 和聚合结果只含 content-free 指标、哈希身份、UTC/monotonic 时钟和 closed reason code；禁止
PDF 内容、文件名、URL、prompt、task ID、原始 GPU UUID、Windows 主机名与 SSH 凭据。端点、runtime、
observer source、collector、node、GPU 与 clock domain 都必须由 exact digest 绑定。

任一 sampler 失败只让 evidence `incomplete`，不得中止健康数据面。OOM、restart、epoch drift、cgroup
memory event、GPU identity drift 或安全阈值越界让 receipt `unsafe`，但 Observation 本身仍不得执行 actuator。

输出固定为：

```text
$DISCLOSURE_RUNTIME_ROOT/reports/capacity/<run-id>/
  raw-samples.v1.jsonl
  intervals.v1.jsonl
  run.v1.json
```

run 目录 new-only、0700；文件 new-only、0600、单 hardlink、拒绝 symlink，并有记录与总字节上限。
raw/interval 用 canonical JSONL 写入并 fsync；两条 stream 关闭并 fsync 后才创建 final run receipt。

## 4. 重放和资格边界

`capacity verify` 只接受 configured runtime bundle 与 exact-current observer/CLI source，重验
owner/mode/link/size、JSON shape、sequence、hash chain、artifact SHA、UTC 和 interval geometry，再从 raw
sample 纯函数重建 interval 与 run receipt。canonical bytes 不一致、GPU identity 不符或 coverage 不完整
均 fail closed；图表和 terminal 截图不是资格证据。

操作入口：

```bash
make capacity-observe \
  RUNTIME_MANIFEST=/private/runtime.json \
  DURATION_SECONDS=600 \
  SSH_HOST=<pinned-host> SSH_USER=<operator-user> \
  SSH_IDENTITY=/private/key SSH_KNOWN_HOSTS=/private/known_hosts

make capacity-verify RUN_ID=<uuid> REQUIRE_COMPLETE=YES
make capacity-summary RUN_ID=<uuid>
```

这些命令只证明旁路证据完整。新的短时 held-out 容量搜索必须另行使用 synchronized telemetry、phase
closure 与 unique durable pages/full host-hour KPI；在对应 runner 和 receipt 完成前保持 STOP。
