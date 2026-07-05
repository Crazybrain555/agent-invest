# api — Filing API 边界

```text
routers/health.py      服务健康与降级状态
routers/documents.py   documents_v1 与 processing_runs_v1 读端点
routers/filings.py     latest filings 集合端点
routers/units.py       document_units_v1 / source_refs_v1 / context 端点
routers/changes.py     change_events_v1 增量 feed
routers/admin.py       本地同步写端点，编排已有 use case
schemas/               public/admin/health Pydantic DTO
pagination.py          opaque keyset cursor 与 limit 校验
errors.py              public error envelope 与 contract-version guard
db.py                  request.state engine 读取
```

API 只暴露 public 契约。读端点只从 `disclosure_public.*_v1` 或等价派生查询返回数据；
不要把 `disclosure_core`/`disclosure_ops` 表结构、SQLAlchemy model、绝对路径、堆栈、
MinerU raw JSON、parser block、bbox、page internals 暴露到响应体。

读 DTO 规则：字段同 public view 列名同义；`document_unit` API 派生字段全集仅为
`asset_uri` 与 `is_active_run`。这两个字段不入库、不进 public view；contract 导出与测试用
DERIVED 白名单处理。新增或改名 public 字段必须同步 schema/export、contract 测试与
`docs/implementation/checks/contract-checklist.md`。

错误模型固定为统一 envelope，业务错误码全集：

```text
NOT_FOUND
GONE_SUPERSEDED
L1_PROCESSING_REQUIRED
CONTRACT_VERSION_MISMATCH
VALIDATION_ERROR
```

`X-Contract-Version` 当前支持集固定为 `{"v1"}`。坏 cursor、limit 超上限、非法过滤值走
`VALIDATION_ERROR`；superseded reject 只在 document detail 与 document units 声明的
参数上生效。

DB 拓扑：read routers 使用 `reader_db_engine`（来自 `DISCLOSURE_READER_DATABASE_URL`，
缺省回落 `DATABASE_URL`）；admin router 使用 `app_db_engine`（app 角色 `DATABASE_URL`）。
不要让读端点依赖 admin/app engine 的写权限。

admin 端点仅供本地运维同步执行；请求/响应体跟 use case 契约对齐。`quarantined_path` 只返回
basename。不要在 API 层新增鉴权、多租户、MCP 包装、全文/向量检索或 retrieval projection。
