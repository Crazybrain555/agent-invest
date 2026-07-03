---
id: disclosure_anchor_milestone_07_cninfo-sync
project: disclosure_anchor
title: CNINFO 增量同步
status: ready-for-implementation
created_at: 2026-06-26
---

# Milestone 07: CNINFO 增量同步

## 1. 目标

实现 CNINFO source adapter，让精选股票池的公告索引、PDF 下载、source_access、checkpoint 和重试机制进入正式管道。

## 2. 范围

范围内：

- CNINFO client。
- provider mapper。
- rate limit / retry。
- tracked_companies。
- source_checkpoint。
- index sync。
- PDF download。
- source_access 查空记录。
- CNINFO 凭据从 `settings.py` 注入 adapter；不在 adapter 外直接读取 `.env` 或 `os.environ`。


## 3. 实施细则

1. 实现 `DisclosureSourcePort`：

```text
search_announcements(...)
download_pdf(...)
```

2. CNINFO adapter 负责 provider 参数转换和返回映射。
   - 接口口径参考 `docs/architecture/cninfo-webapi-usage-reference.md`。
   - 机器可读字段/参数参考 `docs/architecture/cninfo-interfaces.schema.json`。
   - 凭据变量名参考 `docs/巨潮api.md`，真实值只来自仓库外私有环境。
3. `sync_disclosure_index`：

```text
tracked_companies
+ filing_type rules
+ time window
→ candidates
→ source_access
→ document candidates
```

4. `download_document`：

```text
provider ref
→ PDF bytes
→ RawDocumentStore
→ document
```

5. source_checkpoint 保存最近成功游标。
6. rate_limit 和 retry 不使用外部队列。
7. 查空也写 source_access。
8. token 刷新、HTTP status、`resultcode`、行数、耗时可以记录；token、secret 和完整敏感响应不得写日志或入库。
9. company / security 建档遵守主体匹配键采集义务（service-purpose §10.1 / 顶层协议 §6.5.1 规则 4、5）：
   统一社会信用代码有则必填（use case 入参 `company_credit_code`，scheme=uscc，对应 DB 列
   `unified_social_credit_code`）；`(exchange, security_code)` 必填；CNINFO orgId 等 provider ID
   只作 provider 侧标识，不作内部主键；identifier 校验 / 富化数据只经定时本地快照消费，
   不得在同步链路实时调用外部 identifier API。


## 4. 检查点

- 指定 10 家公司可稳定同步公告索引（10 家为验收样本规模；生产 tracked_companies 为 ≥500 精选池，见 service-purpose §4.1）。
- 指定公告类型可下载 PDF。
- 下载结果进入 raw archive。
- 查空有 source_access。
- 失败可重试。
- provider ID 不作为内部主键。
- 重复公告不重复写 raw。
- 代码和日志不泄露 `CNINFO_ACCESS_KEY`、`CNINFO_ACCESS_SECRET` 或 `CNINFO_ACCESS_TOKEN`。


## 5. Definition of Done

- CNINFO 同步可跑通小样本。
- 失败状态可定位。
- 可从 CNINFO document 进入 Phase 04/05 管道。


## 6. 明确不做

- 不抓全市场。
- 不做复杂 anti-bot 规避。
- 不做标准数据 provider。
- 不做 L2 claim。


## 7. 交付给下一阶段

- CNINFO source adapter。
- source_checkpoint。
- index sync。
- download pipeline。


## 8. 常见失败与处理

- CNINFO 参数变化：只改 adapter/mapper，不改 domain。
- 限流失败：降低并发，记录 retry，不绕过。
- PDF hash 变化：作为新文件版本处理，不覆盖旧文档。
