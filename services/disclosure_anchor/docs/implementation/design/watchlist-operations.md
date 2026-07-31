---
id: disclosure_anchor_design_watchlist_operations
project: disclosure_anchor
title: 股票池运维设计评审（round8 问题清单 + 业界调研 + 决议建议）
status: adopted-and-implemented (2026-07-07, user authorization); §5.1 真源归属已被 §7 改判 (2026-07-08)
created_at: 2026-07-07
inputs: 用户 round8 提问 + Codex round8 实测 + 2026-07-07 四路调研（qlib/LEAN/zipline/vnpy/py-sec-edgar/Prometheus/GitOps）
decides_for: milestone 09（生产就绪）
---

# 股票池运维设计评审

**背景**：用户的生产用法 = "我维护 200 只的池子，今天加 5 只，服务按默认参数自动盯盘"。
Codex round8 实测了"增量加 5 只"（000002/000651/600900 成功，601398/300750 失败），
判定"有雏形，但差一层生产运维闭环"。用户追问：**用 CSV 维护股票池是业界常见做法吗？**
本文档先盘点问题、给调研结论、逐条判定合理性，再给设计决议建议。**本轮不改代码。**

## 1. 问题清单（共 7 条）

| # | 提出者 | 问题 |
|---|---|---|
| B1 | 用户 | 维护 200 只池子、今天加 5 只，这种真实场景后端有没有准备？ |
| B2 | 用户 | CSV 是常见做法吗？还是 YAML/别的配置形态？ |
| B3 | Codex P1 | 首次回补失败恢复不生产化：resultcode 429 记为 retryable=false；300750 失败后无 checkpoint、无 source_access 可追 |
| B4 | Codex P1 | 三年回补 × 200 只候选量爆炸（3 只已产生 1143 个待下载），需要批次/限速/进度视图 |
| B5 | Codex P1 | `--codes` 是 DB 临时 upsert 不回写 CSV；缺 operator 工具（list/diff/apply/pause/resume） |
| B6 | Codex P1 | watchlist CSV 表达不了 filing_categories（"默认事件类型"无法由池子配置维护） |
| B7 | Codex P2 | checkpoint cursor 只有 window_end，没记本次实际 window_start，回补口径不可审计 |

## 2. 现状取证（file:line + live DB，2026-07-07）

- CSV parser 只读 4 列（security_code/exchange/lookback_days/sync_frequency）：
  `cli/pipeline.py:399-430`；而 TrackEntry 本身支持 filing_categories
  （`use_cases/track_companies.py:33`）——**只差 CSV/CLI 表面**。B6 属实。
- `--codes` 路径不碰 CSV（`cli/pipeline.py:401-409`）；live DB 已出现漂移：
  **tracked_company 10 行 vs watchlist.csv 5 行**（Codex 加的 5 只只在 DB）。B5 属实。
- 429 语义分裂：HTTP 状态 429 **可重试**（`client.py:425-430`），但 200 信封内
  resultcode=429 不在 `RETRYABLE_RESULT_CODES = {-1,403,404,405}`（`client.py:24`）
  → fail-fast。B3 属实（限流/配额属临时态，应重试+熔断）。
- 回补量控：有排水限速（全局 1 QPS token bucket `client.py:62-74`；每轮
  WORKER_BATCH_DOWNLOAD=10、间隔 900s），**没有入队上限**——首同步把三年索引一次性
  全部入 source_access。live 证据：1143 pending ≈ 115 轮 ≈ 29 小时才排完（默认参数）。
  200 只全量入池 ≈ 数万-十万级候选。B4 属实。
- checkpoint cursor 全部内容 = `{"window_end": ...}`（`sync_disclosure_index.py:368`）。B7 属实。

## 3. 业界调研结论（8 个同类项目 + 2 个 ops 基础设施）

**CSV/平面文件为真源是这个规模的主流，零个项目用 DB 作名单真源**：

