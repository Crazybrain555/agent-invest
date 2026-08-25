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
canonical runtime manifest v3，分别绑定本地 client、固定 MinerU API orchestrator、vLLM inference
server 与网络 topology。它至少包含两个 immutable image digest、模型仓库与不可变 revision、served
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
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v3.json \
  RECEIPT=/private/path/mineru-smoke-receipt.v2.json \
  CANARY_CACHE=/private/path/mineru-canary.v2.json
```

该命令不读取 PostgreSQL 或队列，也不会把 worker/CNINFO/DB/admin 凭据传给 MinerU 子进程；
它从实际 venv package listing 机械核对 manifest canonical hash、本地 client digest、四个内容相关包、
固定页窗口与 writer code digest，以及远端 immutable
API image/config/env/mount/network、inference image/model/config/env、live served model ID、唯一出现的
`max_num_seqs=128` 与 `mm_processor_cache_gb=0`，并要求 API health 固定为 MinerU 3.4.4/protocol 2、
task slots=3、window=16、retention=600s、cleanup=30s。它连续三次走 96×48 `M7` PNG 的精确 OCR
多模态请求，再通过固定 API 对冻结单页 PDF 跑一次官方 full-PDF Hybrid-medium writer/artifact reader。
API 完成数必须恰好 +1、失败数不变且终态 queued=processing=0。独立 `TMPDIR`、MinerU 进程和
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
v3 manifest 是闭合字段合同且会内嵌到 mode 0600 的私有 receipt；其中只能存不可变身份、hash 与
非秘密启动参数，command 出现 credential flag 会 fail closed，原始 token/密码不得进入 manifest。

bootstrap 与 steady state 是两个独立 profile：bootstrap 不启动 resident worker、不开放 queue
admission；steady state 只在当前 runtime smoke 与两次相互独立的完整 staged load 通过后使用已验证
包络。当前候选 steady 参数是 `WORKER_BATCH_SYNC=13`、download/parse=`50/50`、
backfill waterline=`2000`、loop=`900..1800`、document concurrency=`16`、GPU budget/max-seq=
`21/128`、API task slots/inference cap=`3/7`、finalize=`2`、regular/heavy/huge=`11/4/1`、oversized=`10240 KiB`、
soft parse expected=`3600s`。任何新 OOM/EngineCore death、429/5xx、持续 preemption 或 vLLM
waiting≥64 持续 30 秒都停止 staged load；不能据一次利用率截图扩大 16 submitted / 21 active / 128 seq。

在同一 v3 manifest 的 smoke PASS 后、启动任何 producer 前，显式运行固定 staged-load gate：

```bash
make mineru-staged-load \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v3.json \
  INPUT=/private/path/frozen-representative-multipage.pdf \
  EXPECTED_SHA256=<reviewed-sha256> \
  RECEIPT=/private/path/mineru-staged-load-receipt.v2.json \
  WORK_ROOT=/private/disposable-scratch

# 第一轮完整 PASS 后，用不存在的新路径原样再运行一次：
make mineru-staged-load \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v3.json \
  INPUT=/private/path/frozen-representative-multipage.pdf \
  EXPECTED_SHA256=<same-reviewed-sha256> \
  RECEIPT=/private/path/mineru-staged-load-confirmation-receipt.v2.json \
  WORK_ROOT=/private/disposable-scratch
```

该入口没有自定义 stage 参数，固定按 4、8、16 份同一份已审阅、精确 SHA 绑定的代表性 PDF
复制件运行真实 official writer；输入解析结果少于 7 页会 FAIL，单页 smoke fixture 不能冒充
负载验证。4/8/16 是 client 同时提交的文档数，不是 active GPU 请求数；固定 API 只允许 3 个 processing
tasks，每个 active task 的 server-side inference cap 是 7，因此三阶段的 active upper bound 始终为 21，
其余文档在 API 队列等待。启动前会拒绝 active worker/pipeline producer、MinerU 残留进程和
`mineru-api-client-*` 残留目录，核对 v3 manifest、
本地 client/content packages、writer code、window=16、provider runtime 与 served model，并先探测
`/v1/models` 和 `/metrics`。每阶段运行中及结束时都采样 running、waiting、preemptions、KV cache；
每个逻辑 metrics 样本只对 transport failure 允许在同一 10 秒总预算内做两次最长 4.5 秒尝试；HTTP
429/5xx、响应超限、缺字段或非法值不重试。两次 transport 尝试都失败只形成一个 transport-only
sample gap。当上一成功样本 waiting<64 时，每阶段最多记录并容忍一个实际持续不超过 10 秒的中途
gap；第二个 gap、waiting 已达 64 后的任何 gap、超过 10 秒的 gap 或终样本 gap 都立即 FAIL。
PASS receipt 必须原样保留 gap 的时间/时长，并证明其后有更晚的成功终样本供 admission 复核。
同时连续采样 API `/health`：processing 不得超过 3，8/16 阶段必须真实观察到 queue，terminal 的
completed delta 必须精确等于 4/8/16、failed delta 必须为 0，并在每阶段结束前自然 drain 到
queued=processing=0。API 没有 cancel endpoint；本地中止后只能关闭新 admission、终止本地 CLI 并等待
远端自然 drain。无法证明 drain 就 FAIL，不能重启服务、进入下一阶段或把 receipt 用于 admission。
metrics 持续不可用、waiting≥64 连续 30 秒、preemption counter 任意变化，以及任一解析失败、429/5xx、
overload、OOM 或 EngineCore failure 都立即 FAIL，清理本地子进程/临时目录且不进入下一阶段。
每阶段 receipt 除 min/max 外还保留 running/waiting/KV 的 p95；当前只作容量诊断，不在没有现场
证据时把历史经验阈值升级成正确性门禁。
receipt 必须是不存在的新路径，按 mode 0600 创建；每一轮 PASS 必须同时有三阶段完整结果、精确 API
任务账和零清理残留；两轮必须是时间上相继、路径不同的完整运行，
FAIL receipt 不能用于开启 steady state。parse-capable worker/pipeline/admin 会在连 DB 前重算并核验
这个 PASS 的 runtime/client/code、代表性输入、逐阶段/逐文档、metrics、时间与 cleanup；路径缺失、
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

当前 vLLM 0.21 MinerU2.5-Pro 服务还必须把
`--mm-processor-cache-gb 0` 纳入远端容器命令和上述 manifest。2026-08-13 的 221 页半年度
样本连续两次触发 vLLM 多模态 IPC cache 失步（`Expected a cached item for mm_hash`），远端
返回 500；关闭该 cache 后同一页窗口与完整文档重放恢复。这个参数是运行时身份的一部分，
不能只在手工 compose 中修改而继续沿用旧 digest。

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
