# 生产运维 Runbook（disclosure_anchor，单机单人）

2026-07-14 上线加固批次（batch 4）建立。读者是三个月后忘光细节的运维者本人。
配置生效矩阵见 `config/README.md`；健康一眼看 `make doctor-full` + `make worker-status`。

## 1. 开机 / 重启顺序

正常情况全自动：`com.agentinvest.postgres`（launchd 一次性 pg_ctl start，等 AgentSSD 挂载）
→ `com.agentinvest.disclosure-worker`（RunAtLoad 常驻；`KeepAlive={SuccessfulExit=true}`，只在
退出码 0 时重启）。人工核对：

```bash
launchctl list | grep agentinvest     # 五个 label：postgres；doctor/gc；tunnel/worker
make pg-status && make doctor-full    # exit 0 才算活
make worker-status
```

worker 任何非 0 退出（78 公共停止、75 单例被占、70 watchdog、77 TCC、崩溃）都不会被 launchd 自动
重启，`launchctl print` 的 `last exit code` 和 worker 日志说明原因；公共停止见 §1.1f。
手工恢复（自动链路失效时）：`make pg-start` → `make worker-restart`（先要求
`make worker-control-status` 为 RUNNABLE）→ `make doctor-full`。
launchd job 丢失时重装：`make install-ops-launchd`（postgres+doctor+gc）、
`make install-mineru-tunnel`（MinerU tunnel）、
`./scripts/install_launchd.sh`（worker）。其中 postgres 是一次性启动，doctor/gc 是日历任务，
tunnel/worker 是常驻链路。

`staged-v4` 常驻配置必须设置 `DISCLOSURE_V4_SECRET_KEYRING_FILE`，指向已有的私有
provider 密钥环。安装器在修改 plist 或 launchd 状态前，用实际 loader 检查文件格式、
所有者和权限；缺失或不可读即停止。配置从实验环境提升到常驻 `worker.env` 时必须保留
这项依赖。不要为了通过预检重新生成密钥，旧密钥仍可能用于解密持久任务的恢复凭据。
仅通过基础 Settings 或空业务库的 doctor 检查，不代表密钥环已被验证。

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
bootout 新 job、恢复旧 plist 和原 disabled 状态。worker 安装器例外：存在公共停止记录或 control
状态不可信时以 78 拒绝；label 处于 disabled 时不会隐式 enable，确认只是普通维护禁用（不是记录写入
失败的原生公共停止，见 §1.1f）后才用 `./scripts/install_launchd.sh --confirm-operator-disabled`。
恢复后用上面两条命令反查（期望状态为 `=> enabled`、`print` 退出 0）。

### 1.1a MinerU runtime bundle attestation（任何 fresh parse 的前置）

parser target 契约要求 `DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256`
（否则 parse 在 parser_identity 阶段 fail loud）。它必须来自 operator/provider 保存于仓外的
canonical runtime manifest v8，分别绑定本地 client、固定 MinerU API orchestrator、vLLM inference
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
  RECEIPT=/private/path/mineru-smoke-receipt.v6.json \
  CANARY_CACHE=/private/path/mineru-canary.v2.json
```

该命令不读取 PostgreSQL 或队列，也不会把 worker/CNINFO/DB/admin 凭据传给 MinerU 子进程；
它从实际 venv package listing 机械核对 manifest canonical hash、本地 client digest、四个内容相关包、
固定页窗口与 writer code digest，以及远端 immutable
API image/config/env/mount/network、inference image/model/config/env、live served model ID、唯一出现的
`max_num_seqs=128` 与 `mm_processor_cache_gb=0`，并要求 API health 固定为 MinerU 3.4.4/protocol 2、
task slots 与 pending 必须同时等于 versioned serial manifest/worker profile 的 1，window=16、retention=600s、cleanup=30s；2/2、3/3 与 1/3 均 fail-closed。它连续三次走 96×48 `M7` PNG 的精确 OCR
多模态请求，再通过 protocol-v2 固定 API 对冻结单页 PDF 跑一次官方服务端
full-PDF Hybrid-medium writer / 共用 artifact reader，不再用缺少任务身份字段的官方 CLI 提交。
API before/after 必须原样保留 retained terminal gauges 且两端 queued=processing=0；smoke 成功由
official writer、ProviderDocument 和清理证据证明，不对 completed/failed 人口 gauge 做差值推断。
MinerU 进程和外部 `mineru-api-client-*` 差集必须在 PASS 前证明为零。
每次以 `<receipt>.diagnostic/` 的 new-only 0700 私有目录保留 0600 intent/accepted/terminal/
validated/disposal 审计记录。先验证任务身份、ZIP hash/bytes、完整源/输出页数和产物，
清理本次生成的本地副本后落盘独立诊断处置意图，再 ACK 这个 exact task，并核验 consumed
响应及同 key 的精确 404。它不伪造 PG `finish_committed`，不能被生产发布路径用作 ACK 权限。
失败或响应不确定时保留原 episode 和残留，不自动换 key 重提、不 ACK 未验证任务、不删除未知树；
停止后续提交，按私有意图中的原 key 做只读恢复核对。原始 PDF 不在清理范围。已有输出路径会 fail closed，
不能覆盖旧 PASS。smoke 成功只产生 bootstrap 证据，此时仍不得启动 worker；还要完成下述短
held-out validation。最终把三条新路径分别写入 `DISCLOSURE_MINERU_SMOKE_RECEIPT`、
`DISCLOSURE_MINERU_CANARY_CACHE` 与 `DISCLOSURE_MINERU_VALIDATION_RECEIPT`，并固定
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
v8 manifest 是闭合字段合同且会内嵌到 mode 0600 的私有 receipt；其中只能存不可变身份、hash 与
非秘密启动参数，command 出现 credential flag 会 fail closed，原始 token/密码不得进入 manifest。

MinerU 3.4.4 在 WSL/FastAPI 连续处理大 PDF 后可能把已经 free 的 glibc arena 长期保留在 API
PID1 RSS；本机真实 heavy 文档已复现该行为，与上游
[issue #5313](https://github.com/opendatalab/MinerU/issues/5313) 的现场一致。当前使用一个临时、
精确源码兼容层，而不是复制未合并的 [PR #5354](https://github.com/opendatalab/MinerU/pull/5354)：
derived image 固定 base digest、MinerU 版本和兼容层目标源文件的 preimage hash。其中内存归还逻辑
在每个处理窗口及文档 final cleanup 后显式调用 glibc `malloc_trim(0)`；开关必须为闭合值，启用时缺少 glibc/hook 会
fail loud。collector、install receipt 和 manifest v8 同时绑定 patcher/Dockerfile hash、derived
image ID、base digest、策略名、patched source hash 与 live hook；任一漂移都拒绝准入。它不改变
解析语义、页窗口或并发，只把 allocator 可归还的空闲页返还给 OS；上游正式修复合并并通过同一
held-out 验收后，应删除这个兼容层而不是永久形成私有 fork。

Hybrid 异步服务路径的窗口 heap trim 和文档结束时的 CUDA cache/heap 归还使用已有
`to_thread_owned`，取消时仍等待已开始的清理结束。该路径不再逐文档强制执行
`gc.collect()`：CPython 全量循环回收会持有 GIL，即使移入线程也可能阻塞健康检查。
自动循环回收保持启用、阈值不变；同步 `doc_analyze` 和 VLM backend 保留原清理路径。
显式容量部署在 Linux CPython 3.12 的单个 API 主进程中，先用原构造器初始化实际 Hybrid
`(None, False)`、`(None, True)` 模型键，等待 CUDA 构造操作完成，再执行一次各代回收及
`gc.freeze()`。该步骤必须早于服务事件循环、task manager、持久任务恢复和请求准入；
禁止在处理 PDF 后或每个请求结束时重新冻结。构造器初始化不代替完整 PDF canary；
canary 新建的对象仍参与正常循环回收。非托管入口不启用此策略，托管入口拒绝 reload、
已有 runtime 或绕过 CLI 的 ASGI 启动；该独占 API 进程不得同时启用另一套堆冻结策略。
CPython 3.12 会把部分 immortal 对象自动放入永久代，因此 `gc.get_freeze_count()` 的
非零值不是外部冻结证明，关闭后计数也不必归零；该计数仅作观测。
退出时必须先完成全部 owned runtime 清理，才能解除冻结、释放静态模型根并回收；
清理失败保持可见，不把仍有工作持有者的状态称为静止。冻结对象仍可修改，也可能通过
引用环保留后续对象，因此此策略不保证没有长停顿或长期内存增长。部署后须同时
检查原健康检查限时、资源门槛和连续文档的内存走势；若仍超时或内存持续增长，停止新
准入并排空已有任务，保留日志后处理原因，不能通过放宽限时或关闭自动回收掩盖问题。

异步入口的 PDF 重写、输出目录创建和输出生成也通过 `to_thread_owned` 执行；Hybrid 的
OCR 分类、PDFium 打开、页数读取和关闭由同一文档持有者管理，取消后先等待已开始的操作
结束，再完成必要清理。PDFium 全局互斥锁保留，不能为增加并发而删除；关闭已尝试后不重复
调用原生关闭。同步入口、模型和解析算法不因此改变。`mineru/cli/common.py` 同样进入
compatibility marker、collector 和 attester 的精确源码清单，须重新构建并核实实际部署字节。

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
health、网络、egress、output-root 或 formal collector 校验失败时，若已尝试启动候选 API 且存在
旧 API，必须先证明输出根与部署前完整 witness 一致，才恢复旧 tag、compose、collector、receipt
和容器运行态。根身份、registry 哈希或物理缺失状态、计数、水位变化时返回
`rollback_blocked_registry_changed`；无法读取或验证证据时返回
`rollback_blocked_registry_unverified`。被阻断时保留现场、旧备份及原始失败，不自动启动旧 reader，
也不覆盖 registry。不要手工把 v6 reader 改回兼容任意历史路径，也不要在同一次处置中
重启 Docker Desktop、Windows、Tailscale 或 v2rayN。
安装器与 collector 在 Windows PowerShell 5.1 中通过同一 `System.Diagnostics.Process` 调用层取得
显式 `ExitCode/stdout/stderr`，并并行排空双流、以 UTF-8 字节写入 `docker exec -i`；不得改回依赖
`$LASTEXITCODE` 的调用运算符，也不得跳过 exact source preflight。

仅升级兼容层源码、且必须保留现有推理服务与代理进程时，追加
`-ApiOnlyCompatibilityUpgrade`。此模式要求完整旧部署和与 live target **完全相同**的
compose 源字节（包括现场内存/交换上限），拒绝与 `-ReuseCurrentPublishedImage` 同用。
它仍从审核后的源码构建新镜像、备份旧文件及 tag；部署和失败回滚均只执行
`up --detach --no-build --no-deps --force-recreate mineru-api`。回滚通过上述 witness 检查后才恢复
旧 tag 与文件，再重建 API、验证旧 API image、健康状态以及推理服务/代理的原 ID、image、started_at。
机器断线或监督进程超时仍须核对 daemon 状态；不能把客户端退出当作完成回滚。
完整项目安装模式保留给明确授权的初装或拓扑变更，不能借 dry-run 代替此 API 单独升级边界。
以上 compose 字节不变要求描述默认兼容层升级。已显式选择容量配置时，容量参数变化以及
`-ApiDeviceProfile cpu|cuda0` 的设备变化分别遵循
[显式容量部署约束](../design/mineru-explicit-capacity.md#installation-and-observation)：
每次只放行选定轴，保留其他配置和推理服务/代理进程。设备配置通过不代表解析质量已通过。
显式容量投影同时把 `services.mineru-api.stop_grace_period` 固定为 `10s`：API-only 升级允许该键
从缺省引入或保持 `10s`，现场取其他值视为漂移并拒绝；安装后与每次 collector 运行都断言 API 容器
实际 `Config.StopTimeout` 等于 10；该断言只针对已部署的候选，回滚与 `-ReuseCurrentPublishedImage`
的变更前校验面对的是旧 compose，不要求该键。该期限只界定停止信号之后的强制收尾上限，不替代
发信号前的业务软排空、900ms freshness 或 M6 `max_close`。
`scripts/windows/test_mineru_api_only_installer.ps1 -InstallerPath <reviewed-installer.ps1>`
在 Windows PowerShell 5.1 中抽取并执行真实安装器函数，使用独立临时文件和模拟 Docker
验证配置保护、旧 tag/文件恢复顺序以及健康/镜像不符的失败路径；不调用真实 Docker。
它是部署前回归，不能替代安装后的实际镜像、服务 identity、容量与输出根验证。

protocol-v2 启动后的输出根并非物理零文件：唯一控制树
`.agent-task-protocol-v2/registry.json` 保存提交水位与已消费 tombstone，不能删除来通过安装门。
runtime observation v5 保留真实 `file_count/total_bytes`，另附 `mineru-output-quiescence.v1`：
在 Linux bind-mount 命名空间固定根/目录/文件身份、读取有界稳定 canonical registry-v2/v3 字节，
只接受空登记或已完全清理的 consumed 记录；未知目录（包括空目录）、链接、临时文件和任何
仍持有资源的记录均拒绝。首次旧 API 安装前才允许真正空根；新 API commissioning 必须有登记。
安装器与 collector 调用同一已绑定源码的只读检查，前后核对 API idle；这不代替停用 producer
和实际 writer drain，也不把瞬时快照当成防止新提交的锁。排除写入的窗口必须从 preflight 持续到
部署或回滚结论，覆盖所有调用方已经发出的 submit/ACK/lease/GC 请求，包括尚在 Form 解析、未计入
ingress 的请求。v3 writer 可以读取旧 v2，但旧 reader 不能读取 v3；即使只有 consumed 记录，也不能
用旧 registry 备份覆盖新的 tombstone 或水位。旧 v4 observation 不能用于新 attestation。

collector 的独立 Python 进程只检查安装字节/package/marker，不能读取服务进程的
`get_task_manager()`。真实对象容量由正在服务的 `/health` 返回闭合
`task_protocol_runtime`（新部署为 `mineru-task-runtime.v2`）以及
`mineru-task-admission.v1`；包含实际 registry/executor 容量及 durable/ingress/排队/执行责任，
effective pending 也取自同一 HTTP 证据。新 collector 与部署资格检查要求 v2 admission；明确的
旧 v1 分支仅用于观察旧服务。collector 有界读取并复核 API container/start epoch。
所有 wire health 消费者先严格验证 protocol-v2 标记与 runtime 子证明，再显式投影为原有
13 字段 receipt/observer 形状；旧规范化 receipt 可继续读取，但不能冒充 live wire health。

bootstrap 与 steady state 是两个独立 profile：bootstrap 不启动 resident worker、不开放 queue
admission；steady state 只在当前 runtime smoke 与短 held-out validation 通过后使用已验证包络。
当前候选 steady 参数是 `WORKER_BATCH_SYNC=13`、download/parse=`50/50`、
backfill waterline=`2000`、loop=`900..1800`、document concurrency=`16`、GPU budget/max-seq=
`7/128`、API task slots/inference cap=`1/7`、worker client outstanding window=`1`、finalize=`2`、oversized=`10240 KiB`、
soft parse expected=`3600s`。任何新 OOM/EngineCore death、429/5xx、持续 preemption 或 vLLM
waiting≥64 持续 30 秒都停止 candidate 验证；不能据一次利用率截图扩大 1 outstanding / 7 active / 128 seq。

在同一 v8 manifest 的 smoke PASS 后、启动任何 producer 前，先冻结 clean service epoch；随后用
`make mineru-smoke INPUT=/private/path/heldout.pdf` 分别解析 2..8 份操作者选择、互不相同的完整多页
PDF，并在结束后再次冻结 epoch。epoch 只绑定 runtime、collector/node、三只容器 identity、API container
identity 以及 restart/OOM/unsafe counters；它不伪造跨机器的固定内存 reserve。

```bash
make mineru-service-epoch-freeze \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  EPOCH_RECEIPT=/private/path/epoch-before.json \
  SSH_HOST=<pinned-windows-host> SSH_USER=<operator> \
  SSH_IDENTITY=/private/path/operator-key \
  SSH_KNOWN_HOSTS=/private/path/known_hosts