| 项目 | 名单真源 | 形态 |
|---|---|---|
| Microsoft qlib | `instruments/csi300.txt` | TSV 三列：symbol + start_date + end_date |
| py-sec-edgar（与本服务最同构：按名单盯披露） | `--ticker-file examples/portfolio.csv` | 单列 CSV，按用途维护多份 |
| QuantConnect LEAN | 代码优先；官方文件式示例 = 远端 CSV universe | CSV |
| zipline-reloaded | 每 ticker 一个 CSV + `zipline ingest` 幂等落 SQLite | 文件真源 + 生成式 apply |
| sec-edgar / edgar-crawler | 每行一个 ticker/CIK 的文件或 CLI 参数 | txt/CSV |
| vnpy | `.vntrader/*_setting.json`（UI 落盘） | JSON——因每 entry 挂嵌套策略参数 |
| Prometheus file_sd | targets YAML/JSON + 自动 reload | YAML——因 target 挂 labels |
| Grafana provisioning | 版本控制 YAML，UI 改动锁死强制走 git | YAML |

三条定型经验：

1. **CSV vs YAML 分界不是规模，是每条记录的形状**：扁平同构标量 → CSV（git diff
   可读性最好）；entry 需要嵌套 per-entry 覆盖 → YAML/JSON（Prometheus labels、
   vnpy 合约参数）。
2. **暂停不是删行**：qlib 用 end_date 关区间（point-in-time 可回放），Prometheus
   靠 git 历史留痕。名单行加 status/end_date 列是标准做法。
3. **没有一个项目提供 add/remove CLI**——增删就是编辑文件 + git commit（diff 即
   变更评审单），工具侧只负责**幂等 apply**（zipline ingest、qlib dump、Prometheus
   自动 reload）+ 只读 list/status。复杂度放在对账，不放在 CRUD。

## 4. 逐条判定

| # | 判定 | 依据 |
|---|---|---|
| B1 | 合理，已部分成立 | track/幂等/默认回补已通（Codex 实测确认）；缺失项见 B3-B7 |
| B2 | **CSV 是正确且主流的选择，保持** | 见 §3；等到需要每股嵌套覆盖（如每股解析参数）再升 YAML，当前 6 列扁平结构 CSV 更优 |
| B3 | 合理，P1 | 限流/配额是临时态；resultcode 429 应进重试+熔断（已在 09 背账 quota metering 项下） |
| B4 | 合理，P1 | 排水限速已有、入队无上限；200 只需要入池批次计划 + 进度可查 |
| B5 | 合理但**修正方向**：不做 add/pause/resume CLI | 业界共识 = 文件即接口；缺的是"对账"而不是 CRUD（见 §5.2） |
| B6 | 合理，S 修 | parser 补一列即可，实体/用例早已支持 |
| B7 | 合理，P2 | cursor 加 window_start（审计字段，不参与判定逻辑） |

## 5. 设计决议（用户已授权，2026-07-07 实施完毕）

实施对照：§5.1 watchlist 列扩展 ✓（status/joined_date/filing_categories 列 + parser）；
§5.2 对账 apply ✓（reconcile+drift 报告，PRUNE_DRIFT=YES 置 paused；track-status 只读
进度视图；--codes 打漂移提示）；§5.3 回补背压 ✓（DISCLOSURE_BACKFILL_MAX_PENDING_
DOWNLOADS=2000，changedetection.io MAX_QUEUE_SIZE 模式）+ 429 配额语义 ✓（参照
edgartools：请求内 fail-fast 不烧配额、retryable=true 留下轮、worker 轮级熔断
sync_quota_break）+ 同步失败留痕 ✓（profile 失败也写 source_access）；
§5.4 cursor 审计字段 ✓（window_start/synced_at）。

### 5.1 watchlist.csv 仍是唯一真源，列扩展

```csv
security_code,exchange,status,joined_date,lookback_days,sync_frequency,filing_categories,note
600519,SSE,active,2026-07-06,,,,贵州茅台
000002,SZSE,paused,2026-07-07,30,hourly,,万科-测试暂停
```

- `status`（active|paused，缺省 active）：暂停=改字段不删行（qlib 模式）。
- `joined_date`：审计 + point-in-time（"今天加 5 只"=append 5 行带今日日期）。
- `filing_categories`：分号分隔的 F006V 前缀，补 B6。
- 升 YAML 的触发条件（明确写死）：当出现第二个需要嵌套结构的 per-entry 字段时。

