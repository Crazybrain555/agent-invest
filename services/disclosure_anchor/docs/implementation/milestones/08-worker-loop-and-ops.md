---
id: disclosure_anchor_milestone_08_worker-loop-and-ops
project: disclosure_anchor
title: worker loop 与本地运行
status: complete
created_at: 2026-06-26
updated_at: 2026-07-07
depends_on: milestones 04R / 05 / 07
delivers_to: 常态运行（demo 期定时批处理，协议 §15）
---

# Milestone 08: worker loop 与本地运行

把 sync / download / parse / build / publish 串成可重复运行的本地 worker。worker 是**纯调度壳**：
所有业务动作都是既有 use case（07 的 sync/download、04 的 parse、05 的 build/publish/process），
worker 只做扫描、加锁、调用、重试和报告，不含业务逻辑。

## 1. 状态模型（04R-D4 枚举是唯一事实源）

worker 队列直接由 DB 状态派生，**不建独立任务表**；判定 SQL 固化为 `ops.*_v1` 内部视图，
worker / doctor / 人工排查共用同一套定义，杜绝状态机漂移。**SQL 权威与迁移归属**：
五个 0007 视图（pending_parse / pending_build / pending_publish / retryable_failed_run /
stale_running_run）的完整 SQL 以 **04R-R1 第 10 条为唯一权威**，0007 迁移逐字复制；
另两个视图 `ops.sync_due_v1` / `ops.pending_download_v1` 由**本 milestone 新建迁移**
`src/…/migrations/versions/0009_ops_sync_queue_views.py`
（down_revision="0008_unit_builder_provenance"）创建，只授 app 角色，downgrade 删两视图，
SQL 定死：

```text
sync_due_v1:
  SELECT tc.tracked_company_id, tc.company_id, tc.security_id,
         sc.cursor->>'window_end' AS window_end
  FROM core.tracked_company tc
  LEFT JOIN core.source_checkpoint sc
    ON sc.provider='cninfo' AND sc.scope_key = tc.company_id || ':p_info3015'
  WHERE tc.status='active'
  （无 checkpoint 行 → window_end 为 NULL → 视为 due；键名与 07 的写侧格式逐字一致）
pending_download_v1:
  候选集 = source_access(provider='cninfo',
           provider_interface='cninfo:p_info3015', status='ok') 的
           jsonb_array_elements(result_snapshot->'candidates') AS c，
           按 c->>'provider_document_id' 去重取 accessed_at 最新一份
  过滤：NOT EXISTS (SELECT 1 FROM core.document d WHERE d.provider='cninfo'
        AND d.provider_document_id = c->>'provider_document_id')
    AND NOT 终态失败（= 存在 provider_interface='cninfo:download_pdf' 且
        error->>'retryable'='false' 的失败行，或该 provider_document_id 的
        download 失败行数 ≥ 3——视图暴露 failed_download_count 列，
        ≥3 的截断由调用方施加）
  输出列：provider_document_id, download_url, title, announcement_date,
          source_access_id, failed_download_count
```

语义表（0007 五视图，权威 SQL 见 04R-R1.10）：

```text
pending_parse_v1        document.status IN ('registered','parse_failed') 且无 running run；
                        暴露 failed_parse_count 与 last_failed_retryable 事实列
pending_build_v1        run.status='succeeded' 且 unit_build_status IN ('not_started','failed')；
                        暴露 unit_build_attempt_count
pending_publish_v1      run.status='succeeded' 且 unit_build_status='succeeded' 且
                        is_active=false 且晚于当前 active run（无 active run 时恒入队，
                        COALESCE '-infinity' 已处理）；worker 对同一 document 只处理
                        started_at 最新一行（ORDER BY started_at DESC, processing_run_id DESC）
retryable_failed_run_v1 可重试失败 run 明细（error 为 jsonb——0007 已 ALTER）
stale_running_run_v1    status='running' 的 run 全集，暴露 started_at
```