# 逐份生成新路径的 diagnostic_custom smoke v6；示例只列两份。
make mineru-smoke RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  INPUT=/private/path/heldout-a.pdf EXPECTED_SHA256=<exact-source-sha256> RECEIPT=/private/path/heldout-a-v6.json \
  CANARY_CACHE=/private/path/heldout-a-canary.json
make mineru-smoke RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  INPUT=/private/path/heldout-b.pdf EXPECTED_SHA256=<exact-source-sha256> RECEIPT=/private/path/heldout-b-v6.json \
  CANARY_CACHE=/private/path/heldout-b-canary.json

make mineru-service-epoch-freeze \
  RUNTIME_MANIFEST=/private/path/mineru-runtime-bundle.v8.json \
  EPOCH_RECEIPT=/private/path/epoch-after.json \
  SSH_HOST=<same-pinned-windows-host> SSH_USER=<same-operator> \
  SSH_IDENTITY=/private/path/operator-key \
  SSH_KNOWN_HOSTS=/private/path/known_hosts

make mineru-validation-receipt \
  SMOKE_RECEIPTS="/private/path/heldout-a-v6.json /private/path/heldout-b-v6.json" \
  EPOCH_BEFORE=/private/path/epoch-before.json \
  EPOCH_AFTER=/private/path/epoch-after.json \
  RECEIPT=/private/path/mineru-heldout-validation-v2.json