### 5.2 运维命令面收敛为"对账"而非 CRUD

- `make track` = 幂等 apply（已有），语义扩展为**全量对账**：CSV 有 DB 无 → 创建；
  两边都有 → 同步覆盖字段；**DB 有 CSV 无 → 报告为漂移**（默认只报告；
  `--prune-drift` 才置 paused）。这一条直接消化 B5 的 list/diff/apply/pause 诉求。
- `make track-status` = 只读列表：每公司 tracked 配置 + checkpoint 进度 +
  pending 计数（B4 的进度视图入口）。
- `--codes` 保留为临时通道，但输出尾行固定提示"未写入 watchlist.csv，
  下次对账将显示为漂移"。

### 5.3 首次回补的批次与失败恢复（B3/B4）

- 入队水位：`DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS` 保留兼容名，但计数口径为
  **待下载 + 已下载待解析**；达到 2000 时 sync 跳过尚未回补的新公司。每轮首次精确计数，
  同轮按已放行公司 `candidate_count` 保守累计，下一轮校正；单公司原子同步可越线一次。
  这样 GPU 故障时下载只搬移队列，不会把整池变成 raw backlog。
- resultcode 429 → retryable=true + 轮级熔断（连续 N 次 429 停掉本轮剩余 sync，
  记 quota_exhausted），与 09 背账"配额计量"项合并实施。
- 首同步失败必须留痕：失败也写 source_access（error 信封），消除"300750 无迹可查"。

### 5.4 checkpoint 审计字段（B7）

cursor 增记 `window_start` 与 `synced_at`（只增不改判定逻辑，向后兼容旧 cursor）。

## 6. 与 09 背账的关系

本文档吸收/细化 09 中这些条目：公司清单 operator 工具（ops-deploy #6）、
回补批次（critic 配额项）、429（failure-paths #4/#10）、CSV filing_categories
（company-watch #3 残留）。实施时按 §5 顺序：5.1+5.2（S/M）→ 5.3（M）→ 5.4（S）。

## 7. 真源归属改判（round22，2026-07-08，用户裁决）

**§5.1"watchlist.csv 仍是唯一真源"被改判为：DB（tracked_company）是唯一真源，
CSV 降级为导入/种子 + git 快照格式。**

改判依据（§3 调研的适用前提变了，不是调研错了）：

1. 用户实际用法不是"编辑文件 + git commit"，而是直接看库、问 AI、走接口；
2. 池子管理要暴露给内部其他服务（L2-L6）程序化调用——顶层协议 §10.9 新增个股
   初始化与 §1.5 拉式回路都要求机器可调用的写入口；
3. 业界参照系随之从 qlib/Prometheus（人编辑文件）切到 Miniflux/changedetection.io
   （产品化服务：DB 真源 + OPML/文件导入导出）。

实施形态：`disclosure_public.tracked_companies_v1` 读视图 +
`GET /v1/tracked-companies`（含级联生效值）+ `PUT /v1/admin/tracked-companies`
（复用 TrackCompanies，写语义仍是整行 upsert、空可选字段=清除覆盖）；
`make track` 语义从"对账权威"变"导入/恢复"，新增 `make track-export`
（DB → watchlist.csv git 快照）。§5.2 的对账/漂移机制保留（导入即对账），
§5.3/§5.4 不受影响。

删除语义（同轮补齐，业界对照 Miniflux DELETE-feed / GLEIF registry 双状态）：
§3 经验三"没有项目提供 add/remove CLI"随真源翻转一并失效——DB 真源下 CRUD 就是接口。
三层：`status=paused` 可逆停（原 qlib 模式保留）；`DELETE /v1/admin/tracked-companies/{code}`
= `make untrack`（删订阅行，公司/文档留档；下载队列谓词同轮从"paused 才拦"翻转为
"active 行才放行"，否则删行会重新打开积压下载）；`make purge-company CODE=.. PURGE=YES`
（测试期专用、CORPUS exclusive，只撤销明确识别的单公司失误）。
