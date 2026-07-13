---
id: disclosure_anchor_worker_dynamic_scheduling
title: Worker 动态调度与 GPU 双层流控
date: 2026-07-09
updated_at: 2026-07-13
status: implemented-with-follow-ups
authority: tracked implementation design; live runtime values must be re-verified
---

> 本文由原 gitignored 本地任务 note 提升而来。稳定设计约束可作为实现依据；GPU 占用、
> backlog、window 和并发数字是带日期的事故/运维证据，使用前必须按当前 MinerU、vLLM、
> GPU 和运行环境重新核验。

# Worker 动态调度设计（round22g，2026-07-09）

用户问题：worker 是不是"有点笨"——固定批量、阶段串行、下载与解析互相等；要动态、
不冲突、不爆内存的执行模型。

## 业界调研

| 项目 | 模型 | 借什么 |
|---|---|---|
| Miniflux | 调度器把到期 feed 入队，WORKER_POOL_SIZE 个 worker 各自跑"抓取+解析"完整链 | **每实体端到端一条链 + 有界池**——不是按阶段分层排队 |
| changedetection.io | async 队列 + N 个 fetch worker（默认 10）持续拉取；全局限速独立于并发 | **限速（对上游礼貌）与并发（本地吞吐）是两个旋钮** |
| Scrapy | 单进程异步下载器（CONCURRENT_REQUESTS + AUTOTHROTTLE 按延迟动态调并发）喂 item pipeline | 动态并发的参照系；autothrottle 是 phase 3 候选 |
| pgboss / solid_queue / oban / procrastinate | Postgres 即队列：`SELECT … FOR UPDATE SKIP LOCKED` 认领任务 | **DB-as-queue 是我们已有形态**（ops.pending_*_v1），认领原语现成 |
| vllm（GPU 侧） | continuous batching：并发请求在服务端动态合批 | 客户端只需并发发请求，**动态合批发生在 GPU 服务端**——这就是"动态"的正解 |
| Celery/Redis、Airflow/Dagster | 外部 broker / DAG 编排 | **否决**：单机单服务引入外部基础设施纯负担 |

结论：本服务的 DB 队列视图 + 文档级 advisory 锁（worker/locks.py DOC_NS，
register/parse-finish/publish 三事务内已注入）= 业界 DB-as-queue 模式的现成实现，
"笨"的只是执行壳（固定批量 + 阶段串行 + 2h 节拍），不是架构。

## 分阶段方案

**Phase 1（本轮已实现）：解析链有界并发。**
`WORKER_PARSE_CONCURRENCY`（默认 1=旧行为，上限 16）——parse→build→publish 以
"每文档一条链"提交线程池（Miniflux 模式）。安全性：每任务独立 parser（无共享
_version_cache）、每写各自 UoW、文档级 xact 锁已有、report 只在主线程折叠、
屏障单测证明真并行。GPU http-client 场景下本地只是 HTTP 等待，服务端 vllm 合批。

**Phase 2（需要时再做）：阶段解耦为常驻循环。**
单进程内三条循环线程：采集环（sync 按配额+到期 / download 按令牌桶持续排水）、
K 条解析链 worker（SKIP LOCKED 或沿用 advisory 锁认领）、收尾环。launchd 从
StartInterval 改 KeepAlive + worker-loop。触发条件：稳态运行后仍出现"下载等解析/
解析等下载"的空转证据。

**Phase 3（候选）：Scrapy 式自适应并发**——按 GPU 服务端延迟动态调 K；仅当 5080
出现过载/排队证据才值得。

**否决项**：外部队列 broker（Celery/Redis/RQ）、Airflow/Dagster 编排、
每阶段独立进程池（三个进程抢一个 1QPS 桶需要跨进程限速器，收益不抵复杂度）。

## 防爆炸边界

并发≤16（settings 硬顶）；每链一个 MinerU 子进程或一个 HTTP 请求，内存 O(K)；
下载仍单线程过令牌桶（对巨潮恒定 1QPS，与 K 无关）；回压沿用
DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS；单例锁保证全局只有一个 worker 进程。

## 实战教训：2026-07-12 GPU OOM 事件（首个排空轮）

现象：44 篇在 2 分钟内齐刷 connection reset。初判"新连接风暴"是错的——Windows 侧
日志给出完整因果：文档级并发 8 × MinerU 默认页面窗口 64 = 服务端瞬时 ~255 并发序列
→ KV cache 97.7%（叠加 NBA 2K 抢显存）→ CUDA OOM → vLLM EngineCore 死亡 →
connection reset 只是尸体现象 → restart:always 73 秒自愈，44 篇 retryable 自动重排。

结论：**流控必须双侧**——文档级并发（当时为 8）只是外层，真正的请求单位是页面窗口。
当时的事故缓解是客户端 `MINERU_PROCESSING_WINDOW_SIZE=16`；服务端重启候选为
`--max-num-seqs 128`，若仍 OOM 再评估 memory utilization。它们不是仓库永久默认值，启用前
必须按当前版本、GPU 占用和 workload 复核。事故时 GPU 利用率 93–100%，说明不能靠盲目提高
文档并发解决吞吐；网络直连优化应排在显存安全之后。运行批任务时也必须避免其他显存竞争负载。
