# M6 campaign 入口：一个 Mac 组合根、按值绑定的 spec 与跨机生命周期

状态：implementation-contract（WP2，R20）。从属于 `m6-owner-control.md` 与 `mineru-release.md`；
不改变解析/模型/表格/发布/ACK 语义，也不引入新的账本、调度器或框架。

## 1. 决定

- **一个纯工厂**：`application/services/m6_run_spec_factory.build_run_spec(anchor, intent, runtime)`。
  anchor 证明 T0/时钟/原始区间/资源/owner 与 GPU 身份；`M6RunIntent` 声明成员与阶段；
  `M6CampaignRuntimeBinding` 提供发布身份。不一致即拒绝，不做调和；无 IO。
  `cli/m6_run_control.py bind` 与 campaign 入口都只经此工厂。
- **按值绑定**：`m6.owner-request.v2` 的 `bind` 携带 canonical spec（`spec_utf8` ≤ 49152 UTF-8 字节），
  整条转义 wire 另受 65536 上限。原生 host 在任何私有写入前完成授权、闭合形状、哈希与 anchor 校验，
  再不可变落盘 `spec.json`；相同重绑幂等（不产生新 T0/记录），不同一律拒绝。未绑定的 owner 在
  `bootstrap_bind_seconds`（部署 `m6.owner-deployment.v2` 的闭合字段）后以 126 退出。
- **私有祖先先于秘密**：launcher `-Prepare` 先建立受保护的 private 目录并返回 ACL 收据，控制端校验后才
  上传私有部署到 `private\staging`；`-Run` 在私有 staging 内校验并原子提交，再创建全新 attempt 目录。
- **严格 READY 与固定期限**：七字段 READY、默认 30 s；host 生命周期硬期限从 spawn 起算且不续期。
- **同实例取消**：`-Cancel` 只从该 attempt 自己的 `process-start.json` 派生命令（记录哈希/run/attempt/
  pid/创建时间/二进制与配置哈希/原因）；运行中的 launcher 逐字段核对后经持有的句柄终止。无 PID 盲杀。
- **一个组合根**：`cli/m6_campaign.py run|bootstrap-check` 复用既有 runner（`staged_campaign`）、
  verifier supervisor 与 owner 控制客户端；子进程以 `run_worker_once` 同样的 shell source 语义启动
  （`set -a; . worker.env; . cninfo.env; set +a; exec …`），并发监督，首错写 STOP 后排空，只经 runner 的
  产品收据关闭 owner，取回 `process-exit.json` 独立核对外部退出。

## 2. 输入与权威

| 输入 | 契约 | 说明 |
| --- | --- | --- |
| campaign intent | `m6.campaign-intent.v1`（canonical 字节即身份） | run 意图、runtime 身份引用、预算、`binding_sha256`、`release_manifest_sha256` |
| 私有绑定 | `m6.campaign-private-binding.v1`（0600） | env 目录、service root、python、runtime root、pinned ssh/sftp、Windows 目标与 owner 二进制/launcher 哈希 |
| 发布绑定 | WP1 `binding.json`（intent 钉住哈希） | source head、bundle/profile/qualification、`worker_env_sha256` 必须与实际 env 一致 |
| 发布清单 | `m6.release.v1` | launcher 与 native 源清单哈希必须等于私有绑定所指 |
| manifest/scope/plan | 既有契约 | 哈希与 intent 一致；scope 的 campaign 与 intent 一致 |

## 3. 流程

`load_campaign_inputs` → 生成四角色 epoch/token（0600，写既有 `M6RunDirectory` 布局）→ Prepare → sftp 上传
私有部署 → Run（head/tail 保留的有界 SSH 会话）→ 解析 `M6-READY` 并 canonical 校验 anchor → 工厂冻结 spec →
controller bind（v2）→ 幂等重绑核对 → open → 由 open 回复的 owner 时钟与本地单调采样保守推算 runner
`--max-seconds` 与 verifier `--deadline-seconds` → 并发监督 runner/verifier/launcher 与 owner 状态 →
runner 收据 `m6_assembly.closure.complete` 才 close → 等待 launcher 退出 → sftp 取回记录并核对 →
`campaign-summary.json`（`m6.campaign-run-summary.v1`）。

`bootstrap-check` 在 open 后不准入、不连库：以 runner 角色存入零计数的真实 resource_audit /
admission_reconciliation / ownership_closure 收据并 ACK，quality/public verifier 以零事件完成并写
`drain-receipt.json` 发出唯一 `verifier_drained`，controller close；摘要钉住 `admitted_count=0`、
`database_access=none`、`hidden_setup=false`。

## 4. 退出码与失败可见性

CLI：0 complete；1 failed；64 输入；65 身份不一致；70 outcome unknown。摘要 `status` 只在 owner 关闭回复
`ok`、本地子进程全部回收、外部退出记录取回且 `exit_code=0` 且非强制终止时为 `complete`。任何部分或不确定
结果都是 `failed`/`unknown`，绝不报告为成功。B×P≤L 的 Mac process profile 限制保持为显式既有兼容约束。

## 5. 验证

- 离线：`make agent-check`；根据本合同的独立测试（工厂反例、intent/绑定契约、READY 解析、预算推算、
  零准入闭合、launcher Prepare/Run/Cancel 原生反例）由 Codex 编写。
- 实机：全新 workspace 的 bootstrap-check 与随后的正式 campaign 只在获准的运行时窗口执行。
