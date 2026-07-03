---
id: disclosure_anchor_service_framework
project: disclosure_anchor
title: disclosure_anchor 本地单机服务框架实施方案 v1.0
status: final-for-implementation
layer: L1
layer_name: 披露文件接入与结构化准备层
service_type: local_single_machine_independent_service
architecture_style: modular_monolith_ports_adapters
runtime_target: macOS Apple Silicon + Samsung 990 PRO 4TB at /Volumes/AgentSSD
postgres_mode: native_brew_pgdata_on_external_ssd
mineru_mode: native_macos_batch_worker
pg_layout: single_cluster_multiple_databases
created_at: 2026-06-26
---

# disclosure_anchor 本地单机服务框架实施方案 v1.0

## 0. 已锁定决策

本服务按以下形态实施：

```text
disclosure_anchor
= 本地单机、独立、自包含的 L1 披露文件服务
= FastAPI 原生进程
+ native PostgreSQL
+ 原生 macOS MinerU batch worker
+ 文件系统工件库
+ 模块化单体 ports/adapters
```

已锁定，不再让实施 agent 选择：

| 决策项 | 采用方案 |
|---|---|
| PostgreSQL | Homebrew/native PostgreSQL（`postgresql@18`），`PGDATA` 指向 `/Volumes/AgentSSD/agent_system/postgres/pg18-main` |
| PG 启动约束 | localhost-only TCP + AgentSSD socket：`port=55432` / `listen_addresses='localhost'` / `unix_socket_directories=.../sockets` 已固化在 `postgresql.conf`；唯一合法启动 `pg_ctl -D <PGDATA>`，**禁止** `brew services start postgresql@18`（指向内置盘默认 cluster） |
| MinerU | 原生 macOS batch worker 调用 MinerU，不放入 FastAPI 进程，不优先容器化 |
| PG 多服务布局 | 单个 PostgreSQL cluster；未来每个兄弟服务独立 database；本服务库内分 private/public/ops schema |
| 服务形态 | FastAPI + worker + PostgreSQL + 文件系统；不拆微服务 |
| 调度形态 | v1 使用 DB 状态 + worker 扫描 + `make worker-once/worker-loop`；不引入 Celery/Redis/Kafka/Airflow |

本服务只负责 L1 披露文件接入与结构化准备。claim、证据账本、假设账本、预测快照、调度脊柱都属于未来兄弟服务。

---

## 1. 服务定位

一句话定义：

> `disclosure_anchor` 是投研预测引擎 L1 中负责交易所披露文件的接入、归档、解析、结构切分、A 类确定性清洗和 L2-ready document units 发布的独立服务。

处理链：

```text
CNINFO / 交易所 / 本地 PDF
→ source_access
→ document
→ raw PDF immutable archive
→ MinerU parser artifact
→ parser-neutral normalized IR
→ A 类确定性清洗
→ document_unit(text/table/qa)
→ active processing_run
→ Filing API / public read view / source_ref / change_event
```

### 1.1 范围内

本服务做：

1. 维护 `tracked_companies` 对应的披露文件获取范围。
2. 增量同步公告索引。
3. 下载 PDF，保存原始 bytes 与 hash。
4. 登记 `source_access`。
5. 建立 `document`。
6. 调用 MinerU 解析 PDF。
7. 保存 parser artifacts。
8. 将 MinerU 输出转成 parser-neutral `NormalizedIR`。
9. 做 A 类确定性清洗。
10. 按标题树、完整表格、完整问答生成 `document_unit`。
11. 标记质量状态。
12. 发布 active `processing_run`。
13. 对下游提供 Filing API、public read views、source_ref 和 change feed。

### 1.2 范围外

本服务不做：

```text
claim 抽取
事件 canonical 化
metric normalization
披露锚对账
冲突裁决
证据账本
假设账本
预测快照
口径契约执行
L6 调度脊柱
向量数据库 / RAG chunk
标准财务数据 provider 全库镜像
Wind / iFinD / Choice / Tushare 标准数据仓库
```

这些对象和能力属于未来兄弟服务，不进入本仓库。

---

## 2. 对外契约

