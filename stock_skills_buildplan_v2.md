# 公司研究 Skills 体系规划 v2

> **核心公式：估值 = 利润 × 质量系数**
>
> 一切分析围绕两个因素展开：**未来可持续的经济利润（Owner Earnings）** 和 **对这个利润的确定性系数（Quality Coefficient）**

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

> 这是 v2 的核心架构升级。

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

#### 验收用例（6-K 分类）

- **季度/半年业绩材料（应归入 financial_report）**：标题/附件描述同时出现"期间口径"与"财务结果/报表"，且常伴随 exhibits 99.*
- **非财报事项（应归入其他 taxonomy 类别）**：例如 monthly return、股本变动/治理/合规披露等
- **"presentation/guidance" 不能单独触发 financial_report**：如果仅出现 deck/指引更新但没有明确期间口径/中期报表信号，应归入 `earnings_release_guidance` 或 `other_material`

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

这会自然覆盖：
- AAPL 10-K：primary doc 就是主体
- SNDL 40-F：primary doc 可能是"外壳"，但 EX-99.2（FS）、EX-99.3（MD&A）会被 bucket 规则选中

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
report_date: "2024-12-31"          # 若 SEC 可提供；否则 null
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
  - filename: "sndl-ex99_3.htm"
    doc_type: "EX-99.3"
    description: "Management's Discussion and Analysis"
    category: "exhibits"
  - filename: "sndl-20241231_htm.xml"
    doc_type: "XML"
    description: "XBRL INSTANCE DOCUMENT"
    category: "xbrl"
  - filename: "sndl-20241231.xsd"
    doc_type: "EX-101.SCH"
    description: "XBRL TAXONOMY EXTENSION SCHEMA"
    category: "xbrl"
  - filename: "FilingSummary.xml"
    doc_type: "XML"
    description: "Filing Summary"
    category: "xbrl"

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
  index/0000950170-25-040545-index.html: {bytes: 5678, sha256: "...", url: "...-index.html"}
  submission/0000950170-25-040545.txt: {bytes: 9012, sha256: "...", url: ".../0000950170-25-040545.txt"}
  documents/sndl-20241231.htm: {bytes: ..., sha256: "...", url: ".../sndl-20241231.htm"}
  exhibits/sndl-ex99_2.htm: {bytes: ..., sha256: "...", url: ".../sndl-ex99_2.htm"}
  xbrl/sndl-20241231_htm.xml: {bytes: ..., sha256: "...", url: ".../sndl-20241231_htm.xml"}
completeness:
  has_index_json: true
  has_filing_index_html: true
  has_submission_txt: true
  has_primary_document: true
  has_xbrl_package: true          # 对 has_xbrl=true 的 filing 必须为 true
```

### 7.5 events/sec/ingest_state.yaml

```yaml
as_of: "2026-01-14"
issuer_type: "fpi"                     # domestic | fpi
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

### 7.6 events/sec/filings_index.parquet

```
Column Schema:
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
| event_id | string | Y | 关联的 event_id（可能后续分配） |
| bucket | string | N | periodic_core / event_stream |
| sixk_class | string | Y | interim_results / other_event（非6-K为空） |
| sixk_reasons | list[string] | Y | 6-K 分类命中原因 |
| is_event_stream | bool | N | 是否事件流 |
| vmf_triggered | bool | Y | 是否通过 VMF |
| vmf_hard_triggered | bool | Y | 是否硬触发 |
| vmf_reasons | list[string] | Y | 触发原因列表 |
| vmf_score | int32 | Y | VMF 打分 |
| items | list[string] | Y | 8-K items |
| downloaded | bool | N | 是否已下载 |
| download_level | string | N | metadata_only / primary / primary_plus_exhibits |
| source | string | N | 元数据来源 |
```

### 7.7 events/sec/events_index.parquet

