---
id: disclosure_anchor_milestone_09_production_readiness
project: disclosure_anchor
title: 生产就绪（production readiness）
status: in-progress
created_at: 2026-07-07
depends_on: milestones 00-08 + phase008 数据质量整改（rounds 1-7）
---

# Milestone 09: 生产就绪

目标（用户指令 2026-07-06）："尽全力把 disclosure_anchor 做到能够实现生产的状态"。
生产形态 = 运营者只维护公司清单（config/watchlist.csv），服务按默认参数持续盯盘：
初始回补三年（用户裁决"三年是底线"），全量登记 + processing_policy 分层处理，
launchd KeepAlive 常驻 worker；有积压零等待排水，空队列 15→30 分钟退避。

背账来源：2026-07-07 六维度并行审计 + 完备性 critic（≈39 万 token 通读代码，
findings 全部带 file:line 证据；critic 纠正的 2 条假阳性已剔除）。
执行纪律：每项完成后在本表打勾并记录方案；blocker 全清 + major 清到无数据正确性风险
才可宣布生产就绪。

## 已完成（2026-07-07 第一批）

- [x] **[blocker]** No batch company-list intake → pipeline track + config/watchlist.csv（离线幂等，PENDING_LEGAL_NAME 占位+首同步升级）
- [x] **[blocker]** No default initial backfill window → DISCLOSURE_INITIAL_LOOKBACK_DAYS=1095（用户裁决三年底线）+ tracked.lookback 覆盖，CLI/worker 双路径
- [x] **[blocker]** Oversized documents permanently occupy parse-queue slots and can wedge the whole → pending_parse SQL 级排除 oversized（原为 LIMIT 后跳过占满槽位）
- [x] **[blocker]** No mechanism delivers required environment to automated runs; the shipped launch → ~/.config/agent-invest/disclosure_anchor/{worker,cninfo}.env + scripts/run_worker_once.sh + launchd 模板/安装脚本
- [x] **[blocker]** Real CNINFO credentials stored inside the repo checkout .env despite placeholder → 凭据迁至 ~/.config（chmod 600），.env 只留指引；密钥需用户轮换
- [x] **[blocker]** Unauthenticated /v1/admin write endpoints are always mounted on the same app as  → DISCLOSURE_ENABLE_ADMIN_API 默认关闭 + ParserOptions 闭集词表（防 argv 注入）
- [x] **[blocker]** No automated determinism regression → tests/contract/test_fixture_determinism.py：4 fixture 逐字节对比当前规则重建
- [x] **[major]** Default filing-category scope and per-company filing_categories are dead config → sync_due 返回 filing_categories → worker/CLI 传入 SyncCommand；列由 track 命令填充
- [x] **[major]** tracked_company.sync_frequency never honored, and the global due-predicate's  → sync_due 谓词按 hourly/daily/weekly 词表逐行生效 + updated_at 时间戳比较（修双倍周期 bug）
- [x] **[major]** PENDING_LEGAL_NAME placeholder companies poison the first credentialed sync — bl → resolver 占位名就地升级，真名冲突仍 contested（回归测试）
- [x] **[minor]** Live CNINFO credentials sit in the working-tree .env under a header claiming it  → 同上（凭据迁出+轮换提示）
- [x] **[acceptance follow-up]** 2026-07-08 clean-DB P0/P1 → `781e7ec` 为 launchd 日志增加
  TCC 失败时的用户日志目录回退，并让 `purge-company` 清除按 scode/security_code 关联的 unlinked
  CNINFO profile `source_access`；旧本地 acceptance report 不再是当前 No-Go 权威。

## 设计评审文档（2026-07-07 round8 后，先设计后实施）

- `docs/implementation/design/watchlist-operations.md` — 股票池运维（CSV 真源判定、
  对账式 apply、回补批次、429 熔断、cursor 审计字段）
- `docs/implementation/design/retrieval-and-semantic-keys.md` — 非 embedding 检索
  数据面（06R 投影草案、semantic_key 附注词表 ~80-90 键方案）

## 待办背账（按严重度）

### 2026-07-13 数据质量与可观测性后续（从本地任务状态提升）

