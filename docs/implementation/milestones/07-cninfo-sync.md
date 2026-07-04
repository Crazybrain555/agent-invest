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
- 凭据经 `settings.py` 注入（`CNINFO_ACCESS_KEY/SECRET/TOKEN`），adapter 外不得读 env。

## 2. 组件与数据流

```text
tracked_companies（≥500 精选池；本期验收样本 10 家）
  → SyncDisclosureIndex use case
      按 (company, 时间窗, filing_type 规则) 调 CNINFO 公告索引接口
      每次调用（含查空）写 source_access（query_params / result_hash / status / 耗时），
      **标准化候选列表持久化在 source_access.result_snapshot.candidates[]**（B6：候选先落库，
      进程 crash 不丢公告；每个候选含 provider_document_id / provider_org_id / 证券信息 /
      标题 / 公告日期 / download_url / 原始分类 / file_signature_hint{file_size, etag,
      last_modified, index_updated_at，可为 null}）
      下载队列 = 从已持久化 candidates 中派生"尚未注册 document 且未终态失败"的项（见 08 §1），
      不存在独立内存队列
      去重预检（B7，防同 ID 换文件）：仅当 provider_document_id 已注册
        且 file_signature 与既有记录一致
        且候选在重叠核验窗口（近 N 天，默认 7）之外
        且无更正/替换信号 → 跳过；
      provider 无可靠 signature 时，重叠窗口内一律重新下载并以 raw_file_hash 复核
      （hash 相同 → register_document 去重键幂等吸收；不同 → 新版本 + supersedes）
  → DownloadDocument use case
      下载 PDF bytes → RawDocumentStore（不可变归档，既有实现）
      → register_document 核心（去重 / supersedes / source_access / document / outbox）
      → document.status='registered'，进入 04 的 parse 管道
  → source_checkpoint 记录 (provider, scope_key=公司+接口) 的最近成功游标/时间窗；
     **checkpoint 只能在该窗口的 source_access（含 candidates）持久化成功后推进**（B6），
     推进永不越过未持久化的候选
```

## 3. 实施细则

1. `DisclosureSourcePort`：`search_announcements(security, window, categories) -> [AnnouncementRef]`、
   `download_pdf(ref) -> bytes`。CNINFO adapter 做参数转换与返回映射：
   - 接口口径：`docs/architecture/cninfo-webapi-usage-reference.md`；
   - 机器可读字段/参数：`docs/architecture/cninfo-interfaces.schema.json`；
   - 凭据变量名：`docs/巨潮api.md`，真实值只来自仓库外私有环境。
2. **filing_type 映射**：CNINFO 公告分类（`cninfo-announcement-categories.json`）→ D7 词表的
   映射表落在 adapter 配置（版本化，随 rule bundle 纪律）；映射不到 → `other`。
   原始分类 / orgId 等稳定小体积 provider 元数据落 `document.provider_metadata`（0007 列，
   E10）；完整 index response 留在 `source_access.result_snapshot`，两者分工不重叠。
   临时公告 `report_period=null` 合法（B8），不得为凑格式伪造 period。
3. **主体建档**：走 `SubjectResolver`（D5 顺序 + identifier ledger）；CNINFO orgId 以
   scheme='cninfo_org_id' 入 ledger（仅 provider 命名空间内稳定，不等同法律身份）；
   USCC 若可从 CNINFO 档案接口取得则"有则必填"（§6.5.1 规则 4）；identifier 校验数据只经
   定时本地快照消费，同步链路禁止实时调外部 identifier API（规则 5）。
4. **限流与重试**：进程内令牌桶（settings：`CNINFO_MAX_QPS` 默认 1、`CNINFO_MAX_RETRIES`
   默认 3、指数退避 + 抖动）；HTTP 429/5xx → 可重试；4xx（限流除外）→ 不可重试并落
   source_access(status='failed')。不引入外部队列。
5. **增量语义**：checkpoint 游标 = 每 (company, 接口) 的最近成功同步时间窗上界，且仅在候选
   持久化后推进（B6）；重叠窗口（回看 N 天，默认 7）承担两职：容忍索引晚到 + B7 的同 ID
   换文件核验；幂等由 register_document 去重键保证。
6. **查空**：窗口内无公告也写 source_access(status='ok', result='empty')——协议 §3.9。
7. **安全**：token 刷新、HTTP status、`resultcode`、行数、耗时可记录；token/secret/完整
   敏感响应不得写日志或入库。下载文件非 PDF（魔数校验失败）→ 走隔离路径 + source_access 失败记录。
8. CLI：`python -m disclosure_anchor.cli.pipeline sync --company <code> [--window N]`、
   `make sync`；单公司可独立触发（08 才做批量循环）。

## 4. 检查点

- 指定 10 家公司可稳定同步公告索引（验收样本规模；生产池 ≥500，service-purpose §4.1）。
- 指定公告类型可下载 PDF 并进入 raw archive；重复公告不重复写 raw（去重键复用）。
- 同一公告新版本（hash 变化）→ 新 document + supersedes 链（核心复用，无重复实现）。
- 查空/失败均有 source_access 可查；checkpoint 断点续跑正确（中断后重跑不漏不重）。
- crash 注入：index 持久化后、下载前中断 → 重启后候选从 result_snapshot 恢复，零丢失（B6 测试）。
- 同 provider_document_id 静默换文件（signature 缺失场景）→ 重叠窗口内重下并产生 supersedes（B7 测试）。
- provider ID 不作内部主键；filing_type 映射表有测试；原始分类保留。
- 代码与日志不泄露凭据（现有 test_permissions/日志测试样式扩展）。

## 5. 测试要求

单测：provider mapper（真实响应样本做 fixture，不打真实 API）、filing_type 映射、令牌桶/退避、
checkpoint 窗口推进、查空路径。集成（DB-gated + 录制响应）：sync→download→register 全链、
中断续跑、新版本 supersedes。真实 API 冒烟（人工触发、不进 CI）：1 家公司 1 窗口。

## 6. Definition of Done

- 10 家样本池全链跑通（sync→download→register→parse→build→publish 手动串行）；
- 失败状态可定位（source_access + 结构化错误）；acceptance-matrix 对应行置 pass。

## 7. 明确不做

- 不抓全市场；不做复杂 anti-bot 规避；不做标准数据 provider；不做 L2 claim；
- 不做批量调度循环（08）；不做港股/美股 provider（后续按同一 Port 扩展）。

## 8. 常见失败与处理

- CNINFO 参数/字段变化：只改 adapter/mapper，不改 domain 与注册核心。
- 限流失败：降低 QPS、记录 retry，不绕过。
- PDF hash 变化：新文件版本 + supersedes，不覆盖旧档。
- 索引有而 PDF 404：source_access(status='failed', retryable)，进重试队列，不建 document。