**阈值策略（与 04R-R1.10 一致）**：视图只暴露事实列，不内嵌阈值；阈值全部来自 settings——
`DISCLOSURE_MAX_PARSE_RETRIES=3`、`DISCLOSURE_MAX_BUILD_RETRIES=3`、
`DISCLOSURE_STALE_RUN_THRESHOLD_SECONDS=3600`（=2×parse timeout 默认值的固定折算）、
`DISCLOSURE_SYNC_INTERVAL_SECONDS=86400`（首版忽略 tracked_company.sync_frequency 列，
全局间隔统一生效）。防漂移：阈值过滤统一实现在 `application/worker/queries.py` 的查询
helper（每视图一个函数，SELECT … FROM ops.<view> WHERE <阈值谓词>），worker 与 doctor
都只经该 helper 读队列，禁止各自手写判定。

注意（B1）：published 文档的重解析失败**不出现**在 pending_parse 的降级路径里——document.status
保持 published，重试判定基于 run 层状态；`non_retryable` 与超次失败只进报告与人工待办。
stale 回收的执行载体定死：run_once 第一步经 queries.py 执行单条 UPDATE——
`UPDATE core.processing_run SET status='failed', finished_at=now(),
 error='{"stage":"parse","error_code":"stale_reclaimed","retryable":true}'::jsonb
 WHERE processing_run_id IN (SELECT processing_run_id FROM ops.stale_running_run_v1
 WHERE started_at < now() - make_interval(secs => :threshold))`；回收数计入 WorkerReport。

## 2. 并发与锁

- **worker 单例锁**：进程持有一条**专用连接**（不走连接池），其上
  `pg_try_advisory_lock(WORKER_NS, 0)`（session 级），拿不到即退出；连接随进程存活，
  进程崩溃锁自动释放。专用连接的获得方式定死（E6：从业务 engine 取连接会随归还回池而
  泄漏 session 锁）：`create_engine(url, poolclass=sqlalchemy.pool.NullPool)` 上
  `engine.connect()` 一条，由 cli/worker.py 持有至进程退出；禁止使用业务 engine 的池化连接。
- **document 级锁**：两参形式 `pg_try_advisory_xact_lock(DOC_NS, stable_hash(document_id))`；
  常量定死（worker.py 顶部导出，测试/doctor 按 pg_locks.classid 断言同值）：
  `WORKER_NS = 815001`、`DOC_NS = 815002`。stable_hash 定死（crc32 是无符号 0..2^32-1，
  直接传 int4 会 integer out of range）：
  `h = zlib.crc32(document_id.encode('utf-8')); return h - 2**32 if h >= 2**31 else h`。
  在每个**改写该 document 状态的事务内**获取（register 复用 / finish_run / publish），
  事务结束自动释放。跨事务的整文档互斥由单例锁 + status 声明式判定保证，不做长持锁。
- 不引入 Redis/文件锁。
- **MinerU 子进程组清理（2026-07-05 实战新增，定点加固既有 adapter，不与 04R §1 清单冲突）**：
  MinerU 3.4 CLI 会拉起本机 fast_api 后端子进程；`subprocess.run(timeout=…)` 超时只杀直接
  子进程，fast_api 孤儿会常驻（实测泄漏 1.8GB 内存并抢占后续解析算力）。08 实施时改
  `MinerUProcess.run`：`start_new_session=True` 起进程组，超时/异常路径 `os.killpg` 全组清理；
  worker 每轮开始前的 stale 回收顺带检查无残留 mineru 进程（doctor WARN 项）。

## 3. 实施细则