```

validation builder 与 resident gate 都要求输入/输出为 owner-only、单链接、稳定读取的新文件；每份 PDF
必须是 `diagnostic_custom`、full-PDF、多页、source/provider 页数完全相等，且所有 receipt 使用同一 exact
runtime/topology。before/after epoch 必须 clean、identity 相等并完整夹住每份 smoke；任一 hash、页数、
时间线、restart、OOM、容器或 runtime 漂移都会 fail closed。固定轮次、重复确认 arm、固定 4/8/16 corpus
和固定 Docker reserve 都不是准入条件。需要定位 GPU 空档时，可用 `make mineru-phase-trace-capture`
对这段 validation interval 做 content-free document/page/DAG closure；phase trace 是诊断证据，不是另一个
deployment ritual。
同时连续采样 API `/health`：processing 不得超过 attested task slots；window=1 时不要求人为制造 process-local queue。
一次可分类为 `MinerUOrchestratorUnavailableError` 的 transport gap 只记录
`DEGRADED_TRANSPORT`、关闭后续 admission 并继续当前 owner 的自然 drain；恢复后的样本必须原样保留，
但该阶段仍是 evidence-incomplete。health JSON/identity/slot/window 的严格合同错误仍是 operational failure。
`completed_tasks`/`failed_tasks` 是保留期内 terminal registry 的人口 gauge，可随 600 秒 retention/30 秒
cleanup 合法下降，不能把它们解释为累计任务账。当前 deployment gate 只接受 smoke v6 与 held-out
validation v2；v5 及其他历史 receipt 不具备 admission 权限。
每份输入是否成功改由 exact input SHA、严格 protocol-v2 completed 证据、完整源/输出页数相等和 provider bundle hash
逐文档证明；每阶段仍必须自然 drain 到 queued=processing=0。API 没有 cancel endpoint；observer-only
failure 只能关闭新 admission，不能终止当前本地 CLI 来冒充远端 cancel。文档自身失败或 operator 本地中止
可以终止本地 CLI，但两种路径都必须在同一 deadline 内容忍短暂 health transport failure，并等待远端自然
drain。无法证明 drain 就 FAIL，不能重启服务、进入下一阶段或把 receipt 用于 admission。
metrics 持续不可用、waiting≥64 连续 30 秒、preemption counter 任意变化，以及任一解析失败、429/5xx、
overload、OOM 或 EngineCore failure 都立即 FAIL，不进入下一阶段；诊断 owner 按上述失败规则保留恢复证据。
每阶段 receipt 除 min/max 外还保留 running/waiting/KV 的 p95；当前只作容量诊断，不在没有现场
证据时把历史经验阈值升级成正确性门禁。
receipt 必须是不存在的新路径，按 mode 0600 创建；FAIL receipt 不能用于开启 steady state。
parse-capable worker/pipeline/admin 会在连 DB 前重算并核验当前 PASS 的 runtime/client/code、held-out
逐文档、时间、epoch 与 cleanup；路径缺失或证据漂移一律拒绝入场。该命令声明 database/queue access
均为 none；不得与 worker、pipeline 或 API producer 并行执行。

### Staged-v4 resource cutover (default-off)

当前 serial process profile 的 `cpu_worker_threads=3` 指 `MINERU_PDF_RENDER_THREADS` 的
PDF 渲染 worker 配置上限，不是整个进程的 OS 线程数。默认 v9 的 `omp_thread_count=1`
对应显式 OMP/MKL/OpenBLAS 单线程策略。受控 CPU 对比可以显式选择 attester 的
`--expected-api-cpu-threads 2`：仅在实际 API 环境 OMP/MKL 都为 2、OpenBLAS 为 1、
PDF render 为 3 时生成 `mineru-runtime-bundle.v10`。其闭合 `cpu_thread_policy`
记录这四项配置；原 v8/v9 不接受新增字段，默认 attester 仍要求单线程并生成 v9。
staged profile 的 `omp_thread_count` 必须与已验证 manifest 一致，不能只改 profile 的数值或 SHA。
这项配置证明不代表框架实际执行宽度，也不代表吞吐验收通过。
Compose、collector 的闭合环境清单和 attester 同步核验，
不能把新 probe 进程的默认值或某时刻 `/proc` 线程人口当作这个配置。容器内存上限从实际
Docker/cgroup 读取；v1 要求有限正数，`max`/未设置不能编造为机器总内存或测试 fixture。
现场资源 cap 写私有 compose 并经原安装器在 quiescent 状态应用，重采集后才建新 profile。
线程对比同样要求排空、保留原配置、新 runtime/epoch/held-out 证据；不得重贴旧运行身份。
负载开始前必须确认 GPU/VRAM、CPU/RAM、API 与推理队列监测就绪并覆盖整个运行，
跨主机时间对齐保留运行前后校准及误差范围。以完整任务/源页吞吐及等待阶段定位收益，
保留空闲、采样缺口、OOM、重启和失败证据；单纯增加线程或通过功能检查不构成性能通过。

staged-v4 的默认关闭 source candidate 另有 [V4 资源生命周期与离线切换步骤](../design/v4-resource-lifetime.md)：
0060→全历史 spec 回填→0061 验证；实际旧 writer 退出是前置，不可用 PG lease 代替。
启动历史隔离残留门或 ACK 前残留检查失败时保留证据/信用，不手工伪造清理回执或删除未知树。

上线前有界验证使用 `python -m disclosure_anchor.cli.staged_commission --document-id <id>`
有界连续供给（超过 8 份、小时级）使用 `python -m disclosure_anchor.cli.staged_campaign --manifest <corpus.json> --manifest-sha256 <sha> --scope <scope.json> --scope-sha256 <sha> --max-seconds <n> --receipt-out <runtime-root/...> [--stop-file <path>]`；准入上限就是冻结 scope 的普通成员本身（不另设配额），截止时间或停止文件只关闭新准入不取消已接受任务；它复用相同的锁、部署门与 stream activation，收据只投影协调器的持久结果，不是正式 M6 计分。
正式 M6 关闭（owner 绑定）时按顺序使用：`python -m disclosure_anchor.cli.m6_run_control bind|open --run-dir <dir>`（controller；`<dir>` 含 owner 的 `anchor.json`、`roles.json`（四角色各自的 epoch 与 token 文件路径）、`transport.json`，bind 写入 `run-spec.json`）→ `staged_campaign ... --observation-out <dir> --m6-run-dir <dir>`（runner：生命周期事实经本地 spool 由独立线程作为 `e2e_runner` 事件送达 owner，runner 的 lease 上限不单独配置：READY 之后由私有绑定的 `windows.maximum_lease_ticks` 按 READY anchor 的实际 QPC 频率换算写入 `transport.json`（一次、bind/open 之前；绑定的 propagation_reserve_ticks 须等于 intent 的 stop_propagation_reserve_ns），native 与 client 只有这一个来源；runner 先等 sender 拿到首个 lease（上限 2×控制交换截止且不超过 --max-seconds）再装入会锁存的停止判定，拿不到即 `campaign_startup_error`/退出码 3、不写收据（含义：未准入任何文档、runner 未发 admission_closed ACK、owner 侧 run 仍开着，由 controller 既有关闭路径收尾，不是崩溃；spool 记录含 `lease refresh:` 原因）；若首个 lease 在协调器首次判定前已到期，run 会以 admitted 0 / `external_stop` 收尾而非 startup error，读 spool 与 owner-status 区分；刷新在途时旧 lease 仍可见并按同一连续时钟到期，回复到达后原样替换；owner lease 真正失效只关闭新准入（对本次 run 锁存），排空后按序存入 resource_audit / unresolved_claims / admission_reconciliation 收据、ACK `admission_closed`、存入 ownership_closure；收据 `m6_assembly` 块记录全部 sha 与失败步骤）同时（runner 启动后立即）运行 `python -m disclosure_anchor.cli.m6_verifier_supervisor --m6-run-dir <dir> --runner-spool <observation-out>/m6-assembly/spool.jsonl --runner-receipt <receipt> --output-dir <new> --verifier-identity <id> --plan <plan> --deadline-seconds <n>`（进行中校验：tail runner spool，只在 owner 对该 attempt 的 `attempt_admitted` 与 `publication_committed` 都有持久 `delivered` 回执后才派发；每个 attempt 先 public 后 quality，quality 直接绑定本进程产出的公开受理精确字节；两角色各一条长期 sender/序列；runner 收据出现且 `m6_assembly.status` 为 complete、attempt 集合对账一致后，quality 完成、public 写 `drain-receipt.json` 并发唯一 `verifier_drained`；任何失败/超时/不一致 → 双方显式 abort、不 drain）→ `m6_run_control close --run-dir <dir> --campaign-receipt <receipt> --reason ...`。独立 CLI `m6_public_verify` / `m6_qualify_readonly --public-receipts <supervisor 输出的 public-inputs.json> --plan` 只作证据重跑（sender 完成、不 drain）；顺序不可颠倒：quality 依赖 public 的受理文件，drain 依赖两者全部完成。任一装配步骤失败时对应收据/摘要标记 failed，运行记为证据不完整，不得声明正式关闭。边界规则：`bind` 只在 `run-spec.json` 不存在时写入（整体写入，否则失败），已存在且字节与本次输入重算一致时重放 owner 的幂等 bind 并核对回复状态的 `spec_sha256`/`run_id`，字节不同则拒绝且不发远端命令；runner 在加载 profile、持锁、连库、准入之前先要求 spec 的 manifest/scope/campaign/mode 与本次输入一致，再核对 process profile、runtime bundle 与 worker profile 身份；发出 drain 的一方先写入不可变的 `drain-receipt.json`，`verifier_drained` 只引用该文件的摘要，`run-summary.json` 随后单独写入；连库失败、循环未跑到底、spool 尾部损坏/残缺或收据无法整体写入时以显式 abort 关闭发送线程且不产生任何 drain 声明。
组合入口（R20）：`python -m disclosure_anchor.cli.m6_campaign run|bootstrap-check --intent <m6.campaign-intent.v2> --intent-sha256 <sha> --evaluation-plan <m6.evaluation-plan.v1> --private-binding <0600 m6.campaign-private-binding.v1> --binding <WP1 binding.json> --release-manifest <release-manifest.json> --manifest <corpus.json> --scope <scope.json> --quality-plan <plan.json> --output <runtime-root 下的新目录>` 在一个 Mac 进程内完成上面的整个顺序：生成角色 token、Windows launcher `-Prepare`（先建受保护 private 目录）→ sftp 上传私有部署 → `-Run`（严格七字段 READY、固定期限）→ 工厂冻结 spec 并按值 bind（`m6.owner-request.v2`）→ open → 以 shell source 语义启动 runner 与 verifier supervisor 并发监督 → 只凭 runner 收据 close → 取回 `process-exit.json` 独立核对 → `campaign-summary.json`。`bootstrap-check` 零准入、不连库，只证明全新 workspace 的引导与真实闭合协议；见 `design/m6-campaign-entry.md`。入口要求 intent v2 与 `--evaluation-plan`（准入前冻结）；运行结束后 `python -m disclosure_anchor.cli.m6_campaign summary --run-dir <output> --evaluation-plan <plan> --output <new> [--telemetry-artifact-root <observer root> --telemetry-run-id <uuid>]` 只读派生 `m6.delivery-report.v1`（WP3；远端尾部门来自 runner stage notes，资源门来自回放的 v3 synchronized telemetry）。旧 `run-spec.json` 单独 `bind` 路径保留给手工控制。R23：`run|bootstrap-check` 可加 `--admission-stop-file <output 同父目录下的绝对路径>`，同一文件同时是 runner `--stop-file` 与 controller 的外部准入 STOP（有效控制 = regular/0600/当前 uid/恰好 `stop\n`；FIFO/目录/symlink/错误字节 fail closed 且不打断在途工作）；测量驱动 v3 在 telemetry 失败时发布 `<driver output>/campaign-admission.STOP`，只等到 spawn 时冻结的原绝对期限，不再 SIGINT。观测 run 目录可能是负面终态 `receipt.failure.v1.json + seal.failure.v1.json`：`summary` 照常生成报告并把资源门置 unknown（`telemetry_receipt_failed:*`），驱动 `independent-evidence.json` 同时给出 `business_closed` 与 `measurement_failed`，exit 1。运行者仍须另查 `delivery_pass`。R23 live：`summary` 的每个问题名都折叠为报告形状（空白/控制字符→`_`），失败 owner/缺失终态生成 unknown 报告而非契约拒绝（原文见 `owner-failure.json` 等原证据）；另一 run 的 owner 目录仍被拒绝。runner 收据 `residual_count` 只数 scratch 里的 payload（快照/部分上传/归档/staging/输出/无法命名条目），命名空间与 `.lock` 哨兵不计，`m6_assembly.scratch_residuals` 列出前 64 个相对路径；见 `design/m6-campaign-entry.md`。

（可重复该参数，1..8 个唯一 ID）`--max-seconds <1..86400> --receipt-out <runtime下的新路径>`。
要求显式 staged-v4、真实 process profile/keyring、当前部署证明和完整旧 writer 排空。
只运行一次生产 coordinator，不启动采集、维护或常驻循环。ID 范围在普通 backlog SQL 的
keyset/LIMIT 前生效，并在源 IO、H0、源拒绝前检查；它不放宽原有公司/分类/重试 eligibility。
prepared/recovery 保持全量；有范围外未闭合 V4 owner 就在构造/控制器边界拒绝，不隐藏 owner。
文档数不是 attempt 次数限制，选择内重试沿用原有规则。截止/信号关闭 admission 并走原协调器
撤权/实际排空，不声称远端取消。异常保留 intent、DB owner 和资源，不能删除后重提以掩盖失败。
私有 `staged-v4-commissioning.v1` receipt 逐文档报告 `published|recovered_published|already_ineligible|failed|blocked`。
PASS 要求相对开始时新建的 active+succeeded run、同 run 的 acked attempt、完整恢复、QUIESCENT、
零 held credits 和无 errors；同 run 的恢复发布单独列出，不冒充新 run 证明。它不是吞吐/整小时资格。

本地 writer 热修复后，禁止把新 runtime/profile 写回已有 H0，或跳过普通部署门。只对已 accepted
的结果取回/发布尾部提供显式 `python -m disclosure_anchor.cli.staged_recover`：
`--grant <private-json> --review <private-json> --current-runtime-manifest <private-json>`，
另传 `--ssh-host`、`--ssh-user`、`--ssh-identity`、`--ssh-known-hosts`、`--max-seconds`、
`--receipt-out`。保持原 settings/profile/keyring/资格回执；不启动常驻 worker，不自动签发 grant。
`mineru-accepted-result-recovery-grant.v1` 是仓外 0600、有效期不超过一天的操作员兼容批准，
精确绑定旧/新 runtime 与 writer SHA、原 process/worker profile、1..8 个原 document/attempt/run/
fence/source/H0/spec/accepted receipt SHA，以及独立 GO review 的规范 JSON SHA。
review 绑定新旧 writer 和已审查差异 SHA；没有独立审查不能生成这份批准。
只允许 `identity-content-encoding-recovery` 或在同一尾部另发现跨页祖先定位校验错误时使用
`identity-content-encoding-and-publication-lineage-recovery`；每次代码变化均须新 exact GO/grant。
其他升级不能复用这份窄批准。
`--current-runtime-manifest` 只接受 `mineru-recovery-runtime-manifest.v1`：从原已验证 manifest
仅替换当前 client writer SHA，显式绑定历史 runtime/writer SHA 与原 heldout container epoch SHA。
这是派生恢复身份，不是新的正常部署 attestation；保留的 accepted 输出不可为取得普通资格而删除。
执行前另存只读远端代码核验：实际源文件/兼容 marker、collector/compose SHA 和容器 image/epoch
必须与原观察一致，且不存在代码 bind mount。排空前不得把派生身份冒充新的 smoke/heldout PASS。
恢复门验证旧资格为历史证据、新代码为当前执行身份；除 writer 外，client/model/image/compose/
topology 必须完全相同，并实时检查原容器 epoch 与安全计数。数据库全量责任检查仍保留，
范围外 owner、prepared/reconciling、新 attempt/H0/spec、过期批准或 source/runtime 漂移均拒绝。
普通与 prepared admission 均关闭，transport 在任何 task submit/reconcile IO 前拒绝；lease/GET/
原发布/cleanup/ACK 保留既有 claim/fence/信用保护。私有恢复收据仅可给出 `RECOVERY_PASS`，
要求原 attempt/run active+succeeded+acked、admitted=0、QUIESCENT、无 errors/held credits；
`deployment_qualification=false`。排空后必须重新执行正常 attestation/smoke/heldout 才可接纳新任务。

V4 ZIP 下载显式请求 `Accept-Encoding: identity` 并拒绝非 identity 响应；保留精确 Content-Length、
流字节数、owner/header hash 及落盘 SHA 校验。HTTPX 的 `iter_bytes` 会按 Content-Encoding 解码，
MinerU 的 GZip middleware 会让编码后的响应长度不同，不能拿它与原 ZIP 终态长度比较。
参见 [HTTPX streaming](https://www.python-httpx.org/quickstart/#streaming-responses)。

### 1.1b Semantic provider chain

默认是 Luna low 主用、canonical Sonnet 5 low 备用。自定义完整链只写入仓外
`DISCLOSURE_SEMANTIC_PROVIDERS_JSON`；修改后先跑配置/单测和 provider probe，再重启 worker。
只有 executable/auth/quota/transport/timeout 的闭合 availability 原因会切备用，协议/结果/安全
错误与取消不会。配置格式、收据和终态矩阵见
`../design/semantic-adjudication-runtime.md`。不要用 model alias 代替 canonical identity。

Codex provider 还必须固定无工具模型配置。先用已固定版本的 CLI 离线准备（不调用模型）：

```sh
PYTHONPATH=src .venv/bin/python scripts/prepare_codex_semantic_model_catalog.py \
  --codex "$DISCLOSURE_SEMANTIC_CODEX_BIN" --model "$DISCLOSURE_SEMANTIC_MODEL" \
  --expect-version 0.156.1 --runtime-root "$DISCLOSURE_RUNTIME_ROOT"
