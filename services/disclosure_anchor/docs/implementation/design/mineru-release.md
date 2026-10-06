# MinerU 源码发布装配：唯一配置投影、构建、验证、资格与绑定

状态：implementation-contract（WP1，R20）。从属于 `service-purpose.md`、`mineru-explicit-capacity.md`
与 `mineru-throughput-scheduler.md`；不改变解析语义、公开契约、持久权威或 ACK 边界。

## 1. 问题与决定

此前一次部署由若干临时脚本拼装：上一版临时包被复制后局部替换数值、N/P 投影到 Compose 而 H/L 不投影、
安装外层依赖仓外 DLL 与 `/private/tmp` 启动器、绑定从旧报告树复制 activation。这些都是同一配置的
多份可编辑权威。本合同只保留：

- **一个投影函数**：`application/contracts/mineru_capacity_config.py` 的 `capacity_environment()`
  与 `capacity_http_arguments()`。API 镜像内的 bootstrap 直接导入并再导出同一函数；PowerShell
  安装器保留自己的字面映射，只作为漂移守卫，不是第二份可编辑配置。
- **不可变装配对象只有三个**（R20 跨阶段定义）：`m6.release.v1`（本包，WP1）、campaign intent（WP2）与
  既有 `M6RunSpec`。`mineru.deployment-profile.v1` 与 `mineru.local-worker-profile.v1` 是本包的**配置输入**，
  不是新的装配对象或账本；它们只提供确定值，包只引用其哈希，不复制维护第二套容量数值。
- **一个入口**：`python -m disclosure_anchor.cli.mineru_release build|verify|qualify|bind|install`；同根注册表
  重确认（§4a）是同一入口与同一 Windows owner 的 `same-root <phase>`，不是第二套部署工具。

生产 worker 不依赖本入口；正式 M6 测量按 WP2 的 campaign 入口复用本包的 native 产物。

## 2. 输入与权威

| 领域 | 权威 | 投影/消费 |
| --- | --- | --- |
| API 容量 N/P/F/H/W/线程/ratio/B/L | canonical `mineru.capacity-config.v1` 字节 | 12 个 env + `--max-concurrency <H>`；镜像烘焙路径/SHA anchor |
| 部署拓扑 | `config/mineru-deployment-profile.v1.json` | 完整 Compose（结构化生成，解析回读比对） |
| Mac 本地并发与声明上限 | `config/mineru-local-worker-profile.v1.json` | process profile v2 的 ceiling、activation 策略、私有 overlay |
| 实际生效 | collector v6 观察、attester runtime bundle v11、health/pressure 实时样本 | `bind` 只消费；未观察到的值保持 unavailable |
| 部署资格 | `mineru_heldout_validation_receipt.v2` 的 canonical SHA | `bind` 与 M6 runtime identity 的 `deployment_qualification_sha256` |

C（在线 remote-wait 上限）的结构域是 P（`total_nonterminal_limit`），不是 N：activation 加载器只做
`qualified_max <= P`；协调器仍以 `remote_waits`（=P）为硬信用上限，API 仍以 N 限制并行解析。
`qualified_max` 是选定上限，不是资格证明；当前候选仍为 C7。

发布构建不加 `B×P<=L`；Mac 的 process profile 合同仍要求该关系，因此这类容量会在 `bind` 可见失败，
`build/verify` 以 `local_profile_admits_full_pending=false` 提前提示。