1. `application/worker/worker.py`：`run_once(limits, deps) -> WorkerReport`——按 §1 队列顺序
   （stale 回收 → sync → download → parse → build → publish）各处理至多 N 个。
   既有空包 src/disclosure_anchor/worker/（Phase01 骨架遗留）删除，实现统一放
   application/worker/。类型定死：WorkerLimits 与 WorkerReport 定义在
   `application/dto/worker_report.py`——WorkerLimits{sync, download, parse, build,
   publish: int}（由 settings 组装：WORKER_BATCH_SYNC=5 / WORKER_BATCH_DOWNLOAD=10 /
   WORKER_BATCH_PARSE=3 / WORKER_BATCH_BUILD=10 / WORKER_BATCH_PUBLISH=10，按 settings.py
   现有"小写字段 + AliasChoices 大写别名"模式，同步 .env.template）；
   WorkerReport{started_at, duration_seconds, stale_reclaimed, synced_companies,
   candidates_discovered, downloaded, parsed, built, published, failed,
   skipped_oversized, failures: list[WorkerFailure]}，
   WorkerFailure={stage, document_id|item_ref, error_code}。
   依赖注入定死：deps = WorkerDeps{engine, source_port_factory, parser_factory, clock}，
   生产 wiring 在 cli/worker.py 经 bootstrap 组装；集成测试注入 FakeCninfoSource 与
   fake parser，全程不出网。
   parse 阶段跳过 `provider_metadata.oversized=true` 的 document（07 §3.9 护栏；
   计入 skipped_oversized 并出现在报告，人工单跑提高 timeout 处理）。
   阶段↔use case 接法定死：parse 阶段对 pending_parse 的每个 document 调 **05 的 process**
   （单文档内 parse→build→publish 串行——检查点"含 05 process"即此意）；build/publish 队列只消化
   process 中断留下的残留，分别调 build_units / publish_run。不同文档可按
   `WORKER_PARSE_CONCURRENCY` 有界并行，默认 1 是安全退化路径；GPU 流控同时受文档并发和
   MinerU 页窗口约束，详见 `../design/worker-dynamic-scheduling.md`。
   异常隔离粒度按阶段定死：sync=每 tracked_company、download=每候选、
   parse/build/publish=每 document；单项异常记入失败清单（含 stage 与标识）后 continue——
   一个坏项不得打死整个 loop（04R-R4 的异常分型保证 run 已持久化 failed 状态）。
2. `worker-loop`：`run_once` + sleep（`WORKER_LOOP_INTERVAL_SECONDS` 默认 900）循环；
   SIGINT/SIGTERM 优雅退出（完成当前 document 后停）。
3. 重试策略只读结构化错误：`error.retryable`（04R-R4 已收紧分类）+ 次数上限；
   worker 不自行解释错误文本。
4. 报告路径与写入语义定死：`<DISCLOSURE_RUNTIME_ROOT>/reports/worker/YYYY-MM-DD.md` 与
   `<DISCLOSURE_RUNTIME_ROOT>/reports/parse_quality/YYYY-MM-DD.md`（date = 本地时区的轮次
   开始日期；**同日多轮追加写入**，每轮一个 `## run <ISO8601 开始时间>` 小节——覆盖式写法会让
   挂机一晚只剩最后一轮）。内容：WorkerReport 全字段 + 失败清单（document_id + error_code）；
   parse_quality 汇入 05 的 build_stats.v1.json 统计。不进 git。
5. CLI 与 Makefile 定死：cli/worker.py 用 argparse 子命令 `once|loop`；Makefile 追加
   （并入 .PHONY）：
   `worker-once:` → `PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker once`
   `worker-loop:` → `PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker loop`
   `doctor-full:` → `PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor --full`
   拿不到单例锁的行为定死：stdout 打印 `[skip] another worker holds the singleton lock`，
   **退出码 0**（launchd/make 不得把正常互斥当失败），不写报告文件。