```

这里的版本是已验证示例，升级时填写实际固定版本并重新验收。输出的 `model_catalog_sha256`
填入 `DISCLOSURE_SEMANTIC_PROVIDERS_JSON` 对应 Codex 条目；使用默认 provider 链则设置
`DISCLOSURE_SEMANTIC_CODEX_MODEL_CATALOG_SHA256`。文件及来源记录保存在 runtime 的
`semantic/codex_model_catalogs/`；只改变工具字段，保留模型原始提示、推理参数和业务规则。
配置核验、空工具列表和真实结构化输出验证通过后，才按常规流程重启。不要改共享的
`~/.codex/models_cache.json`，不要通过开启代码宿主或忽略工具错误来恢复服务。
备用 Claude 也应在 provider 条目中固定已验收的版本化可执行文件路径；不要让全局 CLI
自动升级悄悄改变生产的工具或结果协议。升级后先验证安全模式、结构化输出和错误 envelope。

staged V4 的语义 `failed_closed`（协议、安全、结果契约）在 commit 中出现时，worker 以公共停止
（§1.1f）持久停下，不再被 launchd 重驱到同一个未缓存 group；不要靠反复重启或清理结果掩盖。
保留原任务、收据和日志，修复并验证后显式放行。本机制不做单文档隔离、跨启动计数或半开探测；
已证实的 item-local 类型失败与 availability 降级保持原有有界语义。

`invalid_decision` 表示答案通过了协议解码、但没通过路由校验（决策没有恰好覆盖所请求的 Unit 时记为
`invalid_contract`）；校验在写缓存之前、以及每次命中缓存时都会运行，所以被拒的新答案从不入缓存。
按停止记录中该 attempt 的 `cache_key` 查
`$DISCLOSURE_RUNTIME_ROOT/cache/semantic_routes/v2/<taxonomy_version>/<provider_id>/<前两位>/<hex>.json`：
文件存在，说明被拒的是已存条目（篡改或语义改了却没升版本），原样保留作证据，只有明确授权才可改名
隔离；文件不存在，说明被拒的是这次调用的新答案。两种情况都先修复原因、离线验证，再按 §1.1f 放行。

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
复核 v8 manifest，再验证 operator SSH key/known_hosts 是 owner-only 0600，且 known_hosts 内
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

### 1.1e 容量探索边界

当前操作真相以 `../design/mineru-throughput-scheduler.md` 为准。已退役的固定轮次、固定 corpus、
固定内存门槛和 catalog builder 不再构成当前操作面。同步 telemetry 与容量搜索尚未形成受支持命令前，
不得仅凭历史 receipt 改变运行参数。

仓库中的 `mineru_resident_telemetry_exporter.ps1`、`start_mineru_resident_telemetry.ps1` 和
`windows_resident_telemetry.py` 当前仍为 default-off 源码与诊断合同，不是受支持的生产命令。不得
安装或接入现有 worker；显式授权的有限机制诊断使用 `test_mineru_resident_endpoint.ps1` 或
`test_mineru_resident_session.ps1`，先核验私有 config、source、prepared manifest 与实际 runtime。
诊断用新 session/私有目录，不复用旧 READY；零 sample 只证明关闭链，不能冒充采样通过。
host_slow session config 的 `backend` 必须携带 `capacity_config_sha256`（release 冻结 capacity 的 hash）；resident owner 请求
必须携带同一 capacity 的 exact bytes（`capacity_config_bytes`），owner 核对 profile 是其投影、config hash 一致，Mac 侧对每个
host 样本的原始 health 运行共用 capacity validator（wire v2 转发原始字节，C# 不再镜像规则）。失败会话在 run 目录留下
`exporter-failure.txt` / `supervisor-failure.txt`（嵌套异常链），reconciliation 读它而不是 stderr 的外层消息；改动 PS1 源后须
重新 stage 并刷新 config `sources` hash，改动 .cs 源后还须重建 telemetry assembly 并刷新 release inventory/native proof。
改动任一 Windows 入口脚本后、stage 之前，必须先做静态“函数定义/调用”核对（入口 + dot-source 的 load 脚本），并由 root 在
Windows 上对已 stage 的入口跑一次不存在 config 的负向调用：Mac 上没有 PowerShell，Python gate 不执行入口，2026-09-17 r5
两个入口丢失 `Get-MineruBootstrapSha`/`Read-MineruBootstrap` 正是这样漏到实机的。resident owner 在等 READY 控制时同步
轮询两个 starter：starter 先于 READY/observer 结束即按 lane 命名失败并立即保留其 exit/stdout/stderr；失败路径先做有界
drain（poll ≤0.25 s）再 abort，`failure-command-N.state.json` 记录 exit/是否先于 abort 结束/保留统计。
R22 测量协议（v4）：owner 显式选 `receipt_version=4`；observer 首次采集前把 `owner-intent.json` 的时长投影为不可变
`sampling-plan.v1.json`，帧为 `frames.v3.jsonl`（每帧带 nonce/after/s/f/a/d/b 的 fresh-pull 见证），`receipt.v4.json`/`seal.v4.json`
绑定 plan hash，CPU 分母为固定 D；native 端点只在 `/after/{n}/request/{nonce}` 上按请求新采集（无自主定时/缓存）。关闭顺序：
`sampling_drained` 控制事件后先并行关 native lane，再等 child 退出、再做完整回放；plan.end+10 s 无 drained 记
`telemetry_drain_timeout`，plan.end+60 s 为全部收尾步骤（关 lane、starter/closed 控制收尾、child 退出、回放）的绝对截止，
每步取 min(自身上限, 剩余)，回放每 64 行检查一次。owner 在 `plan_recorded` 时即有界读取 child 原始 plan 文件、校验 hash/intent/时长/run/
observer identity/cadence 后才写入证据目录 `sampling-plan.v1.json` 并推导截止；不符即在采样前结束。summary 用 `--telemetry-receipt-version 4` 读 v4，
`--resident-owner-evidence-dir` 只读回放 owner 原始 READY/start/closed/Job/mapping/CPU；缺失记 named unknown。v4 summary 要求
owner-result 为 `mineru.resident-owner-diagnostic.v2`/receipt 4，plan 字节 hash 等于 receipt 所绑 plan，plan 的 owner_intent_sha256 与
duration_ns 分别等于原始 owner-intent 字节与其 duration_seconds；starter 原始 stdout 与 closed 观测的 job_raw 逐字节一致（与在线 owner 同一校验）。
旧 v2/v3 记录只读保留，不补字段升级。
任何 live session/负载之前，先把最近一次真实捕获的 `/health`+HTTP snapshot+metrics 离线推过当前产品路径
（`decode_windows_resident_sample` → `project_queue_vllm`，绑定 exact capacity/profile/READY 身份）：hash 一致只证明跑的是哪份代码，
不证明它与当前 serving API 兼容。
有限诊断链的寿命上限（R22 向量，仅此链；来源 `resident_measurement_policy.py`）：Mac observer/resident 采样 ≤8500 s，
resident-owner lane lifetime ≤8580 s（= 采样 + 20 s pre-GO + 60 s 收尾），wire/config lane lifetime ≤8590 s，底层 finite
deadline/Job/有限命令上限 8600 s；30 s lease 不变。lane 实际 lifetime 按各自 D 取 D+80 s，短测不跑满 8580。
`BoundedOwnerCommand` 默认上限仍 7200 s，只有 resident start 传输与 M6 campaign 的 launcher 传输（预算超过 7200 s 时，G3 为 7605 s）显式选 8600 s；生产 worker 循环不经 `BoundedOwnerCommand`，无总运行时限；安装器 7200000 ms 与 M6 业务
planned/grace/max_close（4800/2400/7200）不变。跨语言常量由独立上限向量核验，改动 telemetry assembly 源后须重建其
manifest/DLL 并刷新 release inventory，不重装 API。
跨主机运行显式选择 observer receipt/seal v3：API profile 不携带本机启动时钟，Mac observer
identity 单独从实际进程和本机 clock 绑定；Windows READY 保留独立 QPC identity。禁止通过
给两个 clock 填同一 hash 或把 API epoch 当 observer epoch 来通过校验。旧 v2 不能自动升级。
新增 0062 仅允许私有 publish supplement 指向 exact v3；需要受控 migration gate，不能以
手工重标旧 supplement 代替迁移。纯机制 replay 通过仍不等同于 installer-owned activation。
激活前必须在同一 exact source identity 下完成 Windows PowerShell 5.1、
Job Object 树归属、无 per-tick helper、GPU 250--500 ms、host/queue 1 s、exporter+observer 总 CPU 开销、
断线/重启和完整 UTC 3600 秒门禁；任何 unsupported/gap/stale 都只能生成 incomplete evidence。
此外必须先新增 installer-owned 的原子 private identity artifact：绑定 exact exporter/supervisor source SHA、
host assignment、boot、runtime/profile/clock/exporter process epoch 与端口拓扑；installer/attester 必须机械
验证同一字节和 supported backend 后才能产生 activation receipt。当前 installer 没有这个闭包，因此
resident 代码在 installer/worker/settings 的 activation caller closure 下仍保持不可达。

默认构建和部署只提供单一串行执行身份，并保持 `MINERU_PHASE_TRACE=0`、
`MINERU_API_MAX_CONCURRENT_REQUESTS=1`、`MINERU_API_MAX_PENDING_TASKS=1`。旧的双模式、并行候选 profile
及其 fallback 已从执行面删除；任何 process-startup 参数变化都要求先完全 quiesce，产生新的 exact
runtime/profile identity，并重新通过 multimodal canary、epoch、OOM/restart 和 drain 门。

显式容量部署由 `python -m disclosure_anchor.cli.mineru_release build|verify|qualify|bind|install`
从精确 tracked commit 装配（见 [源码发布合同](../design/mineru-release.md)）：容量文件、
`config/mineru-deployment-profile.v1.json` 与 `config/mineru-local-worker-profile.v1.json` 是仅有的输入，
Compose 由包生成，不再手工编辑；`config/mineru-windows.compose.yaml` 只是 legacy 串行基线/回滚参考，
不是当前生产参数表。Windows 侧安装由 `run_mineru_installation.ps1`（有限 Job、独占锁、记录、
daemon 侧 unknown 不自动回滚）拥有；旧 `/private/tmp` 启动器与仓外 DLL 不再是生产依赖。
显式容量试验使用独立的 canonical `mineru.capacity-config.v1` 配置及 SHA，安装器明确选择
`explicit-capacity` Docker target；省略配置仍选 `legacy-runtime`。它支持单进程/单事件循环下
分别配置 N/P/F/H，并保留原解析语义、真实责任与 rollback 门。完整17字段 health、collector v6、
runtime v11 与外部配置逐层对应；不得把 v11 降格投影成旧串行 manifest。
具体参数意义、安装和 no-DB 验证边界见 [显式容量合同](../design/mineru-explicit-capacity.md)。
该试验入口不自动启用 Settings/staged worker/PG 发布，也不单凭功能通过授予 M6 吞吐验收。

新的受控容量搜索必须满足：

- 使用独立 held-out 完整真实 PDF，覆盖 regular/heavy/huge、OCR、表格/公式、跨页结构和
  controlled failure；按具体证据问题确定试验范围，不固定单轮时长或机械重复数小时 baseline；
- 仅当 current synchronized evidence 缺失、超过允许时效、无法与当前 epoch 对齐，或噪声导致证据
  不可用时，才补采一个短串行 anchor；已有可用 current evidence 时不得机械重跑；
- 从当前已验证 exact profile 出发，只对实测瓶颈提出有明确假设、安全预算和验收依据的调参；
  不要求 ratio `1/2/4/8`、固定 arms 或 ABBA，不能用本轮授权绕过 profile 变更和实际 drain 门；
- 全程用 synchronized telemetry 记录 GPU 250--500 ms、host/queue 1 s、phase transition 与 durable commit；
  主指标是同一完整 GPU-host wall span 内按 source identity 去重、成功整文档发布的页数/小时；
- Docker/WSL/CPU/GPU 各资源域使用实测 baseline、active leases 和动态 guard；7 GiB 不是下限或目标；
- 任一 source/page/structure closure、顺序、fallback、OOM、restart、epoch、credit、drain 或 telemetry
  completeness 失败立即停止该 setting；GPU 峰值或均值本身不能宣告胜出；
- 用正确整文档发布 goodput、资源稳定性和观测不确定性判断是否继续，不强制平台期阈值或固定轮数；
  utilization 或 queue 单项不能宣告胜出。完整 UTC 主机小时覆盖仍是 KPI 证据门，不是固定调参仪式。
  当前没有在线自调执行面。

安装器仍必须以 `--provenance=false` 构建并闭合 base digest、Dockerfile/patcher hash、image
labels/marker、安装 receipt 与 runtime attestation。复用已发布镜像时，只允许既有
`-ReuseCurrentPublishedImage -CampaignApiCompatImageId sha256:<exact-id>` 路径，并必须证明 proxy/vLLM
container ID、StartedAt 与 image ID 均未改变。以上供应链约束不等于吞吐 profile 已获授权。

### 1.1f Worker 公共停止（F5）与显式放行

合同见 `../design/worker-operational-stop.md`。

- **触发**：staged V4 语义 `failed_closed`（如 `forbidden_tool_call`）、未能对账的 claim/lease 丢失、
  stage/claim 截止、协调器 circuit（重试预算耗尽、持久转移契约违例、credit 不可得、stream pressure
  关闭、admission 中断；容量 hold 中单个 attempt 自己解不开的：`native_storage_hold`、
  `transfer_integrity_hold`、`stage_grant_unsatisfiable`、`capacity_holds_exhausted`，见 §1.1h）、
  控制器/维护/启动恢复致命错误。第一个原因不可覆盖。worker 依次 latch 停止
  所有平面、对受监督根绑定的 label `launchctl disable` 并回读（launchd 或手工启动都一样；其它根不调用
  launchctl）、写入 `$DISCLOSURE_RUNTIME_ROOT/control/worker-circuit-stop.json`，排空后以 **78** 退出，
  launchd 不再重启。重试预算只在同一 attempt 连续失败时耗尽：provider 权威 status 回答任务仍在处理会
  复位该 attempt 的次数与 300 s 窗口；日志 `[staged-v4] … retry budget exhausted (attempts=…, elapsed=…,
  last=…, causes=…[, http_status=…])` 给出耗尽的边界与最后失败类别（不含异常原文）。
  纯操作员 TERM/INT、无故障且在途工作收尾时以 0 退出；取消（`cancelled`）不计为故障。
- **查看**：`make worker-control-status`（`FORMAT=json` 给 Agent；只读 control 文件与 launchd，不连
  DB/MinerU/模型）、`make worker-status`（stderr 报 STOPPED，退出 3）、`make doctor-full`
  （`worker operational control` 为 FAIL，DB 宕机也能报）。
- **门控范围**：启动门先看本进程 latch、再看活动记录；只有 macOS 上 `DISCLOSURE_RUNTIME_ROOT` 等于
  `DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT`（默认即生产 runtime 根）时，才再读回 worker label
  （未设 `DISCLOSURE_WORKER_LAUNCHD_LABEL` 时为生产 label）的 disabled 状态，手工从生产 shell 启动也同样
  受限。已知 disabled → `OPERATOR_DISABLED`，读回失败/未知 → `CONTROL_UNAVAILABLE`，都拒绝；只有已知
  enabled 才可能 RUNNABLE。其它根（临时/测试/scratch/离线）与非 macOS 不调用 launchctl，status 的
  `supervision` 显示 `unsupervised_runtime_root`/`not_macos`；生产 label 配非生产根（或生产根配其它
  label）显示 `label_root_mismatch`，同样不调用 launchctl 并拒绝启动。部署后先确认生产 status 为
  `supervision=supervised`。生产根还必须是非 symlink、归 worker 用户、不可被组/他人写，且与挂载
  sentinel 同一设备，否则 `CONTROL_UNAVAILABLE`。
- **放行顺序**：
  1. 读 worker 日志 `[worker-control]` 行和记录中的 cause（kind/reason/attempt/lane/provider）。不删除、
     不改写 control 文件；不清理解析输出、receipt 或 group cache。`semantic_failed_closed/invalid_decision`
     先按 §1.1b 判断被拒的是新答案还是已存缓存条目。
  2. 修复原因并离线验证；不要用新 GPU/PDF 负载或注入故障试错。
  3. 确认旧 wrapper/Python 与其 MinerU/语义子进程都已退出（`launchctl print`、`pgrep`）。放行命令
     自己也要求可证明的收尾：label 未 loaded（`print` 精确的 113/`Could not find service`）或 loaded
     且 `not running`，进程表可读且无已知 owned 子进程；launchd 结果未知或进程表不可读都以 75 拒绝并
     保持停止。dry-run 用 `native_closure`/`process_closure` 如实报告 `unknown`。
  4. 先 dry-run，再执行：

     ```bash
     make worker-release-circuit SHA=sha256:<active> DECIDED_BY=<task-id> \
       REASON="<修了什么>" FIXED_BY="<版本或证据 hash>" DRY_RUN=1
     make worker-release-circuit SHA=sha256:<active> DECIDED_BY=<task-id> \
       REASON="<修了什么>" FIXED_BY="<版本或证据 hash>"
     ```

     它持 `WORKER_NS` 单例和短控制锁，按原字节建只增归档、写放行决定、复核后才删除活动记录；任何
     失败都保持停止，崩溃后同一决定重跑幂等。dry-run 不建锁文件、不占单例、不写任何文件。
  5. 放行本身不 enable/启动：`launchctl enable gui/$(id -u)/com.agentinvest.disclosure-worker`；
     已 loaded 未运行用 `launchctl kickstart gui/$(id -u)/com.agentinvest.disclosure-worker`（不带
     `-k`）；未 loaded 则 bootstrap 已审阅 plist。
  6. 恢复屏障重驱同一 attempt/run/输出，已成功 group 命中缓存，只有未缓存 group 可能再调模型；
     观察第一轮 commit/ACK 后再离开。
- **异常状态**：
  - `INVALID_STOP`（损坏、权限、超限）：保留原件，按 status 给出的精确 SHA 放行；symlink/非普通/
    超大未哈希的记录需先修复可信存储。`CONTROL_UNAVAILABLE`：先修挂载/权限/runtime 根（symlink、
    属主、组或他人可写、sentinel 缺失或不在同一设备）或 launchd 读回，此时不能证明没有停止。
  - 日志出现 `STOP_PERSISTENCE_FAILED`：若原生 disable 已生效，把紧随的 `STOP_RECORD` JSON 存为证据
    文件，`make worker-record-circuit-stop EVIDENCE=<file> EVIDENCE_SHA256=sha256:<...> DECIDED_BY=...
    REASON=... DRY_RUN=1` 核对后执行，得到 `operator_reconstructed` 记录再按上文放行。两路都失败时只有
    当前 loaded job 的退出策略拦着（`SUPERVISOR_ONLY_STOP`，启动门同样拒绝），先 `launchctl disable`
    再重建；确认是 wrapper 缺 env 的 78 时修 env 后显式 kickstart 或 bootout。
  - `OPERATOR_DISABLED`：label 被禁用但没有记录，原因不由任何记录证明：可能是普通维护，也可能是记录
    失败的原生停止。此时生产根上的一切业务启动（含手工 `worker loop|once`、pipeline/admin/replay 组合）
    都拒绝。先查日志；确认普通维护后显式 `launchctl enable`（或安装器 `--confirm-operator-disabled`），
    确认是原生公共停止则重建记录再放行。
  - 退出 75：单例被另一 owner（commission/recovery 或旧进程排空）持有，不是故障，确认后手工重启。
- 手工业务入口同样受门控且无绕过开关：`pipeline build-units|publish|process|rebuild-units` 与
  current-source replay 以 78 拒绝，admin API build/publish 返回 503；staged commission/campaign/
  recover 在组合时拒绝。pipeline/admin/replay 在原请求/工厂组合处检查（pipeline 在此之前已有只读 DB
  角色检查，`process` 还有 MinerU 准入探测），任何语义/解析/发布副作用之前拒绝。只读与修复命令
  （status/doctor/parse/track/sync/parse-requeue 等）不受影响。

### 1.1g 本地执行升级（U01）与部署预检

合同见 `../design/local-execution-upgrade.md`。只在“新代码只改了本地 writer，MinerU 计算/依赖/原生 epoch 与仍新鲜的
父资格 Q0 完全相同”时使用；它不是新的资格，也不能串联第二条边。以下步骤均由 root 执行，产物全部为新建 0600 文件。

1. worker 已停止且 unloaded（F5 或维护），代码已冻结，独立测试与独立代码审阅完成；记下三份证据的 SHA-256。
2. 只读生成 E1 与派生身份（加载 worker.env/cninfo.env；此时环境仍是父配置）：

   ```bash
   PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.execution_upgrade release-manifest \
     --source-revision <rev> --output /private/<dir>/release-E1.json
   PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.execution_upgrade derive \
     --parent-process-profile <P0> --parent-activation <A0> --output-dir /private/<dir>/derived
   PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.execution_upgrade legacy-scope \
     --output /private/<dir>/legacy-inventory.json
   ```

   `legacy-scope` 在一个 READ ONLY REPEATABLE READ 快照里记录全部 current V4 head（不按 owner/文档/数量过滤）；
   存在 non-current `prepared`（staged superseder）时拒绝，先如实上报，不要绕过。
3. 私有 env 改为 derive 打印的 R1/P1/A1 五个值（smoke/canary/validation 仍指向 Q0 原文件，容量不变），再
   `execution_upgrade propose --release-manifest ... --runtime-bundle <derived>/runtime-bundle.json
   --parent-process-profile <P0> --parent-activation <A0> --inventory ... --exact-change-manifest-sha256 ...
   --test-evidence-sha256 ... --code-review-sha256 ... --output /private/<dir>/u01.json`。
4. 审阅者对 u01.json 的文件 SHA-256 写 `worker-local-execution-upgrade-review.v1`（verdict=GO）。四个
   `DISCLOSURE_WORKER_EXECUTION_UPGRADE_*` 一起写入私有 env；缺任何一个都拒绝。
5. 预检（只读；不写库、不 claim、不 POST、不记停止）：

   ```bash
   PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.worker deployment-preflight \
     --prepared-key-ttl-seconds <部署中实际的 key 生命周期> [--format json]
   ```

   退出 0 才可安装；78 时逐条处理 `BLOCKER`。TTL 是 provider 幂等 key 的生命周期（已安装 MinerU API 的
   key/tombstone TTL，`enforce_key_lifecycle` 开启时强制；不是任务 retention），由 root 从运行中的 API 读回后
   传入，产品不硬编码、不持久化。key 年龄 = 预检时钟 − key 的 `submission_epoch_unix`；未给 TTL 而存在
   prepared/reconciling head 时为 `unverified` 并阻断，年龄达到 TTL 为 `expired` 并阻断（worker 没有过期重投
   分支）；为时钟偏差与排空时间留余量，真实恢复前重新读回。停止/无效/不可读的 control 在 checker 与 DB 之前
   即拒绝；`native_identity_match` 需要 API 健康且无 queued/processing（保留的 completed 任务不阻断）。
6. `scripts/install_launchd.sh [--confirm-operator-disabled] --prepared-key-ttl-seconds <N>`：安装器在 F5 停止
   预检之后、任何 plist/enable/bootstrap 之前运行同一预检，未就绪时 78 且零 launchd 变更。
7. 启动后核对日志 `[execution-upgrade] scope verified ...` 与 `[execution-upgrade] boot receipt=... receipt_sha256=...`；
   每次启动在 `$DISCLOSURE_RUNTIME_ROOT/reports/execution-boot/<owner>.json` 新建一份 0600 只读收据（绑定
   owner、U01、E1、W0→W1、R0→R1、P0→P1、WP0→WP1、容量、A1、父资格日期、清单与观测范围），只作证据。在所有清单成员终结前，进度行显示
   `blocked=admission_deferred:legacy obligations open (<open>/<total>)`，已有 head 继续 GET/结果/ACK
   或以原请求/原 key 单次提交；`make doctor-full` 显示 `worker execution qualification`（compatible_parent，
   继承）与 `worker legacy obligations`。
8. worker 启动时在单例下重验 E1 字节与全部 head；不一致以 F5 `startup_fatal`/`execution_upgrade_scope_failed`
   停止（按 §1.1f 查因与放行，不要改写清单/H0/Q0）；数据库暂不可达时非零退出、不记停止。
9. 父资格按原日期老化（默认 30 天）：到期后 admission 拒绝，需要对当前 runtime 重新做真实资格，而不是再造一条升级边。
   回退：恢复旧 release 字节与父配置（R0/P0/A0，去掉四个 U01 值），只有字节级恢复旧 writer 才能回到 exact 路径。

**U01 v2（已在一次升级 E1 下冻结了未决责任，再发布 E2）。** v1 的清单成员必须绑定 Q0 的 R0/P0/WP0，不能覆盖
在 E1（R1/P1/WP1）下冻结的责任；v2 把三个角色分开：`qualification_anchor`=Q0（原日期、原期限）、
`recovery_origin`=归档的 E1（release/runtime bundle/P1/A1 归档文件）、`current_execution`=E2。合同见设计文档
“U01 v2”一节。步骤与上面相同，差别只在：

- `derive --contract-version v2 --anchor-process-profile <P0> --anchor-activation <A0> --output-dir ...`：从 Q0 的 M0 与
  当前 writer 派生目标 R/P/A。本次改动若未触及 writer 指纹文件，目标 writer 等于 E1 的 W1，R/P/WP/A 与 E1 相同，
  只有 release（E）变化；不要为了造出新 R 增删指纹成员。
- `legacy-scope` 仍对全部 current V4 head 取一个只读快照；这些成员绑定的是 E1 的 R1/P1/WP1。
- `propose --contract-version v2 --anchor-process-profile <P0> --anchor-activation <A0> --origin-release-manifest <归档 release-E1.json>
  --origin-runtime-bundle <归档 E1 runtime-bundle.json> --origin-process-profile <归档 P1> --origin-activation <归档 A1>
  --release-manifest <release-E2.json> --runtime-bundle <目标 runtime bundle> --inventory ... ...`；混用 `--parent-*` 为用法错误。
- 预检终端分三行列出 qualification anchor / recovery origin / target；启动写 `worker-execution-boot-receipt.v2`，日志与
  doctor 的 legacy 行附 `final_states=`（按终态计数，合法 `local_failed` 与 `acked` 同为终态）。
- 目标 R 等于来源 R 时屏障照样生效：清单成员全部终结前不创建新 H0（`legacy obligations open (<open>/<total>)`）。

### 1.1h 结果存储容量（capacity v2）与新合格运行时升级

合同见 `../design/mineru-result-storage.md` 与 `../design/local-execution-upgrade.md` 的 “Qualified result runtime
upgrade”。所有数值（D/H/P/C/M、单项许可、硬包络、Mac 配额与 W/J、传输期限与进展窗）由 root 按实测卷与支持包络
选定并写入 `capacity-config.json`（v2），产品不推导、不带默认值；两卷以总字节绑定，原生输出卷不符时 API 拒绝启动。
策略要求 C 容纳硬结果的物理计费（分配单元取整 + 每文件开销），硬结果恰等于 C 的策略会被拒绝。

1. 发布：release package/plan/binding 以 v2 容量构建；binding 拒绝不能兑现一份最大 grant 的 Mac 上限（临时盘 ≥
   Z_hard+S_single+W、decoded ≥ W、terminal output ≥ S_single+W、source_pdf 等于策略值），产出 process profile v3。
   安装器与采集器对 v2 不投影 B/L 环境变量（容器 bootstrap 会拒绝它们）。
2. 运行中核对：`/health` 的 `capacity_observation.result_storage` 给出 source/ingress/result 字节、生长中的
   producer、未兑现承诺、completion 队列、各等待原因计数与 `blocked_tasks`；任务 status 的 `storage` 给出阶段、
   等待原因与起始时间、hold 标志。等待是正常背压（同一任务、同一源，不重解析、不失败）；`blocked=true` 是 hold：
   任务保持 processing，字节继续计费，不会自己恢复。原生 hold 原因为 hard_envelope_exceeded、
   codec_bound_exceeded、tree_integrity（写入路径/根/叶身份不符，属完整性而非容量）与 seal_integrity。
   Mac 工作卷实时余量不足（余量 < 下限 + 进程内全部在途承诺）时同一 attempt 记
   `materialization_capacity_waiting` 并等待，不失败、不耗重试预算；续传/续解包与已准入 grant 等容量同样是健康等待。
   Mac 工作配额 D（`mac_work_disk_limit_bytes`）按每个 attempt 在工作卷上的独立占用计：源快照 + max(临时盘,
   压缩结果 + 输出) + 分配余量（(max_members + 8) × 4 KiB），已持久的 credit（恢复出的已有归属只计一次）加在途
   阶段的承诺；会让总和越过 D 的转移在队列里等待（进度/观测的 `credit_blocked_by_lane` 显示 `work_disk_bytes`），
   准入始终给一份最大 grant 留出 D（`work_disk_local_reserve_bytes`），并先为每个新文档扣掉分配余量再给快照字节，
   所以只持有源快照的文档不会把 D 占到等待中的 grant 无法开始；排在车道队首等待的 grant 会挡住后面新的 grant，
   只等前面的 LOCAL 工作排空（排空不需要增长）。只有增长受检，COMMIT/cleanup/ACK 不增长，从不被 D 挡住；策略拒绝
   装不下一份最大 grant + 源快照 + 余量的 D。在另一策略下准入、超出 D 减预留的已恢复快照，会让装得下的 grant 越过
   队首先跑、逐步排空。LOCAL 的那一次解码与 COMMIT 的重开/Unit 构建/就绪/晋升共用一个重活许可（当前 1 个）：
   等许可的 lane 显示 `heavy_work`，是健康等待，不耗重试预算；ACK、cleanup、续租与远端对账从不需要它。就绪文件
   写到发布语料目录（D 之外），第一次写入前按实时余量承诺尚未落盘的文件（`publication_capacity_waiting`，健康等待）。
   - 站点级 hold → F5 公共停止（§1.1f，`coordinator_circuit`）：原生 `blocked=true` 为
     `native_storage_hold`；spool_owner_unproven、spool_progress_unproven、spool_part_identity、spool_part_short、
     spool_prefix_mismatch 为 `transfer_integrity_hold`；任何账本都装不下的 grant 为
     `stage_grant_unsatisfiable`。任务、证据、spool、ZIP 与 credit 原样保留，不失败、不 cleanup、不 ACK。
   - 单文档 hold（decode_input_bytes、decode_output_bytes、transfer_logical_deadline、transfer_progress、
     transfer_range_unsupported、publication_envelope）→ `stage_capacity_hold:<维度>`：该 attempt 保持 claim 可见，其它工作继续；
     只有这些 hold 自己占满某个限额为正的账本维度（例如 materialization_items = Mac finalize 并发）时才
     `capacity_holds_exhausted` 停止；限额为 0 的维度不算。
   - 放行后同一 attempt 只重派一次；原因仍在则以同一 reason/指纹再次停止，不循环。不要删文件、不要手工
     ACK、不要改 registry 或重排来“腾空间”。
   - 原生 hold 的唯一受管出口是对这一个任务的显式终止决定（持有 blocked 任务时安装器拒绝 idle 检查，
     registry 也拒绝以不同策略载入在途任务，所以不能靠“先升包络”解开）。原生路由
     `POST /agent/storage-holds/{task_id}` 与普通 worker 共用同一条 TCP/SSH 隧道 origin，所以它只认操作员凭据：
     没有登记时一律 403 `storage_hold_operator_disabled`，缺失/错误凭据 401，都在读请求体、碰 registry 之前；
     隐藏 OpenAPI、预览 sha 与 decided_by 都不是认证。凭据只由操作员生成与保管，不进 worker.env、settings、
     仓库或日志，也不复用 admin API token 或隧道 key：
     1. Mac 操作员：`umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >
        /private/<dir>/storage-hold-operator.token`（本人所有 0600，一行 43–128 个 URL 安全字符），再
        `PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.storage_hold operator-verifier
        --operator-token-file /private/<dir>/storage-hold-operator.token`，它只打印 `credential_sha256`。
     2. Windows 管理员会话（不是隧道账户）在 API 容器内登记这个 sha（容器只存 sha，不存凭据）：
        `docker exec mineru-api /usr/bin/python3.12 -I -c "import sys; from mineru.cli.agent_task_protocol_v2
        import enroll_storage_hold_operator as enroll; print(enroll(sys.argv[1]))" sha256:<64 hex>`。登记写在
        容器自身文件系统 `/run/agent-invest-operator/`（非 bind mount，0700/0600），立即生效，容器重建即失效；
        用完撤销：`docker exec mineru-api /usr/bin/python3.12 -I -c "from mineru.cli.agent_task_protocol_v2
        import revoke_storage_hold_operator as revoke; print(revoke())"`。
     3. worker 停止状态下：`... storage_hold preview --attempt-id <attempt> --operator-token-file <token>
        --out /private/<dir>/hold-preview.json` 审阅原生预览（任务/key/attempt/fence、hold 原因、字节、策略与
        运行时身份及其 sha），再 `... storage_hold execute --attempt-id <attempt> --operator-token-file <token>
        --expect-preview-sha256 <sha> --decided-by <人> --reason "<为何终止>" --fixed-by "<修复或 none>"
        --out /private/<dir>/hold-decision.json`。
     原生侧在自己的 registry 锁内把该任务记为 failed，闭合原因 `storage_hold_terminated` 带 hold 原因、决定
     sha 与规范决定本身（`mineru.storage-hold-decision.v1`：预览 sha、decided_by、reason、fixed_by），应答前已
     持久；字节与封存保留到普通 failed-task ACK。命令只接受 Mac 上仍为 submitted、无活 claim 的 attempt 所
     对应的那个任务；过期预览、非 hold、已完成、在途 producer、别的决定都拒绝，同一决定重跑得到同一收据。
     execute 的应答丢失（连接断开/超时）时，命令从该任务的普通 status 读回持久决定：与本次决定逐字段相同才完成，
     否则拒绝并说明没有以本决定终止任何东西（重跑同一命令即可）。之后任何时候（worker 放行前）都可用
     `... storage_hold recover --attempt-id <attempt> --out /private/<dir>/hold-decision.json` 只从持久决定重建
     收据（走普通 status 路由，不需要操作员凭据，不改任何状态）。
     然后按 §1.1f 放行：worker 轮询到失败 → `provider_storage_hold_terminated`（provider_terminal，不自动重试）
     → cleanup → ACK。之后若包络已按新资格提高，再按 §5.1 对该失败 run 做 parse-requeue。
   - Mac 单文档 hold 的出口是更大的已声明包络（新策略/资格）；本阶段没有 Mac 侧放弃命令。
     `publication_envelope` 表示该文档的规范请求（8 MiB）、准备记录（24 MiB）或就绪清单（8 MiB）超出私有包络：
     在任何就绪写入与事务 P 之前拒绝，已物化的输出原样保留；出口是包络足够的新发布，不要截断内容、手工改写记录
     或反复重排。
