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

worker 队列直接由 DB 状态派生，**不建独立任务表**：

```text
队列                    判定（SQL）
sync_due               source_checkpoint 窗口上界落后于 now - sync_interval 的 tracked_company
pending_download       07 的候选（索引已见、raw 未归档）——由 sync 产出，检查点重叠窗口内自然重现
pending_parse          document.status = 'registered'
parse_retry            document.status = 'parse_failed' 且最新 run 结构化 error.retryable = true
                       且重试次数（该 document 的 failed run 计数）< MAX_PARSE_RETRIES(默认 3)
pending_build_publish  document.status = 'parsed'（有 succeeded run、无 active run）
stale_running          processing_run.status = 'running' 且 started_at < now - STALE_RUN_THRESHOLD
                       （默认 2×parse timeout）→ 标记 failed(error_code='stale_reclaimed',
                       retryable=true)，进 parse_retry
```

`non_retryable` 与超次失败停在 `parse_failed`，只进报告与人工待办，不再自动重试。

## 2. 并发与锁

- **worker 单例锁**：PG advisory lock（`pg_try_advisory_lock(常量 key)`），拿不到即退出
  （防止两个 worker-loop 并发）；进程崩溃锁自动释放，无 stale 锁文件问题。
- **document 级锁**：处理单个 document 前 `pg_try_advisory_xact_lock(hashtext(document_id))`，
  拿不到跳过本轮（另一进程正在处理）。
- 不引入 Redis/文件锁；锁的生命周期与连接/事务绑定，天然无泄漏。

## 3. 实施细则

1. `application/worker/worker.py`：`run_once(limits) -> WorkerReport`——按 §1 队列顺序
   （stale 回收 → sync → download → parse/retry → build+publish）各处理至多 N 个
   （settings：`WORKER_BATCH_*`，默认 sync 5 / download 10 / parse 3 / publish 10；
   parse 串行执行，MinerU 吃满单机资源）。
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
- publish 中断：旧 active run 保持（05 事务保证）；下轮 pending_build_publish 重入。
- 长时间卡住：stale_running 回收 + 报告可见；人工 `make doctor-full` 定位。
- MinerU 内存压力：parse 串行 + 超时（04R-R4）兜底；必要时调小 WORKER_BATCH_PARSE。
