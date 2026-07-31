# application — 编排层（ports 定义边界，services 复用核心，use_cases 组合）

```text
ports/         抽象接口：UnitOfWork(默认回滚、显式 commit)、各 Repository、
               DocumentParserPort(parse + identity)、SourceEvidenceValidatorPort
               （provider artifact 校验）、file_store（路径/原始档/artifact）
services/subject_resolver.py   D5 主体解析顺序：security 命中 → USCC 强键查 ledger → 新建；
               legal_name 只校验不合并；冲突置 contested（resolver 内 commit 后抛——
               让 contested 标记在注册回滚后仍持久，这是刻意的）
services/register_document.py  注册核心：去重键(provider,pid,raw_hash)幂等吸收、
               supersedes 链、source_access、document_registered/observed 事件；
               07 的 provider 下载路径必须复用它，不得重实现
services/unit_builder/  纯 S1-S7 证据投影与只读 retrieval taxonomy；不做 IO，不反向依赖 adapter，
               taxonomy 只能标注已完成单元，不能改变 title/边界/归属/顺序
use_cases/register_local_pdf.py  preflight 冲突检查 → 归档 → 核心；竞态只重试一次
worker/queries.py     队列读取唯一入口（视图 facts + 阈值谓词都在这；worker/doctor 共用，
               禁止旁路手写判定）；reclaim_stale_runs 是钉死的回收 UPDATE
worker/worker.py      run_once 是 count-bounded 运维调度壳；production resident 由常驻
               parse coordinator 持续从 PG 补槽，acquisition/project 在 maintenance
               thread 独立运行，周期 report 只转移已封存对象、不排空 future；stale 仅在
               singleton 首次准入前回收；解析按文档大小三 lane 配额/借用，
               WORKER_PARSE_CONCURRENCY 只计 GPU-producing parse；build/publish 释放该槽后
               进入唯一有界 finalize owner，新 run 与周期 crash-leftover 补漏按 run id
               排重（每任务独立 parser/UoW，report 只在 coordinator 线程折叠）；
               worker/locks.py 定义 WORKER_NS=815001/DOC_NS=815002 与文档级 xact 锁
               （register 复用/parse finish/publish 三事务内注入，内存 fake 无 session 自动跳过）
dto/worker_report.py  WorkerLimits/WorkerReport/WorkerFailure（08 报告契约）
use_cases/parse_document.py      run 生命周期：prepare(落 run+created 事件) → 真解析 →
               finish（同事务更新 document.status；published 永不降级；
               typed 异常 → 结构化 error{stage,error_code,retryable}；未知异常持久化后 re-raise）
```

规范：use case 不直接读 settings（构造参数注入）；DB 写全部经 UoW 单事务；
事件只经 domain/entities/outbox_events.py 工厂构造。