3. 九个 E7 prepared 责任迁到新原生运行时（只在 Qnew 真实资格通过后）：
   - 旧 API 仍在服务、worker 停止时，只读取得旧 API 实际 key/tombstone TTL，然后
     `PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.execution_upgrade legacy-key-lookups
     --inventory <清单> --key-ttl-seconds <实际 TTL> --output /private/<dir>/key-lookups.json`；
     任何成员不是 prepared、已有 accepted、或查询不是闭合 404 都拒绝。这份证据是操作者采集的：文件哈希只固定字节，
     不证明是正确的旧 API 作答；须在监督下对已识别的旧 origin API 采集，并附同一 API 读回的 TTL 记录。之后预检
     必须传同一 TTL，传其他值即阻断。
   - 新原生安装、attest、完整 canary 与 held-out 资格（Qnew）完成，环境指向目标 runtime/profile v3/容量 v2/activation v2，
     smoke/canary/validation 指向 Qnew 文件后：`execution_upgrade propose-qualified --release-manifest ... --runtime-bundle ...
     --origin-release-manifest <归档 E7> --origin-runtime-bundle ... --origin-process-profile ... --origin-activation ...
     --inventory ... --key-lookups ... --exact-change-manifest-sha256 ... --test-evidence-sha256 ... --code-review-sha256 ...
     --output /private/<dir>/qualified-upgrade.json`，审阅者写 GO review，四个 `DISCLOSURE_WORKER_EXECUTION_UPGRADE_*` 一起配置。
   - 预检与启动同 §1.1g（预检终端列出 new qualification / recovery origin / target 与原 key 证据；启动收据为
     `worker-execution-boot-receipt.v3`；doctor 显示 `exact (new qualification)`）。清单成员全部终结前不创建新 H0。
   - key 过期的 prepared 没有本分支路径：预检阻断已过期的 key；运行中久等后才过期的，POST 边界以同一批准 TTL
     按传输墙钟在发送前拒绝（查询命中的任务仍照常对账），head 保持 reconciling、协调器可见停止，不失败、不 POST。
     key 仍有效时继续用受保护的原 key；只有已过期的才走受管结案（有条件，不是强制迁移）：
     `PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.expired_prepared_closure preview --inventory <清单>
     --key-lookups <过期前监督采集的查询证据> --origin-runtime-identity-sha256 <旧 origin> --key-ttl-seconds <旧 API
     实际 TTL> --out /private/<dir>/closure-plan.json`（只读；要求每个 key 在寿命内、清单采集后由 origin 作答闭合
     404，且 head 仍是采集时从未提交的 prepared H0；过期后才查到的 404 不算证据；仍有效的只列出不结案），
     审阅计划后在 worker 停止时 `... expired_prepared_closure execute --plan <计划> --expect-sha256 <sha>
     --inventory <清单> --key-lookups <证据> --decided-by <人> --reason "<原因>" --out /private/<dir>/closure.json`。
     它持 worker 单例，逐个成员以精确 CAS claim，经普通 V4 `pre_submission_failure`（`original_key_expired`，
     重试类 `original_key_lifetime`）与自有本地 cleanup 到 `pre_submission_failed`，run/文档/outbox 同事务；不 POST、
     不查询、不换 key、不 ACK、不改 TTL。成员按计划顺序逐个以各自的持久 CAS 提交，没有批量事务：某个成员被
     拒（活 claim、别的决定、已推进的 head）时停在该成员、不为它写任何东西，前面的成员保持已结案；同一计划与
     决定重跑是安全的——已结案成员原样报告，中断的只在失败原因恰为本决定时续做。
     重排是另一个显式决定，结案从不自动重排：已 `make migrate` 到 0065（`ck_parse_requeue_decision_class` 接受
     `original_key_lifetime`）之后，对每个结案 run 按 §5.1 做 parse-requeue（先 DRY_RUN），`FIXED_BY` 写明允许
     以新 key 重新提交的合格运行时/新 H0 授权；在那之前这些 run 由队列的合同失败闸门保持在队列外。

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