本服务对外只发布以下对象：

| 对象 | 用途 | 典型消费者 |
|---|---|---|
| `document` | 一份披露文件版本的元数据、来源、raw hash、状态 | L2、人工工具 |
| `document_unit` | L2 可直接消费的 text/table/qa 结构单元 | L2 |
| `processing_run` | 一次解析、清洗、切分运行版本 | L2、复盘、人工诊断 |
| `source_ref` | 下游 claim 可引用的披露来源锚 | L2/L3 |
| `change_event` | 新增、重跑、内容变化、active run 切换 | L2/L6 |

对外契约不暴露：

```text
MinerU raw JSON
SQLAlchemy model
private schema 表结构
绝对文件路径
parser block / table cell / page bbox
密钥
内部错误堆栈
```

### 2.1 G0 追溯锚

披露侧 G0 由以下组合构成：

```text
raw PDF bytes
+ raw_file_hash
+ source_access_id
+ document_id
+ processing_run_id
+ asset_id
+ unit content_hash
+ exact payload snapshot
```

page / bbox / 坐标可以作为 parser artifact 中的复核信息，但不是本服务的核心身份锚。

### 2.2 source_ref v1

source_ref 逻辑格式：

```json
{
  "source_type": "disclosure_document_unit",
  "service": "disclosure_anchor",
  "contract_version": "source_ref.v1",
  "source_access_id": "sa_01J...",
  "document_id": "doc_01J...",
  "provider": "cninfo",
  "provider_document_id": "1225087169",
  "raw_file_hash": "sha256:...",
  "processing_run_id": "run_01J...",
  "asset_id": "du_01J...",
  "payload_kind": "table",
  "heading_path": ["第八节 财务报告", "财务报表附注", "应收账款", "按账龄披露"],
  "title": "应收账款按账龄披露",
  "unit_content_hash": "sha256:...",
  "quality_status": "ok",
  "artifact_locator": {
    "artifact_kind": "normalized_ir",
    "artifact_unit_ref": "table-312",
    "order_index": 312
  }
}
```

L2 claim 应保存：

```text
source_ref
+ exact snapshot 或 snapshot_hash
+ L2 自己引用的派生视图版本，如有
```

---

## 3. 代码架构

采用：

```text
FastAPI + SQLAlchemy/Alembic + Pydantic Settings + ports/adapters + use cases
```

依赖方向：

```text
api / cli / worker
  → application
  → domain

adapters
  → application ports
  → domain
```

禁止：

```text
domain → FastAPI / SQLAlchemy / MinerU / CNINFO / filesystem path
application → concrete adapters
兄弟服务 → disclosure_anchor private Python modules
```

### 3.1 仓库目录

代码仓库放内置盘：

```text
/Users/zhang/dev/agent-invest/services/disclosure_anchor/
```

推荐结构：

```text
disclosure_anchor/
  pyproject.toml
  README.md
  Makefile
  alembic.ini
  .env.example

  docs/
    implementation/
    operations/
    api/

  contracts/
    filing_api.openapi.yaml
    public_models/
      document.v1.json
      document_unit.v1.json
      processing_run.v1.json
      source_ref.v1.json
      change_event.v1.json
    normalized_ir/
      normalized_ir.v1.json

  config/
    disclosure_anchor.default.yaml
    filing_type_rules/
    semantic_keys/
    quality_rules/

  src/disclosure_anchor/
    main.py
    settings.py
    bootstrap.py

    api/
      routers/
      schemas/

    domain/
      ids.py
      entities/
      value_objects/
      ir/
      services/
      errors.py

    application/
      ports/
        repositories.py
        file_store.py
        parser.py
        disclosure_source.py
        unit_of_work.py
        clock.py
        change_publisher.py
        lock.py
      use_cases/
      commands/
      dto/

    adapters/
      db/postgres/
        connection.py
        models.py
        repositories.py
        unit_of_work.py
        migrations/versions/
      storage/
        filesystem_store.py
        path_builder.py
        atomic_write.py
      parsers/mineru/
        mineru_adapter.py
        mineru_process.py
        artifact_reader.py
        mapper_to_ir.py
      sources/cninfo/
        client.py
        mapper.py
        rate_limit.py
        retry.py
      publisher/
        pg_outbox.py
      runtime/
        logging.py
        metrics.py
        locks.py
        doctor.py

    cli/
    worker/

  tests/
    unit/
    integration/
    contract/
    fixtures/
```

