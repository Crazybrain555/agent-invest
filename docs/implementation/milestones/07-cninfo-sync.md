---
id: disclosure_anchor_milestone_07_cninfo-sync
project: disclosure_anchor
title: CNINFO 增量同步
status: ready-for-implementation
created_at: 2026-06-26
updated_at: 2026-07-04
depends_on: milestone 04R（注册核心 / SubjectResolver）、milestone 05（可选并行，publish 由 08 串联）
delivers_to: milestone 08
---

# Milestone 07: CNINFO 增量同步

实现 CNINFO source adapter：精选股票池的公告索引增量同步、PDF 下载归档、source_access /
checkpoint / 重试进入正式管道。下载后的登记**必须复用 04R-D10 的 `register_document` 核心与
`SubjectResolver`**——本 milestone 不重新实现去重、supersedes、主体解析。

## 1. 前置依赖

- 04R：`register_document` 核心、`SubjectResolver`、filing_type 词表（D7）、
  `ReportPeriod` 校验、`DISCLOSURE_PARSE_TIMEOUT_SECONDS`、隔离/查空落 source_access 的惯例；
- 凭据经 `settings.py` 注入（`CNINFO_ACCESS_KEY/SECRET/TOKEN`），adapter 外不得读 env；
- 与 05 的顺序关系：若 05 未先行，07 自建 `src/disclosure_anchor/cli/pipeline.py` 并只注册
  sync 子命令（05 在同一模块加其余子命令）；§6 DoD 的 parse→build→publish 段在 05 完成后
  补验，§4 前 6 项检查点不依赖 05。

## 2. 组件与数据流

```text
core.tracked_company（0001 已有表，单数名；≥500 精选池，本期验收样本 10 家）
  → SyncDisclosureIndex use case（application/use_cases/sync_disclosure_index.py）
      按 (company, 时间窗, filing_type 规则) 调 CNINFO 公告索引接口 p_info3015
      候选字段 ← p_info3015 响应的封闭映射（字段名已对照 cninfo-interfaces.schema.json）：
        provider_document_id = TEXTID（字符串原样；OBJECTID/RECID 只存元数据，不作 ID——
                               用错会改变去重键语义）
        title = F002V；download_url = F003V；原始分类 = F006V 原样串；
        announcement_date = F001D（北京时间日期串直接解析为 date，不换时区；
                            所有时间窗/"今天"计算固定用 Asia/Shanghai）
        证券信息 = SECCODE + SECNAME；index_updated_at = RECTIME
      每次调用（含查空）写 source_access（query_params / result_hash / status / 耗时），
      **标准化候选列表持久化在 source_access.result_snapshot.candidates[]**（B6：候选先落库，
      进程 crash 不丢公告；file_signature_hint 定死 = {file_size: F005N 原样数值,
      etag: null, last_modified: null（07 不做 HTTP HEAD 预检）, index_updated_at: RECTIME}）
      下载队列 = 从已持久化 candidates 中派生"尚未注册 document 且未终态失败"的项；
      07 内以 repository 内联查询实现（08 再把同一 SQL 固化为 ops.pending_download_v1 视图，
      07 不建 ops 视图）；终态失败 = 该 provider_document_id 存在 retryable=false 失败记录，
      或 failed 记录数 ≥ CNINFO_MAX_RETRIES；不存在独立内存队列
      去重预检（B7，防同 ID 换文件）：仅当 provider_document_id 已注册
        且 file_signature 一致（比较规则：register 时把 hint 存入
          document.provider_metadata.file_signature；"一致" = 双方均非 null 的字段逐项相等
          且至少一个字段可比；两侧无可比字段 → 走"无可靠 signature"分支）
        且候选在重叠核验窗口（近 CNINFO_OVERLAP_DAYS 天，默认 7）之外
        且无更正/替换信号（封闭规则：F002V 标题含 {"更正","修订","更新后","补充","取消"}
          任一子串，除此不认定其他信号）→ 跳过；
      provider 无可靠 signature 时，重叠窗口内一律重新下载并以 raw_file_hash 复核
      （hash 相同 → register_document 去重键幂等吸收；不同 → 新版本 + supersedes）
  → DownloadDocument use case
      下载 PDF bytes → RawDocumentStore（不可变归档，既有实现）
      → register_document 核心（去重 / supersedes / source_access / document / outbox）
      → document.status='registered'，进入 04 的 parse 管道
  → core.source_checkpoint（0001 已有表）记录格式定死：provider='cninfo'、
     scope_key=f"{company_id}:p_info3015"、cursor={"window_end": "YYYY-MM-DD"}；
     p_info3015 的 sdate/edate 统一 YYYY-MM-DD、edate 含当日；
     下一窗口起点 = cursor.window_end - CNINFO_OVERLAP_DAYS；
     **checkpoint 只能在该窗口的 source_access（含 candidates）持久化成功后推进**（B6），
     推进永不越过未持久化的候选
```