慢下载：HTTP 的 30 秒超时只约束每次 connect/read/write 的单次等待（read 是等下一块数据），
不约束整个响应。网站与 API 两条下载通道因此共用一个整段逻辑下载预算
`CNINFO_DOWNLOAD_DEADLINE_SECONDS`（默认 1800 秒）：token 等待、每次尝试、重试退避和流式读取的
响应体都计入，重试不重置。超出即记一条 `transfer_deadline_exceeded`（retryable=true）下载失败，
部分字节丢弃、不归档不登记；它计入同一 `CNINFO_MAX_RETRIES` 次数预算，并像 `transport_error`
一样触发 `source_outage_break` 与既有 provider 冷却。实际上界是预算加一次钳制后的读等待
（≤30 秒）和一个 token 间隔；响应头阶段与 chunked 分块头逐字节滴送、以及 DNS 解析不在逐块检查
之内，这种病态情况仍由 maintenance watchdog 以 exit 70 兜底。报告里频繁出现该错误码说明当前
provider 很慢：先看冷却是否生效，不要调高预算来硬等；确需调高时必须同时保持它比
`WORKER_WEDGE_TIMEOUT_SECONDS` 至少低 60 秒（否则配置加载失败）。

采集空间与原件交接：`DISCLOSURE_ACQUISITION_FREE_FLOOR_BYTES` 可显式设置 tmp/归档卷的保留空间，
未设置时保留该卷总容量的 10%。API、网站下载与本地登记都在实际增长前检查：下载逐块写 owned staging，
归档／quarantine 先检查新增整份副本所需空间，再逐块检查。已存在且 hash 一致的归档直接复用，不要求再留一份
副本空间。这是实时余量检查，不是跨进程磁盘预留；其他写入者仍可能耗用空间。