### 3.2 关键边界

- `domain/ir/normalized_ir.py` 是领域代码唯一读取的 parser-neutral IR。
- `contracts/normalized_ir/normalized_ir.v1.json` 是落盘 derived artifact 的版本化格式。
- 领域代码不读取 MinerU JSON。
- `FileStorePathBuilder` 是唯一生成 raw/parser/derived 路径的组件。
- `UnitOfWork` 是 use case 的事务边界。
- 内部 ID 使用 ULID 或 UUIDv7；provider ID 只作为 `ProviderRef`。
- `settings.py` 是运行配置入口；业务代码不直接读取 `.env` 文件或 `os.environ`。
- CNINFO 凭据只允许通过环境变量注入到 settings，不进入 domain、DB、artifact、日志或 tracked repo 文件。

### 3.3 CNINFO adapter 与凭据边界

CNINFO 属于 source adapter，不属于 domain：

```text
application/ports/disclosure_source.py
  定义 DisclosureSourcePort

adapters/sources/cninfo/
  client.py      # token、HTTP request、result envelope
  mapper.py      # CNINFO 字段 → provider-neutral DTO
  rate_limit.py
  retry.py
```

未来 `settings.py` 读取：

```text
CNINFO_ACCESS_KEY
CNINFO_ACCESS_SECRET
CNINFO_ACCESS_TOKEN   # optional；优先用 key/secret 刷新 token
```

真实值放在仓库外，例如：

```text
~/.config/disclosure_anchor/cninfo.env
```

运行前由 shell、launch wrapper、Keychain/secret provider 或外置盘私有 config 注入环境变量。本仓库只保留变量名和占位符。

接口字段、API code、token endpoint、返回 envelope 和公告字段映射以以下资料为准：

```text
docs/architecture/cninfo-webapi-usage-reference.md
docs/architecture/cninfo-interfaces.schema.json
docs/巨潮api.md
```

---

## 4. 部署拓扑

### 4.1 进程拓扑

```text
macOS host / Apple Silicon

├─ PostgreSQL native process
│  ├─ PGDATA: /Volumes/AgentSSD/agent_system/postgres/pg18-main
│  ├─ socket: /Volumes/AgentSSD/agent_system/postgres/sockets （:55432）
│  ├─ localhost TCP: 127.0.0.1:55432 / ::1:55432（不监听局域网）
│  └─ logs:   /Volumes/AgentSSD/agent_system/postgres/logs
│
├─ disclosure-api
│  ├─ FastAPI / uvicorn
│  ├─ 127.0.0.1:8711
│  └─ 查询、注册、触发、状态查看
│
├─ disclosure-worker
│  ├─ sync worker
│  ├─ download worker
│  ├─ parse worker
│  └─ publish worker
│
└─ MinerU native subprocess / native Python call
   ├─ pipeline backend: CPU 可跑
   └─ VLM/MLX backend: Apple Silicon 加速路径保留
```

FastAPI 不加载 MinerU 模型。

### 4.2 内置盘与外置盘

代码放内置盘：

```text
/Users/zhang/dev/agent-invest/services/disclosure_anchor/
```

所有持久化数据和运行态放外置盘：

```text
/Volumes/AgentSSD/agent_system/
  README.md
  MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL

  config/
    disclosure_anchor.env
    postgres.env

  postgres/
    pg18-main/
    sockets/
    logs/

  shared/
    model_cache/
      mineru/
      huggingface/
      modelscope/
    staging/
      inbox/
      downloads/
      manual_uploads/
    tmp/

  services/
    disclosure_anchor/
      data/
      runtime/

    future_l2_ingest/
      data/
      runtime/

    future_evidence_ledger/
      data/
      runtime/

    future_prediction_core/
      data/
      runtime/
```

`agent_system` 是多服务体系根，不是 `disclosure_anchor` 私有根。

