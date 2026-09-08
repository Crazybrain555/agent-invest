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

Mac resident collector 可显式选择 `resident-ssh` extra（Paramiko 5.0.0），使用进程内常驻 SSH
访问配置中唯一固定的 `127.0.0.1:port` HTTP 目标。IP、用户、key/known_hosts 的绝对路径是私有
config 引用，密钥 bytes 不进入 collector spec 或 telemetry；仅读取指定文件，不读取 SSH config、
agent、邻接 certificate 或其他密钥。文件须当前用户所有、有限 regular file；key 禁止其他用户权限，
known_hosts 禁止 group/world write；未知/冲突/不匹配 host key 在认证前失败。
只尝试一次 Ed25519 public-key auth，部分认证直接失败，无密码/交互 fallback，无 shell、远程命令、
子进程、新监听或每 tick SSH 握手。Transport 启动在 factory/READY 内，close 有界 join 并确认线程退出
后才允许 observer preseal；SSH 客户端线程计入所属 collector process CPU，Windows sshd 转发 CPU
不在 Mac `RUSAGE_SELF` 中，不能由该值宣称两端 SSH 的完整成本。
机制依据为 Paramiko 5.0.0 commit `710cc5c02e2ded370d8d24e261e2baa8317a20fa` 的
[Transport](https://github.com/paramiko/paramiko/blob/710cc5c02e2ded370d8d24e261e2baa8317a20fa/paramiko/transport.py)；
低层 handshake/key check/auth_publickey 避开 SSHClient 旧认证路径的 partial-auth interactive fallback。

默认关闭的原生 backend 分片在 `scripts/windows/linux_resident_host_sampler.py` 与
`scripts/windows/mineru_nvml_backend.cs`。Linux backend 仅依赖 stdlib：进程/cgroup 目录 FD 固定，
每次采样前后核对 boot/PID/starttime/cgroup/父目录身份，有限输入输出与 kernel read；stdin EOF、
有限租约或不可续期的 hard lifetime 结束进程。native SIGALRM 使用默认终止动作，不能换成可能延迟的
Python signal handler。执行 owner 按实际 container ID 核验 pinned image、host PID/cgroup namespace、
无网络、只读、cap-drop 和退出后不存在；Windows Job 退出不能替代 Linux absence。自身 namespace ID
由 helper 实读，host 模式由 Docker 实际配置证明；不为读取 `/proc/1/ns` 增加权限。
父 cgroup 指整个共同父层级，可能包含其他容器和 helper，不冒充三个服务之和；不再次累加子组。
CPU throttle 仅代表该父层级自身带宽限制，VmRSS/HWM 与 MemAvailable 是内核近似统计。
缺失字段只令所属 section unsupported；malformed、身份变化、累计计数回退不能变成成功或补零。
helper 的 `cpu` 是本进程自启动至该次 snapshot 的 user/system 累计，不覆盖后续 emit/退出开销，
因此不能独自证明完整联合生命周期的 2% 门禁。
`scripts/windows/linux_resident_host_supervisor.py` 提供独立的默认关闭
`mineru.linux-resident-supervisor.v1`：在全新单线程 stdlib interpreter 中仅 fork 一次，
子进程执行 SHA 固定的 sampler；父进程有界转发 canonical READY/sample/close。
正常 close 后必须读到子 stdout EOF，并对直接创建的精确 PID 执行 `wait4` 得到 exit 0，
才发送 closed 核算帧。该帧的 `sampler_exit_cpu` 覆盖子进程从 fork 到退出的全部 user/system CPU，
包括最后一帧之后的收尾；不再重复加采样帧中的子 CPU，也不使用聚合 `RUSAGE_CHILDREN`。
`supervisor_pre_attestation_cpu` 仅是父进程自启动至该帧生成前的 `RUSAGE_SELF`，
不包括该帧编码/写出与父进程退出，禁止称为所有进程的 full-run CPU。
操作 deadline 先触发有界 TERM/KILL/精确 reap，另留 2 秒默认动作 ALRM 兜底原生阻塞，
故 supervisor 最长为配置 lifetime 加 2 秒（上限 7202 秒）；采样 lifetime 与 KPI 分母不延长。
EOF、非法命令、异常子退出、超时或未回收均不能生成成功 closed receipt。
外部 owner 仍须验证实际容器消失；源码中这项明确的一次子进程能力不放宽 observer collector
的禁止 descendants 边界，也不引入 per-tick fork 或新镜像。
NVML backend 固定 System32 DLL bytes 并全程持有 deny-write/delete handle，UUID 选卡，直接读取
utilization/memory/power；只有 NVML NOT_SUPPORTED 可投影 unsupported。原生调用仍须外层 Job/deadline
约束。power 保留驱动提供的平均语义，不把以 250ms 读取误称为 250ms 瞬时功耗。
这些 backend 的独立实机 smoke 不替代 exporter 接线、联合退出/CPU 或完整 host-hour 验收。

`mineru_telemetry_job_supervisor.cs` 是默认关闭 Windows 生命周期 backend，由显式 session PS
入口加载预编译 DLL 后调用。它使用 Windows 10+ `STARTUPINFOEX/PROC_THREAD_ATTRIBUTE_JOB_LIST`，使 suspended child
在创建时即属于非继承 handle 的 unnamed kill-on-close Job；核验 `IsProcessInJob` 后才 Resume。
禁止降级为 Create 后再 Assign（owner 在两者之间退出会遗留 suspended child）。有限 wait 后必须
实读 Job `ActiveProcesses=0`，才读取包含已退出成员的 `TotalUserTime/TotalKernelTime`，100ns 转 ns。
`mineru.windows-job-accounting.v1` 中 `forced_termination=true` 或 child exit 非 0 均不能通过正常
关闭验收；active0 不替代 Linux container absence。父进程 CPU 使用原生 `GetProcessTimes`，
只标为 pre-attestation；同一父 process/creation 的累计值不能在多张 Job receipt 中重复加总。
源码 SHA 字段是待核验绑定，单独回显它不能证明运行代码。

`build_mineru_telemetry_assembly.ps1` 是测量之外的显式准备步骤：固定 source/recipe/compiler
文件 handle，直接调用所固定的 csc，有限编译与退出，记录三个 C# 源、参数、编译器、System/System.Net.Http assembly 和
实际 DLL SHA，输出至新 GUID 子目录；不覆盖已有构建。失败保留诊断，但不返回成功 manifest。
运行进程仅经 `load_mineru_telemetry_assembly.ps1` 读取有限且固定的 manifest/DLL bytes，核验外部
owner 已严格验证并绑定构建的 manifest SHA 与预期源码 SHA，随后 `Assembly.Load(byte[])`，
全程持有 deny-write/delete pins；fresh process 才允许加载。运行阶段不得 Add-Type 编译或起 compiler。
编译准备不计入受测进程生命周期，但加载与启动 CPU 要计入；这不改变 observer preseal 的原有边界。
prepared manifest v2 固定 NVML、Job supervisor 与 `mineru_resident_wire.cs`；后者提供有限递归
JSON parser、raw subtree、LF framing 和单个 Docker stdio child。所有子树均验证 UTF-8/重复键/
grammar/depth/node/byte bounds，raw slice 保留 Python/NVML 浮点拼写；最终 canonical bytes 仍由
Python owner 验证，C# parser 不是通用 Python 浮点 canonicalizer。连续的 close/closed 帧可在一次 OS
read 中到达，必须保留尾部。写 BaseStream 后显式 bounded Flush；失败后禁止复用或覆盖 pending task。
CLI exit0/EOF/空且已结束的 stderr 是必要但非充分条件，不能替代 exact container absence。

同源 `MineruBoundedHttp` 固定实际加载的 System.Net.Http DLL hash/handle，每实例只访问一个
loopback port 和 health/private HTTP/metrics 三个路径；禁用 proxy/redirect/cookies/default credentials/
自动解压。ResponseHeadersRead 的完成不是 body 完成：同一绝对 QPC deadline 覆盖请求和逐段 body
读取，响应头上限 16 KiB、body 至多 192 KiB、严格 UTF-8，拒绝状态/编码/长度异常。
失败永久停用该实例，cancel/dispose 后等待 pending I/O 结束；无法 quiesce 必须暴露失败。
native startup/disposal 的最终 hard fuse 仍是外层 Job。
`MineruQueueTelemetry` 镜像既有 closed wire health/runtime；HTTP PID 由 owner 从已固定 host PID 的
实际 NSpid 映射取得并绑定 boot/starttime，禁止猜测 PID 1。vLLM 只接受四个精确 metric 名称，各唯一
`engine="0"`/固定 `model_name` series，无 alias/sum/stale fallback；计数使用精确十进制数位处理，
不通过 float/decimal 四舍五入；仅 preemption 是单调 counter，health terminal registry gauge 允许下降。
`test_mineru_resident_wire.ps1` 和 `test_mineru_bounded_http.ps1` 是显式独立机制测试；其中 test-only
Add-Type/loopback server 不得进入 measured exporter，也不替代正常退出/联合 CPU/hour 门禁。

两个 PS session 入口只接受私有 `ConfigJsonPath` 与其 exact SHA。配置闭合绑定新 session GUID、
lane/cadence/loopback port、有限 lease/不可续期 lifetime、prepared manifest、六份 bootstrap/backend
脚本源码及 PowerShell executable SHA；config/源码/DLL 全程固定文件 handle，运行期无编译。
`load_mineru_resident_session.ps1` 构建的 READY 包含实际 PID/creation、QPC frequency、已加载
manifest/DLL、NVML 设备或 Docker/Linux READY。owner 提供的 host/boot/runtime/profile hashes
仍是待外部独立核验的 claims；回显配置不是 activation receipt。
artifact 先在同一私有目录的 new-only pending 文件写完并 Flush(true)，关闭后以不覆盖的
File.Move 发布最终名，再固定其 bytes。失败保留 pending 证据，不能删除旧 READY 来重试；
不声称 Windows 文件机制等同于 POSIX anchored directory seal。
starter 在启动 Job 前先原子发布 new-only `supervisor-started.json`，记录同一 config/session
和父进程实际 creation；响应丢失只能读取这个 marker 与后续 READY/Job 解决，不得盲目重启同一 session。

`MineruResidentEndpoint` 只监听 `/v1/<session>/<lane>/after/<sequence>` 与 `/close`；READY 后
尚不采样，首次合法 after/0 才启动绝对 cadence。只保留最新一帧，允许取回 exact next，不重置
旧 checkpoint；跳过的 slot 保留在 sequence 中暴露缺口，无 catch-up burst。有效请求才续租，
但过期和 closing 不可复活，hard lifetime 不延长。一个待 accept task 或 held request，等待有界；
回复 WriteAsync/FlushAsync 共享期限，失败 Abort 后必须确认 pending I/O 结束。仅明确的
HttpListener Windows error 64 可视为连接丢失；deadline、任意其他异常或未 quiesce 均失败。
close 先禁止采样/续租，再核对 Linux close→closed→EOF→exit0/空 stderr、释放 backend 并写独立
closed 文件；回复丢失可从文件恢复，但仍必须有正常 Job 回执和外部 exact container absence。
同步 native startup/disposal 的最后期限由 atomic Job 提供，不由协作式 callback 假装保证。

`test_mineru_resident_endpoint.ps1` 是合成会话/发布机制测试；
`test_mineru_resident_session.ps1` 是显式实际 backend 诊断 owner，启动一个有限 Job，独立匹配
live READY PID/creation 与退出后的 Job accounting。host 检查实际 Docker ID/image/namespace/
无网络/只读/cap-drop/no-new-privileges，并在正常退出后核验 exact ID 不存在。
SampleCount=0 仅检验关闭链，不证明 host/queue sample。诊断 owner 的控制 CLI/HTTP 成本不在
该 Job 的 CPU 中，不能据短测宣布 combined/full-hour 通过。Docker 29.6.1 的
[generateSecurityOpt](https://github.com/moby/moby/blob/8ec5ab355a34b2a0e2b3238d67bdefe77fefa982/daemon/create.go)
会为 host PID 自动附加 `label=disable`；该 runtime 诊断要求与 `no-new-privileges` 恰为两项，
不接受任意额外 security option。代码对应 `docker-v29.6.1`，不是以 upstream main 代替实际版本。
File.Move 的 [Framework no-replace 合同](https://learn.microsoft.com/en-us/dotnet/api/system.io.file.move?view=netframework-4.8.1)
不允许覆盖已有目标；listener 的 [Abort](https://learn.microsoft.com/en-us/dotnet/api/system.net.httplistenerresponse.abort?view=netframework-4.8.1)
关闭响应连接，异步完成仍须单独核验。
机制依据：Microsoft [Job accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)、
[creation attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)、
[PowerShell 5.1 Add-Type](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/add-type?view=powershell-5.1)、
[ResponseHeadersRead body timeout boundary](https://learn.microsoft.com/en-us/dotnet/api/system.net.http.httpcompletionoption?view=netframework-4.8.1)。

API outgoing HTTP 的只读快照由 patched serving process 内的
`GET /agent/telemetry/http-requests/v1` 返回，不加入原有 closed `/health`。
闭合字段为 `contract_version=mineru.api-http-request-snapshot.v1`、`active_requests`、
`pending_requests`、`process_id`；计数非负，PID 是实际 serving namespace 的进程号。
每个请求仅在现有 final `client.post` semaphore 前计入 pending，acquire 后原子移至 active，
返回/异常/取消后归还；transport 内部重试始终属于该次 logical POST。所有 loop 的计数用同一个
短持有 thread lock 汇总，原 loop semaphore 容量与 outer predict semaphore 保持不变。
它既不是 TCP 连接数，也不是入站 PDF task 数；无流量时真实 0 是有效观测。
端点不初始化 task manager、不写状态，响应 `Cache-Control: no-store`，不进入 OpenAPI 文档。
“private”指运维接口范围，不是新增鉴权；沿用 serving service 既有访问边界。
collector 必须检查部署/进程 identity，不能从另起 Python 进程的全局变量取得这些计数。

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

跨主机 resident 会话使用显式 `receipt.v3.json` / `seal.v3.json`，继续使用原封不动的
`frames.v2.jsonl`。v3 的 `FrozenApiProcessProfile` 只保存实际 API epoch、runtime/profile 与
startup-only parameters，不伪造 API 的启动 UTC 或 Mac 单调时间。独立 `observer_identity`
保存实际 Mac observer process epoch 与本机 monotonic clock domain；frame/receipt clock 必须
对应这个本机 domain。Windows QPC 的原始 clock/source/process/config 留在私有 READY 中，
每帧 exporter provenance 的 source sequence/QPC/UTC 通过同一 exporter epoch 绑定该 READY，
不得把 Mac clock hash 填成 Windows QPC hash，也不得跨域相减单调时间。
reader 必须显式选择 v3，旧 v1/v2 bytes、schema、校验和默认 reader 行为不变；不自动升级旧证据。
已有 v1 phase-trace consumer 不获得 v3 同步资格。历史 v2 coverage 保留历史投影语义，不用于
这次跨主机的实际 observer 身份证明；v3 coverage 从独立 observer_identity 取值。

`resident_session_evidence.py` 只重放机制，不发放 activation：strict canonical config/READY/
prepared/source/closed-v2/Job/Linux receipts 相互 hash 绑定，保留精确 Int64；拒绝同 PID、
父进程晚于子进程、重复 service PID/cgroup、非法 GPU UUID，以及非正常退出。
observer mapping 除 owner/source/runtime/profile 外，还须逐帧比较 GPU UUID 派生设备身份、
API 的 `boot_id + members.api` epoch，以及 sampler 的五字段 HostSampler.identity
（boot/members/parent_path/device/inode）派生父 cgroup epoch；后者不是加了 helper PID/source/
namespace 的 sampler process epoch。远端 source 首末/closing 与相邻 QPC/UTC 均校验 50ms+50ppm；
仅 UTC 桥接本机收集 bracket，首帧必须 sequence1，无缺 slot、反向时间或静默重设。
关闭期间允许 consumed prefix 后的连续采样尾部，但不能延长 observer 分母或补齐缺口。
执行 owner 另须核验实际 Mac 进程/boot/clock、两个 Windows READY 的共同 host/boot/runtime/profile、
真实 source/DLL 与 live PID/creation、实际 Docker 配置和退出后 exact ID absence；caller boolean、
仅自报 hash 或孤立短测均不能代替这些原始证据。

Mac identity reader 通过安装的 Apple SDK `PROC_PIDTBSDINFO` 读取真实 PID/parent/UID/出生秒与微秒，
通过只读 `sysctlbyname(kern.bootsessionuuid)` 取得 boot UUID，并绑定实际
`time.get_clock_info('monotonic')` 与 kernel release。原始 process/clock 分别 hash；重复核验
boot/process 稳定，不调用 shell、不制造启动时间。依据为本机 SDK 的 136-byte `proc_bsdinfo`
布局和 [Apple sysctlbyname](https://developer.apple.com/documentation/kernel/1387446-sysctlbyname)、
[Python 3.13 ctypes](https://docs.python.org/3.13/library/ctypes.html)/
[time](https://docs.python.org/3.13/library/time.html) 接口。
`dedicated_mac_observer.py` 显式 spawn 一个独立 observer，外层从 kernel 独立读取该子 PID 并
比较原始 bytes 后才发送一次 GO；此前不创建 collector/观测 artifacts。控制 socket 有消息长度与
绝对读 deadline，EOF 触发协作取消，非法消息即使到达 drain/seal 阶段也不得返回正常结束。
正常退出要求 collector 由原 runner 关闭回收、watcher join、前后 observer identity 相同、exit0，
再由外层做显式 v3 anchored replay。`poll(timeout)` 只限定进程等待，不限定之后的 file replay。
依据 [Python 3.13 multiprocessing](https://docs.python.org/3.13/library/multiprocessing.html)，
terminate 父进程不会终止 descendants；故 GO 后若协作回收超时，保留明确 PID/run 的未解决 ownership，
不以单独 kill observer 冒充完整清理，也不发放 activation。该工具不是强制清理保证。

`resident_combined_cpu.py` 对已完成 canonical replay 的七项成本做整数加总：Mac preseal delta、
两个 Windows Job 全成员总 CPU、两个不同父进程的 pre-attestation CPU、Linux sampler wait4
退出 CPU 与 Linux supervisor pre-attestation CPU。每份 closure 必须匹配精确 config/READY hash；
同 session 的另一个启动实例也不能提供 CPU。两个 lane 的 session/Job/Windows PID 角色不得重叠。
只以 observer sampling elapsed 为共同分母，使用 `100 * total <= 2 * elapsed` 判定限定成本是否
不超过 2%；不得重复添加每帧或 collector CPU，也不能延长分母。明确排除 observer 父 bootstrap、
未回收 resource tracker、外层 owner、SSH server/control CLI、seal/attestation serialization/exit。
它不是全系统 full-run 开销，且算术通过不替代外部实际身份/退出/absence 证明或 full-hour gate。

`resident_telemetry_owner.py` 是显式调用、default-off 的有限组合运行器，不部署服务、不创建
业务任务、不签发 activation。调用前必须准备私有远端目录/config/compiled bundle，并另行完成
适用的真实 runtime/profile 核验。运行器在任何启动前验证两份完整 canonical config、manifest/
source SHA、互异 session/port/path、共同 host/boot/runtime/profile 与实际计划的寿命预算；
本 owner 的 lifetime 最大 7,180,000ms，为外层命令保留 20s，不能超过其 7200s 硬上限。
先写 new-only 私有 intent 和本机 composition 源码快照，再并行持有两个 Windows starter；
独立 source-pinned READY 控制命令最多等 10s artifact（总命令期限 25s），不采样、不续租。
原始 STARTED marker 的 SHA 必须贯穿 READY/closed，完整 canonical Job/Windows/Linux closure
均重算，且最终 Job 父身份必须与同一 marker 精确相等；只检查 absence 字符串不足以闭合。
两路外部 READY 后才建立 Mac observer/独立 GO；观察器已回收后，同线程拥有的两个独立 HTTP
客户端并行请求 close，各仅一次。200 body 必须等于原始 closed 文件；仅 transport response
loss 可依赖原 session 的正常 Job、独立文件重读与 exact helper absence 对账，其他异常仍失败。
最后验证 source mapping、七项 CPU 与本机 source 未变化，保存原始 stdout/stderr/exit、证据
SHA 和明确的 diagnostic-only 结果。外层命令采用显式 known-hosts/key、publickey-only，
禁用 global known-hosts、agent、默认 identity file 与用户 SSH config；不改信任或凭据。
失败保留原始输出并报告精确 session 待对账；终止本机 SSH 不代表远端已消失，也不得自动重启。
Darwin 对已退出但未回收的 zombie-only 本机进程组可能返回 EPERM；此时仅回收自己持有的
Popen leader，再要求 signal 0 明确返回 ESRCH 才视为本机组已消失。live leader、仍存在的
descendant 或持续 EPERM 均继续报错；清理报错也不能跳过已捕获 stdout/stderr 的保存。

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

`full-gpu-host-hour-kpi.v1` 只接受显式版本验证的 v2/v3 observer coverage 摘要和带原 publish commit UTC、source
identity、profile identity、闭合页数的 durable evidence。bucket 固定为 UTC `[hour, hour+3600s)`；idle、
readiness、restart、故障和恢复均留在分母。coverage gap/overlap、identity drift、unsafe observer、页数冲突
或旧 outbox 缺 profile/runtime 都令结果 incomplete，既不缩短分母，也不补零或外推。late supplement 仍按
原 publish commit hour 回填。host goodput 可跨 profile 汇总；profile-eligible goodput 只在整小时 profile
单一且所有 publish evidence 同 profile 时给出。

本地 relay checkpoint 只是 owner-only、canonical 的 cache，不能单独证明 restart continuity。
0054 已提供 append-only publish evidence ledger 与 DB relay head，V4 publisher 已在整文档事务中写入
base evidence；后续生产入口须接入这些现有机制并从其 replay 推导 first durable publish，不另造 ledger。
append-only 0062 只扩展 supplement 的版本约束，允许 exact v2 或 v3；不重写 0054 或历史行。
版本属于 conflict identity：同 run/source/receipt/seal 被标成不同版本也必须保留冲突，不当作重复成功。
存在 v3 数据时降级旧约束必须失败，不能删除/重标证据来降级。
调用者布尔值或事后补写文件都不能使 host-hour complete。
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
