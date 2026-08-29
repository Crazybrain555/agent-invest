# 生产运维 Runbook（disclosure_anchor，单机单人）

2026-07-14 上线加固批次（batch 4）建立。读者是三个月后忘光细节的运维者本人。
配置生效矩阵见 `config/README.md`；健康一眼看 `make doctor-full` + `make worker-status`。

## 1. 开机 / 重启顺序

正常情况全自动：`com.agentinvest.postgres`（launchd 一次性 pg_ctl start，等 AgentSSD 挂载）
→ `com.agentinvest.disclosure-worker`（KeepAlive 常驻）。人工核对：

```bash
launchctl list | grep agentinvest     # 五个 label：postgres；doctor/gc；tunnel/worker
make pg-status && make doctor-full    # exit 0 才算活
make worker-status
```

手工恢复（自动链路失效时）：`make pg-start` → `make worker-restart` → `make doctor-full`。
launchd job 丢失时重装：`make install-ops-launchd`（postgres+doctor+gc）、
`make install-mineru-tunnel`（MinerU tunnel）、
`./scripts/install_launchd.sh`（worker）。其中 postgres 是一次性启动，doctor/gc 是日历任务，
tunnel/worker 是常驻链路。

### 1.1 首次从旧 worker 切换到当前 plist

这条路径只用于旧 job 的有效 `ExitTimeOut` 仍小于 60 秒、且旧代码还不认识
`parser_cancelled` 的首次切换。此时禁止 `kickstart -k` 或让安装脚本自动 bootout；
否则 launchd 会在 5 秒后强杀长文档，既可能留下 MinerU 临时 API，也会消耗业务重试。

1. 先通过本分支全部发布门，再写入 disabled 标记，但不终止当前进程：

   ```bash
   WORKER_DOMAIN="gui/$(id -u)"
   WORKER_LABEL="com.agentinvest.disclosure-worker"
   launchctl disable "$WORKER_DOMAIN/$WORKER_LABEL"
   ```

   这只是安装前置状态，不是互斥保证。2026-07-25 首次切换实测：一个已经 loaded
   的 KeepAlive job 仍可在 Python 子进程退出后、`bootout` 前立即重拉 wrapper。
   因此不得在下文安全零点先单独终止 Python；真正阻止重拉的是对 loaded job 的
   `bootout`。

2. 等旧波次自然排空。三个条件必须同时为零：

   - `disclosure_core.processing_run` 中 `run_kind='parse' AND status='running'`；
   - `pgrep -afil '/bin/mineru -p |mineru.cli.fast_api'` 的旧 MinerU/临时 API；
   - vLLM `/metrics` 的 `vllm:num_requests_running` 和
     `vllm:num_requests_waiting`（先排除其他合法客户端）。

3. 零点可能很短。`launchctl` 显示的是 zsh wrapper PID，不能停它（Python 子进程会继续
   补槽）；必须解析且只接受它唯一的直接 `disclosure_anchor.cli.worker loop` 子进程。
   对 **Python PID** 先 `SIGSTOP` 冻结，再重复核对上述三个条件；若任一非零，
   `SIGCONT` 后继续等。三者仍为零时，保持 Python 为 STOP 并直接移除整个 loaded job：

   ```bash
   WRAPPER_PID="$(launchctl print "$WORKER_DOMAIN/$WORKER_LABEL" |
     awk '/pid =/{print $3; exit}')"
   PYTHON_PID="$(pgrep -P "$WRAPPER_PID" -f \
     'disclosure_anchor.cli.worker loop')"
   case "$PYTHON_PID" in
     ""|*$'\n'*) echo "expected exactly one worker Python child" >&2; exit 1 ;;
   esac
   kill -STOP "$PYTHON_PID"
   # 在进程保持 STOP 时重新核对 PG、MinerU/API、vLLM 三个零条件。
   # 任一非零：kill -CONT "$PYTHON_PID"，继续等待；不得 bootout。
   #
   # 三项仍为零：保持 Python 为 STOP，直接移除整个 loaded job。不要先
   # TERM/CONT Python；否则旧 KeepAlive 可在 bootout 前重拉一轮新任务。
   launchctl bootout "$WORKER_DOMAIN/$WORKER_LABEL"
   while launchctl print "$WORKER_DOMAIN/$WORKER_LABEL" >/dev/null 2>&1; do
     sleep 1
   done
   # 再确认 wrapper/Python/MinerU/API 均不存在，且 PG/vLLM 仍为零。
   ./scripts/install_launchd.sh
   ```

4. 安装器必须先看到一条启动后新写入的 `worker_progress.v2`，再确认同一 PID 连续稳定且
   `ExitTimeOut` **有效值至少 60 秒**。模板请求 90 秒；2026-07-25 当前 macOS 对 user LaunchAgent 实测把 plist 的
   90 秒请求报告为 60 秒；安装器校验有效下界而不是假设请求值会原样呈现。
   安装脚本发现 label 仍 loaded，或仍有 MinerU CLI/临时 API 进程，都会退出 75；这是安全
   保护，不得绕过。

完成首次切换后，常规代码/env 重载才使用 `make worker-restart`；新 worker 的取消是
retry-neutral，且有效值至少 60 秒（worker 自身 graceful window 为 35 秒）会给官方
cleanup 路径和 wrapper 回收留出余量。

**GC label 的同等静默（未来获批的派生全量重置前必做）**：静默
`com.agentinvest.disclosure-gc` 与 worker 切换无关；operator 必须逐个检查 worker 和 GC
**两个 label**，要求它们都既未 loaded、又已持久 disable，缺一即 fail loud。GC 是
19:30 日历作业、没有 KeepAlive，不需要上面的排空舞蹈：