### 4.3 本服务数据目录

```text
/Volumes/AgentSSD/agent_system/services/disclosure_anchor/
  data/
    raw_documents/
    parser_artifacts/
    derived/
      normalized_ir/
      document_unit_snapshots/
      exports/

  runtime/
    inbox/
    quarantine/
    failed/
    tmp/
    locks/
    pid/
    logs/
    reports/
      doctor/
      parse_quality/
```

规则：

1. `raw_documents` 只追加，不覆盖。
2. `parser_artifacts` 按 run 版本化。
3. `derived/normalized_ir` 是本服务 parser-neutral 派生。
4. `document_unit_snapshots` 保存每次 run 的 unit 快照。
5. `runtime/inbox` 是入口，不是长期归档。
6. `runtime/quarantine` 存异常输入，不进入 G0 raw archive。
7. `runtime/tmp` 可清理，不作为数据资产。

---

## 5. PostgreSQL 布局

当前只创建一个 cluster：

```text
/Volumes/AgentSSD/agent_system/postgres/pg18-main
```

当前只创建一个 database：

```text
disclosure_anchor
```

未来兄弟服务使用独立 database：

```text
future_l2_ingest
future_evidence_ledger
future_prediction_core
future_scheduler_spine
```

本服务 database 内 schema：

```text
disclosure_core      私有业务表，本服务写入
disclosure_public    只读对外 view
disclosure_ops       运行状态、doctor 报告、outbox、错误队列
```

角色：

```text
disclosure_owner      owns schema and migrations
disclosure_app        app read/write private schema
disclosure_reader     read-only public views
future_l2_reader      read-only disclosure_public views
```

权限原则：

```text
disclosure_app 可写 disclosure_core / disclosure_ops
兄弟服务只能读 disclosure_public
不跨服务建硬 FK
跨服务引用用 source_ref
```

---

## 6. 数据架构

### 6.1 本服务拥有

```text
company
security
tracked_company
source_access
source_checkpoint
document
processing_run
document_unit
outbox_event / change_event
raw_documents 文件
parser_artifacts 文件
derived/normalized_ir 文件
document_unit_snapshots 文件
```

### 6.2 本服务不拥有

```text
claim
event fact
metric observation
metric normalization
reconciliation
adjudication
evidence ledger
assumption ledger
forecast node
forecast snapshot
rule artifact
framework entry
scheduler lineage graph
human todo
M-LOG 全局执行日志
```

### 6.3 核心对象

`source_access` 记录一次访问或获取行为，包括查空。

`document` 对应一份具体披露文件版本。更正公告、新 hash、不同 provider 文件版本都不覆盖旧 document。

`processing_run` 记录一次 parse / normalize / build_units / publish 运行。历史 run 不回改。

`document_unit` 保存某个 run 生成的 text/table/qa 单元。`payload` 保存快照本身，不只是 locator。

`outbox_event` 提供 `seq`，供 L2 增量读取。

### 6.4 hash 策略

| hash | 对象 | 用途 |
|---|---|---|
| `raw_file_hash` | 原始 PDF bytes | G0 原文锚 |
| `artifact_hash` | parser artifact / normalized IR | parser 输出可复现 |
| `content_hash` | unit canonical payload | 判断 L2 是否重处理 |
| `structure_hash` | heading_path、payload_kind、order、边界结构 | 判断结构变化 |
| `content_hash_aggregate` | run 下所有 unit content_hash 聚合 | 判断 run 内容整体变化 |

触发规则：

```text
raw_file_hash 变化
  → 新文件版本，必须重跑

unit content_hash 变化
  → L2 应重新处理该 unit

structure_hash 变化但 content_hash 不变
  → 记录结构变化，通常不触发 L3

parser artifact 变化但 unit content_hash 不变
  → 不触发 L2，只记录 run
```

---

## 7. API、public views 与 change feed

### 7.1 API

v1 只监听本机：

```text
127.0.0.1:8711
```

最小 endpoint：

