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
- **一个入口**：`python -m disclosure_anchor.cli.mineru_release build|verify|qualify|bind|install`。

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

## 3. 包布局与身份

```text
release-manifest.json            m6.release.v1（canonical；身份 = 其 SHA-256）
inputs/{capacity-config.json, deployment-profile.json, local-worker-profile.json}
compose/mineru-windows.compose.yaml
api-context/{Dockerfile, patch_mineru_344.py, agent_task_protocol_v2.py, agent_capacity_*.py, capacity-config.json}
windows/{install_mineru_fixed_api.ps1, collect_mineru_runtime.ps1, run_mineru_installation.ps1,
         build_mineru_telemetry_assembly.ps1, load_mineru_telemetry_assembly.ps1, 三份 supervisor 源}
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
5. `bind`：从 runtime bundle + 容量 + 部署/本地 profile 构造 process profile v2；从实时 health/pressure
   取 owner/cgroup 构造 activation；输出私有 overlay 片段（可选合并到 `--base-env`，基值不回显）。

`install_mineru_fixed_api.ps1` 只改资源管理：`-OperationBudgetSeconds` 一个单调预算、逐命令剩余期限、
双管道持续排空与字节上限（`docker build` 用 head/tail 保留并计 dropped）、`-OperationRecordDirectory`
下 write-new 的开始/阶段/首错/结果记录。部署判定、守卫、rollback witness 与 receipt 不变。

## 5. 退出码与失败可见性

0 通过；64 参数/输入格式；65 身份或语义不一致；70 执行失败或结果不可确定。所有失败输出结构化 JSON，
只含非机密引用。任何 `unknown` 都不是成功，也不触发自动恢复。

## 6. 验证

- 离线：`make agent-check`；根据本合同的独立测试由 Codex 编写（投影逐字段反例、包构建/验证、C/P 域）。
- Windows：`build_mineru_m6_owner.ps1` 的生产编译与 `run_mineru_installation.ps1` 的受控子进程/竞争/
  daemon-unknown 反例由独立作者驱动；本仓不把测试源装入生产包。
- 实机 `install`/`qualify`/`bind` 只在获准的运行时窗口执行，并保留全部原始 stdout/stderr/退出码。