```bash
GC_DOMAIN="gui/$(id -u)"
GC_LABEL="com.agentinvest.disclosure-gc"
launchctl disable "$GC_DOMAIN/$GC_LABEL"
launchctl bootout "$GC_DOMAIN/$GC_LABEL" 2>/dev/null || true
# 两项都必须成立才过门：disable 记为 disabled，且 print 找不到该 label。
launchctl print-disabled "$GC_DOMAIN" | grep "$GC_LABEL"   # 期望 => disabled
launchctl print "$GC_DOMAIN/$GC_LABEL" >/dev/null 2>&1; echo "loaded? exit=$?"  # 期望非 0
```

恢复必须显式做，disable 是持久状态，重启机器不会自愈，否则 GC 静默停摆、派生垃圾无限堆积：
worker 走 `./scripts/install_launchd.sh`，GC（连同 postgres/doctor）走
`make install-ops-launchd`。两个安装器都拒绝替换任何 loaded label；operator 先确认 idle 并显式
bootout。安装器随后预渲染/校验全部 plist、按需清除 persistent disable、bootstrap，并在任一步失败时
bootout 新 job、恢复旧 plist 和原 disabled 状态。恢复后用上面两条命令反查（期望状态为
`=> enabled`、`print` 退出 0）。

### 1.1a MinerU runtime bundle attestation（任何 fresh parse 的前置）

parser target 契约要求 `DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256`
（否则 parse 在 parser_identity 阶段 fail loud）。它必须来自 operator/provider 保存于仓外的
canonical runtime manifest v6，分别绑定本地 client、固定 MinerU API orchestrator、vLLM inference
server 与网络 topology。它至少包含 derived API image、原始 base image 和 inference image 三个
immutable digest、模型仓库与不可变 revision、served
model ID、两侧配置/env/mount/network policy hash，以及 pinned SSH host-key 与 Windows node identity；以 sorted-key、
UTF-8、无多余空白的 canonical JSON 对 `manifest` 对象计算 hash 后
写入 worker.env。`scripts/attest_mineru_runtime.py --mineru-bin "$DISCLOSURE_MINERU_BIN"`
只测量本地 client venv，是 manifest 的一个输入，**不能直接冒充完整 runtime digest**。任一
client、image、模型或配置变化都必须重做 manifest/digest 并重启 worker。
manifest 还必须逐项记名本地内容相关包的精确版本，至少包括 `mineru`、`pdftext`、`pypdfium2`
和 `mineru-vl-utils`；venv 路径、Python 版本或环境总 hash 不能替代这些可对比的组件身份。

fresh deployment 必须先在 worker/GC 保持 unloaded + persistently disabled 时运行 DB-free gate：

```bash
make mineru-smoke \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  RECEIPT=/private/path/mineru-smoke-receipt.v4.json \
  CANARY_CACHE=/private/path/mineru-canary.v2.json
```

该命令不读取 PostgreSQL 或队列，也不会把 worker/CNINFO/DB/admin 凭据传给 MinerU 子进程；
它从实际 venv package listing 机械核对 manifest canonical hash、本地 client digest、四个内容相关包、
固定页窗口与 writer code digest，以及远端 immutable
API image/config/env/mount/network、inference image/model/config/env、live served model ID、唯一出现的
`max_num_seqs=128` 与 `mm_processor_cache_gb=0`，并要求 API health 固定为 MinerU 3.4.4/protocol 2、
task slots 必须等于 manifest/worker profile（当前 30 GiB Windows profile 为 1）、window=16、retention=600s、cleanup=30s。它连续三次走 96×48 `M7` PNG 的精确 OCR
多模态请求，再通过固定 API 对冻结单页 PDF 跑一次官方 full-PDF Hybrid-medium writer/artifact reader。
API before/after 必须原样保留 retained terminal gauges 且两端 queued=processing=0；smoke 成功由
official writer、ProviderDocument 和清理证据证明，不对 completed/failed 人口 gauge 做差值推断。独立 `TMPDIR`、MinerU 进程和
外部 `mineru-api-client-*` 差集
必须在 PASS 前都证明为零，只保留新建且路径不同的显式 receipt/cache；已有输出路径会 fail closed，
不能覆盖旧 PASS。smoke 成功只产生 bootstrap 证据，此时仍不得启动 worker；先继续完成下述
staged-load。最终把三条新路径分别写入 `DISCLOSURE_MINERU_SMOKE_RECEIPT`、
`DISCLOSURE_MINERU_CANARY_CACHE`、`DISCLOSURE_MINERU_STAGED_LOAD_RECEIPT` 与第二轮
`DISCLOSURE_MINERU_STAGED_LOAD_CONFIRMATION_RECEIPT`，并固定
`DISCLOSURE_MINERU_CANARY_MAX_AGE_SECONDS`（规模回补使用 30 天启动租约）。resident
worker 在连接 PostgreSQL 前机械重算内嵌 manifest、冻结输入、endpoint/runtime/provider/client/code/
window/served-model/request identity、cleanup 和有效期；缺失、陈旧或漂移都会拒绝启动，不能拿旧
cache 代替。租约只裁决一次 process composition，避免一个健康多日任务因固定时间戳在中途停止补槽；
任何新进程仍重新检查完整租约。常驻 admission 按 `DISCLOSURE_MINERU_LIVE_PROBE_INTERVAL_SECONDS`
（默认 300 秒）核对 API health/orchestrator 合同和 `/v1/models` 唯一 model ID，process-local incident
立即作废当前 checker 的缓存 proof，并在任一 remote-drain owner 活跃时暂停新 admission；最后一个
owner 退出后仍须重新证明 API idle、精确模型身份和稳定 incident generation 才能恢复。transport、
408/429/500/502/503/504 与截断响应进入有界退避；4xx/501、schema/version/capacity/model drift 仍
fail closed。这些 live probe 不是逐文档 full OCR，也不能替代部署 smoke。
v6 manifest 是闭合字段合同且会内嵌到 mode 0600 的私有 receipt；其中只能存不可变身份、hash 与
非秘密启动参数，command 出现 credential flag 会 fail closed，原始 token/密码不得进入 manifest。