6. doctor 深检建议每日一次（可与 worker-loop 独立运行）。FAIL/WARN 分级封闭表：
   FAIL = PG 不通 / migration 非 head / 三 schema 或角色权限缺 / 同 document >1 active run /
   succeeded run 缺 normalized_ir 或 artifact_hash 不匹配 / unit_build_status='succeeded'
   但快照文件缺失；
   WARN = stale running run 存在 / 孤儿 raw、artifact 文件 / outbox seq 有空洞 /
   failed run 的 error 非合法 JSON。
   退出码：有 FAIL→1，仅 WARN→0。
7. launchd plist 示例定死：`docs/implementation/runbooks/
   com.agentinvest.disclosure-anchor.worker-once.plist`（Label 同文件名去 .plist，
   ProgramArguments 调 make worker-once，StartCalendarInterval 每日 07:30；默认不启用），
   符合协议 §15"定时批处理、人工拉起"。

## 4. 检查点

- `make worker-once` 能把一个 pending document 从 registered 跑到 active run（含 05 process）。
- 队列视图与 worker 行为一致（对 ops.*_v1 视图行断言；worker 只经 queries.py helper 读队列）。
- 注入抛异常的坏 PDF：该 document 进失败清单，loop 继续处理后续 document。
- 两个并发 worker-once：第二个 returncode==0、stdout 含 `[skip] another worker holds the
  singleton lock`、reports/worker/ 无新增小节；同一 document 不被并发处理（文档锁）。
- kill -9 注入程序定死：subprocess.Popen 启 worker once（注入 sleep 的 fake parser 使 run
  停在 running），os.kill(pid, SIGKILL)；随后新连接断言
  `SELECT count(*)=0 FROM pg_locks WHERE locktype='advisory'`；doctor raw hash 检查 PASS；
  用 UPDATE started_at 提前模拟超龄后再跑 run_once，该 run status='failed' 且
  error_code='stale_reclaimed'。
- retryable=false 的失败不再重试且出现在报告失败清单。
- 对账断言组定死（run_once 前后各查一次）：report.parsed == status='succeeded' run 行数增量；
  report.failed == status='failed' 行数增量；report.published == 本轮新置 is_active=true 的
  run 数；report.downloaded == 本轮新增 document 行数。
- doctor 能发现 active run 冲突（FAIL）与 stale running run（WARN），退出码符合 §3.6。

## 5. 测试要求

队列判定测试放 **tests/integration/test_ops_queue_views.py（DB-gated）**——视图是 DB 对象，
no-DB 单测测不到：为每个视图构造正/反例行（必含三个反例：pending_publish 无 active run 的
文档恒入队、pending_parse retryable=false 被 helper 阈值排除、超次数被排除），断言视图行集。
tests/unit 只测 run_once 的调度/报告聚合（fake 队列结果）与 stable_hash 有符号转换。
集成（DB-gated）：run_once 全链（注入 WorkerDeps 的 FakeCninfoSource + fake parser，
全程不出网）、并发锁语义（两连接断言 classid=815001/815002）、stale 回收、
崩溃恢复（事务中断后状态一致）、0009 迁移往返（照 04R §6.2 命令样式）。

## 6. Definition of Done

- 每包提交门禁 = `make agent-check` + live-DB `make test`（04R §6.1 2026-07-05 修订）；
- 本地运行闭环成立：`worker-loop` 挂机一晚，或模拟等价（定义定死：
  WORKER_LOOP_INTERVAL_SECONDS=60 连续运行 ≥30 分钟，期间分两批注入 ≥3 个新候选
  （fake source 或本地 register），全部自动到 active run 且报告文件含 ≥3 个轮次小节）；
- 失败可恢复、可定位；acceptance-matrix A29/A30 置 pass。

## 6.5 实施后修订（2026-07-06，Claude 实现 / Codex 终审，实施中定案）

- **WorkerDeps 实际字段**超出 §3.1 四字段草案：run_once 组装 use case 需要
  uow_factory / path_builder / raw_store / artifact_store / profile_loader_factory /
  parse_timeout_seconds / config(WorkerConfig 阈值集合)；生产 wiring 仍全部在 cli/worker.py。
