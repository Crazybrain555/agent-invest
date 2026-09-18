# M6 campaign 入口：一个 Mac 组合根、按值绑定的 spec 与跨机生命周期

状态：implementation-contract（WP2/WP3，R20；R21 证明闭合候选）。从属于 `m6-owner-control.md` 与 `mineru-release.md`；
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
| campaign intent | `m6.campaign-intent.v2`（canonical 字节即身份） | run 意图、runtime 身份引用、预算、`binding_sha256`、`release_manifest_sha256`、`evaluation_plan_sha256`；v1 只读历史，不作新版冻结证明 |
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

**共享准入 STOP（R23 A）。** `--admission-stop-file <绝对路径>` 可选；路径必须是 `--output` 同父目录的直属文件、
非 symlink。同一路径直接传给 runner `--stop-file`，controller 每轮监督也读它：`O_RDONLY|O_NOFOLLOW|O_NONBLOCK` 打开
（FIFO 不阻塞）、`fstat` 后要求 regular/当前 uid/0600/恰好 5 字节 `stop\n`。有效 STOP 只做一次 owner `stop`
（停止新准入），runner/verifier 继续跑到各自原 closure/原 max_close，绝不 SIGINT/abort；child 失败分支写同一文件
（已存在则忽略）。任何 child spawn 前已有有效 STOP → 复用零准入 `_bootstrap_closure`，摘要 failed
（`external_admission_stop: before business spawn`）。控制文件身份/字节无效（FIFO、目录、错误 mode、错误字节）：
spawn 前 → `CampaignInputError`（无业务）；spawn 后 → fail closed：记录 `external_admission_stop_invalid`，
仍只请求 owner stop、不销毁在途工作（runner 侧对该路径只做存在性判断，因此本来就已停止准入）。
测量驱动（`tests/integration/m6_measured_campaign_independent.py`，`m6.measured-campaign-driver.v3`）在
telemetry 退出/传输失配时发布 `<driver output>/campaign-admission.STOP` 并只等到 campaign spawn 时冻结的唯一
绝对期限（`campaign_spawn + campaign_span_requirement`，不续期）；期限耗尽或控制写入失败才强制收尾，且本地退出
不证明远端 owner 退出。测量失败与业务闭合分别陈述（`measurement_failed` / `business_closed`），驱动 exit 1。

## 3a. WP3：冻结评估计划与派生交付报告（Pro R20 §4）

- **一个冻结计划**：`m6.evaluation-plan.v1`（`application/contracts/m6_evaluation_plan.py`）固定主窗口
  `[T0+start, T0+end)`、等分子窗口、readiness 规则（复用 accounting 的 ready tick：`max(document_qualified,
  publication_committed, public_confirmation)`，最多 3 次观测）、信用排除（replay/carry-in/非首发，不回填 late quality）、
  页数尺寸桶（short ≤50、medium 51–149、long ≥150）与可选的交付/延迟/资源门。计划 canonical 字节即身份。
- **intent v2**：`m6.campaign-intent.v2` 新增必填 `evaluation_plan_sha256`；官方入口 `run|bootstrap-check --evaluation-plan`
  在 Prepare 之前把 intent 原字节复制到 `<output>/campaign-intent.json`、计划复制到 `<output>/evaluation-plan.json`，
  并把两者哈希写入 `campaign-inputs.json`（准入之前冻结，输出目录自包含）。`summary` 只读 `campaign-intent.json` 并按
  `campaign-inputs.intent_sha256` 校验；字节不符即 identity 错误（65），不会改用别的副本；仅当该副本不存在（历史证据）才
  经驱动器 `input-hashes.json` 索引按哈希查找。intent 必须标识整个 run：用 `build_run_spec(anchor, intent.run,
  intent.runtime)` 重建 spec，canonical 哈希必须等于冻结的 `run/run-spec.json`。
  `M6CampaignIntentV1` 保留只读解码（`decode_campaign_intent`），G0 证据仍可读；`M6RunSpec` 不变。
- **只读证据回读**：`run` 模式在 owner 关闭后经 sftp 只读取回 `private\runs\<H(run_id)>` 的 `events.jsonl`
  与 `admission-closed/resources-closed/exit-observation` 到 `<output>/native/`；取不到即 unknown，不写远端。