inference 的 GPU 显存预算只有一个意图输入：部署 profile 的
`inference_declared_defaults.vllm_gpu_memory_utilization_millionths`（整数 millionths）。
`mineru_release_plan.inference_command_argv()` 是 inference 完整 argv（entrypoint 在前）的唯一投影：Compose 生成、
`verify` 对解析回读 Compose 的核对、`bind` 对 runtime bundle 命令的比对都要求与它逐项相等。值为 500000 时保持
历史无 flag argv——MinerU 3.4.4 的 server 入口只在 argv 未给出该参数（精确参数或 `参数=值`）时追加 helper 默认值，
在 >8 GiB 的 GPU 上即 0.5——既有 500000 发布的 Compose 与回执字节因此不变。其它值追加且只追加一次
`--gpu-memory-utilization <整数>.<六位小数>`（400000 → `0.400000`）；缺失、重复、`=`/下划线别名、非规范写法、
KV bytes/blocks 覆盖或其它附加参数都与投影不等而被拒绝。`bind` 由此只证明声明已进入 argv，不证明 engine
实际生效值（vLLM `cache_config_info` 尚无 collector/attester 证据）。当前 attester 与 Windows 安装器（API-only
升级守卫与部署后读回）仍只接受无 flag 的 inference argv，所以非 500000 预算的发布在 install/attest 处失败关闭，
直至后续切片为它们接入同一投影与生效值证据。

## 3. 包布局与身份

```text
release-manifest.json            m6.release.v1（canonical；身份 = 其 SHA-256）
inputs/{capacity-config.json, deployment-profile.json, local-worker-profile.json}
compose/mineru-windows.compose.yaml
api-context/{Dockerfile, patch_mineru_344.py, agent_task_protocol_v2.py, agent_capacity_*.py, capacity-config.json}
windows/{install_mineru_fixed_api.ps1, collect_mineru_runtime.ps1, run_mineru_installation.ps1,
         build_mineru_telemetry_assembly.ps1, load_mineru_telemetry_assembly.ps1, 三份 supervisor 源,
         run_mineru_same_root_maintenance.ps1, mineru_same_root_identity.cs, mineru_same_root_registry_helper.py}
native-m6/{12 份生产 .cs, build_mineru_m6_owner.ps1, run_mineru_m6_owner_host.ps1}
```

`build` 从一个仓库工作树读取字节，逐文件用 `git hash-object` 证明与冻结 commit 的 blob 相同；
范围内漂移或未跟踪即失败（65），范围外脏文件只列出。清单记录 source HEAD、
`git ls-tree -r --full-tree <head> services/disclosure_anchor` 的 SHA、三个输入身份、
投影（仅展示）、API 构建身份（Dockerfile 公式的 `capacity_sources_sha256`）、native 12 源清单
（native suite 同一公式）与安装文件哈希。`verify` 不需要 Git，也不读取任何旧包：重算全部哈希，
从 `inputs/` 重新投影 Compose 并逐字节比对，解析 Compose（拒绝重复键）核对 env/argv，
`--check-active-dependencies` 扫描临时路径/旧脚本引用。

## 4. 流程与所有权

1. `build` → `verify`（离线）。
2. `install`（须显式授权；Mac 侧）：持有 `mac_exclusive_lock_path` 的 flock，确认 launchd worker/GC
   未加载、API 空闲，把包 sftp 到全新的 Windows 目录，在一个自有的有界 SSH 会话内运行
   `run_mineru_installation.ps1`，取回操作记录并只据记录与真实回读判定。本地 SSH 退出不证明远端结果；
   取不到 `operation-result.json` 即 `unknown`，写权限保持关闭。
3. Windows `run_mineru_installation.ps1`：核对清单与每个文件哈希；持有 `compose.tailnet.yaml.installation.lock`
   独占句柄直到退出；从包内三份源编译并加载 `MineruTelemetryJobSupervisor`；以有限 Job 运行现有
   `install_mineru_fixed_api.ps1`（`-ApiOnlyCompatibilityUpgrade -ApiDeviceProfile`，永不传旧
   `ExpectedApiTaskSlots`）；Job 超时/强制终止 → `unknown`（Docker daemon 侧请求不会被 CLI kill 取消），
   不自动回滚；通过 = installer 结果记录、持久 receipt、compose 目标哈希、运行中 API 镜像与空闲 health 全部一致。