- [x] **register_local_pdf 身份防污染（2026-07-13）**：`provider=cninfo` 只接受 1–128 位
  ASCII 数字 TEXTID；非法 ID 在归档/写库前失败，API=422、CLI=exit 2；文件名自动推导只认
  `__<数字TEXTID>.pdf` 尾段，删除 `local-<hash>`/目录名兜底。当前 official provider 闭集只有
  cninfo；若未来引入 manual provider，必须先设计独立 namespace，不能复用 cninfo identity。
- [ ] **S 规则单元噪声升版**：治理纯勾选空壳、参会花名册、尾表夹带 Q&A 和题号跨页错位，
  不得按长度一刀切。验收：使用真实 annual/IR 样本，保留财务数字与有效否定；先把尾表夹带正文
  抽回再删模板；builder rules 版本化并验证 rebuild/change 语义。
- [ ] **N009002 快照标签裁决**：明确“对重组的核查意见/问询函”在注册期 snapshot label 的
  carrier/content 语义；证明不改变 0021 视图 topics 与 processing gate，或同步规则、契约和测试。
- [ ] **processing-policy 回滚可观测性**：doctor/worker report 统计“已下载但按当前 policy
  不再 eligible 且仍为 registered”的文档，显示 policy/rule 版本与数量；不得自动清理。

### MAJOR

- [ ] (M/COMPLETENESS CRITIC) **No rules/parser-version rollout procedure for a live corpus: rebuild is per-document only, MinerU is**
  - 修法：Add a batch rebuild command (`pipeline rebuild-units --all [--rules-older-than <ver>]` looping RebuildUnits→BuildUnits→PublishRun with a rate cap), an ops view + doctor check of builder_rules_version/parser_version distribution over active runs, and a rollout 
