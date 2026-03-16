# 公司研究 Skills 体系总规划

> **核心公式：估值 = 利润 × 质量系数**
>
> 一切分析围绕两个因素展开：**未来可持续的经济利润（Owner Earnings）** 和 **对这个利润的确定性系数（Quality Coefficient）**

---

## 文档索引

- **Skills 总览与实施状态**：[docs/skills/README.md](skills/README.md)
- **Per-skill 详细规格**：[docs/skills/](skills/)
- **SEC/XBRL 技术参考**：[docs/references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md](references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md)
- **MCP 配置指南**：[docs/MCP_SETUP_GUIDE.md](MCP_SETUP_GUIDE.md)

---

## 一、核心思想：估值=利润 × 质量系数

### 1.1 Profit（未来可持续经济利润）

会计利润只是输入；目标口径是：

- **Owner Earnings / Economic Earnings**（含 maintenance capex 估计）
- **NOPAT / FCF / ROIC**（理解"利润从哪来、能不能持续、靠不靠加杠杆/会计"）

### 1.2 Quality Coefficient（确定性/持续性系数）

不是"主观打分"，而是把证据**映射成估值参数**：

- 折现率（风险溢价）
- 优势持续期（CAP / fade period）
- 终值倍数/长期增长
- 情景权重（bear/base/bull）

> **系统目标**：在可追溯证据下，给出：未来利润是什么 + 我对它有多确定 + 因此值多少钱。

---

## 二、设计原则

### 2.1 核心原则

1. **精简到 9 个 Skills**，不再拆细
2. **所有依赖都是 hard**：缺任何"规定产物"就 `blocked`，写 `needs.yaml` 交给编排器补齐
3. **查漏补缺**：已有且新 → 跳过；有缺口 → 增量补齐
4. **不用固定 tag 集**：XBRL 抽取改成 **Statement Atlas（报表图谱）**，保留完整树结构和溯源链路
5. **所有结论必须指向证据**（evidence ledger）

### 2.2 Skill 不互相调用

- 每个 Skill 是"可重复运行的模块"：输入（参数 + 规定文件）→ 输出（规定文件 + meta/result）
- Skill **不调用另一个 Skill**（避免隐式耦合）
- 发现缺口时写 `needs.yaml`，由编排器决定下一步跑哪个 Skill

### 2.3 三层数据架构（raw / events / current）

1. **raw 只做"证据镜像"**：保存 SEC 给你的原件集合（尽量保留原文件名），加上我们自己的 `meta.yaml` / `manifest.yaml` / `index.*` / `submission.txt`，做到可追溯可复现；raw 里绝不出现研究拆解（sections/canon 文本/表格抽取结果）。
2. **events 是未来数据库化的核心层**：以"事件"为主键（event_id），统一做分类（taxonomy）与清洗归档（canonical buckets）。下游 skills 只消费 events，不再直接读 raw 的复杂结构。
3. **财报事件（financial report events）是第一优先级**：在 events 层必须能处理"财报正文不在 primary document，而在 EX-99.*"这种现实（40-F/MJDS、很多 6-K interim 常见）。
4. **Skill2 = raw ingest + events materialize（含财报事件 buckets 做完整）**；**Skill3 = 仅对财报事件做深度 XBRL 解析**，把 per-event 解析产物落到该事件目录，并同时维护 current 的全局合并 atlas。

---

## 三、目录结构：以公司为主体 + raw/events/current/runs 四层

