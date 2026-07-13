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

# Worker 动态调度设计（round22g；2026-07-13 落地修订）

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

## 已落地方案

**Phase 1：解析链有界并发。** `parse→build→publish` 仍是每文档一个不可拆的动作，
由有界线程池并行；每任务独立 parser/UoW，report 在主线程折叠。用户裁决与 round22h
OOM 证据把 `WORKER_PARSE_CONCURRENCY` 的 settings 硬顶从 16 收紧为 **8**；目标机器在
`*-http-client` + server URL 成对配置时取 8，仓内 template/类默认均为 1，settings 对本地
backend 的并发 >1 直接拒绝启动。

**Phase 2：常驻自适应轮询（2026-07-13 用户裁决，已推翻“等 idle-wait 证据”）。**
没有引入三套阶段线程或外部队列，而是复用经过验证的 `run_once`：

```text
有 sync/download/parse/build/publish 进展  -> 下一轮立即开始（sleep=0）
完全空闲                                  -> sleep 900s，再空闲 sleep 1800s 封顶
单项失败且无进展                          -> 60s 指数退避，封顶 1800s
轮级系统异常                              -> 写失败 report，60s 指数退避后继续
CNINFO quota_break                        -> 仅 sync 冷却 30m→60m→120m；
                                             先让 download/parse 立即排水一轮
CNINFO retryable 网络/5xx/坏响应           -> sync+download 冷却 60s 指数退避；
                                             本地 parse/build/publish 继续
parser identity/GPU infra 失败             -> identity在dequeue前fail-fast；仅parse
                                             冷却120s，不消耗item retry
共享 DB/存储 build 失败                    -> 显式infra首错或unknown同码2次后停止refill；
                                             parse+build冷却，build-only探针后恢复
publish 失败                              -> parse+publish 冷却 120s 指数退避；
                                             download 可继续搬入受水位约束的队列
```

launchd 由 `StartInterval=7200 + worker once` 改为 `KeepAlive + worker loop`；每轮仍写
worker/parse-quality report。每轮开始用同一 dedicated connection 复核 singleton
advisory lock，连接或锁丢失就 fail-closed 退出，交给 launchd 重启抢锁。`once` 保留给人工
单轮诊断，拿不到锁仍 exit 0。

常驻进程会缓存 Python 代码、processing policy、filing rule bundle 和 CNINFO client。以后
改 checkout/config/env、执行 `make load-rules` 后，必须 `launchctl kickstart -k` 对应 job
（或 unload 后重装）并再跑 doctor；不能等待旧 2h tick 自动加载。首装流程因先停 worker 再
migrate/load/install，天然满足这一点。

选择这一实现而不是 300 秒 `StartInterval`：200 家点估计约 16,031 份可解析文档，
`BATCH_PARSE=50` 需要约 321 轮；300 秒固定节拍仍平白增加约 26.7 小时，不能满足
“只受 GPU 限制”。常驻轮询只在有实际进展时零等待，不会因未知/不合规 raw queue facts
热循环。

**Phase 3（仍为候选）：** 按 GPU 服务端延迟动态调 K；仅当并发 8、window 16 下仍有
稳定过载/排队证据才评估。外部 broker、DAG 编排和阶段独立进程池继续否决。

## 防爆炸与退出边界

- 文档并发硬顶 8；`MINERU_PROCESSING_WINDOW_SIZE=16` 是当前 GPU 红线，二者不可由
  本调度器自动上调。
- 下载仍单线程经过**进程生命周期共享**的 CNINFO client、token cache 与 1QPS token bucket；
  零等待轮不会重置限速器。
- `DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS=2000` 保留旧名字，实际统计 pending-download
  + 所有 pending-parse/raw（含 oversized/dead-letter）总在途。每轮首次精确扫描一次，同轮
  按 `candidate_count` 保守累计，下一轮校正；单公司原子同步最多越线一次。达到水位后必须等
  GPU 排水才放下一个 never-synced 公司，因此首回补 checkpoint 不承诺 16 轮全齐。
- parse 一次最多保持 K 个 in-flight future，完成一个才补一个；SIGINT/SIGTERM 停止补充、
  取消尚未开始的 future，并终止已登记的 MinerU 子进程组。逐文档失败继续落 retryable
  run，不把常驻进程当作失败状态仓库。
- parser version 在 item dequeue 前 preflight，并由 resident 进程锁保护缓存后注入每个 fresh
  parser；version probe 自身用独立进程组、10s timeout，失败不创建 processing_run。systemic
  build 失败（显式共享故障首个，其他unknown同码需同轮2次）停止 refill，cooldown 后先以
  build-only round 验证恢复；item-local IR poison 继续隔离。
- quota 断路仍是公司窗口级：未完整同步不推进 checkpoint，下轮从该公司窗口重做；本地
  download 进展不重置 30→60→120m quota 退避，不在冷却期空转打接口。
- `source_checkpoint.updated_at` 每次 cursor 更新显式刷新，否则所有公司会永久 `due`，
  使“动态”退化为每轮重复同步。
- 同步失败也写 `cninfo:worker_sync_failure`，失败公司冷却 60s 并排到未尝试公司之后，前 13
  个毒丸不能饿死后 187 家。singleton connection 使用 AUTOCOMMIT，避免常驻 idle transaction。
- report 文件系统异常在 loop 内转为 system backoff，不触发 KeepAlive 30s 重启风暴。

## 真实 backlog 验收（2026-07-13）

从现库只读复制 16 份已发布真实 PDF 到 scratch DB/root（16/16 进入 proposed gate，合计
1,326,079 bytes），以 production MinerU HTTP 配置、并发 8、batch 50 启动真正的
`worker loop`：

- 第 1 轮 36.340s：parsed/built/published = 16/16/16，failed=0，实测 1,585.0 docs/h；
- 第 2 轮在前轮结束后立即开始，0.017s 确认队列为空，然后打印 `sleeping 900.0s`；
- 睡眠 10s 前后 `ps` CPU time 都是 `0:00.57`，证明 idle 近零 CPU；
- SIGTERM 正常退出，16 个 active succeeded run 全部一致，scratch DB/root 自动清理。

该样本刻意选小 PDF，只证明调度不再受轮间节拍限制，不用于乐观容量外推；200 股总时长
仍采用现库 785 份大轮的 255 docs/h 与 73s/份基线。

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