- [ ] (M/COMPLETENESS CRITIC) **The /v1/changes feed can permanently skip events for L2: cursor pagination on an insert-time sequenc**
  - 修法：Either enforce a single-publisher rule (take a global outbox advisory lock in every outbox-writing commit path) and document it as a hard invariant, or fence reads by visibility: serve only outbox rows with xmin older than pg_snapshot_xmin(pg_current_snapshot(
- [ ] (M/COMPLETENESS CRITIC) **Legitimate company renames permanently wedge that company's sync, durably contaminate identifier sta**
  - 修法：In SubjectResolver, distinguish 'same normalized USCC, different legal name' (a rename: update company.legal_name, keep the old name in an identifier/alias history row, emit an event) from 'same security, different USCC' (true conflict: keep contested + raise)
- [ ] (S/COMPLETENESS CRITIC) **No L2 consumer onboarding contract: nothing tells the downstream team how to bootstrap, tail the cha**
  - 修法：Write docs/architecture/l2-consumer-guide.md: bootstrap recipe, change-feed tailing contract (cursor rules, delivery semantics), an event-kind → required-action table, error-code handling (GONE_SUPERSEDED, L1_PROCESSING_REQUIRED), and the DB-view-vs-API decisi
- [ ] (M/COMPLETENESS CRITIC) **No CNINFO quota budgeting or request metering: per-request audits vanish into an unconfigured DEBUG **
  - 修法：Persist per-interface request counts per worker round (thread the client audit counts into WorkerReport and the daily report), map resultcode 407 to a distinct quota_exhausted error that short-circuits the remaining sync/download items in the round, and add ba
  - 2026-07-13：407/408/412/429 已统一映射 quota_exhausted，worker 与 track profile resolution
    均首错断批，sync 30→60→120m cooldown；尚欠持久的按interface计量/预算和报警，故本项不关闭。
- [ ] (S/DOCS/CONTRACT DRIFT) **Canonical service-purpose §5.2 still says document_unit has only three kinds (text/table/qa), contra**
  - 修法：Update §5.2 to list four payload kinds (or say "three content kinds plus mixed composition, see §6.5") and change the v0.7 wording to v0.8 so the canonical contract is internally consistent.
- [ ] (S/DOCS/CONTRACT DRIFT) **Canonical query-key list omits semantic_keys / applicability / page_no, which 0010/0013 made first-c**
  - 修法：Add semantic_keys, applicability, page_no to the frontmatter query_keys and §12 entrance list (with a note that mixed-unit recall requires semantic_keys), and state explicitly which keys are Filing-API filters vs DB-view-only predicates.
- [ ] (S/DOCS/CONTRACT DRIFT) **service-purpose §12.1 pins the document_units_v1 full column set at "32 列 (04R-R7)" — actual view ha**
  - 修法：Update §12.1 to the 36-column reality (or to "04R-R7 32 列 + 0010/0011/0013 增量，全集以 contract-checklist 为准") and mention the 0010-0013 additions the way 0007/0008 are documented today.
- [ ] (S/DOCS/CONTRACT DRIFT) **0012's document_category public contract (document_categories_v1 view + document_category.v1.json) i**
  - 修法：Add document_category to service-purpose (frontmatter core/optional objects, §5 or §12.1: provider-native category dimension, facet semantics, relationship to the 9-value filing_type vocabulary) matching contract-checklist lines 81-84.
- [ ] (S/DOCS/CONTRACT DRIFT) **Migration freeze policy is inconsistent across docs (frozen frontier stated as 0001-0006, 0001-0007,**
  - 修法：Replace enumerated frontiers with a single policy statement in adapters/db/postgres/AGENTS.md (e.g. "所有已应用迁移（当前 0001-0013）一律冻结，新改动开新迁移") and align the 04R/src-map/Prompt mentions to reference that policy instead of hardcoded ranges.
- [ ] (S/DOCS/CONTRACT DRIFT) **adapters/db/postgres AGENTS.md/CLAUDE.md map is stale: migrations listed as 0001…0009 and view set c**
  - 修法：Update the map: migrations 0001-0013 with one-line notes for 0010-0013, and the 36-column view claim (or point at contract-checklist as the single source for the column set).
- [ ] (M/DOCS/CONTRACT DRIFT) **src/disclosure_anchor AGENTS.md code map still describes the pre-milestone-05 world: "04R 已完成；05–08 **
  - 修法：Rewrite the layer map to current reality: adapters gains unit_builder/sources/cninfo (and drop or mention the empty publisher package), cli gains export_contracts.py, api described as implemented Filing API, implementation status "00-08 完成，06R 待做", freeze rule
- [ ] (S/DOCS/CONTRACT DRIFT) **Milestone 06 (declared the "唯一权威" for read semantics) still defines DERIVED = {asset_uri, is_active_**
  - 修法：Add a dated correction to milestone 06 (like 05/07/08's §6.5 实施后修订): since 0011 is_active_run is a view column, DERIVED shrinks to {asset_uri}, and the no-DB assertion is subset-inclusion; or move the authority pointer in contract-checklist to the checklist it
- [ ] (S/DOCS/CONTRACT DRIFT) **Live builder rule bundle ub-2026.07-9 is undocumented — milestone 05's versioned rule log ends at ub**
  - 修法：Append a "### ub-2026.07-9" section to milestone 05 §8.5 describing the round7 rules (approval-style proposal anchor, QA-table-as-table needs_review, caption/flat-doc anchoring) so builder_rules_version provenance resolves to documented semantics.
- [ ] (M/FAILURE PATHS AND RESILIENCE) **Publish stage has no attempt counter or persisted failure state — poison runs retry forever (confirm**
  - 修法：Add publish_attempt_count/publish_error to processing_run, persist non-retryable publish failures, filter them in pending_publish, and surface exhausted runs in an ops view plus a CLI resolution path (allow-empty publish already exists for operators).
  - 2026-07-13 临时保护：空 run 已从自动队列排除；非空 publish 失败每轮最多一次并触发
    parse+publish 120s 指数 cooldown。持久 attempt cap/人工 requeue 仍未完成，故本项不关闭。
- [x] (S/FAILURE PATHS AND RESILIENCE) **Build attempt cap 曾被 poison-IR 绕过（2026-07-13）**
  - 已将 post-context IR/rule 构建纳入 failure-marking 边界；已知 BuildUnitsError/OSError 持久化，
    未知错误先持久化 structured failure 再抛，现有 attempt cap 对整条 execute 路径生效。
- [ ] (S/FAILURE PATHS AND RESILIENCE) **Web fallback transport exceptions still需统一进 retry/failure accounting**
  - 2026-07-13：生产 WebAPI client 已将 JSON/PDF `httpx.TransportError` 包为
    `transport_error,retryable=true` 并按 cap 重试；worker 继续 stage-local cooldown。web fallback
    channel 的统一包装仍是剩余项，故不把原“两通道” finding 整体关闭。
- [x] (M/FAILURE PATHS AND RESILIENCE) **Silent partial index sync advanced checkpoint — fixed 2026-07-13**
  - `records` 非 array/含非 object、`count/total` 缺失或不等于 records 长度均 retryable
    fail-closed；任一 ≤30d chunk 失败则整家公司不推进 checkpoint，下一轮重做窗口。
- [ ] (M/FAILURE PATHS AND RESILIENCE) **Worker download queue never re-downloads revised announcements — the supersede/B7 logic is unreachab**
  - 修法：Port the B7 predicate (correction signal, signature mismatch, overlap window) into pending_download_v1 or queries.pending_downloads so the worker and CLI share one queue definition.
- [x] (M/FAILURE PATHS AND RESILIENCE) **One systemic error killed the worker loop（2026-07-13）**
  - 常驻 loop 现在逐轮 catch + synthetic failure report + 60s 指数退避；每轮复核 dedicated
    singleton lock，锁/连接丢失 fail-closed；launchd KeepAlive 负责进程级重启。source 构造、
    CNINFO retryable outage、parser、publish、report I/O 各自 cooldown，本地阶段不被上游故障拖死。
- [ ] (M/FAILURE PATHS AND RESILIENCE) **Transient 403 / non-PDF download responses are recorded retryable=false and permanently poison annou**
  - 修法：Make download 403s and non-PDF bodies retryable up to the cap; add an ops dead-letter view for exhausted/quarantined candidates and a requeue command.
- [ ] (S/FAILURE PATHS AND RESILIENCE) **Unknown parse exceptions persist retryable=False — transient infra faults (disk full) permanently dr**
  - 修法：Classify OSError and DB disconnects as retryable in the fallback handler, or default unknown errors to retryable=True so they consume the existing max_parse_retries cap instead of being terminal on first occurrence.
- [ ] (M/FAILURE PATHS AND RESILIENCE) **CNINFO quota/auth outage: no circuit breaker, no automatic web-channel failover, signal buried in ma**
  - 2026-07-13：quota breaker 已完成；category lookup 不再吞 quota，retryable 5xx/transport 也首错停止
    剩余source batch并stage cooldown。剩余范围收窄为auth专用判定、自动web-channel fallback和100%
    source failure报警，故本项不关闭。
- [ ] (M/FAILURE PATHS AND RESILIENCE) **No dead-letter observability anywhere: exhausted parse/download/publish items are invisible and the **
  - 修法：Add per-stage exhausted/dead-letter ops views, a doctor check that FAILs on nonzero dead-letter counts, and nonzero exit or an alarm marker when rounds are fully failing.
- [x] (M/OPERATIONS AND DEPLOYMENT) **worker-loop supervision（2026-07-13）**：`run_once`
  轮级异常有 report/backoff，singleton 每轮复核；plist 已改 KeepAlive，wrapper 以 `loop` 启动；
  SIGTERM 停止补 future、终止活跃 MinerU 子进程组，launchd 可安全重启。
- [ ] (M/OPERATIONS AND DEPLOYMENT) **Operator cannot tell why a document failed without psql: no logging is configured, worker reports re**
  - 修法：Configure stdlib logging (timestamped, to stderr and optionally runtime/logs/) at CLI entry; include str(exc) and a traceback log line for unexpected failures; add exception message to WorkerFailure; persist MinerU stderr tail into processing_run.error or the 
- [ ] (M/OPERATIONS AND DEPLOYMENT) **No database backup story at all, and both the DB cluster and the immutable raw archive live on the s**
  - 修法：Add a `make pg-backup` target (pg_dump -Fc to a different physical volume, or wal-g/pgBackRest), schedule it alongside doctor, and document restore. Decide raw-archive redundancy (second disk or cloud sync of raw_documents, which is only 45MB today). Define re
- [ ] (M/OPERATIONS AND DEPLOYMENT) **Default worker knobs 对 ≥500-company 仍需单独压测**
  - 2026-07-13 已完成 200 股目标的最小修复：有进展零等待、sync/download/parse=13/50/50、
    concurrency 硬顶 8、回补背压 2000；500 家候选量、磁盘与 quota 尚未实测，故本项不关闭。
- [ ] (M/OPERATIONS AND DEPLOYMENT) **The one human-maintained input — the company list — has no operator tooling: no bulk seed, no list/p**
  - 修法：Add a `tracked-companies` CLI (import CSV of scodes with default backfill window, list, pause/resume) reusing the existing upsert; make the worker skip-and-report companies without securities once (mark tracked_company status or write a checkpoint) instead of 
- [ ] (M/OPERATIONS AND DEPLOYMENT) **Disk growth is unmanaged: ~17x artifact amplification per parse, retries and superseded runs never g**
  - 修法：Add a GC command that removes artifact/derived dirs for runs that are (a) failed and retry-exhausted or (b) pruned/superseded beyond a retention window, keeping the DB as source of truth; add a doctor WARN/FAIL on free-space thresholds for data root and PGDATA
- [ ] (S/OPERATIONS AND DEPLOYMENT) **wipe_test_data.sh is a production-corpus-destroying command guarded only by an env var, sitting in t**
  - 修法：Make the wipe refuse to run unless the target database name/host matches an explicit allowlist (e.g. require `WIPE_DB=invest_engine_test` matching current_database()), or remove the target from the production Makefile and keep it in a tests-only script; at min
- [ ] (S/SECURITY AND CREDENTIALS POS) **Parse-failure error text (MinerU stderr, absolute paths) leaks into the public change feed and /v1/c**
  - 修法：Drop the free-text message from outbox event payloads (keep stage/error_code/retryable, which is what the queue views consume), or sanitize messages to relpaths/basenames before they enter outbox_event.
- [ ] (M/SECURITY AND CREDENTIALS POS) **DB role separation exists only on paper: runtime service, migrations login, and DBHub MCP all connec**
  - 修法：Before production: provision real LOGIN roles (disclosure_app member of disclosure_app group, disclosure_reader) with scram-sha-256 auth, re-init or update pg_hba away from trust for TCP, point DATABASE_URL at the app login and DISCLOSURE_READER_DATABASE_URL a
- [ ] (M/TEST COVERAGE GAPS for produ) **Golden determinism baselines cover only 3 of 9 filing types; inquiry_reply, quarterly/semiannual, fo**
  - 修法：Extend tests/fixtures/phase00 via the existing regen protocol (04R §6.4) with at least one real sample per remaining filing type — priority: inquiry_reply, quarterly_report or semiannual_report (适用/不适用-rich), performance_forecast — and fold them into the deter
- [ ] (S/TEST COVERAGE GAPS for produ) **Migration downgrade round-trips: only 0009 is automated; 0010–0013 downgrades have never been execut**
  - 修法：Generalize the scratch-DB round-trip test to walk head → 0009 → head (exercising every post-0009 downgrade/upgrade pair) and assert view/column shape at each stop; make 'add a migration' imply extending this test per the milestone protocol.
- [ ] (M/TEST COVERAGE GAPS for produ) **Web fallback channel (CninfoWebSource) has zero end-to-end/integration coverage; queue view's hisAnn**
  - 修法：Add an integration test on the scratch-DB pattern: run SyncDisclosureIndex with a fake web-shaped source writing provider_interface='cninfo:hisAnnouncement', assert the candidates appear in pending_download_v1 and download→register completes; add a CLI parse t
- [ ] (M/TEST COVERAGE GAPS for produ) **prune_history.sh and wipe_test_data.sh are untested shell run against the live DB and data root**
  - 修法：Add a scratch-DB integration test (reuse test_worker_integration setUpClass pattern): publish two generations of one document, run prune_history.sh via subprocess, assert the active run/units/events are intact, superseded rows are gone, and the change feed's p
- [ ] (L/TEST COVERAGE GAPS for produ) **No load/volume testing; pending_download_v1 and pending_parse_v1 predicates lack supporting indexes **
  - 修法：Add a volume smoke on the scratch DB: seed 500 tracked companies and a few thousand synthetic source_access snapshots, assert queries.py helpers stay under a latency bound (or EXPLAIN shows no full-corpus jsonb re-explosion); pair with a new migration adding (
- [ ] (M/TEST COVERAGE GAPS for produ) **rebuild_units path tested only against in-memory fakes: real repository query, CLI chain, and supers**
  - 修法：Add an integration test: parse+publish a fixture doc on the scratch DB, then execute the rebuild-units chain and assert a new active rebuild_units run, superseded old run, and the expected observed/changed events; extend test_pipeline_cli with the rebuild-unit
- [ ] (S/TEST COVERAGE GAPS for produ) **Category dimension (0012) verified only by column names: seed load, '||' splitting, ordinal, and unk**
  - 修法：Add an integration content test: insert documents with raw_category '010301||010112', a single code, an unknown code, and empty string; assert row counts, ordinals, joined names, and NULL name for the unknown code; assert provider_category seed count > 2000 po
- [ ] (S/TEST COVERAGE GAPS for produ) **Document-level advisory lock wiring is a silent no-op by design and has no test proving it engages o**
  - 修法：Integration test: open a SqlAlchemyUnitOfWork mid-publish (or just call maybe_lock_document inside a live uow), query pg_locks from a second connection for classid=DOC_NS and objid=stable_document_hash(doc_id); optionally add a two-session blocking test with l

### MINOR

- [ ] (S/COMPANY-LIST INITIALIZATION ) **Sync-side upsert force-reactivates operator-paused companies**
  - 修法：When the intake use case lands, change _upsert_tracked_company to preserve existing.status (only set status='active' on first insert), or gate reactivation behind an explicit command flag; ownership of status transitions moves to the track CLI. One-line change
- [ ] (S/COMPANY-LIST INITIALIZATION ) **Worker's default watching assumes CNINFO WebAPI credentials but never verifies them at startup; no w**
  - 修法：Add a fail-fast preflight in cli/worker.py main() (before acquiring the singleton lock): if worker_batch_sync > 0 and any of cninfo_access_key/secret is unset, exit non-zero with a ConfigurationError-style message (consistent with the fail-closed settings phil
- [ ] (S/COMPLETENESS CRITIC) **Legal/compliance posture of the PDF archive, downstream redistribution, and the web-scrape fallback **
  - 修法：Record a one-page compliance note: what the WebAPI agreement permits (storage, internal redistribution, retention), whether the raw archive and L2 hand-off stay inside it, and whether the web fallback channel is acceptable at all; gate `--channel web` behind t
- [ ] (M/COMPLETENESS CRITIC) **No content-quality drift monitoring: build stats and quality_status are written to append-only markd**
  - 修法：Add a doctor or end-of-round check comparing current build stats (units/document by filing_type, table:text ratio, quality_status counts, zero-unit-build rate) against a rolling baseline persisted in ops, WARN on threshold breach; include the summary in the da
- [ ] (S/DOCS/CONTRACT DRIFT) **Milestone 05 §9 "明确不做" still forbids rebuild_units, Filing API and scheduling that the same document**
  - 修法：Amend §9 to note rebuild_units was added by ub-2026.07-8 and qualify the other items as "本 milestone 范围外（已由 06/08 交付）".
- [ ] (S/DOCS/CONTRACT DRIFT) **Milestones 04R/05/06/07/08 frontmatter still say status: ready-for-implementation despite documented**
  - 修法：Flip the five milestones' status to complete (or complete-with-follow-ups) with updated_at, matching the convention used for 00-04.
- [ ] (S/DOCS/CONTRACT DRIFT) **contract-checklist §4 source_ref required-field list was not updated for 0010: applicability and pag**
  - 修法：Add applicability and page_no to the §4 list (18 fields), keeping it in sync with source_ref.v1.json.
- [ ] (M/DOCS/CONTRACT DRIFT) **README still describes the service as "Phase 01": no mention of migrations, pipeline, CNINFO sync, w**
  - 修法：Refresh the README with a current quick-start (bootstrap DB + migrate, seed a company via make sync, run worker-once/loop, query the Filing API) and pointers to service-purpose.md and the milestone/checks docs.
- [ ] (S/DOCS/CONTRACT DRIFT) **Subordinate architecture docs still declare payload_kinds [text, table, qa] and reference protocol v**
  - 修法：Either patch the frontmatter/payload-kind mentions to include mixed with a pointer to service-purpose §6.5, or stamp both docs with a "superseded on payload kinds by service-purpose v1.2" banner.
- [ ] (S/DOCS/CONTRACT DRIFT) **Acceptance rows A38-A40 and the canonical doc cite milestone "06R", but no 06R milestone document ex**
  - 修法：Create a stub 06R milestone doc (scope = 05-U7 projection + A38-A40) or re-point the references to a "planned, unscheduled" note in the roadmap; update implementation README's file inventory (reviews/, plist) at the same time.
- [x] (S/DOCS/CONTRACT DRIFT) **Legacy durable docs disagreed on the active task.**
  - 处置：2026-07-13 退役本地 Prompt/Plan/Status 多文件栈；长期事实提升到 tracked
    milestone/review，会话级交接改用条件触发的单一 HANDOFF。该历史 finding 不再要求重写 Prompt.md。
- [ ] (S/FAILURE PATHS AND RESILIENCE) **Web/API channel state divergence: shared checkpoint scope and cross-channel candidate override**
  - 修法：Record the channel in the checkpoint cursor (or use channel-scoped scope_keys), and prefer the API-channel candidate when both channels have snapshotted the same announcement.
- [ ] (S/FAILURE PATHS AND RESILIENCE) **Stale reclaim can fail a legitimately running manual parse; no validation that stale threshold excee**
  - 修法：Validate stale_run_threshold_seconds > disclosure_parse_timeout_seconds at settings load; longer term, add a heartbeat column to running runs instead of a fixed age threshold.
- [x] (M/FAILURE PATHS AND RESILIENCE) **Shutdown during parse orphaned MinerU — fixed 2026-07-13**
  - SIGINT/SIGTERM 停止 pool refill、取消未开始 future，并终止登记的 MinerU process group；
    当前文档失败可重试，singleton session lock 随进程退出释放。
- [x] (S/FAILURE PATHS AND RESILIENCE) **sync_due date truncation / stale timestamp — fixed 2026-07-13**
  - due 直接比较 checkpoint `updated_at + interval`；repository cursor update 同步刷新
    `updated_at`，不再把上海日期截断成约两日 cadence。
- [ ] (M/FAILURE PATHS AND RESILIENCE) **Unbounded growth: index snapshots accumulate forever and pending_download_v1 rescans all history; do**
  - 修法：Prune or supersede old index snapshots (or restrict the view to the newest access per company), index the extraction, and stream downloads to the tmp file with a max-size guard.
- [ ] (M/OPERATIONS AND DEPLOYMENT) **Doctor's per-run integrity checks are unbounded and will make `make doctor` unusably slow and noisy **
  - 修法：Apply the sample_size (recent-N plus first-N, like raw checks) to processing-run and snapshot checks, reserving exhaustive verification for --full; replace the orphan prefix scan with a sorted/prefix-trie match; summarize PASS lines and print only WARN/FAIL de
- [ ] (S/OPERATIONS AND DEPLOYMENT) **Stale-reclaim threshold is not coupled to the configured parse timeout, so a raised timeout silently**
  - 修法：Validate at settings load (or worker start) that stale_run_threshold_seconds >= 2× parse_timeout_seconds, or derive the default threshold from the configured timeout; document that manual long-timeout runs require pausing worker-loop or raising the threshold.
- [ ] (S/OPERATIONS AND DEPLOYMENT) **Worker and pipeline CLIs skip the mount-sentinel/preflight that the API enforces, writing to data/ru**
  - 修法：Run the fast `_environment_checks` (roots writable + sentinel present) at worker and pipeline startup, exiting with a clear `[FAIL]` before acquiring the lock or touching the DB.
- [ ] (S/OPERATIONS AND DEPLOYMENT) **Worker and pipeline silently fall back to the migration-owner DSN when DATABASE_URL is unset, runnin**
  - 修法：Remove the fallback (require DATABASE_URL for worker/pipeline, erroring with the existing ConfigurationError message), or gate it behind an explicit flag for bootstrap-only scenarios.
- [ ] (M/OPERATIONS AND DEPLOYMENT) **Makefile entry points are machine-specific with a misleading Python fallback, and there is no end-to**
  - 修法：Replace the conda fallback with an explicit error (`$(error .venv missing — run make venv)`); write a single ops runbook (docs/implementation/runbooks/production-bringup.md) covering cluster init settings, migration, seeding, worker + API supervision, doctor/b
- [ ] (S/SECURITY AND CREDENTIALS POS) **Reader API engine silently falls back to the write-capable app credentials**
  - 修法：In production mode treat a missing DISCLOSURE_READER_DATABASE_URL as a startup failure (turn the doctor WARN into FAIL behind a production flag) so reads always run on the reader role.
- [ ] (S/SECURITY AND CREDENTIALS POS) **Absolute filesystem paths persisted into core DB error/reason columns, violating the stated relpath-**
  - 修法：Normalize exception messages before persistence: have raw_document_store raise messages with basenames/relpaths, or scrub known roots (data_root/runtime_root) in the use cases that persist str(exc).
- [ ] (S/SECURITY AND CREDENTIALS POS) **CNINFO web fallback channel fetches index and PDFs over cleartext HTTP into the immutable corpus**
  - 修法：Switch the three web-channel constants to https:// (CNINFO serves both hosts over TLS) and keep the existing dedup/file-signature logic unchanged.
- [ ] (S/SECURITY AND CREDENTIALS POS) **MinerU subprocess inherits the full service environment including CNINFO secrets and DB DSNs**
  - 修法：Replace dict(os.environ) with a minimal allowlist (PATH, HOME, LANG, MINERU_MODEL_CACHE, HF_HOME, MODELSCOPE_CACHE, NO_PROXY) plus extra_env.
- [ ] (S/SECURITY AND CREDENTIALS POS) **Migration 0012 grants SELECT on a disclosure_core table to the reader role, eroding the views-only r**
  - 修法：Issue a follow-up migration revoking the reader grant on disclosure_core.provider_category; if readers need the category dimension, expose it through a disclosure_public view (document_categories_v1 already exists for the join case).
- [ ] (S/TEST COVERAGE GAPS for produ) **Worker loop mode, interruptible sleep, and successful-run report writing are untested**
  - 修法：Extend the existing subprocess worker test: run `worker once` with zero limits but the lock free and assert both report files gain a '## run' section; add a loop test with a 1-second interval that SIGTERMs after the first round and asserts clean exit 0 with ex
- [ ] (S/TEST COVERAGE GAPS for produ) **sync_due interval boundary predicate untested: only the never-synced (NULL checkpoint) arm has cover**
  - 修法：Add two cases to OpsQueueViewTests seeding source_checkpoint cursors with window_end = today and today-2d, asserting due/not-due at a 1-day interval, plus one asserting the same-day boundary behavior explicitly.
- [ ] (S/TEST COVERAGE GAPS for produ) **Units endpoint keyset cursor never walked against a real database; SQL-shape assertions are the only**
  - 修法：Add a runtime test seeding one document with 3+ units and walking /units with limit=1 until next_cursor is null, asserting order and completeness (mirror of test_documents_keyset_paginates_null_announcement_date).
- [ ] (S/TEST COVERAGE GAPS for produ) **Live-DB suite runs only under manual protocol: no CI or scheduled execution of the integration layer**
  - 修法：Wire a scheduled or pre-release target (even a launchd/loop-driven `make test-integration && make test` against the local cluster) whose report lands next to the worker reports, so integration regressions surface within a day rather than at the next manual mil


## 验收口径

1. blocker 全清；major 中数据正确性类（checkpoint 空洞、changes feed 跳事件、
   下载永久毒化、公司改名卡死）全清；其余 major 有排期或明确接受记录。
2. 全链条演练：watchlist 加一家新公司 → launchd 周期内自动 三年回补→解析核心集→发布；
   期间人工不介入。
3. Codex 独立终审 go。

### 追加（2026-07-08 round21）

- [ ] watchlist 真相翻转设计（开放接口增删公司时）：miniflux 形态——tracked_company
      表变真相 + admin API POST/DELETE + watchlist.csv 降级为 import/export 交换格式
      （≈OPML）；过渡期参照 HA deprecation（YAML→UI）双轨。单人 GitOps 阶段不做。