```
/home/help/mcp/work/company_research/
  registry.jsonl                           # 全局运行注册表
  value_summary.csv                        # 全局估值汇总（由编排器生成）
  company/
    {TICKER}/
      company.yaml                         # 必须：公司身份静态信息（ticker/cik/fye/...）
      latest.json                          # 可选：指向最新 run_id 与 current 快照信息

      raw/                                 # 只存原始证据（不可变、可追溯）
        sec/
          accessions/
            {accession}/
              meta.yaml                    # 必须：我们生成的元数据（含 primary 原文件名、doc map）
              manifest.yaml                # 必须：我们生成的清单（文件 hash/bytes/url/时间/完整性）
              index/
                index.json                 # 必须：SEC directory index.json（枚举目录文件/大小）
                {accession}-index.html     # 必须：filing index page（含 Document/Type/Description）
                index.html                 # 可选：SEC 目录 index.html
                index.xml                  # 可选：SEC 目录 index.xml
              submission/
                {accession}.txt            # 强烈建议/默认必须：完整 submission 包（强追溯）
              documents/                   # as-filed 文档（非 EX-*；主文档也在此）
                <original filenames...>    # 例如 aapl-20250628.htm
              exhibits/                    # as-filed exhibits（EX-* 但不包含 XBRL exhibits）
                <original filenames...>    # 例如 exhibit_99_1.htm / pressrelease.htm
              xbrl/                        # as-filed XBRL/iXBRL 文件集合（含 EX-101.* 对应文件）
                <original filenames...>    # .xsd / *_htm.xml / *_pre.xml / FilingSummary.xml ...
              other/                       # 可选：图片/附件等非核心（GRAPHIC、JPG/PNG等）
                <original filenames...>

      events/                              # 事件级数据层（可数据库化；下游 skills 直接消费）
        sec/
          ingest_state.yaml                # 元数据（issuer_type/vmf_version/classifier_version/window/totals）
          filings_index.parquet            # 必须：filing 粒度索引（accession 为主键）
          events_index.parquet             # 必须：event 粒度索引（event_id 为主键）
          events/
            {event_id}/                    # 每个事件一个对象目录（目录名用安全字符）
              event.yaml                   # 必须：事件对象元数据（可数据库化 schema）
              raw_refs.json                # 必须：指向 raw 的引用（path/url/hash/anchor）
              bucket_manifest.json         # 必须：本事件有哪些 buckets、每个 bucket 的 hash/状态
              event_overview/              # canonical buckets（仅在有内容时创建）
                overview.md
                timeline.json
              press_release/
                press_release.md
              presentation_slides/
                deck_ref.json
              financial_statements/
                narrative.md
                tables/                    # 可选：抽取的表格 csv/parquet
              notes_and_accounting/
                notes.md
              mdna_operating_review/
                mdna.md
              risk_factors/
                risk_factors.md
              business_and_strategy/
                business.md
              governance_and_compensation/
                governance.md
              capital_structure_and_liquidity/
                liquidity.md
              mna_and_integration/
                mna.md
              restructuring_and_impairment/
                restructuring.md
              legal_and_regulatory/
                legal.md
              sustainability_esg/
                esg.md
              exhibits_and_material_contracts/
                contracts_index.json
              structured_data/             # Skill3 写入（财报事件为主）
                xbrl_atlas/
                  periods.yaml
                  facts.parquet
                  nodes.parquet
                  edges.parquet
                  paths.parquet

      current/                             # "当前态工作台"（最新可用总表 + 最新分析产物 + gaps + 输出）
        analysis_data/
          market_snapshot.yaml
          events_summary.parquet           # 从 events/sec/events_index 汇总（便于分析/筛选）
          xbrl_atlas/                      # 全局合并（最近10年）- Skill3 维护
            periods.yaml
            facts.parquet
            nodes.parquet
            edges.parquet
            paths.parquet
          economic/                        # Skill4 输出
            recast_policy.yaml
            economic_statements.parquet
            core_metrics.parquet

        analytics/                         # 分析产物（Skill5-9 等）
          diagnostics/
            profit_quality.yaml
            profit_risk_forecast.yaml
            growth_drivers.yaml
            moat.yaml
            quality_coefficient.yaml
            audit.yaml
          valuation/
            valuation.yaml
            valuation_model.csv
          evidence/
            evidence.jsonl                 # 结论型证据账本（claims）
            evidence_index.parquet         # 可选：便于查询的索引

        gaps/                              # 缺口与待解决问题（驱动补数据/补解析）
          artifacts_state.yaml
          questions.jsonl
          missing_data.yaml                # 结构化缺口
          needs_queue.yaml                 # 可选：建议编排器下一步行动

        outputs/                           # 面向决策/汇报的最终输出
          investment_memo.md
          value_state.yaml
          valuation.yaml

      runs/{run_id}/                       # 运行日志与审计追踪（不可变）
        meta.yaml
        result.yaml
        needs.yaml                         # 仅 blocked 时
        outputs/                           # 本次运行产物快照（可选）
```

### 写入规则

