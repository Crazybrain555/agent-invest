# 私有 agent API 与显式处理请求（disclosure-processing-request.v1）

状态：候选实现，未部署。线协议权威副本：运行报告根 `backend-api-contract.md`（r3，线形状与 r2 一致）；生成契约：
`contracts/agent_api.openapi.yaml`。本文只定边界与不变量。

## 1. 范围与进程

- 独立 loopback 进程 `disclosure_anchor.agent_api:create_agent_api_app`（`make agent-api`，默认
  `127.0.0.1:8712`），与公共 Filing API（`main.create_app`，8711，无鉴权）分离；后者不挂这些路由。
- 原样复用既有只读路由（documents/units/filings/tracked/changes/health），另加：
  `POST /v1/processing-requests`、`GET /v1/processing-requests/{id}`、`GET .../{id}/wait`（≤20s）、
  `GET /v1/documents/{id}/text`（quick native text），以及其他作者提供并由本 app 组合的
  `GET /v1/disclosure-inventory`、`GET /v1/documents/{id}/download-link`。
- AWS 侧只做 MCP/HTTP 转发：无队列、无缓存、无签名、无终态推导。

## 2. 鉴权与传输

- 两个令牌两个作用域：`DISCLOSURE_AGENT_API_READ_TOKEN` 只放行 GET；`..._REQUEST_TOKEN` 只放行 POST。
  缺失、短于 32 字符或两者相同则拒绝启动。比较恒定时间。
- ASGI guard 在解析 body 之前裁决：仅 loopback peer；带 `Origin` 拒绝；`Host` 必须在
  `DISCLOSURE_AGENT_API_ALLOWED_HOSTS`；重复的安全头视为无效；POST 必须声明 ≤16 KiB 的 Content-Length。
- uvicorn 关闭 access log 与 proxy headers；应用日志只记方法、路由模板、状态、耗时，不记 query/header/body。

## 3. 请求身份与持久化（0066，`disclosure_ops`）

- `processing_request`：每个 `idempotency_key` 一行，绑定规范化 intent 的 `intent_sha256`（key 不在 intent 内）。
  同 key 同 intent → 返回同一请求（200）；同 key 异 intent → 409；并发同 key 由 xact advisory lock + 唯一约束收敛。
  新请求受 24h 滚动上限约束；replay 不计数。
- `processing_request_attempt`：公告定位/发现尝试历史（不是 PDF 解析尝试）。running→finished 一次性转换；`lease_expires_at` 为执行者租约，
  `next_attempt_at` 为下次到期时间；同一请求至多一个未结束尝试（部分唯一索引）。租约就是结论授权：
  `abandoned` 当且仅当 `finished_at >= lease_expires_at`，其余结论只能在租约内落库（CHECK 与仓储 UPDATE
  条件双重约束）。CHECK 每个子句都 NULL 安全（UNKNOWN 会让 CHECK 通过）：failed/deferred/abandoned 必带
  error 对象，ambiguous 必带 choices 数组，未结束尝试不得带 `next_attempt_at`。`choices`/`error` 是
  `JSONB(none_as_null=True)`，Python `None` 写成 SQL NULL，不是 JSON `null`。
- `processing_request_target`：请求最终绑定的唯一 filing（每请求一行，只写一次）。`admit_download`/`admit_parse`
  是队列谓词读取的唯一入口。
- 三表只授予 app role（SELECT/INSERT，attempt 另有 UPDATE）；reader/L2 role 无权限。状态不落库，每次读取由事实推导。

## 4. 公告定位/发现（discovery）归 worker

- POST 只做数据库工作：`document` 目标与本地已知的 `provider_document`（已登记文档或已持久化候选）在同一事务内绑定；
  其余请求以 pending 落库并立即返回。POST 与 GET/wait 均不调用 provider。
- resident worker 的既有 maintenance 获取泵（`run_once` acquisition pump）在 sync/download 之前跑
  `_request_discovery_stage`：每轮至多 `WORKER_BATCH_REQUEST_DISCOVERY` 个到期请求；无到期请求时不创建 provider client。
  drain_existing 不启动 maintenance；public stop / operator stop 经 `should_stop` 生效；provider 拒绝映射到既有
  `sync_rate_limited` / `sync_quota_break` / `source_outage_break` 冷却标志。worker 不在时请求保持 pending，不伪造进行中。
- 单次尝试：先 claim（key 锁内重检、过期租约记为 abandoned 并按退避排期）；逐 ≤30 日块请求（与 CNINFO 适配器块一致），
  每块之前、以及最后一次 provider 调用返回之后都检查 stop 与 300s deadline（租约 900s）；命中则 `deferred`，
  已取回的观察丢弃不绑定。未完整遍历窗口绝不得出 `not_found` 或唯一命中。
- 结论授权：每个结论（命中、歧义、未找到、失败、延期）都在请求 key 锁内复核“本尝试仍是最新未结束尝试、
  租约未到期、尚无 target”，并由带租约条件的 UPDATE 再核一次。失去授权时该事务里写下的 profile/index
  观察、主体与 target 全部回滚，过期租约改走与 reclaim 相同的 abandon 路径（保留历史与退避）。