```
Column Schema:
| 字段名 | 类型 | Nullable | 说明 |
|--------|------|----------|------|
| event_id | string | N | 事件主键（文件系统安全字符） |
| ticker | string | N | 股票 |
| source | string | N | 固定 sec_edgar |
| category | string | N | taxonomy 主类 |
| subtype | string | Y | 表单/子类 |
| issuer_type | string | Y | domestic/fpi/unknown |
| occurred_at | date32 | Y | 财报事件=period_end；其他事件=filed_at |
| filed_at | date32 | Y | SEC filed date |
| period_end | date32 | Y | 财报事件必须 |
| fiscal_period | string | Y | FY/Q1/Q2/Q3/Q4/H1/H2/unknown |
| primary_accession | string | Y | 主 accession |
| accessions | list[string] | Y | 关联 filings 列表 |
| forms | list[string] | Y | 对应 forms |
| raw_manifest_ref | string | Y | 指向 raw manifest.yaml |
| tags | list[string] | Y | 标签 |
| importance_hint | float32 | Y | 0~1 |
| buckets_present | list[string] | Y | 本事件已产出的 buckets |
| buckets_digest_json | string | Y | JSON：bucket -> {sha256, status} |
| status_rollup | string | N | ok/partial/blocked/error/skipped |
| updated_at | timestamp[us, tz=UTC] | N | 事件索引更新时间 |
```

### 7.8 events/sec/events/{event_id}/event.yaml

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

raw_refs:
  - accession: "0000950170-25-040545"
    filing_index_html: "raw/sec/accessions/.../index/0000950170-25-040545-index.html"
    submission_txt: "raw/sec/accessions/.../submission/0000950170-25-040545.txt"
    primary_document:
      filename: "sndl-20241231.htm"
      path: "raw/sec/accessions/.../documents/sndl-20241231.htm"
      sha256: "..."
    selected_materials:
      - role: "financial_statements"
        filename: "sndl-ex99_2.htm"
        path: "raw/sec/accessions/.../exhibits/sndl-ex99_2.htm"
        sha256: "..."
      - role: "mdna"
        filename: "sndl-ex99_3.htm"
        path: "raw/sec/accessions/.../exhibits/sndl-ex99_3.htm"
        sha256: "..."

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
  shares_float: 80000000
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
  maintenance_capex_estimate: 60000000

quality:
  coefficient_base: 0.72
  implied_multiple_base: 14.0
  advantage_period_years: 8
  discount_rate_base: 0.105
  components:
    financial_quality: 0..5
    moat: 0..5
    governance_capital_allocation: 0..5
    balance_sheet_resilience: 0..5
  confidence: 0..1

valuation:
  intrinsic_value_per_share:
    bear: 10.0
    base: 20.0
    bull: 30.0
  margin_of_safety_base: 0.62
  sensitivity_keys:
    - discount_rate
    - advantage_period_years
    - owner_earnings_margin
    - reinvestment_rate

audit:
  key_challenges_count: 7
  open_questions_count: 12

links:
  memo: "current/outputs/investment_memo.md"
  valuation_yaml: "current/analytics/valuation/valuation.yaml"
  audit_yaml: "current/analytics/diagnostics/audit.yaml"
  evidence_jsonl: "current/analytics/evidence/evidence.jsonl"