MinerU 3.4.4 在 WSL/FastAPI 连续处理大 PDF 后可能把已经 free 的 glibc arena 长期保留在 API
PID1 RSS；本机真实 heavy 文档已复现该行为，与上游
[issue #5313](https://github.com/opendatalab/MinerU/issues/5313) 的现场一致。当前使用一个临时、
精确源码兼容层，而不是复制未合并的 [PR #5354](https://github.com/opendatalab/MinerU/pull/5354)：
derived image 固定 base digest、MinerU 版本和三个源文件 preimage hash，只在每个处理窗口及文档
final cleanup 后显式调用 glibc `malloc_trim(0)`；开关必须为闭合值，启用时缺少 glibc/hook 会
fail loud。collector、install receipt 和 manifest v6 同时绑定 patcher/Dockerfile hash、derived
image ID、base digest、策略名、patched source hash 与 live hook；任一漂移都拒绝准入。它不改变
解析语义、页窗口或并发，只把 allocator 可归还的空闲页返还给 OS；上游正式修复合并并通过同一
held-out 验收后，应删除这个兼容层而不是永久形成私有 fork。

首次安装或兼容层字节变化时，先把 compose、collector、`Dockerfile` 和 patcher 四份已审阅
文件复制到 Windows 同一临时发布目录，再在 worker/GC disabled、API/vLLM idle、旧证据已保存的
条件下运行版本化安装器：

```powershell
& C:\path\to\install_mineru_fixed_api.ps1 `
  -ComposeSource C:\path\to\release\mineru-windows.compose.yaml `
  -CollectorSource C:\path\to\release\collect_mineru_runtime.ps1 `
  -CompatDockerfileSource C:\path\to\release\mineru_heap_trim_compat\Dockerfile `
  -CompatPatcherSource C:\path\to\release\mineru_heap_trim_compat\patch_mineru_344.py
```

安装器在改 live compose 前先校验 base image、旧 API/vLLM idle、四个 source、compose config，
并以 `--pull=false` 构建和验证唯一临时 tag；完成 target writable/output-root 预检并保存旧
stable tag→image ID 后，才把固定发布 tag 原子指向新 image。随后只写固定 compose 以及
`C:\ProgramData\agent-invest\mineru-runtime-v6\` 下的 collector/receipt。任何部署后 identity、
health、网络、egress、空 output-root 或 formal collector 校验失败都会恢复旧 tag 映射、旧 compose、
旧 collector、旧 receipt 和旧容器运行态，并删除临时 tag。不要手工把 v6 reader 改回兼容任意历史路径，也不要在同一次处置中
重启 Docker Desktop、Windows、Tailscale 或 v2rayN。
安装器与 collector 在 Windows PowerShell 5.1 中通过同一 `System.Diagnostics.Process` 调用层取得
显式 `ExitCode/stdout/stderr`，并并行排空双流、以 UTF-8 字节写入 `docker exec -i`；不得改回依赖
`$LASTEXITCODE` 的调用运算符，也不得跳过 exact source preflight。

bootstrap 与 steady state 是两个独立 profile：bootstrap 不启动 resident worker、不开放 queue
admission；steady state 只在当前 runtime smoke 与两次相互独立的完整 staged load 通过后使用已验证
包络。当前候选 steady 参数是 `WORKER_BATCH_SYNC=13`、download/parse=`50/50`、
backfill waterline=`2000`、loop=`900..1800`、document concurrency=`16`、GPU budget/max-seq=
`7/128`、API task slots/inference cap=`1/7`、worker client outstanding window=`1`、finalize=`2`、oversized=`10240 KiB`、
soft parse expected=`3600s`。任何新 OOM/EngineCore death、429/5xx、持续 preemption 或 vLLM
waiting≥64 持续 30 秒都停止 staged load；不能据一次利用率截图扩大 1 outstanding / 7 active / 128 seq。

在同一 v6 manifest 的 smoke PASS 后、启动任何 producer 前，先从当前受信 host collector 冻结一次
campaign service epoch。该命令不发送推理请求、不读 DB/队列，只记录 proxy 与 vLLM 的 container
ID/StartedAt、collector/node 身份和同一 Docker memory reserve；receipt 必须是 mode 0600 的新路径：

```bash
make mineru-campaign-epoch-freeze \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  EPOCH_RECEIPT=/private/path/mineru-campaign-epoch-freeze.v1.json \
  SSH_HOST=<pinned-windows-host> SSH_USER=<operator> \
  SSH_IDENTITY=/private/path/operator-key \
  SSH_KNOWN_HOSTS=/private/path/known_hosts \
  DOCKER_MEMORY_RESERVE_BYTES=<operator-calibrated-bytes>

CAMPAIGN_EPOCH_SHA256="$(jq -r '.campaign_epoch.observed_sha256' \
  /private/path/mineru-campaign-epoch-freeze.v1.json)"
```

冻结必须发生在本轮最后一次允许的 proxy/vLLM 恢复之后；任一服务重启都会使该值和此前全部 arm
失效。随后显式运行固定 staged-load gate：

```bash
make mineru-staged-load \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  CORPUS_MANIFEST=/private/path/frozen-real-pdf-corpus.v1.json \
  EXPECTED_CORPUS_SHA256=<reviewed-canonical-corpus-sha256> \
  EXPECTED_CAMPAIGN_EPOCH_SHA256="$CAMPAIGN_EPOCH_SHA256" \
  RECEIPT=/private/path/mineru-staged-load-receipt.v7.json \
  SSH_HOST=<pinned-windows-host> SSH_USER=<operator> \
  SSH_IDENTITY=/private/path/operator-key \
  SSH_KNOWN_HOSTS=/private/path/known_hosts \
  DOCKER_MEMORY_RESERVE_BYTES=<operator-calibrated-bytes> \
  DOCUMENT_RUNAWAY_TIMEOUT_SECONDS=86400 \
  API_DRAIN_TIMEOUT_SECONDS=86400 \
  WORK_ROOT=/private/disposable-scratch

# 第一轮完整 PASS 后，用不存在的新路径原样再运行一次：
make mineru-staged-load \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  CORPUS_MANIFEST=/private/path/frozen-real-pdf-corpus.v1.json \
  EXPECTED_CORPUS_SHA256=<same-reviewed-canonical-corpus-sha256> \
  EXPECTED_CAMPAIGN_EPOCH_SHA256="$CAMPAIGN_EPOCH_SHA256" \
  RECEIPT=/private/path/mineru-staged-load-confirmation-receipt.v7.json \
  SSH_HOST=<same-pinned-windows-host> SSH_USER=<same-operator> \
  SSH_IDENTITY=/private/path/operator-key \
  SSH_KNOWN_HOSTS=/private/path/known_hosts \
  DOCKER_MEMORY_RESERVE_BYTES=<same-operator-calibrated-bytes> \
  DOCUMENT_RUNAWAY_TIMEOUT_SECONDS=86400 \
  API_DRAIN_TIMEOUT_SECONDS=86400 \
  WORK_ROOT=/private/disposable-scratch
```

该入口没有自定义 stage 参数，固定按 4、8、16 份真实 PDF 运行 official writer。两个 86400s
值是 whole-document runaway 与远端自然 drain 的灾难保险，不是吞吐调参或普通 SLA；formal gate
拒绝更小的值，并把两者写入 exact receipt。正常慢文档不得仅因耗时越过 soft expected 而失败。
若私有环境把任一对应 setting 提高到 86400s 以上，命令必须传入与当前 settings 完全相同的两个值；
deployment gate 不接受“测试边界”和“实际 worker 边界”漂移。
每个 v7 arm 在任何 PDF admission 前先要求当前 host epoch 精确等于冻结值，再对 attested served model
运行一次现有 96×48 `M7` direct multimodal canary；三阶段全部 PASS、自然 drain 且 epoch 仍相同后，
再运行一次同请求 canary。`/health`、`/v1/models` 或空 scheduler gauges 不能替代这两个边界证明。
canary 的 models GET 固定单次 15s logical wall deadline，completion POST 固定单次 90s logical wall
deadline；两者都使用 direct `http.client`、不读取环境 proxy，且响应 header/body 每次 read 共享同一
deadline、严格 framing 与 1MiB 上限，slow-drip 不能把 socket idle timeout 延长成无界等待。
pre-arm 失败时零 admission；workload/epoch/observer 失败时不得用 post-arm canary 掩盖主失败；post-arm
失败则整 arm FAIL。v6 及更早 staged receipt 不含该合同，只可用于 RCA。
每个 stage 都从
冻结 corpus 机械选出 regular、heavy、huge 各至少一份，再按 corpus 顺序补足精确集合。corpus
至少包含 16 个不同 SHA，且必须同时覆盖 regular（<80 页）、heavy（80–499 页）和 huge（≥500 页）；
单页 smoke fixture 或重复同一 PDF 不能冒充负载验证。4/8/16 是每阶段处理的文档总数，不是同时
提交数；客户端 API-facing outstanding 固定为 `min(stage count, task slots)`（当前为 1），huge/未知页数任务
独占该窗口。其余 backlog 只保留在 durable queue，不预先提交到 process-local API registry。固定 API 的 processing task 上限来自 v6 manifest（当前为 1），每个 active task 的
server-side inference cap 是 7，因此当前 active upper bound 为 7。
启动前会拒绝 active worker/pipeline producer、MinerU 残留进程和 `mineru-api-client-*` 残留目录，核对 v6 manifest、
本地 client/content packages、writer code、window=16、provider runtime 与 served model，并先探测
`/v1/models` 和 `/metrics`。同一版本化远端 collector 还会在整轮前、中、后每 5 秒从进程外采样三只
容器的 ID/StartedAt/restart/OOM/exit、cgroup memory、PID1 RSS/HWM 与 Docker VM available memory；
epoch 变化、restart/OOM、15 秒以上采样缺口或跌破 operator-calibrated reserve 均 fail closed。
每阶段运行中及结束时都采样 running、waiting、preemptions、KV cache；
每个逻辑 metrics 样本只对 transport failure 允许在同一 10 秒总预算内做两次最长 4.5 秒尝试；HTTP
429/5xx、响应超限、缺字段或非法值不重试。两次 transport 尝试都失败只形成一个 transport-only
sample gap。transport-only gap 只把 observer 切到 `DEGRADED_TRANSPORT`，不会终止仍健康的 MinerU
数据面；阶段继续自然 drain，并原样记录 gap 与后续恢复。由于 gap 期间无法证明 waiting/preemption/KV
安全，任意 sampling failure 或缺失终样本都会把该阶段判为 evidence-incomplete，不能形成 PASS receipt、
不能进入 commissioning；它与 OOM/restart/reserve/preemption 等 operational failure 的区别仅是不得因此
杀死健康解析进程或丢弃已完成输出，不是降低 promotion 门槛。
同时连续采样 API `/health`：processing 不得超过 attested task slots；window=1 时不要求人为制造 process-local queue。
一次可分类为 `MinerUOrchestratorUnavailableError` 的 transport gap 只记录
`DEGRADED_TRANSPORT`、关闭后续 admission 并继续当前 owner 的自然 drain；恢复后的样本必须原样保留，
但该阶段仍是 evidence-incomplete。health JSON/identity/slot/window 的严格合同错误仍是 operational failure。
`completed_tasks`/`failed_tasks` 是保留期内 terminal registry 的人口 gauge，可随 600 秒 retention/30 秒
cleanup 合法下降；v7 receipt 原样保留 baseline/samples/terminal 与 min/max，但禁止把它们解释为累计任务账。
修复前缺少 orchestrator observer/sampling-failure 固定字段的旧 v6 receipt 只可用于 RCA；exact-current
deployment/commissioning verifier 会拒绝其 shape，不能用于 catalog、Auto 或 worker admission。
每份输入是否成功改由 exact input SHA、official writer 零退出、完整源/输出页数相等和 provider bundle hash
逐文档证明；每阶段仍必须自然 drain 到 queued=processing=0。API 没有 cancel endpoint；observer-only
failure 只能关闭新 admission，不能终止当前本地 CLI 来冒充远端 cancel。文档自身失败或 operator 本地中止
可以终止本地 CLI，但两种路径都必须在同一 deadline 内容忍短暂 health transport failure，并等待远端自然
drain。无法证明 drain 就 FAIL，不能重启服务、进入下一阶段或把 receipt 用于 admission。
metrics 持续不可用、waiting≥64 连续 30 秒、preemption counter 任意变化，以及任一解析失败、429/5xx、
overload、OOM 或 EngineCore failure 都立即 FAIL，清理本地子进程/临时目录且不进入下一阶段。
每阶段 receipt 除 min/max 外还保留 running/waiting/KV 的 p95；当前只作容量诊断，不在没有现场
证据时把历史经验阈值升级成正确性门禁。
receipt 必须是不存在的新路径，按 mode 0600 创建；每一轮 PASS 必须同时有三阶段完整逐文档结果、
retained-gauge 原始观测和零清理残留；两轮必须是时间上相继、路径不同的完整运行，
FAIL receipt 不能用于开启 steady state。parse-capable worker/pipeline/admin 会在连 DB 前重算并核验
这个 PASS 的 runtime/client/code、异构 corpus、逐阶段/逐文档、metrics、时间与 cleanup；路径缺失、
证据漂移或早于对应 smoke 一律拒绝入场。该命令声明 database/queue access 均为 none；不得与 worker、
pipeline 或 API producer 并行执行，也不得将其输出目录指向 AgentSSD 的正式 derived root。

### 1.1b Semantic provider chain

默认是 Luna low 主用、canonical Sonnet 5 low 备用。自定义完整链只写入仓外
`DISCLOSURE_SEMANTIC_PROVIDERS_JSON`；修改后先跑配置/单测和 provider probe，再重启 worker。
只有 executable/auth/quota/transport/timeout 的闭合 availability 原因会切备用，协议/结果/安全
错误与取消不会。配置格式、收据和终态矩阵见
`../design/semantic-adjudication-runtime.md`。不要用 model alias 代替 canonical identity。

### 1.1c Worker progress and GPU telemetry

前台启动用 `make worker-loop`：取得 singleton、通过部署门后会立刻输出公司同步与文档发布两条
进度条，随后按 `WORKER_REPORT_INTERVAL_SECONDS` 更新当前工作、download/parse/build/publish
队列与死信。文档分母随发现增长，终端明确标记 `dynamic total`；不得把它伪装成固定总任务数。
`make worker-status` 可在不触发 MinerU admission、不改变 DB 的前提下读取同一快照；Agent 使用
`python -m disclosure_anchor.cli.worker status --format json`。

每次 resident 快照还 append 到
`$DISCLOSURE_RUNTIME_ROOT/reports/progress/YYYY-MM-DD.jsonl`，合同固定为 `worker_progress.v2`、
文件 mode 0600。未来 SSE/前端只做这个事件的 adapter，不另造第二套进度状态。事件分别记录固定
API `/health` 的 queued/processing/completed/failed 与 task-slot identity，以及 vLLM `/metrics` 的
request running/waiting、preemption 与 KV cache；实际 GPU compute utilization 只从显式配置的
`DISCLOSURE_GPU_METRICS_URL` 读取：Linux 可用 NVIDIA DCGM，原生 Windows 使用固定版本、校验和、
loopback-only 的 nvidia-smi exporter，再经专用 SSH LocalForward 暴露给 Mac。未配置或探针失败时
显示 unavailable，绝不拿 KV cache、请求并发或单次截图冒充 GPU 利用率。旧
`DISCLOSURE_DCGM_METRICS_URL` 仅作同 URL 兼容别名。所有探针都是 best-effort observation，失败
不得停止健康数据面。
事件含进程实例 ID 与单调 sequence/event ID，消费方可区分 worker 重启和事件缺口；探针失败区分
endpoint 不可达与 metric contract 不满足。
Windows exporter freshness 使用远端整数 Unix 秒，允许最多 1 秒跨机未来偏差；更大的时钟漂移、
超过 30 秒的旧 sample 或 collection failure 仍显示 unavailable，不得用最近值填补。

当前 vLLM 0.21 MinerU2.5-Pro 服务还必须把
`--mm-processor-cache-gb 0` 纳入远端容器命令和上述 manifest。2026-08-13 的 221 页半年度
样本连续两次触发 vLLM 多模态 IPC cache 失步（`Expected a cached item for mm_hash`），远端
返回 500；关闭该 cache 后同一页窗口与完整文档重放恢复。这个参数是运行时身份的一部分，
不能只在手工 compose 中修改而继续沿用旧 digest。

### 1.1d Passive capacity observation

需要判断 GPU duty、vLLM waiting/KV、API active/idle 与 Windows/Docker memory 时，使用独立的
Observation v1，不提高 `WORKER_REPORT_INTERVAL_SECONDS`，也不从 `worker_progress.v2` 的文档/队列
字段推断 capacity。Observation 不连数据库、不控制 worker、不改变 current `1×7`：

```bash
make capacity-observe \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  DURATION_SECONDS=3600 \
  SSH_HOST=<pinned-host> SSH_USER=<operator-user> \
  SSH_IDENTITY=/private/path/operator-key \
  SSH_KNOWN_HOSTS=/private/path/known_hosts
```

可选 `INTERVAL_SECONDS` 默认 60；`RUN_ID` 仅接受 canonical UUID，省略时自动生成。运行前会用 pinned
local MinerU client、writer code、configured runtime bundle digest、task slots 和三个 endpoint digest
复核 v6 manifest，再验证 operator SSH key/known_hosts 是 owner-only 0600，且 known_hosts 内
Ed25519 key blob 的 SHA-256 与 manifest topology 完全一致。Observation v1 只接受当前 commissioned、
UUID-pinned 的单卡 nvidia-smi exporter；API/vLLM/GPU 每秒采样，host 每
5 秒采样；任何 sampler 故障只让 evidence `incomplete`，不得中止数据面。安全事件让 receipt
`unsafe`，但本命令仍不执行 actuator。

输出固定在 `$DISCLOSURE_RUNTIME_ROOT/reports/capacity/<run-id>/`。机械复算：

```bash
make capacity-verify RUN_ID=<uuid> REQUIRE_COMPLETE=YES
make capacity-summary RUN_ID=<uuid>
```

`capacity-verify` 不读取数据库或远端端点；它要求 receipt 的 runtime/source identity 等于当前
configured/exact-current identity，再从 owner-owned mode 0600 raw JSONL 按 run 参数机械推导区间并
重建 interval 与 final receipt，检查 hash chain、UTC、边界和文件 SHA。`REQUIRE_COMPLETE=YES` 才把 incomplete/unsafe 映射为非零退出；不带该项
时仅验证“失败证据本身是否完整可信”。详细契约、coverage 和隐私边界见
`../design/capacity-observation.md`。

### 1.1e Default-off capacity profile commissioning

Capacity pipeline 的构建默认必须保持 `MINERU_CAPACITY_MODE=legacy`、`MINERU_PHASE_TRACE=0`，
candidate profile 使用 exact `mineru-execution-profile.v2` 且不携带授权位。先部署并重新生成 exact runtime v6 attestation；这一步只证明新 image
bytes 可运行，不能启用 Auto。受控试验期间 worker 必须卸载、API/vLLM idle、没有第二 producer；每次
compose mode/profile 变化都要新 runtime manifest，不能拿上一 arm 的身份运行下一 arm。

试验顺序固定 A1 legacy → B1 candidate → B2 candidate → A2 legacy。A1 前按上文只冻结一次
campaign epoch，四个 arm 必须传入同一个 `EXPECTED_CAMPAIGN_EPOCH_SHA256`；任一 proxy/vLLM
StartedAt/ID 漂移立即作废整轮。四次都对同一 frozen regular/heavy/huge corpus 运行上文的
`make mineru-staged-load`，每次完成后立即采集：

安装器在每个 arm 仍重做 exact-source/image 校验，但构建命令固定 `--provenance=false`。不得删掉该项：
默认 BuildKit provenance manifest 含执行期元数据，会让相同业务镜像的外层 OCI index ID 跨构建变化；
该项不单独证明“同一 API image”。A1 前必须冻结完整 campaign image ID，四个 arm 和最终 default
恢复都以安装器的 `-ReuseCurrentPublishedImage -CampaignApiCompatImageId sha256:<exact-id>` 路径切换。
该路径 mutation 前要求正式 tag、当前 API 容器和完整 live compatibility proof 都等于冻结 ID；它完全
跳过 build/tag/image-rm，只定向执行 `mineru-api` 的 `--no-build --no-deps --force-recreate`，并在成功和
rollback 两条路径证明 proxy/vLLM 的 container ID、StartedAt、image ID 不变。供应链证明由固定 base
digest、Dockerfile/patcher hash、镜像 labels/marker、安装回执和每 arm 的 runtime v6 attestation 闭合。

```bash
make mineru-phase-trace-capture \
  STAGED_RECEIPT=/private/<arm>.json \
  CAPACITY_MODE=<legacy-or-candidate> \
  PROFILE_SHA256=sha256:<active-profile> \
  CAPTURE=/private/<arm>.trace.json \
  SSH_HOST=<pinned-host> SSH_USER=<operator-user> \
  SSH_IDENTITY=/private/key SSH_KNOWN_HOSTS=/private/known_hosts
```

采集命令从 staged receipt 取得 UTC 边界、collector/node/API container epoch，Windows 端只返回
严格 `MINERU_PHASE_TRACE ` 行。Mac 在写 0600 new-only capture 前要求所有 document trace 成功闭合；
candidate 还必须有 A/B 与 B/C overlap，且 document/page 总数与 staged receipt 相等。任何 fallback、
error trace、profile mismatch、restart/OOM 或日志缺口都会非零退出。

四 arm 闭合后运行 `make capacity-commission`（完整参数见 design §5.3 或 `make -n`）。机械门同时要求：

- frozen input、endpoint topology、model/client/writer 与 proxy/vLLM epoch 不变；每个 arm 的 pre/post
  direct multimodal canary 均 PASS 且 host sample 完整包围两个 canary；
- UTC 证明真实非重叠 A1→B1→B2→A2；page/block/source identity 跨 arm 相等；每 arm 的
  provider bundle SHA 自身合法但不要求 bundle 容器跨执行字节相等；
- 本机 A1 前固定 `DOCKER_MEMORY_RESERVE_BYTES=7516192768`（7 GiB）；完整 host verifier 重算
  collector path/hash、5 s cadence/15 s gap、VM total/available、summary、epoch、OOM/restart 与 reserve；
- sampling、preemption、waiting/KV、API exact retained-gauge range/idle/activity/drain 与 cleanup 全部安全；
  completed/failed gauge 可因 600 s retention 自然减少，不作为任务累计账；
- A1 前冻结 `MINIMUM_IMPROVEMENT_BASIS_POINTS=500`、
  `MAXIMUM_REPEAT_SPREAD_BASIS_POINTS=300`；`min(B)>max(A)`、最低收益、重复稳定性和实测噪声
  四门同时成立。

`COMMISSION` v2 receipt 只给出 `profile_commissioning_authorized=true`，不授权 Auto、也不执行部署。
Operator 不得重写已验证的 profile；必须把 candidate arm 使用的 exact v2 profile 原样交给
`make capacity-catalog`，绑定 exact COMMISSION receipt、evaluator 与 runtime compatibility。Auto compose
只能把 `C:\ProgramData\agent-invest\mineru-runtime-v6\mineru-capacity-catalog.v1.json` 只读挂载到
`MINERU_CAPACITY_CATALOG_PATH`；安装器和 v6 attestor 会同时核对 host source、container destination、
`RW=false` 与 catalog hash。重新 attest 后，还要在 `auto` 下通过 fallback/cancellation smoke，才可按
§8 启 worker。`STOP`、证据不可比或任一 hard gate 失败时保持 legacy；不得降低门槛或用 GPU 峰值截图替代。

### 1.2 批量重解析与派生重置

旧 NormalizedIR corpus reset/exact replay 工具已经删除：它维护第二套 manifest、备份、调度和状态分类，并会把旧 writer 重新引入生产入口。当前没有 production 数据；开发期需要重放时，使用明确的 document 列表走正常 Provider writer，先在仓外保留原 PDF 与 provider artifact，再由 operator 单独授权 DB/AgentSSD 变更。

任何未来的全量 destructive reset 都必须重新设计为 provider_document.v1 专用的一次性操作：先冻结 source identity，停 worker，显式列出目标，事务内改变 DB，再用正常 resident worker 重建；不得恢复 DISCLOSURE_REPLAY_* 环境变量、旧 reparse_corpus.py 或 reset-trash 控制面。

## 2. 告警通道

- 每日 18:30 `com.agentinvest.disclosure-doctor` 跑 `scripts/doctor_daily.sh`：
  doctor FAIL、交易日 18:00 后 24h 零新增（freshness）→ macOS 通知。
- worker 每轮：source 断供或单轮失败 ≥5 → macOS 通知（每小时同题限流）。
- 通知历史落 `$DISCLOSURE_RUNTIME_ROOT/notify-markers/alerts.log`（错过弹窗看这里）。

## 3. MinerU 端点故障（实案：2026-07-12 / 2026-08-13）

症状：worker 报告 parse 失败堆积，`processing_run.error` 为
`parser_invocation_failed` + `httpx.ConnectTimeout`（远端 VLM 端点，如示例地址 100.64.0.1:30000）。
`/health` 只证明进程存活，不能证明图像推理链路可用。每日 doctor 会先读取 singleton
`/v1/models`，再用固定 1x1 PNG 调一次 `/v1/chat/completions`；这个 multimodal canary
失败时不得开始批量 parse。

处置：先看远端容器日志。连接超时、429/容量拒绝、任意 5xx 都按共享基础设施故障处理，
不是坏 PDF；worker 会关闭本轮 parse admission，进入已有退避/恢复探测，不应对每个 PDF
机械消耗 item retry。若日志含 `Expected a cached item for mm_hash`，核对 pinned vLLM 0.21
容器命令和 runtime manifest 均含 `--mm-processor-cache-gb 0`，重建容器后必须先过上述
multimodal canary，再重放原失败页窗口。恢复核对（应为 0 且失败文档最终 published）：

```sql
SELECT count(*) FROM disclosure_ops.pending_parse_v1 WHERE failed_parse_count > 0;
```

## 4. CNINFO 配额 / 封禁

症状：报告 `sync_quota_break: True`（配额熔断，next round 冷却 30→120 分钟自适应）或
`source_outage_break: True`（HTTP 层故障）。处置：配额熔断不用动，等冷却；
持续 outage 先 `curl webapi.cninfo.com.cn` 判断网络/封禁，凭据问题看
`~/.config/agent-invest/disclosure_anchor/cninfo.env`（轮换后要 `make worker-restart`）。
兜底：`make sync COMPANY=x` 走 `--channel web` 免凭据通道验证是否仅 WebAPI 侧故障。

## 5. 死信处置

| 死信 | 找到它 | 处置 |
|---|---|---|
| parse 重试耗尽 / 不可重试 | doctor `parse dead letters` WARN；`pending_parse_v1.last_failed_retryable=false` | 查 `processing_run.error` 根因；修复后 `make process DOC=<id>` 手动重跑 |
| Unit build 重试耗尽 / fail-closed | doctor `build dead letters` FAIL、health degraded、`disclosure_ops.unit_build_terminal_v1`；worker 只在集合变化时告警；0048 起已被后续成功 Unit 代际修复的旧失败自动退出这些运维读面 | 查 `unit_build_error` 与 `semantic_adjudication_summary`；修复 provider/config/规则后 `make rebuild-units DOC=<id>` 生成新 run，再发布；不得改旧 run 或紧循环 |
| provider 全部暂时不可用 | active run 的 `semantic_adjudication_status='degraded_unavailable'`、health/doctor WARN | Unit 集仍保留但无编造语义；恢复任一 provider 后显式 `rebuild-units`，确认新 run 为 complete_primary/backup |
| 空发布（0 unit） | doctor `empty publish dead letters`（实存案例：美的 3 篇「日常关联交易预计」，疑似表格型盲区） | 人工看原 PDF：确属无正文可切 → `make publish RUN=<id> ALLOW_EMPTY=1 REASON=...`；是切分盲区 → 修规则后 `make rebuild-units DOC=<id>` |
| HUGE lane 长任务 | worker report 的 `parse_huge_dispatched` 与 processing_run 时长；不再有大小排除死信 | 以归档 actual byte_count/页数核对成本；正常长任务继续运行，只在极端 whole-future runaway 时由 launchd 监督重启 |

下载类死信（新增 2026-07-14）：`invalid_candidate_snapshot` / `raw_archive_error` /
`subject_identity_conflict` 等 retryable=false 的下载失败永久出队，证据在
`source_access(status='failed')` 与 quarantine 目录（含 sha256 manifest）。

## 6. TCC / launchd 假死

worker 以 exit 77 自杀 = TCC 拒绝访问外置盘（详见 `scripts/run_worker_once.sh` 头部注释）。
处置：系统设置 → 隐私与安全性 → 完全磁盘访问 给 `/bin/zsh`（或按注释操作），然后
`make worker-restart`。KeepAlive 30 秒节流重启属预期；除 §1.1 首次 staged cutover 外，
不要手工 bootout。

## 7. 磁盘与产物治理

- doctor 有双卷剩余空间检查（<10% WARN）。
- processing run、document unit、outbox 与公开 evidence 不按年龄自动或人工退役；显式历史
  run/asset 引用保持可解引用。未来若改变 retention 语义，必须先作为公开契约变更单独裁决，
  不能恢复按 cutoff 删除 DB ownership 的旧入口。
- 派生孤儿统一由 `make gc-orphans`（dry-run）盘点，覆盖 `parser_artifacts`、
  `derived/normalized_ir`、`derived/provider_documents` 和
  `derived/document_unit_snapshots`；确认后
  `make gc-orphans APPLY=YES`。apply 全程持 CORPUS exclusive，文件必须至少 24 小时，
  且删除前把 family、relpath、大小和文件身份写入 `audit/gc/` 清单。parser ownership
  是 run 目录前缀；其余三类是精确文件 ownership。原始 PDF 永不在 GC 范围内。

## 8. 数据质量巡检（周节律）

`make audit-weekly` 当前只运行未映射 provider code 审计，任一非零退出即有真 finding。
词表升级流程：改 JSON + 升版本 + `make load-rules`（见 adapters/sources/cninfo 的词表工程原则）。

## 9. 备份与恢复（占位，待新备份盘）

当前 PG 集群与 raw 档案同在 AgentSSD——单盘故障即全损，这是已知的最大风险敞口
（用户决定：等新盘到位再做每日 pg_dump + raw rsync + 恢复演练；本节到位后补全步骤）。

## 10. 危险边界（不要做的事）

- 服务 CLI 不提供 corpus/raw/source_access 删除入口；测试残留只在隔离 scratch runner 内清理。
  旧的 `purge-company` 与全库 wipe/reset/replay 工具均已删除；没有单独授权与新的一次性
  provider-native 方案时，不做任何 DB/AgentSSD corpus 清空。
- `untrack` 是退订（保留全部文档档案），`paused` 是可逆暂停——想停采集永远先用 paused。
- 已应用迁移一律冻结；改视图/约束开新迁移。
- 有数据的数据库只允许走 online Alembic migration。online env 会在仍存在私有
  `document_unit.semantic_key` 时执行 NULL-safe 0047 前置校验；offline SQL 生成不会访问数据，
  因而不能作为跨越 0047 的数据迁移或 losslessness 证明。0047 后的 0050 只验证幸存 plural
  状态，不能重建已删除 scalar；发现 replay 不一致时走正常 rebuild/publish，不手工补 key。
- admin API 需要 `DISCLOSURE_ADMIN_TOKEN`（Bearer）且仅回环可用；token 在 worker.env，
  轮换用 `openssl rand -hex 32` 换值后 `make worker-restart` + 重启 API。