## 3. 实施细则

1. `DisclosureSourcePort`：`search_announcements(security, window, categories) -> [AnnouncementRef]`、
   `download_pdf(ref) -> bytes`。文件落位定死：Port 与 AnnouncementRef 定义在
   `application/ports/disclosure_source.py`（已存在占位文件）；use case 文件 =
   `application/use_cases/sync_disclosure_index.py` 与 `application/use_cases/download_document.py`；
   adapter 在 `adapters/sources/cninfo/`。categories 语义定死：p_info3015 **无分类过滤参数**，
   categories 取 core.tracked_company.filing_categories（null → 不过滤全取），adapter 客户端按
   F006V 拆段（`||` 分隔）前缀匹配过滤；"查空"以过滤前的原始 records 为准（原始为空才算 empty）。
   CNINFO adapter 做参数转换与返回映射：
   - 接口口径：`docs/architecture/cninfo-webapi-usage-reference.md`；
   - 机器可读字段/参数：`docs/architecture/cninfo-interfaces.schema.json`；
   - 凭据变量名：`docs/巨潮api.md`，真实值只来自仓库外私有环境。
2. **filing_type 映射**：映射表落
   `src/disclosure_anchor/adapters/sources/cninfo/filing_type_map.json`
   （顶层 {"version": "2026-07", "rules": [...]}，版本化随 rule bundle 纪律）。
   **F006V 是 `||` 分隔的多段分类串**：按段拆分、逐段映射、取第一个非 other 的结果，
   全部未命中 → `other`（整串不拆直接查表会永远匹配不到）。种子规则（按 SORTNAME 关键词）：
   年度报告→annual_report、半年度报告→semiannual_report、季度报告→quarterly_report、
   业绩预告→performance_forecast、业绩快报→performance_flash、
   投资者关系→investor_relations、说明会→performance_briefing、问询+回复→inquiry_reply。
   原始分类 / orgId 等稳定小体积 provider 元数据落 `document.provider_metadata`（0007 列，
   E10）；完整 index response 留在 `source_access.result_snapshot`，两者分工不重叠。
   临时公告 `report_period=null` 合法（B8），不得为凑格式伪造 period。
3. **主体建档**：走 `SubjectResolver`（D5 顺序 + identifier ledger）。orgId/USCC 的取得通道
   定死（p_info3015 响应**没有** ORGID/USCC，它们在 p_stock2100）：每次 sync 开始对目标公司调
   一次 p_stock2100（scode 传入，写 source_access，provider_interface='cninfo:p_stock2100'），
   ORGID → candidates[].provider_org_id 与 ledger(scheme='cninfo_org_id'，仅 provider
   命名空间内稳定，不等同法律身份)；F050V → uscc（有则必填，§6.5.1 规则 4）；结果在本次
   sync 内缓存不逐候选重调；p_stock2100 失败不阻断索引同步，provider_org_id 置 null 记
   warning。source_access.provider_interface 词表统一（08 队列视图按此过滤）：
   索引='cninfo:p_info3015'、档案='cninfo:p_stock2100'、下载='cninfo:download_pdf'。identifier 校验数据只经定时本地快照消费，同步链路禁止实时调外部 identifier
   API（规则 5）。
4. **限流与重试**：进程内令牌桶（settings：`CNINFO_MAX_QPS` 默认 1、`CNINFO_MAX_RETRIES`
   默认 3、`CNINFO_OVERLAP_DAYS` 默认 7）。退避参数定死：base=1s、factor=2、cap=30s、
   full jitter；令牌桶覆盖对 cninfo 域名的**全部** HTTP 请求（token/索引/PDF 下载共用同一桶）。
   HTTP 429/5xx → 可重试；4xx（限流除外）→ 不可重试并落 source_access(status='failed')。
   失败记录格式定死：error 列存结构化 JSON
   {"stage":"download"|"index","error_code":...,"retryable":bool,"provider_document_id":...}，
   query_params 含 provider_document_id 与 download_url。不引入外部队列。
5. **增量语义**：checkpoint 游标 = 每 (company, 接口) 的最近成功同步时间窗上界，且仅在候选
   持久化后推进（B6）；重叠窗口（回看 N 天，默认 7）承担两职：容忍索引晚到 + B7 的同 ID
   换文件核验；幂等由 register_document 去重键保证。
6. **查空**：窗口内无公告也写 source_access——落库形态定死（source_access **没有** result 列，
   0001 冻结）：status='ok'，result_snapshot={"result":"empty","candidates":[]}，
   result_hash=该 snapshot canonical JSON 的 sha256——协议 §3.9。
