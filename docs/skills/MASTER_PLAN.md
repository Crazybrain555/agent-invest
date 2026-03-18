# 公司研究 Skills 体系总规划

> **核心公式：估值 = 利润 × 质量系数**
>
> 一切分析围绕两个因素展开：**未来可持续的经济利润（Owner Earnings）** 和 **对这个利润的确定性系数（Quality Coefficient）**

---

## 文档索引

- **Skills 总览与实施状态**：[docs/skills/README.md](README.md)
- **Per-skill 详细规格**：[docs/skills/specs/](specs/)
- **SEC/XBRL 技术参考**：[docs/skills/references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md](references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md)
- **MCP 配置指南**：[docs/MCP_SETUP_GUIDE.md](../MCP_SETUP_GUIDE.md)

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
          ingest_state.yaml                # 元数据（issuer_type/classifier_version/window/totals）
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

## 五、Event Taxonomy（subtype + 7 topic families）

> Skill 2 采用两层分类体系：**subtype（操作层）** + **topic family（检索层）**。
> 所有操作逻辑（action / merge / bucket plan）挂在 subtype 上，topic family 只做检索和索引。
> 详细分类器设计见 [Skill 2 规格](specs/skill2-sec-ingest-and-materialize-events.md)。

### 5.1 Topic Families（7）

| topic_family | 含义 |
|---|---|
| `periodic_core` | 周期性财报披露（annual / quarterly / interim） |
| `earnings_market_communication` | 业绩公告、指引、投资者沟通 |
| `transaction_balance_sheet` | M&A、融资、违约、破产等资产负债表事件 |
| `accounting_quality` | 审计师变更、重述、减值、重组 |
| `governance_shareholder` | 治理、高管变动、proxy、股东大会 |
| `capital_equity_mechanics` | 回购、分红、拆股、月度回报 |
| `legal_catchall` | 法律、监管、上市合规、其他重大事项 |

### 5.2 Subtypes（完整列表）

| topic family | subtypes |
|---|---|
| `periodic_core` | `annual_report`, `quarterly_report`, `interim_financial_report`, `annual_report_attachment` |
| `earnings_market_communication` | `earnings_results_announcement`, `guidance_update`, `investor_presentation` |
| `transaction_balance_sheet` | `mna_announcement`, `mna_proxy_material`, `financing_offering`, `debt_pricing_or_completion`, `credit_agreement_or_refinancing`, `default_or_covenant_breach`, `bankruptcy_or_receivership` |
| `accounting_quality` | `auditor_change`, `non_reliance_or_restatement`, `impairment_charge`, `restructuring_program` |
| `governance_shareholder` | `director_or_officer_change`, `charter_or_bylaw_change`, `compensation_or_equity_award`, `annual_meeting_proxy`, `proxy_supplement`, `meeting_vote_results` |
| `capital_equity_mechanics` | `share_repurchase_update`, `dividend_announcement`, `stock_split_or_rights_change`, `monthly_return` |
| `legal_catchall` | `listing_status_or_noncompliance`, `legal_or_regulatory_matter`, `other_material_event` |

### 5.3 主题优先级（多信号冲突时）

```text
accounting_quality
  > distress (bankruptcy/default) within transaction_balance_sheet
  > transaction_balance_sheet
  > periodic_core
  > earnings_market_communication
  > governance_shareholder
  > capital_equity_mechanics
  > legal_catchall
```

越靠前，越直接影响**利润可信度、存续概率、资本结构、未来路径**。

### 5.4 Event 归并的 5 种 group_type

| group_type | 适用情形 | group_key |
|---|---|---|
| `cycle` | annual / quarterly / interim / earnings / guidance / deck | `cycle:{period_end}:{fiscal_period}` |
| `meeting` | proxy / supplement / vote results | `meeting:{meeting_date}` |
| `transaction` | M&A / financing / default / bankruptcy | `transaction:{subfamily}:{hash(transaction_key)}` |
| `calendar_period` | monthly return / monthly buyback updates | `calendar_period:{subtype}:{YYYY-MM}` |
| `filing` | governance / legal / catch-all 单次事件 | `filing:{filed_at}:{accession_suffix}` |

### 5.5 event_id 生成规则

**event_id 不编码 topic / subtype**，只编码稳定 group key：

```python
def generate_event_id(group_type: str, group_key: str) -> str:
    safe = normalize_group_key(group_key)
    return f"sec_{group_type}_{safe}"
```

示例：`sec_cycle_2025-12-31_FY`、`sec_meeting_2026-05-07`、`sec_transaction_6f3a4e0bcb1d`

---

## 六、Canonical Content Buckets（14 个活跃 + 2 个保留）

> 所有事件统一一套 bucket 名字；缺失允许不生成；任何下游 skill 只读 buckets，不关心表单差异。
> bucket 设计原则：**服务下游估值研究问题**，不 mirror topic family。

### 6.1 活跃 Buckets