- **`m6_campaign summary --run-dir --evaluation-plan --output`**：只读；用不变的 `reduce_m6_run` 从原始 journal、
  verifier 不可变证据（`qualification-evidence.json`、`public-confirmation.json`、`private-history-audit.json`）、runner
  闭合收据、owner 侧 sidecar、launcher 外部退出记录与 campaign summary 派生 `m6.delivery-report.v1`：`run_validity`、
  `business_obligations_closed`、`publication_qualified`、`main_window`（3 个子窗口、按 outcome 的排除计数）、`whole_run`、
  `drain`、`service_diagnostic`、`resource_safety`、`latency_by_size`（owner 事件延迟与下述同机 stage 端点延迟分别报告）、`unknowns`、`delivery_pass`。分母是原始 PDF 页数与去重的 source 哈希；缺失的资源/延迟证明是
  unknown，不是 safe/fast；任何未闭合义务或外部退出缺证都使 `delivery_pass=false`。
- **证明规则（独立测试固定）**：`run/run-spec.json` 与 `run/anchor.json` 是必需输入（缺失退出 64），其余证据缺失记入
  `unknowns`。`delivery_pass` 只在 intent 为 v2 且冻结了所评分的计划、`run_validity=complete`、且下列每项控制证明都存在并
  一致时为真：runner 收据 `status=complete`/`closure.complete`、runner 与 `native/admission-closed.json` 的 reconciliation 哈希
  与计数同 journal 一致（未决 0、`last_producer_sequence` 等于 journal 内 runner 事件最大序号）、owner `resources_closed`
  事件、runner 闭合块与 `native/resources-closed.json` 三方一致且残留 0、verifier `run-summary.json` 为 complete、
  `verifier/drain-receipt.json` 哈希等于 journal `verifier_drained` 所记、launcher 外部退出 `exit_code=0` 且无强杀/取消、
  本地子进程已回收且无清理失败。`admission_to_public_max_s` 使用 owner 戳；尺寸桶的 remote 门及
  `remote_to_public_*` 门使用下面的实际 POST/terminal/public 端点，端点或证明缺失才为 unmeasured/unknown。
- **远端尾部与资源门的测量（B.2/B.3）**：不新建平台。runner 进程内 `StageLeaseGuard.note`（`V4StageGuard.note`）经
  现有有界 `JsonlStageObserver` 写 `runner/observation/stage-events.jsonl`：`remote_post_send`（transport 在
  `client.send(POST /tasks)` 之前、submission guard 之后）、`remote_terminal_observed`/`remote_terminal_failed`（backend
  `_poll`）；note 绑定 guard 自己的 attempt/lane（`remote`），scalar 只携带 fence/source/intent/remote task 身份用于与
  journal 对账。verifier 在 `_confirm` 返回后立即记 `public_ns` 与 payload canonical sha 到 `verifier/run-summary.json`。
  runner 与 verifier 都把 `clock`（`python.time.monotonic_ns`、实现名、`kern.bootsessionuuid`）写入各自摘要；两者不等或
  observation 非 `complete`（任何 dropped/late/truncated/writer error）则所有 stage 门 unknown。门映射：`short_*/long_*` →
  该尺寸桶 `remote_post_to_terminal`（首个真实 POST → Mac 观测到 remote terminal）、`remote_to_public_*` → 全桶
  `terminal_to_public_confirmation`、`admission_to_public_max_s` 仍用 owner tick。任何必需 attempt 缺样本/失败/矛盾/时间
  倒置都不允许用更小子集通过。资源门：`summary --telemetry-artifact-root --telemetry-run-id` 经现有
  `verify_synchronized_telemetry_observer`（v3 receipt/seal）回放帧，帧身份须匹配本 campaign 的 runtime/profile/GPU
  device/host+boot（不同 boot 编码须经下述原始身份记录桥接，禁止填入相同假 hash），覆盖用 `campaign-summary` 的 UTC 括号
  （只判覆盖，不跨机相减），聚合为分量增量（`oom_total`/`oom_kill_total`/`oom_group_kill_total`/`vllm_preemptions_total`
  任一正值即 fail；重置/递减/unsupported/样本不足 → unknown）与受支持样本的 GPU free 最小值。
- **unknowns 归属**：读取器发现的每一项证据缺口都进入主报告 `unknowns`（不只放在 `delivery-report-inputs.json`），
  例外只有描述"如何找到证据"的可选发现索引（`driver_input_hashes_absent`）以及计划未声明资源门时的
  `resource_telemetry_receipt_absent`；与 `business_obligations_closed.missing` 同义的 `<x>_absent` 不重复列出。阻断
  `delivery_pass` 的 unknown 按词干匹配其 `_absent`/`_unreadable:*`/`_exceeds_byte_bound`/`_is_not_an_object` 形态：
  `run_receipt`、`owner_journal`、`campaign_intent`/`campaign-intent.json`、`campaign_inputs`/`campaign-inputs.json`、
  `evaluation_plan_not_frozen_in_intent`、`evaluation_plan_not_in_run_output`/`evaluation-plan.json`，以及
  `resource_safety_unproven:*`、`latency_gate_unmeasured:*`。`native_*_absent`（owner 侧重复副本）、
  `verifier_public_inputs_absent`、`verifier_quality_evidence_absent` 等只报告不阻断：它们的效果已体现在计数或义务里。