- **pending_download_v1**：候选来源含 web 兜底通道（provider_interface IN
  ('cninfo:p_info3015','cninfo:hisAnnouncement')），并额外暴露 company_id 与完整 candidate
  jsonb 列（下载复用 07 候选协议所需）；仍为 facts-only。
- **queries.sync_due** 在 helper 层 join core.security 取 scode/exchange；worker 对
  从未同步过的公司只做 overlap 回看（历史回填仍是显式 `make sync WINDOW=N` 人工步骤）。
- **文档级锁**取阻塞形态 `pg_advisory_xact_lock`，经 locks.maybe_lock_document 注入
  register 复用 / parse finish / publish 三个事务（无 session 的内存 fake 自动跳过）；
  常量 815001/815002 从 application/worker/locks.py 导出，worker.py 再导出。
- **op.execute 的 SQL 里字面冒号必须 `\:` 转义**（':p_info3015'、'"retryable":true' 两处实坑）；
  source_access.error 为 Text 列，视图内 `(error)::jsonb` 显式 cast。
- **web 通道候选 raw_category=''**：下载侧候选重建与 provider_metadata 改为可选处理
  （worker 全链测试抓到的 07 缺陷）。
- **worker 集成测试必须用独立 scratch database**（每类 CREATE DATABASE + bootstrap + alembic）：
  队列是全局的，在共享真库上跑 run_once 会把真实 pending 文档灌进测试 fake
  （实测 3 份真实文档被记了 raw_missing 失败 run，已外科清理）。共享库只跑视图行级断言。
- parse_quality 日报聚合 run 内存中的 build_stats（不回读 artifact）。

## 6.6 动态解析链与 GPU 双层流控（2026-07-12 实施后修订）

- Phase 1 已实现 `WORKER_PARSE_CONCURRENCY`：默认 1，settings 硬上限 16；每个文档仍执行
  parse→build→publish 一条完整链，每个任务使用独立 parser/UoW，主线程折叠报告，已有
  document lock 与 worker singleton lock 继续兜底。
- 本地文档并发不能与 provider 下载 QPS 混为一谈；下载仍服从来源端限速与既有批量边界。
- 事故证据表明，文档并发 8 与 MinerU 页窗口 64 叠加时曾形成约 255 sequences、KV cache
  97.7% 并触发 CUDA OOM；connection reset 是后果。流控必须同时限制文档并发和页窗口。
- 当时的运维缓解包括 `MINERU_PROCESSING_WINDOW_SIZE=16`，以及服务重启时评估
  `--max-num-seqs 128`；这些不是仓库永久默认值，使用前必须按当前 MinerU/vLLM 版本、GPU
  占用和真实 workload 重新核验。
- 只有出现阶段空转或服务端排队的实测证据后，才进入常驻流水线或延迟自适应的 Phase 2/3；
  继续不引入 Celery、Redis、Airflow 等外部调度平台。

完整取证、备选设计与回退条件见 `../design/worker-dynamic-scheduling.md`。

## 7. 明确不做

- 不引入 Celery / Redis / Airflow / Prefect / Dagster；不建全局 L6 调度脊柱；
- 不做多机部署；不做实时推送（change feed 拉式消费已够）。

## 8. 常见失败与处理

- worker 重复处理：先查两级锁与 document.status 事务顺序。
- publish 中断：旧 active run 保持（05 事务保证）；下轮 pending_publish_v1 重入。
- 长时间卡住：stale_running 回收 + 报告可见；人工 `make doctor-full` 定位。
- MinerU 内存压力：先降低 `WORKER_PARSE_CONCURRENCY` 与 `WORKER_BATCH_PARSE`，再收紧 MinerU
  页窗口；默认并发 1 仍是安全退化路径，外部服务参数按当前环境重新核验。