```

### 7.12 questions.jsonl / evidence.jsonl

路径：`current/gaps/questions.jsonl`、`current/analytics/evidence/evidence.jsonl`

```json
{"id":"Q_20260105_001","created_at":"2026-01-05","skill":"moat-inferencer","priority":"high","question":"客户集中度是否来自单一合同？续约条款是什么？","status":"open","related_artifacts":["current/analytics/diagnostics/moat.yaml"],"notes":""}
```

```json
{"id":"E_20260105_010","created_at":"2026-01-05","skill":"profit-quality-and-risk","claim":"应收增长显著快于收入，但主要来自并购并表","confidence":0.6,"sources":[{"type":"sec","accession":"...","event_id":"sec_fr_2024-12-31_FY","anchor":"MD&A"},{"type":"data","path":"current/analysis_data/economic/core_metrics.parquet","fields":["revenue","ar"]}]}
```

---

## 八、九个 Skills 总览

> 注：本 v2 规划目标是 9 个 Skills；Phase 1 实现 5 个（其余为 roadmap）。

| # | Skill | 状态 | 职责 | 对"利润×质量"贡献 |
|---|-------|------|------|------------------|
| 1 | `company-foundation` | 已实现 | 身份 + 市场口径（含 shares） | 估值分母/每股化基座 |
| 2 | `sec-ingest-and-materialize-events` | 重构中 | raw ingest + events materialize（含财报 buckets） | 证据池 + 事件数据库 |
| 3 | `xbrl-parse-financial-report-events` | 重构中 | per-event XBRL 解析 + 全局 atlas | 利润事实底座 |
| 4 | `recast-economic-statements` | 已实现 | 经济三表 + 核心指标 | Owner Earnings / ROIC |
| 5 | `profit-quality-and-risk` | 规划中 | 财报质量/操纵风险/利润可持续性 | 质量系数与情景下界 |
| 6 | `growth-driver-explorer` | 规划中 | 增长来源与 ROIIC/生命周期 | 未来利润路径 |
| 7 | `moat-inferencer` | 规划中 | 护城河 → 优势期 → 质量系数映射 | 质量系数主体 |
| 8 | `valuation-and-margin-of-safety` | 已实现 | 估值区间 + MOS + 敏感性 | 输出 IV vs 市场 |
| 9 | `cross-examination-audit` | 规划中 | 反问审计：找矛盾/遗漏/为什么便宜 | 提高确定性，防大错 |

---

## 九、Skill 详细规格

---

### Skill 1: `company-foundation`

> 合并：init-company-dossier + resolve-company-identity + update-market-snapshot

**职责边界**

- 初始化目录（如不存在）
- 解析 ticker → CIK/公司名/交易所等身份信息
- 拉取 market snapshot（price、shares outstanding、float、EV 等）
- **查漏补缺**：已有且新 → 跳过

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- 无（这是链条起点）

**输出**

- `company/{ticker}/company.yaml`
- `company/{ticker}/current/analysis_data/market_snapshot.yaml`
- `company/{ticker}/current/gaps/artifacts_state.yaml`（更新）
- `runs/{run_id}/meta.yaml`, `result.yaml`

**内部步骤**

1. 确保目录树存在（raw/events/current/runs + current 子目录）
2. 身份解析：SEC CIK、公司名、FY end、货币等
3. 市场口径：
   - `alpaca` 优先提供 `price`
   - shares / market cap / EV：优先 trading_mcp/SEC，其次 Yahoo 兜底
   - `market_cap` 默认用来源值，并用 `price * shares_outstanding` 交叉验证
   - `enterprise_value` 以 USD 输出
4. 写 evidence（身份来源、市场数据来源）

**查漏补缺规则**

- identity：若 `company.yaml` 已有 cik 且未 `force_refresh` → `skipped`
- market_snapshot：若 `as_of` 相同且文件存在且字段齐全 → `skipped`

**blocked 条件**

- 只有在"外部源完全不可用导致无法生成最小 company.yaml/market_snapshot.yaml"才 `blocked`

---

### Skill 2: `sec-ingest-and-materialize-events`

> **替代旧 Skill2 `collect-company-facts`**：raw ingest + events materialize

**职责边界（严格）**

- 下载/落盘 raw as-filed（不做投研拆解）
- 解析 filing index page（{accession}-index.html）建立 doc map
- 构建 filing 索引（events/sec/filings_index.parquet）与 event 索引（events/sec/events_index.parquet）
- **对每个 event 做 canonical buckets 归档**：
  - 财报事件：bucket 要尽可能完整（mdna/risk/business/notes/...）
  - 非财报事件：框架统一，内容允许渐进补齐
- 只做 **轻量** XBRL 发现与完整性校验（记录 instance/xsd/linkbases 是否存在），不做事实级解析

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | today | 窗口终点 |
| `lookback_years` | int | - | 10 | init 回溯 |
| `overlap_days` | int | - | 2 | maintenance overlap |
| `force_refresh` | bool | - | false | 强制重建索引/重下 |
| `download_policy` | enum | - | `periodic_full__events_vmf` | 周期性全量；事件按 VMF/预算 |

**Hard 依赖**

- `company/{TICKER}/company.yaml`（必须有 `cik`、`fiscal_year_end`）

**输出（必须）**

- raw：`raw/sec/accessions/{accession}/...`
- events：
  - `events/sec/ingest_state.yaml`
  - `events/sec/filings_index.parquet`
  - `events/sec/events_index.parquet`
  - `events/sec/events/{event_id}/event.yaml`
  - `events/sec/events/{event_id}/{bucket}/...`（至少 event_overview + exhibits_index；财报事件做完整）
- current/gaps：确保存在 `current/gaps/*`（可为空但文件要存在）

**模式判断逻辑**

```python
filings_parquet_path = events_sec_dir / "filings_index.parquet"
if not filings_parquet_path.exists() or force_refresh:
    mode = "init"
    fetch_start = as_of - timedelta(days=lookback_years * 365)
else:
    mode = "maintenance"
    existing_df = pd.read_parquet(filings_parquet_path)
    last_filed_at = existing_df["filed_at"].max()
    fetch_start = last_filed_at - timedelta(days=overlap_days)

fetch_end = as_of
sec_days = (fetch_end - fetch_start).days + 1
```

**内部步骤（概要）**

1. **Step 0 - 初始化 + 身份检查**
   - 确保 ticker 目录结构存在
   - 加载 `company.yaml` 并验证 `cik`
   - 判断 `issuer_type`（domestic vs fpi）

2. **Step 1 - SEC raw ingest**
   - 确定运行模式（init/maintenance）
   - 获取周期性核心 filings（含 FPI 6-K-Interim）
   - 获取事件流（Domestic: 8-K；FPI: 6-K-Event）
   - 对每个 accession：
     - 下载 index.json、{accession}-index.html
     - 解析 doc table → 构建 meta.yaml（documents 列表）
     - 分类下载文件到 documents/ / exhibits/ / xbrl/ / other/
     - 下载 submission.txt（如配置）
     - 写 manifest.yaml
   - VMF 筛选（仅事件流）

3. **Step 2 - Event taxonomy 分类**
   - 对每个 filing 确定 taxonomy category
   - 对 6-K 执行 `period AND results` 严格分类
   - 构建 event_id：
     - 财报事件：`sec_fr_{period_end}_{fiscal_period}`
     - 其他事件：`sec_{category_short}_{filed_at}_{accession_suffix}`

4. **Step 3 - Event materialization（buckets）**
   - 对每个事件构建 source document catalog（从 meta.yaml: documents[]）
   - 按 bucket 映射规则抽取内容
   - 写 event.yaml、raw_refs.json、bucket_manifest.json
   - 写各 bucket 目录内容

5. **Step 4 - 更新索引**
   - 写 events/sec/filings_index.parquet
   - 写 events/sec/events_index.parquet
   - 写 events/sec/ingest_state.yaml
   - 更新 current/gaps/artifacts_state.yaml

**event_id 生成规则**

```python
def generate_event_id(category, filing):
    if category == "financial_report":
        period_end = filing.get("period_end") or filing.get("report_date") or filing["filed_at"]
        fiscal_period = infer_fiscal_period(period_end, company_fye)
        return f"sec_fr_{period_end}_{fiscal_period}"
    else:
        # 非财报事件：用 filed_at + accession 后6位避免碰撞
        acc_suffix = filing["accession"].replace("-", "")[-6:]
        cat_short = category[:8]  # 截断到8字符
        return f"sec_{cat_short}_{filing['filed_at']}_{acc_suffix}"
```

**blocked 判定**

- `company.yaml` 缺 CIK → blocked
- SEC 拉取失败且本地无可用 filings_index.parquet → blocked

**partial 判定**

- 任一 accession raw 下载不完整 → partial
- 财报事件 period_end 无法识别 → partial + 写 gap
- buckets materialize 失败/缺关键 bucket → partial + 写 gap

**result.yaml components**

```yaml
components:
  sec_ingest:
    mode: init|maintenance
    window: {start: "...", end: "..."}
    totals: {filings_fetched: 0, accessions_new: 0, accessions_downloaded: 0}
    warnings: [...]
    errors: [...]
  events_materialize:
    totals: {events_upserted: 0, financial_report_events: 0}
    bucket_coverage: {mdna: 0.0, risk_factors: 0.0, ...}
```

---

### Skill 3: `xbrl-parse-financial-report-events`

> **替代旧 Skill3 `extract-xbrl-timeseries`**：per-event XBRL 解析 + 全局 atlas

**职责边界（严格）**

- 只处理 `events/sec/events_index.parquet` 中 `category=financial_report` 的事件
- 对每个财报事件：
  - 从 raw_refs 定位 raw/xbrl 文件集合
  - 深解析 XBRL/iXBRL（instance + linkbases），构建 per-event atlas
  - 落盘到该事件对象目录：`events/sec/events/{event_id}/structured_data/xbrl_atlas/*`
- 同时维护全局合并 atlas：`current/analysis_data/xbrl_atlas/*`

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `lookback_years` | int | - | 10 | 回溯年数 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `events/sec/events_index.parquet`
- 对于目标财报事件：其 `event.yaml` 与 `raw_refs` 指向的 raw/xbrl 必须存在
- `company.yaml`（用于 fiscal_period 推断/校验）

**输出**

- per-event：`events/sec/events/{event_id}/structured_data/xbrl_atlas/*`
  - `periods.yaml`、`facts.parquet`、`nodes.parquet`、`edges.parquet`、`paths.parquet`
- global：`current/analysis_data/xbrl_atlas/*`
  - 合并所有财报事件的 atlas 产物
- gaps：对缺失/无法解析的事件写入 `current/gaps/missing_data.yaml`

**内部步骤**

1. 读取 events_index，筛选 `category=financial_report` 且窗口内的事件
2. 对每个事件（增量模式：跳过已解析且 raw 未变化的）：
   - 从 event.yaml 的 raw_refs 定位 raw/xbrl 文件集
   - 识别 instance（iXBRL 常见 `*_htm.xml`；传统 `{stem}.xml`）
   - 解析 instance facts：concept + contextRef + unitRef + decimals + value
   - 解析 schema/linkbases：
     - `*_pre.xml`（presentation）→ 报表树（nodes/edges + role_uri）
     - `*_cal.xml`（calculation）→ 加总关系
     - `*_def.xml`（definition）→ 维度/成员
     - `*_lab.xml`（label）→ 标签
   - 产出 per-event atlas：facts/nodes/edges/paths/periods
3. 合并全局 atlas（append + 去重 fact_id）
4. 更新 event.yaml 的 `parse_status.xbrl_parsed`

**增量策略**

- 以事件的 `lineage.raw_manifest_sha256` + `xbrl.instance_filename sha256` 作为 cache key
- 未变化 → per-event 跳过
- 新事件/变化事件 → 只解析增量
- 全局 atlas 用"append + 去重（fact_id）"合并；并更新 periods.yaml

**Fallback 策略**

- 当本地 XBRL 缺失或解析失败时，可用 SEC "已抽取"XBRL / `sec_edgar_mcp.get_financials` 做 bootstrap
- 必须在 result/manifest 中记录降级原因

**blocked 判定**

- events_index 缺失 → blocked
- 目标窗口内财报事件全部无可解析 XBRL → blocked

**partial 判定**

- 部分事件 XBRL 缺失/解析失败，但至少一个事件成功 → partial（全局 atlas 仍更新可用部分）

---

### Skill 4: `recast-economic-statements`

> 三表重铸与核心指标（经济报表层）

**职责边界**

- 从 xbrl_atlas 出发，重铸：
  - operating vs financing 拆分
  - NOPAT、Invested Capital、ROIC
  - FCF、Owner Earnings（含 maintenance capex 估计）
- 输出"经济三表 + 指标宽表"
- 记录重铸规则与映射（recast_policy），用于可追溯与可迭代

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `policy_version` | string | - | "default" | 重铸策略标识 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/analysis_data/xbrl_atlas/nodes.parquet`
- `current/analysis_data/xbrl_atlas/edges.parquet`
- `current/analysis_data/xbrl_atlas/facts.parquet`
- `current/analysis_data/xbrl_atlas/periods.yaml`

**输出**

- `current/analysis_data/economic/recast_policy.yaml`
- `current/analysis_data/economic/economic_statements.parquet`
- `current/analysis_data/economic/core_metrics.parquet`
- evidence

**核心指标输出**

| 指标 | 说明 |
|------|------|
| `revenue` | 主营业收入 |
| `nopat` | 税后经营利润 |
| `invested_capital` | 投入资本 |
| `roic` | 投入资本回报率 |
| `cfo` | 经营现金流 |
| `capex` | 资本支出 |
| `maintenance_capex` | 维护性资本支出（估计） |
| `fcf` | 自由现金流 |
| `owner_earnings` | 股东盈余 |

**查漏补缺规则**

- policy_version 与输入未变 + atlas 未变 + 输出存在 → `skipped`
- policy_version 或 atlas 更新 → 重跑

**blocked 条件**

- atlas 缺失/不完整到无法产出最小 economic_statements → `blocked`

---

### Skill 5: `profit-quality-and-risk`

> 基于财报，发现风险，预测未来利润

**职责边界**

- 基于 economic_statements/core_metrics 做：
  - 利润质量（现金支撑、应计质量、操纵风险）
  - 财务风险（杠杆、流动性、表外压力）
  - 对未来 3-5 年经济利润的**风险拆解**
- 参考框架：Sloan、Piotroski、Beneish、Dechow、Financial Shenanigans

**Hard 依赖**

- `current/analysis_data/economic/economic_statements.parquet`
- `current/analysis_data/economic/core_metrics.parquet`
- `events/sec/events_index.parquet`
- `raw/sec/`（用于引用审计意见、会计政策、风险因素）

**输出**

- `current/analytics/diagnostics/profit_quality.yaml`
- `current/analytics/diagnostics/profit_risk_forecast.yaml`
- questions/evidence

**blocked 条件**

- economic/core 缺失 → `blocked`

---

### Skill 6: `growth-driver-explorer`

> 成长性进一步探索

**职责边界**

- 把增长拆成"能解释"的驱动：量/价/结构/地区/新产品/会计口径/并购 vs 内生
- 输出：再投资率、ROIIC、生命周期阶段

**Hard 依赖**

- `current/analytics/diagnostics/profit_quality.yaml`
- `current/analytics/diagnostics/profit_risk_forecast.yaml`
- `events/sec/events_index.parquet`
- `raw/sec/`

**输出**

- `current/analytics/diagnostics/growth_drivers.yaml`
- questions/evidence

**blocked 条件**

- 缺任一 hard 产物 → `blocked`

---

### Skill 7: `moat-inferencer`

> 护城河推断器 → 质量系数

**职责边界**

- 用可追溯证据识别护城河来源：Greenwald、Porter、Morningstar、Mauboussin
- 产出 **quality_coefficient**：把证据映射成估值参数

**Hard 依赖**

- `current/analytics/diagnostics/growth_drivers.yaml`
- `current/analytics/diagnostics/profit_quality.yaml`
- `events/sec/events_index.parquet`
- `raw/sec/`

**输出**

- `current/analytics/diagnostics/moat.yaml`
- `current/analytics/diagnostics/quality_coefficient.yaml`
- evidence/questions

**blocked 条件**

- 任一 hard 缺失 → `blocked`

---

### Skill 8: `valuation-and-margin-of-safety`

> 估值与安全边际

**职责边界**

- 以"经济利润 × 质量系数"组织估值：EPV / DCF / Residual Income
- 输出：bear/base/bull 估值区间、敏感性、下行保护来源

**Hard 依赖**

- `current/analysis_data/market_snapshot.yaml`
- `current/analysis_data/economic/core_metrics.parquet`
- `current/analysis_data/economic/economic_statements.parquet`
- `current/analytics/diagnostics/profit_risk_forecast.yaml`
- `current/analytics/diagnostics/growth_drivers.yaml`
- `current/analytics/diagnostics/quality_coefficient.yaml`

**输出**

- `current/analytics/valuation/valuation.yaml`
- `current/analytics/valuation/valuation_model.csv`
- `current/outputs/value_state.yaml`
- `current/outputs/investment_memo.md`
- evidence

**blocked 条件**

- 任一 hard 缺失 → `blocked`

---

### Skill 9: `cross-examination-audit`

> 反问和审计

**职责边界**

- 对比：管理层叙事（MD&A/风险因素） vs 数字（经济三表）
- 找矛盾、反向思维审计清单
- 明确：这会如何影响估值参数

**Hard 依赖**

- `current/outputs/value_state.yaml`
- `current/analytics/valuation/valuation.yaml`
- `current/analytics/diagnostics/quality_coefficient.yaml`
- `current/analytics/diagnostics/profit_quality.yaml`
- `current/analytics/diagnostics/growth_drivers.yaml`
- `events/sec/events_index.parquet`
- `raw/sec/`

**输出**

- `current/analytics/diagnostics/audit.yaml`
- `current/gaps/questions.jsonl`（追加）
- `current/analytics/evidence/evidence.jsonl`（追加）

**blocked 条件**

- 任一 hard 缺失 → `blocked`

---

## 十、SEC 下载策略：VMF（Valuation Materiality Filter）

### 10.0 发行人类型识别（Domestic vs FPI）

- 不依赖"CIK 有 FPI 标记"这类不稳定信号；以近年 filings 的 forms 推断 `issuer_type`
- **Init 首跑且 forms 为空时**：做一次轻量 probe（推荐顺序：试查 20-F/40-F → 10-Q → 6-K）
- 若出现 `20-F`/`20-F/A`/`40-F`/`40-F/A` → `issuer_type=fpi`
- 若出现 `10-Q`/`10-Q/A` → `issuer_type=domestic`
- 若主要为 `6-K` 且无 `10-Q`/`10-Q/A` → `issuer_type=fpi`（兜底）
- 推断结果写入 `events/sec/ingest_state.yaml: issuer_type`

### 10.1 周期性核心（Periodic Core）- 10年全量下载

**按发行人类型自动适配**：

| 发行人类型 | 识别方式 | 下载 Forms |
|-----------|----------|-----------|
| Domestic | 近年 filings 存在 10-K/10-Q（且无 20-F） | 10-K, 10-K/A, 10-Q, 10-Q/A, DEF14A |
| FPI | 近年 filings 存在 20-F/40-F 或主要为 6-K 且无 10-Q | 20-F, 20-F/A, 40-F, 40-F/A, 6-K（仅 Interim Financials/Results 子集） |

**下载内容（全部）**：
- `primary_document`（原始文件名落到 documents/）：永远下载
- `xbrl/`：若 `has_xbrl=true`
- `meta.yaml` + `manifest.yaml`：元数据与完整性追踪
- `index/`：index.json + {accession}-index.html
- `submission/`：{accession}.txt
- `exhibits/`：EX-*（排除 EX-101.*）
- **FPI 的 6-K（Interim）额外规则**：必须下载 exhibits/99.*

### 10.2 事件流（Event Stream）- 全量索引 + VMF 筛选下载

**事件流定义**：
- Domestic：8-K, 8-K/A
- FPI：6-K（排除 Periodic Core 的 Interim 子集）

**策略**：
- **索引**：10年全量（所有 accession 都记录到 filings_index.parquet）
- **下载**：只下载通过 VMF 筛选的 filings

### 10.3 VMF 三层筛选规则（仅事件流）

#### 层 1：硬触发（Hard Trigger）— 命中即下载，不受预算限制

**A) 8-K Item 硬触发**（仅 Domestic）：

| Item | 名称 | 估值材料性 |
|------|------|-----------|
| 2.02 | Results of Operations / Earnings | 直接影响 EPS/指引预期 |
| 4.01 | Auditor Change | 财务质量与可信度 |
| 4.02 | Non-Reliance / Restatement | 财务质量与可信度 |
| 2.04 | Default / Covenant breach | 现金流/折现率/生存概率 |
| 2.06 | Impairments | 盈利质量、资产质量 |
| 2.01 | Acquisition / Disposition | 未来现金流路径改变 |

**B) 附件类型硬触发**：

| 附件模式 | 说明 |
|---------|------|
| `exhibit 99.*` | 新闻稿/业绩公告/演示材料 |
| 附件描述含 "earnings release / results / guidance / investor presentation" | 业绩相关 |

**C) 标题/摘要关键词硬触发**：

```python
HARD_TRIGGER_KEYWORDS = [
    "restatement", "material weakness", "auditor", "going concern",
    "default", "covenant", "bankruptcy", "restructuring",
    "impairment", "write-down",
    "guidance", "outlook", "earnings", "results"
]
```

#### 层 2：打分筛选（Scoring）— 阈值控制

| 维度 | 关键词 | 权重 |
|------|--------|------|
| 现金流/融资与生存 | liquidity, refinancing, credit facility, default, covenant | 5 |
| 盈利/EPS/指引 | earnings, results, guidance, outlook, margin | 4 |
| 财务质量/会计可靠性 | restatement, auditor, material weakness | 3 |
| 资产质量与周期拐点 | impairment, restructuring | 3 |
| 并购/剥离 | acquisition, merger, disposition | 2 |

#### 层 3：年度预算 — 防过载

硬触发永远不受预算限制；每自然年最多下载 `vmf_annual_budget` 个评分事件（默认 20）。

---

## 十一、Artifact Ownership Matrix（产物归属与依赖）

| Artifact | Producer | Consumer | 用途 |
|---|---|---|---|
| `company/{TICKER}/company.yaml` | Skill1 | Skill2 | CIK/公司身份 |
| `current/analysis_data/market_snapshot.yaml` | Skill1 | Skill8 | 市场口径 |
| `raw/sec/accessions/{accession}/...` | Skill2 | Skill3 | 原始证据池 |
| `events/sec/filings_index.parquet` | Skill2 | Skill3 | filing 索引 |
| `events/sec/events_index.parquet` | Skill2 | Skill3/Phase2 skills | 事件索引 |
| `events/sec/events/{event_id}/...` | Skill2（buckets）/ Skill3（structured_data） | Phase2 skills | 事件数据包 |
| `current/analysis_data/xbrl_atlas/*` | Skill3 | Skill4 | 全局 XBRL atlas |
| `current/analysis_data/economic/*` | Skill4 | Skill8 | 经济三表与核心指标 |
| `current/analytics/diagnostics/*` | Skill5-7 | Skill8/9 | 诊断产物 |
| `current/outputs/value_state.yaml` | Skill8 | Skill9/编排器 | 估值底座总表 |

---

## 十二、编排器流程

### 12.1 固定队列

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

### 12.2 执行策略

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

## 十三、验收标准（TODO — 后续实现）

> 验收样本应覆盖：10-Q、10-K、20-F、40-F、6-K interim、8-K（至少 Item 2.02 或 4.02）

### 可机读指标

- `raw_completeness_rate`：完整 accessions / 目标 accessions
- `xbrl_package_ok_rate`：xbrl_ok accessions / has_xbrl accessions
- `taxonomy_precision`：分类正确率
- `bucket_coverage_by_category`：每类事件各 bucket 覆盖率
- `event_xbrl_parse_success_rate`：XBRL 解析成功率
- `atlas_period_coverage_years`：覆盖年份跨度
- `incremental_skip_ratio`：增量跳过比例

---

## 十四、扩展插槽

后续优化落在这 4 个插槽里，不改目录和 Skill 关系：

### 14.1 Atlas 层增强（Skill 3）
- 更好的 statement_type 识别
- 更完整的维度/分部展开

### 14.2 经济重铸策略（Skill 4）
- maintenance capex 估计方法库
- operating vs financing 分类规则库

### 14.3 质量系数映射（Skill 7/8）
- 把"证据 → 参数"做成显式函数

### 14.4 审计问题库（Skill 9）
- "反问模板"做成 rule library

---

## 十五、SKILL.md 写作模板

每个 Skill 的 SKILL.md 按以下模板：

```md
---
name: <skill-name>
description: <一句话：做什么，为估值服务的哪一层>
revision: "<YYYY-MM-DD>"   # optional
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
```yaml
# 字段说明
```
```

---

## 十六、实施路径

### 第一阶段：核心估值链（5 个 Skill）

| 顺序 | Skill | 产出 |
|------|-------|------|
| 1 | `company-foundation` | 身份 + 市场口径 |
| 2 | `sec-ingest-and-materialize-events` | raw + events + buckets |
| 3 | `xbrl-parse-financial-report-events` | per-event + 全局 atlas |
| 4 | `recast-economic-statements` | 经济三表 + Owner Earnings |
| 5 | `valuation-and-margin-of-safety` | 估值区间 + value_state |

### 第二阶段：分析能力补齐（4 个 Skill）

| Skill | 提升能力 |
|-------|---------|
| `profit-quality-and-risk` | 财务质量/操纵风险 |
| `growth-driver-explorer` | 成长性拆解 |
| `moat-inferencer` | 护城河 → 质量系数 |
| `cross-examination-audit` | 反问审计，防大错 |

---

**文档版本**: v2.1 (raw/events 解耦 + event taxonomy + canonical buckets)
**更新日期**: 2026-03-02