#### universal buckets（所有 event 都有）

| bucket_name | 服务的研究问题 | 典型内容 |
|---|---|---|
| `event_overview` | 发生了什么、何时发生、证据在哪 | `overview.md`, `timeline.json` |
| `exhibits_and_material_contracts` | 原始合同与材料入口，保证可审计性 | `exhibits_index.json`, `contracts_index.json` |

#### core valuation buckets

| bucket_name | 服务的研究问题 | 典型内容 |
|---|---|---|
| `financial_statements` | 收入/利润/现金流/资产负债的核心结构 | `narrative.md`, `tables/*.csv`（可选） |
| `notes_and_accounting` | 会计政策、估计、重述、或有事项、利润质量 | `notes.md` |
| `mdna_operating_review` | 增长驱动、利润变化、经营解释 | `mdna.md` |
| `risk_factors` | 折现率、情景权重、下行风险 | `risk_factors.md` |
| `business_and_strategy` | 商业模式、分部结构、竞争位置、护城河线索 | `business.md` |
| `capital_structure_and_liquidity` | 杠杆、再融资、稀释、回购、分红、流动性 | `liquidity.md` |
| `governance_and_compensation` | 激励一致性、治理约束、资本配置质量 | `governance.md` |
| `legal_and_regulatory` | 罚款、许可、诉讼、经营约束 | `legal.md` |

#### situation buckets

| bucket_name | 服务的研究问题 | 典型内容 |
|---|---|---|
| `press_release` | 市场叙事、短期预期、管理层 framing | `press_release.md` |
| `presentation_slides` | KPI、分部、战略口径、投资者沟通素材 | `deck_ref.json` |
| `mna_and_integration` | 并购经济学、协同、整合、交易条款 | `mna.md` |
| `restructuring_and_impairment` | 一次性 vs 结构性恶化、利润质量、margin reset | `restructuring.md` |

### 6.2 保留 / 延后 Buckets

| bucket_name | 归属 | 说明 |
|---|---|---|
| `structured_data` | **Skill 3 拥有** | Skill 2 只写 raw_xbrl refs 和 parse readiness |
| `sustainability_esg` | **移出 Skill 2** | 如原始 filing 含 ESG 材料，仅在 `exhibits_and_material_contracts` 建引用 + `event.yaml.tags` 加 `esg_available` |

### 6.3 Bucket Manifest 状态

每个 event 的 `bucket_manifest.json` 必须对每个 bucket 标注状态：

| 状态 | 含义 |
|---|---|
| `present` | 已完整产出 |
| `partial` | 有内容但不完整 |
| `ref_only` | 只有引用，无提取内容 |
| `not_applicable` | 此 event 不需要此 bucket |
| `missing_expected` | 预期存在但未能产出 |

### 6.4 Bucket Plan 推导规则

default bucket plan 由 event 的 **`subtypes_present` 的 union** 推导，而不是只看 primary_subtype 或 topic family。详见 [Skill 2 规格 §11.4](specs/skill2-sec-ingest-and-materialize-events.md)。

### 6.5 Bucket 抽取通用规则

Skill2 必须先构建 `source_document_catalog`（来自 raw `meta.yaml: documents[]`），然后按 bucket 需求做选择 + 抽取。详细规则见 [Skill 2 规格 §12](specs/skill2-sec-ingest-and-materialize-events.md)。

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
classifier_version: "topic7_subtype_v1"
taxonomy_version: "topic7_v1"
merge_policy_version: "v1"
bucket_plan_version: "v1"
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
| `accession` | string | N | SEC accession number（primary key） |
| `form` | string | N | 表单类型 |
| `filed_at` | date32 | N | 提交日期 |
| `period_end` | date32 | Y | SEC period_of_report |
| `has_xbrl` | bool | N | 是否有 XBRL |
| `is_amendment` | bool | N | 是否修订版 |
| `primary_doc` | string | N | 主文档原始文件名 |
| `filing_url` | string | N | SEC 原始 URL |
| `local_dir` | string | N | raw 本地目录相对路径 |
| `filing_action` | string | N | excluded/index_only/full_download/attach_only |
| `download_scope` | string | Y | full/scaffold/none |
| `primary_subtype` | string | Y | 分类结果 subtype |
| `primary_topic` | string | Y | topic family |
| `secondary_topics` | list[string] | Y | 次要 topics |
| `reason_codes` | list[string] | Y | 分类依据 |
| `classifier_version` | string | Y | 分类器版本 |
| `group_type_hint` | string | Y | 预计 group type |
| `group_key_candidate` | string | Y | 预计 group key |
| `event_id` | string | Y | 关联的 event_id |
| `attach_target_event_id` | string | Y | attach 目标 event_id |
| `filing_role` | string | Y | primary/related/attachment/amendment |
| `items` | list[string] | Y | 8-K items |
| `downloaded` | bool | N | 是否已下载 |

### 7.7 events_index.parquet Schema