下载请求使用 `Accept-Encoding: identity`，非 identity 响应在读取正文前以 `unsupported_content_encoding`
拒绝并落有限重试记录；Content-Length 仅用于提前判断和帧长度核对，完整 EOF、实际字节数和同一逻辑期限才决定
下载成功。联系 provider 前空间不足不会新增失败行或消耗下载重试；开始下载后的失败保留既有 `source_access`
计数。归档目标写入／fsync 的 ENOSPC/EIO 记为可重试 `io_error`，容量不足记为 `local_space_shortfall`，不能
把这些存储故障标为无效 PDF。本地登记的存储错误也记录失败访问并显式抛出，输入文件保留。

失败后先检查 `source_access.result_snapshot` 的 `transfer`、`capacity`、`quarantine_complete`、`input_missing`
和 `retained_filename` / `retained_raw_file_hash` / `retained_byte_count`。未完成下载的片段不会登记为原件。
完整下载会先 fsync 文件和目录；只有不可变归档或完整且已校验的 quarantine 副本接管后才删除 staging。
若交接失败，唯一完整材料保留在配置的 `runtime/tmp`，由失败记录中的 basename/hash/size 定位；先核对材料和
失败原因，再按已有具名恢复流程处理。不得把这些保留文件当普通临时文件清理。此阶段没有自动清扫或直接复用
该 staging 的入口；普通下载重试仍会重新下载，并受既有有限重试限制。

maintenance watchdog：采集平面按“已完成的条目”心跳（成功、已落库的失败与被处理的异常都算），
只有单个 sync/下载条目、一次 projection 批次或其间一次数据库调用本身卡住超过
`WORKER_WEDGE_TIMEOUT_SECONDS`（默认 2700 秒）才会触发 exit 70。证据是日志里的
`[watchdog] … exiting 70` 与其后的线程栈，以及 launchd 的 last exit code 70；随后若有
`[staged-v4] circuit opened during watchdog exit; no public stop latched`，那是 watchdog 自己终止
语义子进程引起的取消，不是公共停止，也不会 disable launchd label。worker 保持停止，按栈定位
卡住的条目后 `make worker-restart`（先确认 `make worker-control-status` 为 RUNNABLE）。

## 5. 死信处置

| 死信 | 找到它 | 处置 |
|---|---|---|
| parse 重试耗尽 / 不可重试 | doctor `parse dead letters` WARN；`pending_parse_v1.last_failed_retryable=false` | 查 `processing_run.error` 根因；修复后 `make process DOC=<id>` 手动重跑 |
| Unit build 重试耗尽 / fail-closed | doctor `build dead letters` FAIL、health degraded、`disclosure_ops.unit_build_terminal_v1`；worker 只在集合变化时告警；0048 起已被后续成功 Unit 代际修复的旧失败自动退出这些运维读面 | 查 `unit_build_error` 与 `semantic_adjudication_summary`；修复 provider/config/规则后 `make rebuild-units DOC=<id>` 生成新 run，再发布；不得改旧 run 或紧循环 |
| provider 全部暂时不可用 | active run 的 `semantic_adjudication_status='degraded_unavailable'`、health/doctor WARN | Unit 集仍保留但无编造语义；恢复任一 provider 后显式 `rebuild-units`，确认新 run 为 complete_primary/backup |
| 空发布（0 unit） | doctor `empty publish dead letters`（实存案例：美的 3 篇「日常关联交易预计」，疑似表格型盲区） | 人工看原 PDF：确属无正文可切 → `make publish RUN=<id> ALLOW_EMPTY=1 REASON=...`；是切分盲区 → 修规则后 `make rebuild-units DOC=<id>` |
| HUGE lane 长任务 | worker report 的 `parse_huge_dispatched` 与 processing_run 时长；不再有大小排除死信 | 以归档 actual byte_count/页数核对成本；正常长任务继续运行；极端 whole-future runaway 由 watchdog 以 70 退出，launchd 不再自动重启，排查后 `make worker-restart` |

下载类死信（新增 2026-07-14）：`invalid_candidate_snapshot` / `raw_archive_error` /
`subject_identity_conflict` 等 retryable=false 的下载失败永久出队，证据在
`source_access(status='failed')` 与 quarantine 目录（含 sha256 manifest）。可重试失败（含
`transport_error`、`transfer_deadline_exceeded`）累计达到 `CNINFO_MAX_RETRIES` 次同样出队。
0064 起三处计数（队列、health `download_dead_letters`、doctor `download dead letters`）共用
`disclosure_ops.download_failure_resolution_v1`：只有被 §5.5 保留原件登记逐条解决、且 Document 仍一致的
不可重试失败不再阻断；失败行本身永不删除，doctor 同时显示总数、已解决数与未解决数。新失败记录带
`failure_phase`，注册阶段失败还带 `archive{raw_file_relpath, raw_file_hash, byte_count}` 与 index ID。

### 5.1 契约类 parse 失败的显式放行

契约类 retry budget（`semantic_route_contract` / `provider_protocol` / `provider_artifact_contract` /
`provider_runaway` / `provider_terminal`）与受管过期 prepared 结案（§1.1h）的 `original_key_lifetime`（0065 起）
永远不自动重试：调度端无法判断根因是否已修、是否要以新 key 重新提交。doctor 的
`contract-class parse failures without requeue decision` WARN 列出这些文档（含 document id 与总数）。
确认修复已经在跑的代码里生效之后，由 operator 显式登记一条 append-only 决定放行该次失败：

```bash
# 先 dry-run：跑完全部 guardrail 并打印 receipt，不写任何行
make parse-requeue DOC=<document_id> RUN=<processing_run_id> \
  FIXED_BY="semantic_router.v102 4548ecaa" REASON="<为什么现在可以重排>" \
  DECIDED_BY="<operator>" DRY_RUN=1
# receipt 无误后去掉 DRY_RUN 写入；失败会打印 typed error 并非零退出
```

放行只针对 RUN 指定的那一次失败：它同时解开 `pending_parse_v1.last_failed_retryable` 闩锁（仅在该 run
仍是最新失败时）和契约类排除。之后若再产生一次没有决定的失败，两道门重新关上，需要新的决定。失败 run
本身永不改写（`disclosure_app` 对决定表只有 SELECT/INSERT，没有 UPDATE/DELETE），决定行记录修了什么、
为什么、谁决定；重试预算不变（契约类失败不计费）。

receipt 把三件事分开，不给单一的「成功」：`decision_recorded`（是否真的写了、decision_id）、
`currently_eligible`（写入之后直接问 `pending_parse` 得到的当前可排队性）、`remaining_blockers`
（最新失败 run 及其 retryable/是否已放行、未放行契约类失败数、item/charged 计数与上限、document 状态、
是否有 running run）。**登记决定本身不等于重新解析**：文档只会在正常 worker 轮询或已授权的 campaign
扫到它时才真正进入 parse。若 `retry_budget_class` 缺失、畸形或不在上述闭集合内，或 `error.retryable`
不是布尔值，命令一律拒绝——队列排除它并不能证明 operator 可以重新放行。受管过期 prepared 结案
（§1.1h）写的 `original_key_lifetime` 由 0065 加入这个闭集合（决定表 DB CHECK
`ck_parse_requeue_decision_class` 与合同集合同步，只多这一类）：它同样被队列排除，只能由指名该 run 的
决定放行；结案本身从不排队。0065 未 `make migrate` 的库仍拒绝这类决定。

放行之后用 doctor 的 `released parse failures still pending` 跟踪：该行按决定之后最新一次 provider parse run
的结果把决定分成 released_pending / released_refailed / resolved 三态（成功之后又失败算 refailed，不算 resolved），
并直接向 `pending_parse` 询问 pending 的那些是否真的重新排队；未 resolved 的决定超过 24 小时会 WARN。
`contract-class parse failures without requeue decision` 只看队列会考虑的文档（status 为 registered / parse_failed），
已发布的文档不算积压。诊断 SQL 本身失败时报 FAIL，不会静默报 0。