7. **安全**：token 刷新、HTTP status、`resultcode`、行数、耗时可记录；token/secret/完整
   敏感响应不得写日志或入库。**query_params 持久化前必须剔除
   access_token/client_id/client_secret 键**（token 走 query string，不剔除即入库）。
   泄漏断言定死：新增 tests/unit/test_cninfo_client.py::test_secrets_never_logged_or_persisted
   ——caplog(DEBUG) 跑一次 mock 同步后，断言全部日志记录与构造出的
   source_access.query_params/result_snapshot/error 中不含凭据明文。
   下载文件非 PDF（魔数校验失败）→ 走隔离路径 + source_access 失败记录。
8. CLI 与语义定死：`python -m disclosure_anchor.cli.pipeline sync --company <scode> [--window N]`
   = 索引同步 + **随后串行处理该公司全部待下载候选（下载→register_document）**；
   --window N = 回看天数，窗口=[今天-N, 今天]（Asia/Shanghai），给定时忽略 checkpoint 读取
   但仍按规则推进 checkpoint；缺省时窗口起点 = cursor.window_end - CNINFO_OVERLAP_DAYS；
   无 checkpoint 且未传 --window → exit code 2 并提示需显式 --window。
   Makefile 新增目标（recipe 定死）：
   `sync:` → `@test -n "$(COMPANY)" || (echo 'usage: make sync COMPANY=<scode> [WINDOW=N]' && exit 2); \`
   `PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline sync --company $(COMPANY) $(if $(WINDOW),--window $(WINDOW))`
   （加入 .PHONY）。首次对某公司 sync 时经 SubjectResolver 建 company/security 并
   upsert core.tracked_company(status='active')——这也是验收样本池的种子方式。

## 4. 检查点

- 指定 10 家公司可稳定同步公告索引（验收名单定死，scode：000001、000002、000651、002484、
  002594、300750、600000、600519、601318、688981；经 `make sync COMPANY=<scode>` 首跑种入
  tracked_company；生产池 ≥500，service-purpose §4.1）。
- 查空断言定死：`SELECT count(*) FROM disclosure_core.source_access
  WHERE provider='cninfo' AND result_snapshot->>'result'='empty'` ≥ 1（对空窗口公司）。
- 指定公告类型可下载 PDF 并进入 raw archive；重复公告不重复写 raw（去重键复用）。
- 同一公告新版本（hash 变化）→ 新 document + supersedes 链（核心复用，无重复实现）。
- 查空/失败均有 source_access 可查；checkpoint 断点续跑正确（中断后重跑不漏不重）。
- crash 注入：index 持久化后、下载前中断 → 重启后候选从 result_snapshot 恢复，零丢失（B6 测试）。
- 同 provider_document_id 静默换文件（signature 缺失场景）→ 重叠窗口内重下并产生 supersedes（B7 测试）。
- provider ID 不作内部主键；filing_type 映射表有测试；原始分类保留。
- 代码与日志不泄露凭据（现有 test_permissions/日志测试样式扩展）。

## 5. 测试要求

测试基建定死：fixtures 放 `tests/fixtures/cninfo/{p_info3015_sample.json,
p_info3015_empty.json, p_stock2100_sample.json, sample_announcement.pdf}`，按
cninfo-interfaces.schema.json 的 result_envelope **手工构造去敏样本**（不要求真实录制，
解决"录制需要凭据"的鸡生蛋）；HTTP 层单测（token/429/退避）用 httpx MockTransport 等
传输层 stub；DB-gated 集成测试注入实现 DisclosureSourcePort 的 FakeCninfoSource
读同一 fixtures。
单测：provider mapper、filing_type 映射（含 F006V 多段拆分用例）、令牌桶/退避（断言 base=1s/
factor=2/cap=30s 参数生效）、checkpoint 窗口推进、查空路径、凭据不泄漏（§3.7 断言）。
集成（DB-gated + FakeCninfoSource）：sync→download→register 全链、中断续跑、新版本
supersedes。真实 API 冒烟（人工触发、不进 CI）：1 家公司 1 窗口。

## 6. Definition of Done

- 10 家样本池 sync→download→register 段跑通（不依赖 05）；parse→build→publish 段在 05
  完成后补验（手动串行）；
- 失败状态可定位（source_access + 结构化错误）；
- acceptance-matrix A25/A26/A27 置 pass（A28 此前已 pass，不重复认领）。

## 7. 明确不做

- 不抓全市场；不做复杂 anti-bot 规避；不做标准数据 provider；不做 L2 claim；
- 不做批量调度循环（08）；不做港股/美股 provider（后续按同一 Port 扩展）。

## 8. 常见失败与处理

- CNINFO 参数/字段变化：只改 adapter/mapper，不改 domain 与注册核心。
- 限流失败：降低 QPS、记录 retry，不绕过。
- PDF hash 变化：新文件版本 + supersedes，不覆盖旧档。
- 索引有而 PDF 404：source_access(status='failed', retryable)，进重试队列，不建 document。