| 字段名 | 类型 | Nullable | 说明 |
|--------|------|----------|------|
| `event_id` | string | N | 事件主键 |
| `ticker` | string | N | 股票 |
| `source` | string | N | 固定 sec_edgar |
| `primary_topic` | string | N | topic family |
| `primary_subtype` | string | N | 操作 subtype |
| `secondary_topics` | list[string] | Y | 次要 topics |
| `subtypes_present` | list[string] | Y | event 包含的所有 subtypes |
| `group_type` | string | N | cycle/meeting/transaction/calendar_period/filing |
| `group_key` | string | N | 归并键 |
| `occurred_at` | date32 | Y | 事件日期 |
| `filed_at` | date32 | Y | SEC filed date |
| `period_end` | date32 | Y | 财报事件必须 |
| `fiscal_period` | string | Y | FY/Q1/Q2/Q3/Q4/H1/H2 |
| `primary_accession` | string | Y | 主 accession |
| `accessions` | list[string] | Y | 关联 filings 列表 |
| `buckets_present` | list[string] | Y | 已产出的 buckets |
| `classifier_version` | string | Y | 分类器版本 |
| `bucket_plan_version` | string | Y | bucket plan 版本 |
| `merge_policy_version` | string | Y | 归并策略版本 |
| `status_rollup` | string | N | ok/partial/blocked/error/skipped |
| `updated_at` | timestamp[us, tz=UTC] | N | 更新时间 |

### 7.8 event.yaml

```yaml
event_id: "sec_cycle_2024-12-31_FY"
ticker: "SNDL"
source: "sec_edgar"
issuer_type: "fpi"

classification:
  primary_subtype: "annual_report"
  primary_topic: "periodic_core"
  secondary_topics: ["earnings_market_communication"]
  subtypes_present:
    - "annual_report"
    - "annual_report_attachment"
  classifier_version: "topic7_subtype_v1"
  taxonomy_version: "topic7_v1"
  reason_codes:
    - "FORM_40F"
    - "ANNUAL_REPORT_CYCLE"
  evidence:
    - source_type: "form"
      signal: "40-F"
      strength: "definitive"

grouping:
  group_type: "cycle"
  group_key: "cycle:2024-12-31:FY"
  grouping_basis: "period_end+fiscal_period"

occurred_at: "2024-12-31"
filed_at: "2025-03-28"
period_end: "2024-12-31"
fiscal_period: "FY"
fiscal_year_end: "12-31"

primary_filing:
  accession: "0000950170-25-040545"
  form: "40-F"
  raw_dir: "raw/sec/accessions/0000950170-25-040545"
  filing_role: "primary"
related_filings: []

tags: ["mjds", "ex99_fs", "ex99_mdna"]

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

## 八、SEC Filing Universe 与下载策略

> VMF（Valuation Materiality Filter）已从系统中**彻底删除**。
> Skill 2 采用**确定性策略栈**替代 VMF 的打分 + 年度预算机制。
> 详细策略设计见 [Skill 2 规格 §4-5](specs/skill2-sec-ingest-and-materialize-events.md)。

### 8.1 发行人类型识别（Domestic vs FPI）

- 以近年 filings 的 forms 推断 `issuer_type`
- 若出现 `20-F`/`40-F` → `issuer_type=fpi`
- 若出现 `10-Q` → `issuer_type=domestic`
- 推断结果写入 `events/sec/ingest_state.yaml: issuer_type`

### 8.2 Filing Universe（确定性白名单）

**Domestic**：`10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `8-K`, `8-K/A`, `DEF14A`, `DEFA14A`, `DEFR14A`, `PRE14A`, `DEFM14A`, `PREM14A`

**FPI**：`20-F`, `20-F/A`, `40-F`, `40-F/A`, `6-K`, `6-K/A`

### 8.3 核心原则

**一旦 filing 进入 discovery whitelist，默认立场是"收集"，不是"节约下载"。**

- 白名单内 filing 默认 `full_download`
- 只对少数确定性场景（如 `PRE14A` 等 definitive 前）使用 `index_only`
- 不存在因"预算不够"被降级的情况

### 8.4 确定性策略栈（替代 VMF）

Skill 2 的行为由 5 个版本化策略面控制，每个策略面可独立升级：

1. **`filing_universe_policy`** — discovery 白名单、排除列表
2. **`subtype_registry`** — subtype → action / topic / group_type / bucket_plan 映射
3. **`classifier_rulepack`** — 8-K item / 6-K signal / proxy family 分类规则
4. **`merge_policy`** — cycle / meeting / transaction 归并规则
5. **`bucket_plan_registry`** — bucket 期待值与选择规则

**核心优势**：同一 accession 在同一 `policy_version` 下，结果完全可复现。没有年度路径依赖。

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

**文档版本**: v4.0 (删除 VMF，采用 subtype + 7 topic families，bucket 重构为估值消费接口)
**更新日期**: 2026-03-17
