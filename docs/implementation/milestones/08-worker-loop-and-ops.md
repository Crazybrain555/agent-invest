---
id: disclosure_anchor_milestone_08_worker-loop-and-ops
project: disclosure_anchor
title: worker loop 与本地运行
status: ready-for-implementation
created_at: 2026-06-26
updated_at: 2026-07-04
depends_on: milestones 04R / 05 / 07
delivers_to: 常态运行（demo 期定时批处理，协议 §15）
---

# Milestone 08: worker loop 与本地运行

把 sync / download / parse / build / publish 串成可重复运行的本地 worker。worker 是**纯调度壳**：
所有业务动作都是既有 use case（07 的 sync/download、04 的 parse、05 的 build/publish/process），
worker 只做扫描、加锁、调用、重试和报告，不含业务逻辑。

## 1. 状态模型（04R-D4 枚举是唯一事实源）

worker 队列直接由 DB 状态派生，**不建独立任务表**；判定 SQL 固化为 `ops.*_v1` 内部视图
（0007-R1 第 10 条，S4）——worker / doctor / 人工排查共用同一套定义，杜绝状态机漂移：

```text
队列（ops 视图）          判定
sync_due                source_checkpoint 窗口上界落后于 now - sync_interval 的 tracked_company
pending_download_v1     source_access.result_snapshot.candidates 中"未注册 document、
                        未终态失败"的候选（B6：候选已持久化，crash 后自然恢复）
pending_parse_v1        document.status IN ('registered','parse_failed') 且无 running run
                        且（parse_failed 时）最新 failed run 的 error.retryable=true
                        且 failed run 计数 < MAX_PARSE_RETRIES(默认 3)
pending_build_v1        processing_run.status='succeeded'
                        且 unit_build_status IN ('not_started','failed')
                        且 unit_build_attempt_count < MAX_BUILD_RETRIES(默认 3)
pending_publish_v1      processing_run.status='succeeded' 且 unit_build_status='succeeded'
                        且 is_active=false 且（该 run 晚于 document 当前 active run）
retryable_failed_run_v1 供报告/人工：可重试失败 run 明细
stale_running_run_v1    processing_run.status='running' 且 started_at < now - STALE_RUN_THRESHOLD
                        （默认 2×parse timeout）→ 回收：标 failed(error_code='stale_reclaimed',
                        retryable=true)
```

注意（B1）：published 文档的重解析失败**不出现**在 pending_parse 的降级路径里——document.status
保持 published，重试判定基于 run 层状态；`non_retryable` 与超次失败只进报告与人工待办。

## 2. 并发与锁

- **worker 单例锁**：进程持有一条**专用连接**（不走连接池），其上
  `pg_try_advisory_lock(WORKER_NS, 0)`（session 级），拿不到即退出；连接随进程存活，
  进程崩溃锁自动释放。专用连接是关键——池化连接的 session 锁会泄漏（E6）。
- **document 级锁**：两参形式 `pg_try_advisory_xact_lock(DOC_NS, stable_hash(document_id))`，
  `stable_hash` 用 Python 侧 crc32（显式、跨版本稳定，不用 PG `hashtext` 内部函数）；
  在每个**改写该 document 状态的事务内**获取（register 复用 / finish_run / publish），
  事务结束自动释放。跨事务的整文档互斥由单例锁 + status 声明式判定保证，不做长持锁。
- 不引入 Redis/文件锁。

## 3. 实施细则

1. `application/worker/worker.py`：`run_once(limits) -> WorkerReport`——按 §1 队列顺序
   （stale 回收 → sync → download → parse/retry → build → publish）各处理至多 N 个
   （settings：`WORKER_BATCH_*`，默认 sync 5 / download 10 / parse 3 / build 10 / publish 10；
   parse 串行执行，MinerU 吃满单机资源）。**每个 document 的处理包裹在 try/except**：
   use case 异常（含 re-raise 的未知异常）记入报告失败清单后继续下一个——
   一个坏 PDF 不得打死整个 loop（04R-R4 的异常分型保证 run 已持久化 failed 状态）。
2. `worker-loop`：`run_once` + sleep（`WORKER_LOOP_INTERVAL_SECONDS` 默认 900）循环；
   SIGINT/SIGTERM 优雅退出（完成当前 document 后停）。
3. 重试策略只读结构化错误：`error.retryable`（04R-R4 已收紧分类）+ 次数上限；
   worker 不自行解释错误文本。
4. 报告：每轮写 `runtime/reports/worker/<date>.md`（发现/下载/解析/发布/失败计数、
   失败清单含 document_id + error_code、耗时）；`runtime/reports/parse_quality/<date>.md`
   （needs_review/unusable unit 统计、builder 跳过统计——05 的 build 统计直接汇入）。
   报告目录在 `DISCLOSURE_RUNTIME_ROOT` 下，不进 git。
5. 命令：`make worker-once` / `make worker-loop`；`python -m disclosure_anchor.cli.worker`。
6. doctor 深检（04R-R6 的抽样一致性 + stale run + 孤儿文件）建议每日一次：
   `make doctor-full`，可与 worker-loop 独立运行。
7. launchd plist 示例（`docs/implementation/runbooks/` 附带，默认不启用）：worker-once
   定时执行样式，符合协议 §15"定时批处理、人工拉起"。

## 4. 检查点

- `make worker-once` 能把一个 pending document 从 registered 跑到 active run（含 05 process）。
- 队列视图与 worker 行为一致（对 ops.*_v1 视图行断言，同一 SQL 定义）。
- 注入抛异常的坏 PDF：该 document 进失败清单，loop 继续处理后续 document。
- 两个并发 worker-once：第二个立即退出（单例锁）；同一 document 不被并发处理（文档锁）。
- worker 崩溃（kill -9 注入）后：raw archive 完好、无僵尸锁、stale run 下轮被回收重试。
- retryable=false 的失败不再重试且出现在报告失败清单。
- 报告数字与 DB 实际状态一致（对账测试）。
- doctor 能发现 active run 冲突与 stale running run。

## 5. 测试要求

单测：队列判定 SQL（各状态样本）、重试次数/门槛、报告聚合。
集成（DB-gated）：run_once 全链（用 fake parser 加速）、并发锁语义（两连接）、
stale 回收、崩溃恢复（事务中断后状态一致）。

## 6. Definition of Done

- 本地运行闭环成立：`worker-loop` 挂机一晚（或模拟等价），10 家样本池增量公告自动到 active run；
- 失败可恢复、可定位；acceptance-matrix A29/A30 置 pass。

## 7. 明确不做

- 不引入 Celery / Redis / Airflow / Prefect / Dagster；不建全局 L6 调度脊柱；
- 不做多机部署；不做实时推送（change feed 拉式消费已够）。

## 8. 常见失败与处理

- worker 重复处理：先查两级锁与 document.status 事务顺序。
- publish 中断：旧 active run 保持（05 事务保证）；下轮 pending_publish_v1 重入。
- 长时间卡住：stale_running 回收 + 报告可见；人工 `make doctor-full` 定位。
- MinerU 内存压力：parse 串行 + 超时（04R-R4）兜底；必要时调小 WORKER_BATCH_PARSE。
