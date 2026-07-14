# api — Filing API 边界

```text
routers/health.py      服务健康与降级状态
routers/documents.py   documents_v1 与 processing_runs_v1 读端点
routers/filings.py     latest filings 集合端点
routers/units.py       document_units_v1 / source_refs_v1 / context 端点
routers/changes.py     change_events_v1 增量 feed
routers/tracked.py     tracked_companies_v1 读端点（股票池真源在 DB，round22；
                       effective_* 级联生效值在此层解析——全局 policy 是文件，SQL 看不见）
routers/admin.py       本地同步写端点，编排已有 use case（含 PUT/DELETE tracked-companies；
                       DELETE=出池留档，purge 走 CLI 不进 API；POST {code}/sync =
                       L6 按需取证触发，Miniflux refresh 模式，失败返回 200+failed+留痕）
schemas/               public/admin/health Pydantic DTO
pagination.py          opaque keyset cursor 与 limit 校验
errors.py              public error envelope 与 contract-version guard
db.py                  request.state engine 读取
```

API 只暴露 public 契约。读端点只从 `disclosure_public.*_v1` 或等价派生查询返回数据；
不要把 `disclosure_core`/`disclosure_ops` 表结构、SQLAlchemy model、绝对路径、堆栈、
MinerU raw JSON、parser block、bbox、page internals 暴露到响应体。

读 DTO 规则：字段同 public view 列名同义；API 派生字段全集 = `document_unit` 的
`asset_uri` + `tracked_company` 的 `effective_lookback_days` / `effective_sync_seconds` /
`effective_process_classes`（不入库、不进 public view，contract 导出与测试用 DERIVED
白名单处理）。
`is_active_run` 自迁移 0011 起由 public view 直出（round3 P1#7：DB 直读方也要能
过滤 active run），API 侧继续按视图列返回。新增或改名 public 字段必须同步 schema/export、contract 测试与
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
basename。admin 面自带静态 Bearer token + 回环双闸（用户裁决 2026-07-14，取代 round8 的
"无鉴权+默认关"立场）：挂载需要 `DISCLOSURE_ADMIN_TOKEN`（缺失 fail-closed 拒挂），
401/403/409 用 admin 专用运行期码（UNAUTHORIZED/FORBIDDEN/CONFLICT，不进公开契约枚举——
admin 不在导出契约面内）。读侧公开错误码全集仍为上方 5 个。
不要在 API 层新增多租户、MCP 包装、全文/向量检索或 retrieval projection。