### 5.2 已接受 PDF 的暂时性终态失败：自动有限重排

staged V4 下，Windows API 已接受的任务以 `failed` 结束时，默认仍是上一节的 `provider_terminal`（不自动重试）。唯一例外：
API 在失败发生处给出了 typed 暂时原因——VLM 最终 chat 请求最终观察到的结果是 HTTP 429/502/503/504，或该 POST 抛出
`ConnectError/ConnectTimeout/ReadTimeout/WriteTimeout/PoolTimeout`。只按这个最终结果分类，不代表内层 httpx-retries
已用完（响应体读取超时在 transport 返回后只发生一次、不经内层重试），也不新增重试层。该事实与 failed 状态、原 `error` 同一次写入 registry，
经状态接口的 `failure_cause`（`mineru-task-failure-cause.v1`）送到 Mac。此时失败 run 记为
`error_code=provider_terminal_transient_failure`、`retryable=true`、`retry_budget_class=infrastructure`，message 以
`mineru-task-failure-cause.v1:transient:<code>:<status|错误类>` 开头，附状态响应 sha256 和原 provider error。

- 顺序不变：失败 receipt → 本地清理 → ACK → 失败 run 终态；之后普通 worker 扫描 `pending_parse` 才会给新 attempt/fence/key。
  失败处理本身从不 POST；POST 结果不明仍按原 key 对账，不换 key。
- 上限沿用既有计数：infrastructure 只计入合计上限 `5 × max_retries`（默认同一文档 15 个 item+infrastructure 失败 run），
  item 失败另受 3 次上限；计数来自保留的失败 run，重启/重排不清零。耗尽后进入 doctor `parse dead letters`，按上表处置；
  infrastructure 不是 `parse-requeue` 可放行类，命令会拒绝。
- 不会自动重排：解析/内容错误、其他 HTTP 状态或传输错误（含 ReadError、RemoteProtocolError）、OOM/资源、取消、
  finalizer 失败、同一异常上后来附加的清理失败、字段缺失（旧 API/旧记录）、未知版本或未知 code。这些仍按 5.1 值守处理，
  且按错误文本（包括 "timeout"）判断暂时性一律无效。
- 长时间推理服务故障且队列很短时，文档可能较快用完合计上限；排查以失败 run 的 message 与 doctor 为准，恢复后用 `make process`。
- 部署顺序：先部署能识别该可选字段的 Mac worker，再部署新 API 镜像；旧 Mac 会把带 `failure_cause` 的 failed 状态当协议违约
  （仍清理并 ACK，不自动重试）。新 API 的 registry 只有在 typed failed 任务尚未 ACK 时才含该字段；旧 API 可读已排空的
  registry，遇到未 ACK 的 typed failed 记录会 fail closed，回滚前先让 Mac ACK 完。

### 5.3 发布文本不可表示（`publication_text_unrepresentable`）与 U+0000 标记

PostgreSQL TEXT/JSONB 存不了 U+0000。`provider_unit.v24` 起，provider 文本里未被 native 校正替换的每个 U+0000
在 Unit 哈希前一对一换成 U+FFFD（`provider_text_nul_substitution.v1`），文档照常发布：被标记文本出现在 title、
heading_path（含其后代 Unit）或 payload 的 Unit 为 `needs_review`，locator 为 `provider_unit_locator.v10` 并在
`text_substitutions` 记下 source/payload、两侧 hash 与次数。原 PDF、MinerU artifact、ProviderDocument 不改；不要
去猜丢失的字（例如 `第\x00节` 的节号），也不要在数据库适配层清洗。

发布前的纯检查 `publication_text_representability.v1` 是兜底：v24 之前已封存的请求（或 native 校正文本仍含
U+0000）会在 readiness/事务 P 之前被拒绝，记为 `error_code=publication_text_unrepresentable`、`retryable=false`、
`retry_budget_class=provider_artifact_contract` 的 FailureReceipt，经既有清理/ACK 走到 `local_failed`；message 只含
unit 序号、字段、下标/键序号（`#n`）、码位与次数，不含正文。封存的 preparation、源文件与既有发布全部保留，也不会
被改写或重算。未知的 DataError、hash/locator/IO/完整性错误仍按 F5 停止处理，不属于这一类。

处置（root 值守，不是自动）：

1. 核对 doctor 的 `contract-class parse failures without requeue decision` 列出该文档，失败 run 的 message 以
   `publication_text_representability.v1: request=sha256:...` 开头。
2. 确认运行中的 release 已是 `provider_unit.v24`；若处于 U01 v2 排空期，等全部清单成员终结（该 `local_failed`
   本身计为终态）后再放行。
3. 按 §5.1 对该次失败登记一条 `parse-requeue`（`FIXED_BY` 写 v24 release 与审阅引用）。之后由正常 worker 扫描以新
   attempt/fence/key 重新解析归档 PDF 一次，发布带 U+FFFD 标记与 `needs_review` 的首个 active 代际；不重复盲排。
4. 查看已标记的 Unit：`SELECT asset_id, title, quality_status FROM disclosure_public.document_units_v1
   WHERE artifact_locator->>'contract_version' = 'provider_unit_locator.v10';`

### 5.4 大结果包包络超限（具名 `result_source_zip_envelope_exceeded`）

capacity v2（结果存储）进程不再有这一失败：结果大小由策略硬包络、写前许可与阶段 grant 管理，超出硬包络是可见 hold
（§1.1h）。以下仅适用于 v1 容量进程与已记录的历史事件。

Windows API 的 `_retained_result_sources` 以 `required_envelope = 2 × source_bytes + member_count × 65536 + 1048576`
与 268435456 B 的保守包络预算比较，超出即以有限失败结束该已接受任务；另有 4096 成员/FD、名称唯一与
O_NOFOLLOW/regular/nlink 检查。这一预算是结果包络，不是 ZIP 实测大小、原 PDF 大小或显存上限。该类事件在运维记录中
按 `result_source_zip_envelope_exceeded` 命名，保留 accepted identity → 原 trace → 已记录的失败/清理/ACK 链接；不改旧
wire、不改写旧 FailureReceipt、不按 RuntimeError 类名或文本改判为暂时失败。不上调全局预算、不做 N/线程/压缩参数
试验、不删语义所需文件凑预算；要重试该文档前，先取得真实包络分项与返回文件需求，只有有数据支持的选择性容量方案
才值得重新资格（专用 typed 原因属于后续 Windows wire 小版本，不与本次 Mac 发布捆绑）。

### 5.5 历史证券代码与保留原件登记（`registration_metadata_error`）

症状：候选 `security_code` 是本公司已换掉的旧代码（首例 300114→302132），下载与归档成功后注册报
`registration_metadata_error`（`security must be synced before download` 或历史绑定/证据不足），原件保留在
`raw_documents/cninfo/<旧代码>/<年>/<pid>/`。契约见 `design/historical-security-retained-registration.md`。
不要为此伪造旧代码 profile/USCC、改候选代码、删失败行、重下 PDF 或直接用 `register-local-pdf`
（通用入口遇历史代码一律拒绝）。

每批失败都单独走一次（原 42 条与之后新增的失败是不同 cohort，计划与批准范围不自动扩展）：

```bash
# 0. 前提：0064 已 make migrate；最终代码/发布身份已核对；维护停止窗口（否则常驻 worker 会解析新 Document）
# 1. 绑定预览（只读）：决定文档写明官方证据、锚点 USCC/profile、旧新代码、生效日、批准索引接口与公告范围
make source-recovery ARGS="binding-preview --request /ABS/binding-decision.json \
  --evidence-file /ABS/official-announcement.pdf --out /ABS/binding-plan.json"
# 2. 具名确认：--decided-by 必须等于决定中的 decided_by；同一决定重复执行返回原记录
make source-recovery ARGS="binding-execute --plan /ABS/binding-plan.json --expect-sha256 sha256:<plan> \
  --decided-by <decided_by> --evidence-file /ABS/official-announcement.pdf --out /ABS/binding-receipt.json"
# 3. 保留原件预览（只读）：请求逐条给 failed/index SourceAccess ID 与期望 raw hash/字节；任何一项拒绝即无计划
make source-recovery ARGS="replay-preview --failed-access-ids /ABS/request.json \
  --binding-source-access-id <binding sa> --max-items <N> --out /ABS/replay-plan.json"
# 4. 执行：逐项独立事务、首个拒绝即停、已提交前缀保留；重跑相同计划不新增 Document/回执/事件
make source-recovery ARGS="replay-execute --plan /ABS/replay-plan.json --expect-sha256 sha256:<plan> \
  --max-items <N> --out /ABS/replay-result.json"
# 5. 核对（只读，以数据库回执为准；结果文件不能单独证明提交）
make source-recovery ARGS="reconcile --plan /ABS/replay-plan.json --expect-sha256 sha256:<plan> \
  --out /ABS/reconciliation.json"
```

- 输出文件从不覆盖；每次预览/执行用新路径。计划只在同一 recovery 代码摘要下执行，代码变更后须重新预览。
- 预览拒绝常见原因：`INDEX_AFTER_FAILURE`、`INDEX_MISMATCH`、`RAW_ARCHIVE_NOT_VERIFIED`（目录缺失/多版本/链接/
  非普通文件/名实不符/路径不安全）、`RAW_ARCHIVE_MISMATCH`（与请求期望或失败记录的归档事实不符）、
  `OTHER_UNRESOLVED_FAILURE`（同 pid 还有请求外的失败——一并列入请求）、`RETRY_BUDGET_EXHAUSTED`（本入口不重置预算）、
  `NEWER_VERSION_EXISTS`、`SUBJECT_NOT_VERIFIED`（绑定范围/锚点/org 否证）。
- 执行停止常见原因：`RAW_ARCHIVE_NOT_VERIFIED`（原件变化或被换成特殊文件——不会阻塞）、`RAW_ARCHIVE_MISMATCH`
  （计划 raw 路径不是本条自身的归档路径）、`SUBJECT_CHANGED`/`INDEX_CHANGED`/`FAILURE_RECORD_CHANGED`（计划谱系与
  重验结果不符——重新预览，不要手改计划）、`RECEIPT_CONFLICT`（已有回执不是该失败的同一义务：跨 provider、链接
  错位、错 pid、raw/主体不同或无匹配 Document——停止并人工核查，不要删回执）。
- 执行中断或提交结果不明：先 `reconcile`，不要删除回执重跑；同计划重跑会跳过已核实完成项。
- 核对出口：`resolved + unresolved + conflict = item_count`、`failure_history_unchanged = item_count`、每条失败至多一个回执、
  每个 (provider, pid, raw hash) 至多一个 Document；doctor `download dead letters` 的已解决数随之增加，失败历史总数不变。
- 登记只把原件变成 `registered` Document；之后是否解析、分类如何，由既有公司范围与分类规则决定。

## 6. TCC / launchd 假死

worker 以 exit 77 自杀 = TCC 拒绝访问外置盘（详见 `scripts/run_worker_once.sh` 头部注释）。
处置：系统设置 → 隐私与安全性 → 完全磁盘访问 给 `/bin/zsh`（或按注释操作），然后
`make worker-restart`。非 0 退出不会被 launchd 自动重启（`SuccessfulExit=true`），授权修复前不会
反复自杀；除 §1.1 首次 staged cutover 外，不要手工 bootout。

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
- 0052 的两个 outbox partial index 使用普通 `CREATE INDEX`，不是 concurrent build。开发库可在
  worker/API 停写的维护窗口升级；任何已有持续写流量的环境必须先测量 outbox 大小并安排明确停写
  窗口，不能把 Alembic 事务内的索引创建冒充无阻塞 online migration。
- admin API 需要 `DISCLOSURE_ADMIN_TOKEN`（Bearer）且仅回环可用；token 在 worker.env，
  轮换用 `openssl rand -hex 32` 换值后 `make worker-restart` + 重启 API。