## 3b. R21：同一证明规则，不以摘要布尔授权通过

1. **共享外部退出校验**：在线 `_external_exit_verified` 和离线 `_external_facts` 都调用
   `m6_campaign_assembly.external_exit_problems`。既有 `launcher-command.json` 在 spawn 前记录
   `intent_sha256` 和非秘密 `expected_start` 投影；它不是第二配置源。原 `process-start.json`、根目录与
   fetched `ready.json`、`process-exit.json` 必须绑定相同 run/attempt/host/binary/launcher/configuration、
   预算及 PID/birth，并重现 READY 的 owner epoch。要求真实 handle opened/signaled、READY、两管 EOF、
   整数 exit0，明确无 timeout/forced/cancel/parent failure；缺字段也不等于 false/null。
   原 transport 的 M6-EXIT 如存在须与 fetched record 一致。summary 的 true 不提供正证；已记录失败仍保留。
2. **两个 boot 编码的显式适配**：原生 `owner_identity` diagnostic 已含 boot counter、原七位小数 UTC、
   node/GPU、clock、PID/birth。在线经原 pinned SSH 有界只读取回一个 metadata/body 对，保留原随机文件名
   和原字节；不得新查一套 boot 代替旧事实。纯 `bind_physical_owner_boot` 校验 metadata body SHA、canonical
   native v2 原文、anchor/QPC/GPU/epoch 后才派生 resident UTC hash；它与 native counter hash 保持不同。
   有界取回失败使 campaign 失败，但不把已经验证的进程退出改说成未退出。此传输需要 Windows 零 PDF 实测。
3. **raw 守恒**：stage reader 同一次 limit+1 读取统计全部 raw bytes/JSONL records（包括不计分的 note kinds），
   与 writer 的 bytes_written/events_written 精确相等。要求真实 writer 已退出、关闭时间区间、末条唯一
   observation_closed、全部损失计数及 summary_write_error 为零。缺省补 0、忽略合法截断、只数 remote notes
   均不允许。writer 的 note 入队与 closed 置位在同一短锁内，close 后不能再成功接受一个遗失的 note。
4. **先绑定再相减**：一个 attempt 完整检查 source/fence/submission/task/acceptance/terminal receipt 与
   journal identity，public 还须 confirmed=true、精确 payload canonical hash 和 verifier run/spec。
   重复/矛盾不能 last-write-wins。runner/verifier monotonic source、implementation、boot 不同，跨进程
   public duration 必须 null；独立成立的 runner 内 remote duration 可作诊断保留，但整项门不能借较小子集通过。
5. **计数器保守包络**：可信 host_slow frames 取窗口 start 前/恰好 start 的最近点与 end 后/恰好 end 的最近点，
   两端外延不超过一个既有 nominal period；要求完整 scheduled cadence、无 missed/late、unsupported 或 reset。
   四个累计异常分量分别相减；无边界就是 unknown，不是零。外延中增量按保守上界拒绝零异常证明，不能宣称
   精确发生于窗口内。GPU gauge 仍是窗口内有效样本最低值；采样不证明每一个未采到的瞬间。
6. **原始文件与限额**：reader 以文件描述符读取普通非 symlink 文件，先 limit+1 后解码，读前后大小/mtime
   改变拒绝；不以 read_all 后检查充当内存上限。输入清单在 run 内用相对路径，run 外用绝对路径；每项保留
   exact-byte SHA。缺失/损坏原件不能由手工编辑 summary 修成成功。

以上验证确认受信任生产者保留的原始记录之间一致，不是抗整包伪造的硬件远程证明。
不新造 Mac 子进程账本；Mac 精确生命周期仍由既有 bounded owner、runner closure 与原监督回执承担。
私有 operational delivery schema 从当前模型生成；公有 v1 schema、解析语义与数据库结构不变。

## 3c. 有限 launcher 传输期限与生产循环的分离（R21 预算裁决）