4. `qualify`（须显式授权）：`mineru_smoke.py`（fixture）→ `freeze_mineru_campaign_epoch.py` →
   held-out 完整 PDF smoke（≥2 份，manifest 钉住 SHA 与页数）→ epoch after →
   `build_mineru_validation_receipt.py`。每步是自有的有界子进程；不拿 fresh/publication 信用。
5. `bind`：从 runtime bundle + 容量 + 部署/本地 profile 构造 process profile v2（bundle 的 inference 命令须与
   发布投影逐项相等，见 §2）；从实时 health/pressure
   取 owner/cgroup 构造 activation；输出私有 overlay 片段（可选合并到 `--base-env`，基值不回显）。

`install_mineru_fixed_api.ps1` 只改资源管理：`-OperationBudgetSeconds` 一个单调预算、逐命令剩余期限、
双管道持续排空与字节上限（`docker build` 用 head/tail 保留并计 dropped）、`-OperationRecordDirectory`
下 write-new 的开始/阶段/首错/结果记录。部署判定、守卫、rollback witness 与 receipt 不变。

## 4a. 同根注册表重确认（零职责维护）

Docker Desktop 存储维护（例如官方 VHD 迁移）后，同一 Windows NTFS 输出根在 Linux 侧可能得到新的
`device`/`inode`；普通 reader 对此保持严格拒绝。本操作只在全部旧责任已终态（业务 legacy-scope 零成员、
原生 health 零职责、API 自身的严格 quiescence proof）时，把 v4 registry 的 `output_root.device/inode` 由批准的
旧元组 I0 改写为实测新元组 I1：R0 以 I0、R1 以 I1 严格解码；路径、uid、mode、全部记录（含过期 tombstone、
顺序、可选字段）与 watermark 字节不变。物理连续性由 Windows 证明：同主机、同卷、根/控制/spool 的 FileId128 与
ACL 不变、所有祖先无重解析点；内容相同的副本不是连续性。

- **入口**：`mineru_release same-root prestop|observe|approve|apply|restore|close|status`（`approve` 离线）。
  每个阶段复用 install 的 Mac 锁、worker/GC 未加载证明、全新目录暂存、有界 SSH 与只据取回记录判定；写权限
  始终关闭，从不安装、不放行生产者。binding `m6.installation-binding.v3`（闭合；`phase`、`operation_id`
  `sr-<32 hex>`；`approval_sha256` 仅 apply，`legacy_scope_inventory_sha256` 仅 prestop）。
- **所有权分割**：`run_mineru_installation.ps1` 仍只读并判定（闭合空闲 health、API 已安装模块的
  `inspect_quiescent_output_root`、`docker inspect`）；全部变更在 Job 子进程 `run_mineru_same_root_maintenance.ps1`：
  持久状态、三容器 restart 策略与启停、一次性 helper。v1/v2 binding 与 `install_mineru_fixed_api.ps1` 在
  `pending.json` 存在时拒绝；没有放行开关。