- 窗口完整性依赖 source 自己的完整性证明：API 通道核对 count/total；网页通道要求每页都有 `announcements`
  （数组或 null）与布尔 `hasMore`，`hasMore=true` 的空页或只重复已见记录的页记 `incomplete_response`，
  畸形信封记 `invalid_response_shape`（均 retryable，按既有 provider 基础设施错误归为 outage/deferred）。
  这同样阻止普通 sync 把截断结果当成完整窗口推进 checkpoint。
- 唯一命中的 CNINFO id 若已有本地行（含发现期间新登记的行），只有证券、交易所与公告日窗口都与请求一致
  （与 POST 路径同一谓词）且原件已归档才绑定该 document；否则记 `failed`（`local_filing_mismatch` /
  `local_original_missing`，不可重试），error 与 index 观察的 `selection.local_conflict` 同时保留本地事实
  （found）与 provider 观察（observed），快照不含可获取候选。歧义 choices 只为一致的本地行给出 document_id。
- 失败分类：item 失败（provider item 错误、主体解析拒绝、租约过期）最多 3 次；provider 拒绝、stop、deadline 记 `deferred`，
  不耗 item 预算；总尝试 ≤10。退避 5min×3ⁿ，上限 6h；stop 后立即可再到期。每次尝试保留类型化错误、
  coverage（完成/总块数）与 profile/index source_access 来源。
- 成功：只把选中的候选写入 index snapshot 的 `candidates`，其余 provider 行以 `selection` 汇总（规则、行数、匹配数、
  choices），因此不会扩大获取范围；`query_params` 与普通 sync 同形；provider_interface 与 worker 自身 sync 记录一致
  （`cninfo:p_info3015`）。歧义（>1）返回 ≤20 个 choices 且不绑定；用户以新 key 提交 `provider_document` 选择。
- 不跟踪公司、不改 tracked 行或覆盖、不推进 checkpoint；主体解析可创建 company/security 身份行（与下载登记同理），
  不构成股票池成员关系。

## 5. 队列准入（唯一谓词）

- `pending_parse` / `pending_parse_backlog_count`：普通门（活跃公司 + 分类范围，含 carrier 与 title_noise）与
  `processing_request_target` 精确准入取 OR；准入按 `document_id + raw_file_hash`，或在下载前按 provider 身份。
  重试资格（retryable 闩、契约失败门、item/charged 预算）、running-run 排除、published/parsed 状态排除、
  frozen campaign 的 `document_ids` allowlist 全部在 OR 之外，保持不变。
- staged-v4 候选源（latest/page）、locked H0 ingress 复查、source rejection 复查都调用同一 `pending_parse`，
  因此不会出现 page 选中而 H0 拒绝。有限 pass 的 cursor/ceiling 语义不变，旧 ID 在下一 pass 可见（独立审查 B1–B3）。
- `pending_downloads` / `pending_download_count`：普通池门 + 分类范围与精确下载准入取 OR；准入只覆盖尚未登记候选的首次获取；
  失败计数上限与非重试闩不变。新增 `provider_document_ids` 仅供状态读取精确提问。
- 已 published 不重排；running/queued 直接加入既有职责；耗尽或契约失败报告 `parse_blocked`，从不重置。

## 6. 状态与等待

- `status.state` / `terminal` / `next_action` 由 `derive_status` 从事实纯函数推导；解析中恒为 `wait`。
  `*_queued` 表示 worker 自己的谓词此刻选中，不代表 worker 正在运行。`archived` 只表示原件已登记，不代表文本可提取。
- wait：1–20s，1s 轮询新快照，状态哈希变化或终态即返回；超时不取消任何事。

## 7. Quick text

- 由 `ReadQuickDocumentText` 读取文档自身记录的 relpath/raw hash，root 为 `FileStorePathBuilder.data_root()`
  （`disclosure_data_root/data`），调用已独立 GO 的 quick-text 抽取器；逐页 code-point 偏移、cursor 续读、
  `no_extractable_text` 不等于空白/扫描；无 OCR、无写入、无排队。并发子进程上限 `DISCLOSURE_AGENT_API_TEXT_CONCURRENCY`。

## 8. 已知限制

- 下载顺序沿用普通队列（announcement_date 升序），无优先级；无“下载中”状态。
- discovery 与 worker 的 sync 共享同一进程的 CNINFO 客户端与限速；API 进程不持 provider 凭据。
- 状态读取使用 API 进程加载的处理策略；若 worker 未重启而策略已改，二者可能短暂不一致。
- 网页通道的分页信封规则来自既有实测夹具与两个开源实现（`tr1s7an/CnInfoReports@bc5ae6b`、
  `akfamily/akshare@fac1e50`），未做在线 provider 复核；`totalAnnouncement` 未用作完整性核对（无实测证据
  证明其等于 code,orgId 查询可翻页记录数）。若 CNINFO 某页确实缺 `hasMore`，表现为可见的
  `invalid_response_shape`/outage 冷却，而不是静默截断。
- 部署须联合完成 0066 迁移、worker 身份升级与私有 API/MCP 接入，均未执行。
  新队列谓词改变 writer 指纹，不能简单重启并复用旧身份材料；依照
  `../runbooks/production-operations.md` §1.1j 与 §1.1g，保留全部未结职责并完成适用 U01 审阅与预检。