```text
GET  /v1/health
GET  /v1/documents
GET  /v1/documents/{document_id}
GET  /v1/documents/{document_id}/runs
GET  /v1/documents/{document_id}/units
GET  /v1/units/{asset_id}
GET  /v1/units/{asset_id}/source-ref
GET  /v1/filings/latest?security_code=002484&period=2025A&filing_type=annual_report
GET  /v1/changes?after_seq=12345&limit=500
POST /v1/admin/documents/register-local-pdf
POST /v1/admin/documents/{document_id}/parse
POST /v1/admin/runs/{processing_run_id}/publish
```

### 7.2 Public views

```text
disclosure_public.documents_v1
disclosure_public.document_units_v1
disclosure_public.processing_runs_v1
disclosure_public.source_refs_v1
disclosure_public.change_events_v1
```

兄弟服务只能用只读账号访问 public schema。

### 7.3 Change feed

v1 不上 Kafka。使用 PG outbox + polling：

```text
disclosure_core.outbox_event
→ disclosure_public.change_events_v1
→ GET /v1/changes?after_seq=...
```

---

## 8. MinerU 集成

采用原生 macOS worker：

```text
disclosure-worker
→ native Python venv
→ MinerU CLI / Python API
→ output to parser_artifacts
→ adapter maps to NormalizedIR
```

模型缓存：

```bash
MINERU_MODEL_CACHE=/Volumes/AgentSSD/agent_system/shared/model_cache/mineru
HF_HOME=/Volumes/AgentSSD/agent_system/shared/model_cache/huggingface
MODELSCOPE_CACHE=/Volumes/AgentSSD/agent_system/shared/model_cache/modelscope
```

Phase 0 可以每份文档 subprocess。v1 使用 batch worker。只有在频繁全历史重解析且冷启动成为主要耗时时，再切换为常驻 local HTTP MinerU service。

---

## 9. A 类确定性清洗

L1 只做 A 类确定性清洗：

```text
页眉页脚
页码
目录
签署页
固定法律声明
可稳定识别且基本不含实质信息的模板噪声
```

不得在 L1 做 B 类语义判断型清洗。不得让 LLM 自由判断“是否有投资价值”。

关键约束：

- 清洗的是派生 `document_unit`，不改 raw PDF。
- 不确定时倾向保留。
- “重要提示”“风险提示”这类可能含实质信息的板块，不能按标题整段删除。
- B 类派生视图未来由 L2 生成并引用不可变 unit。

---

## 10. 启动与运行

Makefile 最小命令：

```bash
make doctor
make pg-init
make pg-start
make pg-stop
make pg-status
make db-create
make migrate
make api
make worker-once
make worker-loop
make test-unit
make test-integration
make test-contract
```

`make doctor` 必须检查：

```text
/Volumes/AgentSSD 是否挂载
MOUNT_SENTINEL 是否存在
DATA_ROOT 是否可写
PG localhost TCP 或 AgentSSD socket 是否可连接
DB migration 是否最新
raw_documents 是否可读写
parser_artifacts 是否可读写
model cache 是否指向外置盘
是否存在 active run 冲突
DB raw hash 是否与文件 hash 一致
artifact_locator 是否可达
```

如果外置盘没挂载，程序必须拒绝启动。

---

## 11. 后续调整触发条件

当前阶段不展开备选方案，但保留调整信号：

- 如果 MinerU 冷启动成为主瓶颈，再引入常驻 local HTTP MinerU service。
- 如果 public views 批量扫描慢，再加索引或 materialized view。
- 如果全文关键词检索成为核心需求，再加 PostgreSQL full text，不先上向量库。
- 如果兄弟服务数量变多且权限/生命周期复杂，再重新评估 PG cluster / database 策略。
- 如果进入稳定生产，再另行补备份、恢复、离线存储策略。

---

## 12. 最终收口

`disclosure_anchor` v1 的正确形态是：

```text
可靠保存披露原文
+ 版本化 parser artifacts
+ parser-neutral normalized IR
+ L2-ready document_unit
+ 可追溯 source_ref
+ 可轮询 change feed
```

它是未来多服务投研预测引擎中的第一个服务，必须独立、自洽、边界清楚；同时不提前承担 L2-L6 的职责。