- **持久状态**：install receipt 同目录的 `same-root-maintenance\`（输出树与 Docker VHD 之外）：`pending.json` 与
  `<operation_id>\` 内的 create-new 记录（API proof、冻结容器、R0 字节、身份、plan、helper 意图/结果、apply、
  restore、close）。只做这一次字节维护的状态，不是业务账本；不删除以重试，只有 close 把 `pending.json` 移入操作目录。
- **阶段**：prestop（无 pending 时须实时闭合空闲 + API proof；冻结三容器 ID/镜像/restart 策略，先全部
  `restart=no` 再按 proxy→API→inference 停止，停止前后 R0 与 FileId 不变，同一 VM 内 helper 基线观察须为
  noop/r0 且看到 API 的元组）→ 人工 Docker 存储维护 → observe（plan）→ approve（Mac 用包内
  `api-context/agent_task_protocol_v2.py` 从取回字节重算 R1，审批绑定 plan 摘要）→ apply → restore（启动
  inference、API；API 自身严格 reader 须证明 I1 与 R1）→ close（先写 close 意图，再启动 proxy、恢复冻结
  restart 策略、归档 pending）。再次挂载时以本操作最新 registry（最新 apply）为原件重新 observe，不回写维护前的 R0。
- **中断与续做**：成对记录只在已写的首件与本次结果逐字节一致（R0 副本）或是同一物理对象（apply 后 registry 的
  FileId、字节、ACL 与根链）时补齐，否则保持失败；plan 号与 helper 序号永不复用，缺提交记录的半组（registry
  副本/身份记录，或没有意图记录的 helper 目录）留作证据并取下一个号。已提交的 apply 在应答丢失后重跑只确认同一
  对象（`already_recorded`），不写。close 在第一次变更（启动 proxy、恢复策略）之前写确定性的
  `close-intent.json`（operation、restore 记录名与摘要、三容器 name/ID/冻结策略），续做时须逐字节重建；此后只有
  close 能继续（prestop 续做、observe、apply、restore 均拒绝），非 `no` 的策略只有在该记录列出的容器上、等于其
  记录值时才算本操作已恢复，其余都是外来漂移。prestop 在写 `pending.json` 之前中断只留下无 marker 的目录：
  未冻结、未变更任何对象，新开 operation。
- **版本绑定**：operation 属于开启它的已验证包。`pending.json` 记录 prestop 的 release manifest；之后每个阶段
  （wrapper 早期门、子进程入口）都要求同一 manifest，否则在任何 Docker 调用前拒绝，只有 status 可用别的包查看。
  Mac 核验 plan 时，prestop/observe 阶段结果、Windows `prestop.json` 与 plan 也必须都是核验所用的包。没有迁移
  或换包开关。
- **helper**：pinned API 镜像 + `/usr/bin/python3.12 -I -B`、`--network none --read-only --cap-drop ALL
  --restart no`；输出根用 API 冻结的同一 bind 源/目标（observe 只读，apply 可写），加只读的 helper 目录。
  prestop 基线观察须看到 API 自身的元组，apply 须测得 plan 的当前元组，否则拒绝；确定名
  `mineru-same-root-<op>-<action>-<seq>` 与标签；create 前写意图；create/start 应答丢失只按同名对象读回；
  `docker wait` 超时保持 unknown/pending；结果落盘后才 `docker rm`。写入：确定 temp 名、`O_EXCL|O_NOFOLLOW`、
  0600、fsync、replace 前 CAS（同 inode、R0 字节、路径链仍指向被持有目录），replace 后控制目录 fsync、重开读回；
  失败按 R0/R1/other 三态分类，unknown 永不当成功。
- **数值身份**：PowerShell 只比较与回传解析后的元组；任何精度/类型偏差只能让 helper 拒绝（R0 须在批准元组下
  解码、实时元组须等于批准的 I1），且 Mac 的 plan 解码要求每个字段逐字复述 helper 原始行。
- **未在此证明**：Docker Desktop bind 上 Linux fsync/rename 是否映射为 NTFS 断电持久；Windows Job/daemon、
  FileId/ACL 采集与 PowerShell/C# 实际执行。部署前须在真实后端以 stand-in 证明，不能由 Linux rename 推断。

## 5. 退出码与失败可见性

0 通过；64 参数/输入格式；65 身份或语义不一致；70 执行失败或结果不可确定。所有失败输出结构化 JSON，
只含非机密引用。任何 `unknown` 都不是成功，也不触发自动恢复。

## 6. 验证

- 离线：`make agent-check`；根据本合同的独立测试由 Codex 编写（投影逐字段反例、包构建/验证、C/P 域）。
- Windows：`build_mineru_m6_owner.ps1` 的生产编译与 `run_mineru_installation.ps1` 的受控子进程/竞争/
  daemon-unknown 反例由独立作者驱动；本仓不把测试源装入生产包。
- 实机 `install`/`qualify`/`bind` 只在获准的运行时窗口执行，并保留全部原始 stdout/stderr/退出码。