- **两个起点，一个固定期限**：Mac 侧 launcher 传输（`BoundedOwnerCommand` 运行 `run_mineru_m6_owner_host.ps1 -Run`）
  在原生 owner 存在之前 spawn，期限从本地 spawn 固定；业务窗 `[T0, T0+planned+grace]` 在 owner 时钟上从 T0 起算，
  T0 是 owner 自己的起点，READY 在 T0 之后才被 Mac 观测到（T0 ≤ READY）。因此传输期限不能对 `planned+grace` 封顶，
  而是纯规则 `application/services/m6_launch_budget.py`：
  `timeout = (ready_wait + ssh_overhead 90) + (planned + grace) + (ExitWaitExtraSeconds 180 + drain/record 15 + ssh 90)`。
  前置项正是 `_await_ready` 强制的 spawn→READY 上界，它同时覆盖 spawn→T0；尾项是 launcher 自身 `ExitWaitExtraSeconds`（显式
  传入，与其默认相同）、5 s 管道 drain 与退出记录、ssh 拆除。G3（4800+2400、ready 30）为 7605 s，超出默认 7200 时**仅此
  一次调用**显式使用 8400 s ceiling；G0（600+120）为 1125 s 仍在默认 ceiling 内。`launcher-command.json` 记录
  `timeout_seconds`、`lifetime_ceiling_seconds` 与全部 `launch_budget` 项。
- **纳入 READY，不重复计数**：open 之后用 owner 时钟的 `remaining_to_max_close` 计算
  `launcher_transport_headroom = 传输剩余 − remaining_close − post_close`；为负则在任何准入前 STOP
  （`CampaignOutcomeUnknown`），记入 `budgets`。收尾时 `_finish_launcher` 只等到 launcher 自身绝对期限（+5 s），
  不再额外叠加 `close_grace + 240`。业务 planned/grace/max_close、30 s lease、stop/admission、publication/ACK 语义不变；
  children 仍按 owner 时钟 `max_close` 逐次重算寿命。
- **采样窗口覆盖（R22 定案，单一策略）**：`SamplingCoverageTerms(entry_to_spawn=prepare 180+stage 180,
  owner_transport=timeout, finish_poll=5, post_transport=fetch 180+identity 60+native 180, host_period, edge_periods=2)`，
  `required_seconds` 只定义整个 campaign（入口→summary）加两端各 2 个 host 周期；G3 产品项 8394 s，独立 driver 再加自己的
  sampling-start/campaign-launch/cleanup 各 30 s（driver 自身在相应阶段强制）= 8484 s ≤ 采样 8500 s，余量 16 s。
  有限向量（`resident_measurement_policy` 为唯一来源）：采样 8500 → native lane 8580（+20 s pre-GO +60 s 收尾）→ wire 8590 →
  primitive Job/有限命令 8600；`EXTENDED_COMMAND_CEILING_SECONDS` 由此导入，默认 7200 与业务 4800/2400/7200 不变。最早 native
  starter 的本地 spawn 到 observer 计划起点 ≤20 s（同一 Mac monotonic），超出在任何 PDF 准入前关闭。
- **本地测量窗（C4）**：`campaign-summary.json` 记录 `telemetry_window`（`m6.local-measurement-window.v1`：Mac clock domain、入口
  与收尾的 monotonic ns，前后身份必须一致），v4 reader 只在该窗内用 frame 的因果采集区间做资源包络，不再用 UTC 括号。
- **生产路径不设总时限**：`scripts/run_worker_once.sh loop` 是 `while true` 外循环；`cli/worker.py::run_resident_worker`
  以 `should_stop` 循环、wedge watchdog 只看各执行面 liveness（"bounds nothing — liveness does"），
  `SYNC_COOLDOWN_MAX_SECONDS=7200` 是同步冷却上限；`BoundedOwnerCommand` 仅用于安装/资格/resident telemetry owner 与本
  组合根，永久 worker 不经其运行。M6 的两小时是有限测量会话的边界，不是生产运行时限；多日 soak 尚未进行。

## 4. 退出码与失败可见性

`run|bootstrap-check`：0 complete；1 failed；64 输入；65 身份不一致；70 outcome unknown。
`summary` 的 exit0 仅表示成功生成报告，**必须另查 delivery_pass**；它可以合法输出 unknown/false。摘要 `status` 只在 owner 关闭回复
`ok`、本地子进程全部回收、外部退出记录取回且 `exit_code=0` 且非强制终止时为 `complete`。任何部分或不确定
结果都是 `failed`/`unknown`，绝不报告为成功。B×P≤L 的 Mac process profile 限制保持为显式既有兼容约束。

## 5. 验证

- 离线：`make agent-check`；根据本合同的独立测试（工厂反例、intent/绑定契约、READY 解析、预算推算、
  零准入闭合、launcher Prepare/Run/Cancel 原生反例）由 Codex 编写。
- 实机：全新 workspace 的 bootstrap-check 与随后的正式 campaign 只在获准的运行时窗口执行。