- Skill **先写 runs/{run_id}/**
- 成功（ok/partial）后，把关键产物 **原子替换** 到对应层（raw/events/current）
- `latest.json` 只在成功后更新
- 每次 run 追加 `registry.jsonl`

### 三层职责边界

| 层 | 职责 | 不可变性 | 写入者 |
|---|------|---------|--------|
| `raw/` | 证据镜像（SEC 原件 + 我们的索引/manifest） | 不可变（append-only） | Skill2 |
| `events/` | 事件级数据（分类 + buckets + 结构化数据） | 可更新（event 级 upsert） | Skill2（buckets）、Skill3（structured_data） |
| `current/` | 当前态工作台（全局合并 + 分析产物 + 缺口 + 输出） | 可覆写（atomic replace） | 所有 Skills |
| `runs/` | 运行日志 | 不可变（append-only） | 所有 Skills |

---

## 四、统一执行协议

### 4.1 result.yaml 结构

每个 Skill 跑完在 `runs/{run_id}/result.yaml` 落：

```yaml
skill: <skill-name>
ticker: ABC
run_id: 20260105_143012
as_of: 2026-01-04
timezone: America/New_York

status: ok | partial | blocked | skipped | error

requires:
  hard:
    - <path>

missing: []                    # 仅 blocked 时列出缺失的 hard 依赖

warnings: []                   # partial 时的降级说明

outputs:
  - <path>

# 可选：Skill 自定义扩展字段
components: {}
```

### 4.2 needs.yaml 结构（blocked 时必须）

```yaml
blocked_by:
  - artifact: events/sec/events_index.parquet
    producer_skill: sec-ingest-and-materialize-events
    reason: "缺 events_index，无法做 XBRL 解析"

suggested_plan:
  - sec-ingest-and-materialize-events
  - xbrl-parse-financial-report-events

priority: high
```

### 4.3 状态定义

| Status | 含义 |
|--------|------|
| `ok` | 完全成功 |
| `partial` | 部分成功（有降级/警告但可继续） |
| `blocked` | 缺 hard 依赖，无法继续 |
| `skipped` | 输入未变化，无需重跑 |
| `error` | 运行时错误 |

---

## 五、Event Taxonomy（事件主分类 — 12 类）

> 投研检索友好（按日期+主类），覆盖跨表单差异；Skill2 能用稳定规则自动分类。

### 5.1 Taxonomy 列表

| category_id | 名称 | 定义 | 识别信号（优先级从高到低） | raw 典型材料位置 |
|---|---|---|---|---|
| `financial_report` | 财报事件 | 周期性财报披露：10-K/10-Q/20-F/40-F + FPI 6-K interim results | form in {10-K,10-Q,20-F,40-F, *-A}；或 6-K 且"period+results"信号成立 | primary doc；或 EX-99.*（40-F/6-K 常见）+ xbrl/ |
| `earnings_release_guidance` | 业绩公告/指引 | 非完整财报但影响预期：业绩快报、指引更新 | 8-K Item 2.02；或 exhibits 描述含 earnings/results/guidance；或 6-K + EX-99.* 且 results/guidance | EX-99.* |
| `mna` | 并购/剥离 | 收购、合并、资产出售、重大重组交易 | 8-K Item 2.01；或 EX-2.*；或 merger/acquisition/disposition 关键词 | primary + EX-2.* + EX-99.* |
| `financing_liquidity` | 融资/流动性 | 债务/股权融资、信贷额度、再融资 | 8-K Item 2.03 / 1.01 / 3.02；或 EX-10.*(credit agreement) | EX-10.* + press release |
| `default_covenant` | 违约/契约 | covenant breach、default、going concern | 8-K Item 2.04 / 2.05；或 default/covenant 关键词 | primary + EX-99.* |
| `auditor_restatement` | 审计/重述 | 审计师变更、非依赖、重述、重大内控缺陷 | 8-K Item 4.01 / 4.02；或 restatement/material weakness/auditor | primary + auditor letter |
| `impairment_restructuring` | 减值/重组 | impairment、资产减记、重组/裁员/退出业务 | 8-K Item 2.06 / 2.05；或 impairment/restructuring | primary + EX-99.* |
| `governance_management` | 治理/高管 | 董监高变动、章程变更 | 8-K Item 5.02 / 5.03；或 DEF14A | primary + proxy materials |
| `capital_return_equity` | 资本回报/股本 | 回购、分红、拆股、增发 | EX-99.* + buyback/dividend/split 关键词；或 8-K Item 3.03 / 3.01 | press release + agreements |
| `legal_regulatory` | 诉讼/监管 | 重大诉讼、调查、监管处罚 | lawsuit/investigation/settlement/SEC/DOJ 关键词；或 8-K Item 8.01 | primary + exhibits |
| `shareholder_meeting_proxy` | 股东大会/代理 | DEF14A、投票结果、提案 | form=DEF14A/DEFA14A；或 meeting/vote 关键词 | proxy docs |
| `other_material` | 其他重大事项 | 不属于以上但仍应归档的重大披露 | 8-K Item 8.01；或 6-K other event | primary + exhibits |

### 5.2 FPI 6-K Interim Results 识别规则

用于把 6-K 分成：
- `financial_report`（interim results）：进入"财报事件"
- 其他 taxonomy 类别：走普通事件流

**严格规则（唯一规则，避免误判）：**

- Period 信号：`three months ended / six months ended / quarter ended / interim / half-year / unaudited interim / condensed consolidated` 等
- Results 信号：`results / earnings / financial statements / interim results` 等
- 判定：**(period AND results)**，来源可来自：
  - 6-K filing title/description（来自 SEC metadata）
  - exhibits 描述（尤其 EX-99.*）

约束：
- 仅出现 `presentation / outlook / guidance` **不能**单独判为财报事件；只能作为已判为财报事件后的附带材料

---

## 六、Canonical Content Buckets（16 个）

> 所有事件统一一套 bucket 名字；缺失允许不生成；任何下游 skill 只读 buckets，不关心表单差异。

| bucket_name | 用途（对估值/投研的价值） | 典型内容 |
|---|---|---|
| `event_overview` | 统一入口：事件摘要、关键时间线 | `overview.md`, `timeline.json`, `classification.json` |
| `press_release` | 快速拿到"市场叙事/指引/要点" | `press_release.md`, `qa_highlights.jsonl` |
| `presentation_slides` | investor deck / 会议材料 | `deck_ref.json`, `slides_text.md`（可选） |
| `financial_statements` | 人读财务报表/摘要（XBRL 数字由 structured_data 提供） | `narrative.md`, `tables/*.csv`（可选） |
| `notes_and_accounting` | 会计政策、关键估计、footnotes | `notes.md`, `critical_accounting.md` |
| `mdna_operating_review` | 经营回顾（增长驱动、利润变化） | `mdna.md` |
| `risk_factors` | 风险因素（折现率/情景权重的重要证据） | `risk_factors.md` |
| `business_and_strategy` | 业务结构、竞争、产品、地区 | `business.md`, `segments_overview.md` |
| `governance_and_compensation` | 治理与激励 | `governance.md`, `compensation.md` |
| `capital_structure_and_liquidity` | 资本结构、债务、流动性 | `liquidity.md`, `debt_summary.json`（可选） |
| `mna_and_integration` | 并购/剥离的交易条款与整合影响 | `mna.md`, `proforma_refs.json` |
| `restructuring_and_impairment` | 重组/减值 | `restructuring.md` |
| `legal_and_regulatory` | 诉讼/监管 | `legal.md` |
| `sustainability_esg` | ESG/可持续 | `esg.md` |
| `exhibits_and_material_contracts` | 合同/协议/附件索引（证据入口） | `exhibits_index.json`, `contracts_index.json` |
| `structured_data` | 结构化数据出口（Skill3 的 XBRL atlas） | `xbrl_atlas/*` + 未来 `normalized_tables/*` |

> `structured_data` 由 **Skill3 写入**；Skill2 只负责把 raw_xbrl refs 以及"是否可解析"的状态写进 event.yaml / bucket_manifest。

### 6.1 财报事件 Buckets 映射与抽取规则

#### 6.1.1 解决"财报不在 primary document"的统一机制

Skill2 必须先做：构建"source document catalog"（每个事件都一样），来自 raw 的 `meta.yaml: documents[]`：
- 包含每个原始文件：filename、doc_type、description、category
- 然后按 bucket 的需求做"选择 + 抽取"

#### 6.1.2 财报事件 Bucket 映射规则

**(1) `financial_statements` / `notes_and_accounting`**

优先来源：
1. 若 `xbrl.has_xbrl=true`：数字由 Skill3 的 `structured_data` 提供；Skill2 在 `financial_statements/narrative.md` 里放人读版摘要 + raw_refs 指向"包含 FS/notes 的主要文件"
2. 对 40-F / 6-K interim（FS 常在 EX-99.*）：选择 exhibits 中 description/doc_type 命中 `financial statements`, `audited`, `unaudited interim`, `condensed consolidated` 等；多份命中时按 bytes 降序取前 1~2 份

**(2) `mdna_operating_review`**

优先来源：
1. exhibits 中 description 命中：`management's discussion`, `MD&A`, `operating and financial review`, `OFR`
2. 否则 primary doc 按表单 heading 抽取：
   - 10-K：Item 7
   - 10-Q：Part I Item 2（**排除 Part II Item 2**）
   - 20-F：Item 5
   - 40-F：通常从 EX-99.*（MD&A/AIF）抽取
3. 6-K interim：从 EX-99.* 或 primary 按 heading 兜底

**(3) `risk_factors`**

- 10-K：Item 1A
- 10-Q：Part II Item 1A（若无则为空）
- 20-F：Item 3.D
- 40-F：通常来自 AIF（EX-99.*）

**(4) `business_and_strategy`**

- 10-K：Item 1
- 20-F：Item 4
- **10-Q：通常没有 Business（这是现实，不是漏抓）**
- 40-F：通常在 AIF（EX-99.*）

**(5) `governance_and_compensation`**

- 通常来自 DEF14A（它本身也是一个事件）
- 财报事件目录里可以只放 ref，不强制复制

### 6.2 非财报事件 Buckets 抽取规则

统一策略（Skill2）：
1. 每个事件至少产出：`event_overview/overview.md` + `exhibits_and_material_contracts/exhibits_index.json`
2. 按 taxonomy 额外产出对应 buckets：
   - `earnings_release_guidance` → press_release + presentation_slides + mdna（如有）
   - `mna` → mna_and_integration + exhibits（EX-2.*, EX-10.*）
   - `financing_liquidity` / `default_covenant` → capital_structure_and_liquidity
   - `auditor_restatement` → notes_and_accounting + risk_factors
3. 对 PDF/图片等：不强制 OCR；放引用 + 可选纯文本提取；parse_status 标记 `partial`，写 gap

---

## 七、核心产物契约（最小 Schema）

### 7.1 company.yaml

```yaml
ticker: ABC
company_name: "ABC Inc."
cik: "0000123456"
exchange: "NYSE"
sic: "1234"
fiscal_year_end: "12-31"
currency: "USD"
```

### 7.2 market_snapshot.yaml

路径：`current/analysis_data/market_snapshot.yaml`

```yaml
as_of: 2026-01-04
currency: USD
price: 12.34
shares_outstanding: 100000000
shares_float: 80000000
market_cap: 1234000000
enterprise_value: 1500000000
source: "mixed:alpaca.get_stock_latest_trade+yfinance.get_stock_info"
```

### 7.3 raw/sec/{accession}/meta.yaml

```yaml
accession: "0000950170-25-040545"
cik: "0000950170"
ticker: "SNDL"
form: "40-F"
filed_at: "2025-03-28"
report_date: "2024-12-31"
primary_document:
  filename: "sndl-20241231.htm"
  doc_type: "40-F"
  description: "Form 40-F"

documents:
  - filename: "sndl-20241231.htm"
    doc_type: "40-F"
    description: "Form 40-F"
    category: "documents"
  - filename: "sndl-ex99_2.htm"
    doc_type: "EX-99.2"
    description: "Financial Statements"
    category: "exhibits"
  # ...

xbrl:
  has_xbrl: true
  is_inline_xbrl: true
  instance_filename: "sndl-20241231_htm.xml"
  schema_filename: "sndl-20241231.xsd"
  linkbases: ["..._pre.xml","..._lab.xml","..._cal.xml","..._def.xml"]

source:
  sec_dir_url: "https://www.sec.gov/Archives/edgar/data/950170/000095017025040545/"
  fetched_at: "2026-03-01T12:34:56-05:00"
```

### 7.4 raw/sec/{accession}/manifest.yaml

```yaml
downloaded_at: "2026-03-01T12:34:56-05:00"
files:
  index/index.json: {bytes: 1234, sha256: "...", url: ".../index.json"}
  # ...
completeness:
  has_index_json: true
  has_filing_index_html: true
  has_submission_txt: true
  has_primary_document: true
  has_xbrl_package: true
```

### 7.5 events/sec/ingest_state.yaml

```yaml
as_of: "2026-01-14"
issuer_type: "fpi"
sixk_classifier_version: "strict_period_and_results"
vmf_version: "standard"
window:
  mode: "maintenance"
  start: "2026-01-08"
  end: "2026-01-14"
  overlap_days: 2
  lookback_years: 10
totals:
  filings_fetched: 320
  accessions_new: 12
  accessions_downloaded: 8
  stored_total: 5420
  events_total: 180
  financial_report_events: 42
```

### 7.6 filings_index.parquet Schema

| 字段名 | 类型 | Nullable | 说明 |
|--------|------|----------|------|
| accession | string | N | SEC accession number（primary key） |
| form | string | N | 表单类型 |
| filed_at | date32 | N | 提交日期 |
| period_end | date32 | Y | SEC period_of_report |
| has_xbrl | bool | N | 是否有 XBRL |
| is_amendment | bool | N | 是否修订版 |
| primary_doc | string | N | 主文档原始文件名 |
| filing_url | string | N | SEC 原始 URL |
| local_dir | string | N | raw 本地目录相对路径 |
| event_id | string | Y | 关联的 event_id |
| bucket | string | N | periodic_core / event_stream |
| sixk_class | string | Y | interim_results / other_event（非6-K为空） |
| vmf_triggered | bool | Y | 是否通过 VMF |
| vmf_score | int32 | Y | VMF 打分 |
| items | list[string] | Y | 8-K items |
| downloaded | bool | N | 是否已下载 |

### 7.7 events_index.parquet Schema

| 字段名 | 类型 | Nullable | 说明 |
|--------|------|----------|------|
| event_id | string | N | 事件主键 |
| ticker | string | N | 股票 |
| source | string | N | 固定 sec_edgar |
| category | string | N | taxonomy 主类 |
| subtype | string | Y | 表单/子类 |
| occurred_at | date32 | Y | 事件日期 |
| filed_at | date32 | Y | SEC filed date |
| period_end | date32 | Y | 财报事件必须 |
| fiscal_period | string | Y | FY/Q1/Q2/Q3/Q4/H1/H2 |
| primary_accession | string | Y | 主 accession |
| accessions | list[string] | Y | 关联 filings 列表 |
| buckets_present | list[string] | Y | 已产出的 buckets |
| status_rollup | string | N | ok/partial/blocked/error/skipped |
| updated_at | timestamp[us, tz=UTC] | N | 更新时间 |

### 7.8 event.yaml

```yaml
event_id: "sec_fr_2024-12-31_FY"
ticker: "SNDL"
source: "sec_edgar"
category: "financial_report"
subtype: "40-F"
issuer_type: "fpi"
occurred_at: "2024-12-31"
filed_at: "2025-03-28"
period_end: "2024-12-31"
fiscal_period: "FY"
fiscal_year_end: "12-31"

primary_filing:
  accession: "0000950170-25-040545"
  form: "40-F"
  raw_dir: "raw/sec/accessions/0000950170-25-040545"
related_filings: []

tags: ["mjds", "ex99_fs", "ex99_mdna"]
importance_hint: 0.8
parse_status:
  raw_ingest: "ok"
  buckets_materialized: "ok"
  xbrl_parsed: "not_run"

lineage:
  raw_manifest_ref: "raw/sec/accessions/.../manifest.yaml"
  raw_manifest_sha256: "..."
updated_at: "2026-03-01T12:34:56-05:00"
```

### 7.9 xbrl_atlas（报表图谱）

路径：per-event 在 `events/sec/events/{event_id}/structured_data/xbrl_atlas/`；全局合并在 `current/analysis_data/xbrl_atlas/`

**facts.parquet 最小字段：**

| 字段 | 说明 |
|------|------|
| `event_id` | 关联事件 |
| `period_end` | 期末日期 |
| `fiscal_period` | FY/Q1/Q2... |
| `statement_type` | IS/BS/CF/CI/Equity |
| `role_uri` | XBRL role |
| `concept` | XBRL concept/tag |
| `label` | 标签文本 |
| `value` | 数值 |
| `unit` | 单位 |
| `decimals` | 精度 |
| `accession` | 来源 filing |
| `context_id` | XBRL context |
| `fact_id` | 唯一键 |
| `dimensions` | json: axis→member |

**nodes.parquet：** `node_id`, `statement_type`, `role_uri`, `concept`, `label`, `depth`, `order`

**edges.parquet：** `parent_node_id`, `child_node_id`, `arcrole`, `weight`

**paths.parquet：** `node_id`, `period_end`, `statement_type`, `path_str`, `value`, `accession`, `event_id`

### 7.10 economic 重铸层

路径：`current/analysis_data/economic/`

**recast_policy.yaml：**

```yaml
policy_version: "default"
mapping_rules:
  - target: revenue
    selector:
      statement_type: IS
      match: ["Revenue", "Net sales", "Total revenues"]
    chosen_concepts: ["us-gaap:Revenues"]
    rationale: "主营业收入口径"
maintenance_capex_method:
  name: "depr_floor"
  params: { floor_ratio: 0.8 }
owner_earnings_definition: "CFO - maintenance_capex +/- normalized_wc + other_adjustments"
```

### 7.11 value_state.yaml（估值底座总表）

路径：`current/outputs/value_state.yaml`

```yaml
ticker: ABC
as_of: 2026-01-04

market:
  price: 12.34
  shares_outstanding: 100000000
  market_cap: 1234000000
  enterprise_value: 1500000000

profit:
  base_period: "TTM"
  owner_earnings: 180000000
  owner_earnings_per_share: 1.80
  nopat: 210000000
  invested_capital: 1200000000
  roic: 0.175
  fcf: 160000000

quality:
  coefficient_base: 0.72
  advantage_period_years: 8
  discount_rate_base: 0.105
  confidence: 0..1

valuation:
  intrinsic_value_per_share:
    bear: 10.0
    base: 20.0
    bull: 30.0
  margin_of_safety_base: 0.62

links:
  memo: "current/outputs/investment_memo.md"
  valuation_yaml: "current/analytics/valuation/valuation.yaml"
  evidence_jsonl: "current/analytics/evidence/evidence.jsonl"
```

### 7.12 questions.jsonl / evidence.jsonl

```json
{"id":"Q_20260105_001","skill":"moat-inferencer","priority":"high","question":"客户集中度是否来自单一合同？","status":"open"}
```

```json
{"id":"E_20260105_010","skill":"profit-quality-and-risk","claim":"应收增长显著快于收入","confidence":0.6,"sources":[{"type":"sec","accession":"...","event_id":"..."}]}
```

---

## 八、SEC 下载策略：VMF（Valuation Materiality Filter）

### 8.1 发行人类型识别（Domestic vs FPI）

- 以近年 filings 的 forms 推断 `issuer_type`
- 若出现 `20-F`/`40-F` → `issuer_type=fpi`
- 若出现 `10-Q` → `issuer_type=domestic`
- 推断结果写入 `events/sec/ingest_state.yaml: issuer_type`

### 8.2 周期性核心（Periodic Core）- 10年全量下载

| 发行人类型 | 下载 Forms |
|-----------|-----------|
| Domestic | 10-K, 10-K/A, 10-Q, 10-Q/A, DEF14A |
| FPI | 20-F, 20-F/A, 40-F, 40-F/A, 6-K（仅 Interim Financials/Results 子集） |

### 8.3 事件流（Event Stream）- 全量索引 + VMF 筛选下载

- Domestic：8-K, 8-K/A
- FPI：6-K（排除 Periodic Core 的 Interim 子集）
- **索引**：10年全量
- **下载**：只下载通过 VMF 筛选的 filings

### 8.4 VMF 三层筛选规则

#### 层 1：硬触发（Hard Trigger）— 命中即下载

**8-K Item 硬触发**（仅 Domestic）：

| Item | 估值材料性 |
|------|-----------|
| 2.02 | 直接影响 EPS/指引预期 |
| 4.01 / 4.02 | 财务质量与可信度 |
| 2.04 | 现金流/折现率/生存概率 |
| 2.06 | 盈利质量、资产质量 |
| 2.01 | 未来现金流路径改变 |

**标题/摘要关键词硬触发**：

```python
HARD_TRIGGER_KEYWORDS = [
    "restatement", "material weakness", "auditor", "going concern",
    "default", "covenant", "bankruptcy", "restructuring",
    "impairment", "write-down",
    "guidance", "outlook", "earnings", "results"
]
```

#### 层 2：打分筛选

| 维度 | 关键词 | 权重 |
|------|--------|------|
| 现金流/融资与生存 | liquidity, refinancing, credit facility, default, covenant | 5 |
| 盈利/EPS/指引 | earnings, results, guidance, outlook, margin | 4 |
| 财务质量/会计可靠性 | restatement, auditor, material weakness | 3 |
| 资产质量与周期拐点 | impairment, restructuring | 3 |
| 并购/剥离 | acquisition, merger, disposition | 2 |

#### 层 3：年度预算

硬触发永远不受预算限制；每自然年最多下载 `vmf_annual_budget` 个评分事件（默认 20）。

---

## 九、Artifact Ownership Matrix（产物归属与依赖）

| Artifact | Producer | Consumer | 用途 |
|---|---|---|---|
| `company.yaml` | Skill1 | Skill2 | CIK/公司身份 |
| `market_snapshot.yaml` | Skill1 | Skill8 | 市场口径 |
| `raw/sec/accessions/...` | Skill2 | Skill3 | 原始证据池 |
| `filings_index.parquet` | Skill2 | Skill3 | filing 索引 |
| `events_index.parquet` | Skill2 | Skill3+ | 事件索引 |
| `events/{event_id}/...` | Skill2/3 | Skill5+ | 事件数据包 |
| `xbrl_atlas/*` | Skill3 | Skill4 | 全局 XBRL atlas |
| `economic/*` | Skill4 | Skill8 | 经济三表与核心指标 |
| `diagnostics/*` | Skill5-7 | Skill8/9 | 诊断产物 |
| `value_state.yaml` | Skill8 | Skill9 | 估值底座总表 |

---

## 十、编排器流程

### 10.1 固定队列

```
1. company-foundation
2. sec-ingest-and-materialize-events
3. xbrl-parse-financial-report-events
4. recast-economic-statements
5. profit-quality-and-risk
6. growth-driver-explorer
7. moat-inferencer
8. valuation-and-margin-of-safety
9. cross-examination-audit
```

### 10.2 执行策略

```
for ticker in pool:
    queue = [skill1, skill2, ..., skill9]

    while queue:
        skill = queue.pop(0)
        result = run_skill(skill, ticker)

        if result.status == "blocked":
            needs = read_needs_yaml()
            producer = needs.blocked_by[0].producer_skill
            if retry_count[ticker][skill] > MAX_RETRY:
                mark_manual_required(ticker, skill)
                continue
            queue.insert(0, skill)
            queue.insert(0, producer)
            retry_count[ticker][skill] += 1

        elif result.status in ["ok", "partial", "skipped"]:
            continue

        elif result.status == "error":
            log_error(ticker, skill)
            continue

    collect_value_state(ticker)

generate_value_summary()
```

---

## 十一、SKILL.md 写作模板

每个 Skill 的 SKILL.md 按以下模板：

```md
---
name: <skill-name>
description: <一句话：做什么，为估值服务的哪一层>
revision: "<YYYY-MM-DD>"
---

# <Skill Name>

## 职责边界
<一段话描述>

## 输入参数
| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|

## Hard 依赖
- `<path>`

## 输出
- `<path>`

## 内部步骤
1. <step>

## 查漏补缺规则
- <condition> → skipped

## partial 条件
- <condition>

## blocked 条件
- <condition>

## 输出 Schema
### <file1>
(yaml/schema 说明)
```

---

## 十二、扩展插槽

后续优化落在这 4 个插槽里，不改目录和 Skill 关系：

1. **Atlas 层增强（Skill 3）**：更好的 statement_type 识别、更完整的维度/分部展开
2. **经济重铸策略（Skill 4）**：maintenance capex 估计方法库、operating vs financing 分类规则库
3. **质量系数映射（Skill 7/8）**：把"证据 → 参数"做成显式函数
4. **审计问题库（Skill 9）**："反问模板"做成 rule library

---

**文档版本**: v3.0 (docs reorganization — 去 phase 化)
**更新日期**: 2026-03-15
