# Skill 2: `sec-ingest-and-materialize-events`（权威规格）

> **替代旧 `collect-company-facts`**：raw ingest + events materialize
> **本文档是 Skill 2 的唯一权威规格**：当本文档与 MASTER_PLAN 或其他文档存在表述冲突时，以本文档为准。

**文档版本**: v3.0
**更新日期**: 2026-03-17

---

## 1. 核心设计决策

| 议题 | 结论 | 落地方式 |
|------|------|----------|
| VMF（Valuation Materiality Filter） | **彻底删除** | 去掉所有 score / budget 字段与逻辑，改成确定性策略栈 |
| 12 类 taxonomy | **退出主逻辑** | 改为 `subtype + 7 个 topic family` 两层结构 |
| bucket 体系 | **重构为估值消费接口** | 保留低十几个稳定 bucket，由 `subtypes_present` union 驱动默认 bucket plan |
| event_id | **不编码 category/topic/subtype** | 只编码稳定 group key，避免分类器升级导致 id 抖动 |
| Phase 划分 | **取消** | 按长期最优方向一次性构建 |

**删除 VMF 的根本原因**：VMF 的问题不是"分不准"，而是**同一 accession 是否被纳入，取决于当年已经下载了多少别的 filing**。这破坏了历史回溯一致性。删除后，同一 accession 在同一 `policy_version` 下结果完全可复现。

---

## 2. 架构总览

```text
discovery whitelist
  -> minimal index ingest（for every discovered filing in-universe）
  -> signal bundle extraction
  -> filing subtype classification
  -> subtype registry lookup
       {topic_family, filing_action, seed_policy, group_type, default_bucket_plan}
  -> full raw ingest（only for full_download / attach_only）
  -> event grouping / merge
  -> event-level rollup
       {primary_subtype, primary_topic, secondary_topics, subtypes_present}
  -> bucket materialization
  -> filings_index / events_index update
```

Skill 2 最合理的核心对象不是 filing，也不是旧 category，而是：

```text
raw filing signals
  -> filing subtype
  -> subtype spec
       {topic_family, filing_action, seed_policy, group_type, bucket_plan}
  -> event grouping
  -> event rollup
  -> canonical buckets
```

---

## 3. 职责边界

### 3.1 Skill 2 负责

#### raw 层

raw 层只做两件事：

1. **把纳入 discovery universe 的 filing 建成 accession 级证据对象**
2. **把 SEC 给你的目录、submission、原始文档、附件尽量原样镜像下来**

分两档：

- 对 `full_download` / `attach_only`：落完整 raw mirror
- 对 `index_only`：落**最小 raw scaffold**（`meta.yaml` + `manifest.yaml` + `index/index.json` + filing index page），以便分类器升级时可直接 promotion 而不必重新 discover

#### events 层

- filing-level subtype classification
- filing_action 判定
- event grouping / merge
- event-level rollup classification
- canonical buckets materialization
- 更新 `filings_index.parquet` / `events_index.parquet`

### 3.2 Skill 2 不负责

- facts 级 XBRL 解析（→ Skill 3）
- statement graph / atlas / normalized facts（→ Skill 3）
- OCR 作为默认路径（仅保留引用与 `partial` 标记）
- 数值重铸、估值参数推断、投资结论（→ Skill 4+）
- Forms 3/4/5、13D/G、S-/F-/424B* 这类平行宇宙的事件模型
- `importance_hint` 或任何形式的"重要性打分"（Skill 2 是证据规整层，不应混入估值判断）

### 3.3 Skill 2 与 Skill 3 的边界

边界必须硬：

- **Skill 2**：把"财报类 cycle event"规整好，准备 narrative buckets，写 raw xbrl refs，标明 raw xbrl availability
- **Skill 3**：只对这些 event 写 `structured_data/`

因此：
- `structured_data` 在 Skill 2 里不再算一个由它 materialize 的 bucket，只是一个**保留出口**
- Skill 2 只写 raw_xbrl availability / parse readiness / refs 到 event.yaml

---

## 4. 确定性策略栈（替代 VMF）

删除 VMF 后，Skill 2 的确定性规则栈由以下 5 个版本化策略面组成：

| # | 策略面 | 职责 |
|---|--------|------|
| 1 | `filing_universe_policy` | discovery 白名单、hard exclude families、preliminary proxy policy |
| 2 | `subtype_registry` | `subtype -> topic_family`、`subtype -> default_action`、`subtype -> seed_policy`、`subtype -> group_type`、`subtype -> default_bucket_plan`、`subtype -> precedence_rank` |
| 3 | `classifier_rulepack` | 8-K item rules、6-K signal rules、proxy family rules |
| 4 | `merge_policy` | cycle / meeting / transaction / calendar_period / filing 归并规则 |
| 5 | `bucket_plan_registry` | bucket 期待值、bucket 选择规则、bucket 优先级 |

**替代 VMF 的核心思想**：

- **缩窄 discovery universe**（明确白名单）
- **对纳入宇宙的 filing 采取"默认收集"而不是"默认节约下载"**
- **只对少数确定性场景使用 `index_only`**
- **把 action / merge / buckets 绑定到 subtype，而不是绑定到 category 或打分**

---

## 5. Filing Universe（纳入 / 排除边界）

### 5.1 适用发行人范围

默认适用：
- Exchange Act reporting operating companies
- US-listed domestic issuers
- US-listed foreign private issuers（FPI）

默认不作为主目标：
- ETF / mutual fund / closed-end fund / BDC 专属 form family
- ABS / trust / SPV / structured product issuers
- 只做注册声明、几乎无经营披露的壳体

### 5.2 Discovery 白名单

**Domestic issuer**

- `10-K`, `10-K/A`
- `10-Q`, `10-Q/A`
- `8-K`, `8-K/A`
- `DEF14A`, `DEFA14A`, `DEFR14A`
- `PRE14A`
- `DEFM14A`, `PREM14A`

**FPI issuer**

- `20-F`, `20-F/A`
- `40-F`, `40-F/A`
- `6-K`, `6-K/A`

### 5.3 默认排除

- insider / ownership：`3`, `4`, `5`
- beneficial ownership reporting：`SC 13D`, `SC 13D/A`, `SC 13G`, `SC 13G/A`
- Securities Act / prospectus / resale：`S-1`, `S-3`, `S-4`, `S-8`, `F-1`, `F-3`, `F-4`, `424B*`
- proxy plumbing：`PX14A6G`
- funds / structured products：`N-*`, `ABS-*`, `10-D`, `11-K`, `N-CSR`, `N-CSRS`
- EDGAR admin / misc：`UPLOAD`, `CORRESP`, `EFFECT`, `RW`, `POS AM`

这些 form 有独立研究价值，但不属于 Skill 2 的"经营公司 Exchange Act 事件证据流"问题域，应作为独立 skill 或未来扩展。

### 5.4 四种 filing_action

| filing_action | 含义 | 触发原则 |
|---|---|---|
| `excluded` | 不纳入 Skill 2 | discovery universe 外 |
| `index_only` | 只做最小 raw scaffold，不下正文 | 只用于少数确定性场景 |
| `full_download` | 下载完整 raw，可 seed / upgrade event | whitelist 内默认动作 |
| `attach_only` | 下载完整 raw，但不单独起 event | amendment / supplement / sidecar / follow-on |

### 5.5 默认 filing_action 映射

#### `full_download`（默认）

| form / 情形 | 默认动作 |
|---|---|
| `10-K`, `10-Q`, `20-F`, `40-F` | `full_download` |
| `8-K`, `6-K` | **默认 `full_download`**，然后由 subtype 决定 seed/attach |
| `DEF14A` | `full_download` |
| `PREM14A` | `full_download` |
| `DEFM14A` | `full_download`，若已存在对应 transaction event 则 attach-preferred |

#### `attach_only`

| form / subtype | 默认动作 |
|---|---|
| `10-K/A`, `10-Q/A`, `20-F/A`, `40-F/A` | 找到 base event 则 `attach_only` |
| `8-K/A`, `6-K/A` | 找到 base event 则 `attach_only` |
| `annual_report_attachment` | `attach_only` |
| `proxy_supplement` | `attach_only` |
| `meeting_vote_results` | `attach_only` |
| `debt_pricing_or_completion` | `attach_only` |
| 同 cycle 的 `investor_presentation` / supplemental PR | `attach_only` |

#### `index_only`

`index_only` 不再表示"分数不够高"，只表示**规则上不值得现在就下正文**。仅保留 3 类：

1. `PRE14A`：默认先 index，等 definitive
2. 明显 duplicate / superseded / shell-only filing
3. 支持 form family 但当前 policy 明确"不创建 event 只保留目录索引"的 filing

### 5.6 PRE / amendment 明确规则

- `PRE14A`：默认 `index_only`；45 天内仍无 definitive proxy，则 promotion 为 `full_download`
- `PREM14A`：直接 `full_download`（始终收集）
- `DEFA14A` / `DEFR14A`：`attach_preferred`；找不到目标 event 时 `full_download`
- `*/A`：优先 attach；匹配失败则 `full_download + orphan tag`

### 5.7 关键原则

**一旦 filing 进入 discovery whitelist，Skill 2 的默认立场应是"收集"，不是"省流量"。**
这是删除 VMF 之后必须切换的架构心态。

---

## 6. 分类体系（subtype + 7 topic families）

### 6.1 两层结构

- **topic family = 检索层**（7 个，用于索引和上层检索）
- **subtype = 操作层**（驱动 action / merge / bucket plan）
- **旧 12 category = 已退出主逻辑**（可选保留为兼容视图，不得驱动任何操作）

### 6.2 Topic families（7）

1. `periodic_core`
2. `earnings_market_communication`
3. `transaction_balance_sheet`
4. `accounting_quality`
5. `governance_shareholder`
6. `capital_equity_mechanics`
7. `legal_catchall`

### 6.3 Subtypes（完整列表）

| topic family | subtypes |
|---|---|
| `periodic_core` | `annual_report`, `quarterly_report`, `interim_financial_report`, `annual_report_attachment` |
| `earnings_market_communication` | `earnings_results_announcement`, `guidance_update`, `investor_presentation` |
| `transaction_balance_sheet` | `mna_announcement`, `mna_proxy_material`, `financing_offering`, `debt_pricing_or_completion`, `credit_agreement_or_refinancing`, `default_or_covenant_breach`, `bankruptcy_or_receivership` |
| `accounting_quality` | `auditor_change`, `non_reliance_or_restatement`, `impairment_charge`, `restructuring_program` |
| `governance_shareholder` | `director_or_officer_change`, `charter_or_bylaw_change`, `compensation_or_equity_award`, `annual_meeting_proxy`, `proxy_supplement`, `meeting_vote_results` |
| `capital_equity_mechanics` | `share_repurchase_update`, `dividend_announcement`, `stock_split_or_rights_change`, `monthly_return` |
| `legal_catchall` | `listing_status_or_noncompliance`, `legal_or_regulatory_matter`, `other_material_event` |

### 6.4 为什么 subtype 必须存在

以下逻辑都不是 `topic family` 能直接承载的：

- `annual_report` vs `annual_report_attachment`：topic 相同，action / merge 完全不同
- `earnings_results_announcement` vs `investor_presentation`：topic 相同，bucket plan 不同
- `financing_offering` vs `debt_pricing_or_completion`：topic 相同，但后者通常 attach
- `annual_meeting_proxy` vs `meeting_vote_results`：topic 相同，但后者通常更新已有 event
- `8-K`、`6-K`、`DEFA14A` 本身都是容器，不是语义

---

## 7. Filing-level classification 与 Event-level rollup

### 7.1 filing-level classification schema

```yaml
filing_classification:
  primary_subtype: "interim_financial_report"
  primary_topic: "periodic_core"
  secondary_topics: ["earnings_market_communication"]
  reason_codes:
    - "FORM_6K"
    - "DOC_DESC_INTERIM_REPORT"
    - "DOC_DESC_FINANCIAL_STATEMENTS"
  evidence:
    - source_type: "form"
      signal: "6-K"
      strength: "definitive"
    - source_type: "document_description"
      signal: "Interim Report"
      strength: "strong"
```

### 7.2 event-level rollup schema

```yaml
classification:
  primary_subtype: "interim_financial_report"
  primary_topic: "periodic_core"
  secondary_topics: ["earnings_market_communication"]
  subtypes_present:
    - "interim_financial_report"
    - "earnings_results_announcement"
    - "investor_presentation"
  classifier_version: "topic7_subtype_v1"
  taxonomy_version: "topic7_v1"
  reason_codes:
    - "ROLLUP_FROM_GROUP"
  evidence:
    - representative_filing: "0000000000-26-000001"
      source_type: "document_description"
      signal: "Interim Report"
      strength: "strong"
```

### 7.3 events_index.parquet 保留字段（扁平摘要）

不要把全量 evidence JSON 塞进 Parquet。只保留：

- `event_id`
- `primary_topic`
- `primary_subtype`
- `secondary_topics`
- `subtypes_present`
- `group_type`
- `group_key`
- `primary_accession`
- `accessions`
- `buckets_present`
- `classifier_version`
- `bucket_plan_version`
- `merge_policy_version`
- `status_rollup`
- `updated_at`

全量 `classification_evidence` 放 `event.yaml`，不放 Parquet 主索引。

---

## 8. 分类器设计

### 8.1 分类输入信号（统一抽取）

对每个 filing，先构建 `signal_bundle`，来源按强度排序：

1. **form family**（10-K / 8-K / 6-K / proxy）
2. **8-K items**（若有）
3. **document catalog**（doc_type / description / filename / category）
4. **filing title / SEC metadata description**
5. **primary document headings**（仅轻量解析）
6. **exhibit title / opening paragraphs**（可选）

统一信号组至少包括：

- `period_signal`, `fs_signal`, `results_signal`
- `guidance_signal`, `presentation_signal`
- `proxy_signal`, `vote_results_signal`, `mna_proxy_signal`
- `management_change_signal`, `charter_signal`
- `auditor_signal`, `restatement_signal`, `non_reliance_signal`
- `default_signal`, `bankruptcy_signal`
- `impairment_signal`, `restructuring_signal`
- `mna_signal`, `financing_signal`
- `capital_return_signal`, `dividend_signal`, `rights_change_signal`
- `share_repurchase_update_signal`, `monthly_return_signal`
- `legal_signal`, `listing_status_signal`
- `annual_report_attachment_signal`, `interim_report_signal`

### 8.2 总体流程（两段式）

1. **discovery + minimal index ingest**
   - 拿到 filing metadata
   - 拉 index page / document catalog
   - 生成初始 `signal_bundle`
   - 跑第一次 classification，得到 provisional action

2. **full raw ingest + final classify**
   - 对 `full_download` / `attach_only` 拉完整 raw
   - 用更多 signal 刷新 classification
   - 再做 merge / bucket materialize

### 8.3 `classify_filing()` 主入口

```python
def classify_filing(filing, signals, state):
    form = filing.form

    if form in EXCLUDED_FAMILIES:
        return excluded("EXCLUDED_FORM_FAMILY")

    # periodic core direct forms
    if form in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}:
        return classify_annual_family(form, filing, signals)

    if form in {"10-Q", "10-Q/A"}:
        return classify_quarterly_family(form, filing, signals)

    # proxy family
    if form in {"DEF14A", "DEFA14A", "DEFR14A", "PRE14A", "DEFM14A", "PREM14A"}:
        return classify_proxy_family(form, filing, signals, state)

    # current reports
    if form in {"8-K", "8-K/A"}:
        return classify_8k(form, filing, signals, state)

    if form in {"6-K", "6-K/A"}:
        return classify_6k(form, filing, signals, state)

    return excluded("UNSUPPORTED_FORM")
```

### 8.4 `classify_proxy_family()`

```python
def classify_proxy_family(form, filing, signals, state):
    if form == "PRE14A":
        if definitive_proxy_exists_soon_or_expected(filing, state):
            return cls(
                filing_action="index_only",
                primary_subtype="annual_meeting_proxy",
                primary_topic="governance_shareholder",
                reason_codes=["PRELIMINARY_PROXY_INDEX_UNTIL_DEFINITIVE"],
            )
        return cls(
            filing_action="full_download",
            primary_subtype="annual_meeting_proxy",
            primary_topic="governance_shareholder",
            reason_codes=["PRELIMINARY_PROXY_PROMOTED"],
        )

    if form == "PREM14A":
        return cls(
            filing_action="full_download",
            primary_subtype="mna_proxy_material",
            primary_topic="transaction_balance_sheet",
            reason_codes=["PRELIMINARY_MNA_PROXY_ALWAYS_COLLECT"],
        )

    if form == "DEFM14A":
        return attach_if_target_else_full(
            subtype="mna_proxy_material",
            topic="transaction_balance_sheet",
            target_key=infer_transaction_key(signals),
        )

    if form == "DEF14A":
        return cls(
            filing_action="full_download",
            primary_subtype="annual_meeting_proxy",
            primary_topic="governance_shareholder",
            reason_codes=["DEFINITIVE_PROXY"],
        )

    if form in {"DEFA14A", "DEFR14A"}:
        if signals.mna_signal:
            return attach_if_target_else_full(
                subtype="mna_proxy_material",
                topic="transaction_balance_sheet",
                target_key=infer_transaction_key(signals),
            )
        return attach_if_target_else_full(
            subtype="proxy_supplement",
            topic="governance_shareholder",
            target_key=infer_meeting_date(signals),
        )
```

### 8.5 `classify_8k()`

```python
def classify_8k(form, filing, signals, state):
    # === accounting quality first ===
    if signals.item_4_02 or signals.non_reliance_signal:
        return current_report_cls(form, "non_reliance_or_restatement", "accounting_quality")

    if signals.item_4_01 and not (signals.item_4_02 or signals.non_reliance_signal):
        return current_report_cls(form, "auditor_change", "accounting_quality")

    # === distress / survival ===
    if signals.item_1_03 or signals.bankruptcy_signal:
        return current_report_cls(form, "bankruptcy_or_receivership", "transaction_balance_sheet")

    if signals.item_2_04 or signals.default_signal:
        return current_report_cls(form, "default_or_covenant_breach", "transaction_balance_sheet")

    # === charges / restructuring ===
    if signals.item_2_06 or signals.impairment_signal:
        return current_report_cls(form, "impairment_charge", "accounting_quality")

    if signals.item_2_05 or signals.restructuring_signal:
        return current_report_cls(form, "restructuring_program", "accounting_quality")

    # === transaction / financing ===
    if signals.item_2_01 or signals.mna_signal or signals.has_ex2:
        return current_report_cls(form, "mna_announcement", "transaction_balance_sheet")

    if signals.item_1_01 or signals.item_2_03 or signals.item_3_02 or signals.financing_signal:
        subtype = choose_financing_subtype(signals)
        return current_report_cls(form, subtype, "transaction_balance_sheet")

    # === cycle / results / market communication ===
    if signals.item_2_02 or signals.results_signal:
        return current_report_cls(
            form,
            "earnings_results_announcement",
            "earnings_market_communication",
            secondary_topics=["earnings_market_communication"]
                if signals.guidance_signal or signals.presentation_signal else []
        )

    if signals.item_7_01 and signals.presentation_signal and not signals.results_signal:
        subtype = "guidance_update" if signals.guidance_signal else "investor_presentation"
        return current_report_cls(form, subtype, "earnings_market_communication")

    # === meeting / governance ===
    if signals.item_5_07 or signals.vote_results_signal:
        return attach_if_target_else_full(
            subtype="meeting_vote_results",
            topic="governance_shareholder",
            target_key=infer_meeting_date(signals),
        )

    if signals.item_5_02 or signals.management_change_signal:
        subtype = choose_governance_subtype(signals)
        return current_report_cls(form, subtype, "governance_shareholder")

    if signals.item_5_03 or signals.charter_signal:
        return current_report_cls(form, "charter_or_bylaw_change", "governance_shareholder")

    # === listing / capital / legal ===
    if signals.item_3_01 or signals.listing_status_signal:
        return current_report_cls(form, "listing_status_or_noncompliance", "legal_catchall")

    if signals.capital_return_signal:
        subtype = choose_capital_subtype(signals)
        return current_report_cls(form, subtype, "capital_equity_mechanics")

    if signals.legal_signal:
        return current_report_cls(form, "legal_or_regulatory_matter", "legal_catchall")

    # === fallback: no VMF, still collect ===
    return current_report_cls(form, "other_material_event", "legal_catchall")
```

### 8.6 `classify_6k()`

```python
def classify_6k(form, filing, signals, state):
    # annual sidecar
    if signals.annual_report_attachment_signal:
        return attach_if_target_else_full(
            subtype="annual_report_attachment",
            topic="periodic_core",
            target_key=infer_cycle_key(signals),
        )

    # cycle / interim
    if signals.period_signal and signals.fs_signal and signals.results_signal:
        return current_report_cls(form, "interim_financial_report", "periodic_core")

    if signals.period_signal and signals.results_signal and not signals.fs_signal:
        return current_report_cls(form, "earnings_results_announcement", "earnings_market_communication")

    if signals.guidance_signal and not signals.fs_signal:
        return current_report_cls(form, "guidance_update", "earnings_market_communication")

    if signals.presentation_signal and not signals.results_signal:
        return attach_if_cycle_else_full(
            subtype="investor_presentation",
            topic="earnings_market_communication",
            cycle_key=infer_cycle_key(signals),
        )

    # meeting / proxy
    if signals.mna_proxy_signal:
        return attach_if_target_else_full(
            subtype="mna_proxy_material",
            topic="transaction_balance_sheet",
            target_key=infer_transaction_key(signals),
        )

    if signals.proxy_signal:
        return current_report_cls(form, "annual_meeting_proxy", "governance_shareholder")

    if signals.vote_results_signal:
        return attach_if_target_else_full(
            subtype="meeting_vote_results",
            topic="governance_shareholder",
            target_key=infer_meeting_date(signals),
        )

    # capital / financing / distress
    if signals.monthly_return_signal:
        return current_report_cls(form, "monthly_return", "capital_equity_mechanics")

    if signals.share_repurchase_update_signal:
        return current_report_cls(form, "share_repurchase_update", "capital_equity_mechanics")

    if signals.dividend_signal or signals.rights_change_signal:
        subtype = choose_capital_subtype(signals)
        return current_report_cls(form, subtype, "capital_equity_mechanics")

    if signals.bankruptcy_signal:
        return current_report_cls(form, "bankruptcy_or_receivership", "transaction_balance_sheet")

    if signals.default_signal:
        return current_report_cls(form, "default_or_covenant_breach", "transaction_balance_sheet")

    if signals.financing_signal:
        subtype = choose_financing_subtype(signals)
        return current_report_cls(form, subtype, "transaction_balance_sheet")

    if signals.mna_signal:
        return current_report_cls(form, "mna_announcement", "transaction_balance_sheet")

    # accounting / governance / legal
    if signals.non_reliance_signal or signals.restatement_signal:
        return current_report_cls(form, "non_reliance_or_restatement", "accounting_quality")

    if signals.auditor_signal:
        return current_report_cls(form, "auditor_change", "accounting_quality")

    if signals.impairment_signal:
        return current_report_cls(form, "impairment_charge", "accounting_quality")

    if signals.restructuring_signal:
        return current_report_cls(form, "restructuring_program", "accounting_quality")

    if signals.management_change_signal:
        subtype = choose_governance_subtype(signals)
        return current_report_cls(form, subtype, "governance_shareholder")

    if signals.listing_status_signal:
        return current_report_cls(form, "listing_status_or_noncompliance", "legal_catchall")

    if signals.legal_signal:
        return current_report_cls(form, "legal_or_regulatory_matter", "legal_catchall")

    return current_report_cls(form, "other_material_event", "legal_catchall")
```

### 8.7 多信号冲突时的优先级

#### 证据强度优先级

```text
form-family semantics / 8-K item
  > explicit doc_type / exhibit class
  > exhibit description / title
  > SEC filing title / metadata description
  > primary headings
  > keyword fallback
```

#### 主题优先级（同强度时）

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

设计原因：越靠前，越直接影响**利润可信度、存续概率、资本结构、未来路径**。

### 8.8 无法匹配时的 fallback

- form 在白名单内，但未命中任何 subtype → `other_material_event` + `full_download`（**注意：不再有 VMF 的 index_only fallback**）
- 若 `period_end` / `meeting_date` / `transaction_key` 无法识别：
  - 不得阻塞 raw ingest
  - event_id 回退到 `filed_at + accession_suffix`
  - 同时写 `missing_data.yaml`

---

## 9. Event 归并规则

### 9.1 group_type（5 种）

| group_type | 适用情形 | group_key |
|---|---|---|
| `cycle` | annual / quarterly / interim / earnings / guidance / deck | `cycle:{period_end}:{fiscal_period}` |
| `meeting` | proxy / supplement / vote results | `meeting:{meeting_date}` |
| `transaction` | M&A / financing / default / bankruptcy | `transaction:{subfamily}:{hash(transaction_key)}` |
| `calendar_period` | monthly return / monthly buyback updates | `calendar_period:{subtype}:{YYYY-MM}` |
| `filing` | governance / legal / catch-all 单次事件 | `filing:{filed_at}:{accession_suffix}` |

### 9.2 `cycle` 归并规则

适用 subtypes：
- `annual_report`, `quarterly_report`, `interim_financial_report`, `annual_report_attachment`
- `earnings_results_announcement`, `guidance_update`, `investor_presentation`（若同 cycle）

关键含义：
- 10-Q + 8-K Item 2.02 + earnings deck → 合并成一个 cycle event
- 6-K results announcement + 6-K interim report → 合并成一个 cycle event
- 20-F + annual report attachment 6-K → 合并成一个 FY cycle event

#### annual report 主 filing 与 attachment 的归并

- `20-F` / `40-F` / `10-K` seed `cycle` event
- `annual_report_attachment` 6-K 默认 attach 到同一 `cycle`
- 如果 sidecar 先到、annual 主 filing 后到：
  - 先 seed provisional cycle event
  - 后续 annual filing 到来时**升级 primary_subtype 为 `annual_report`**
  - event_id 不变

### 9.3 `meeting` 归并规则

- `DEF14A` seed `meeting` event
- `PRE14A` 默认只 index，不 seed event
- `DEFA14A` / `DEFR14A` attach 到 meeting event
- `8-K Item 5.07` / `6-K vote results` attach 到同一 meeting event
- `meeting_vote_results` 更新 `event_overview.timeline` 与 `governance_and_compensation`

### 9.4 `transaction` 归并规则

- 默认作为 related filing attach 到已有 transaction event
- 只在以下情况才起新 event：
  1. 找不到已存在 transaction key
  2. instrument / deal economics 已实质变成另一个东西
  3. 前次 transaction event 已明确 closed / terminated，且这是新的 episode

### 9.5 amendment 如何 attach / override

- raw 永远不覆盖
- event buckets 可以 override，但必须记录 superseded lineage
- 对 periodic event：amended section 替换对应 bucket 的当前版本，timeline 记录 amendment
- 对 supplement / vote results：以 append 为主，不轻易替换
- 对 classification：amendment 只有在新证据更强时才允许升级 primary_subtype
- 匹配失败时，不得丢弃：改成 `full_download + orphan tag`

---

## 10. event_id 生成规则

**不要把 topic / subtype 编进 event_id。**

```python
def generate_event_id(group_type: str, group_key: str) -> str:
    safe = normalize_group_key(group_key)
    return f"sec_{group_type}_{safe}"
```

示例：

- `sec_cycle_2025-12-31_FY`
- `sec_meeting_2026-05-07`
- `sec_transaction_6f3a4e0bcb1d`
- `sec_calendar_period_monthly_return_2026-02`
- `sec_filing_2026-02-19_040545`

好处：

- 分类器升级不改 event_id
- cycle event 可从 `earnings_results_announcement` 升级成 `quarterly_report`
- 交易 event 可从 `financing_offering` 后续吸收 `debt_pricing_or_completion`
- 历史回溯与 classifier 升级时更稳定

---

## 11. Canonical Buckets（估值消费导向）

### 11.1 Bucket 集

#### universal buckets（所有 event 都有）

- `event_overview`
- `exhibits_and_material_contracts`

#### core valuation buckets

- `financial_statements`
- `notes_and_accounting`
- `mdna_operating_review`
- `risk_factors`
- `business_and_strategy`
- `capital_structure_and_liquidity`
- `governance_and_compensation`
- `legal_and_regulatory`

#### situation buckets

- `press_release`
- `presentation_slides`
- `mna_and_integration`
- `restructuring_and_impairment`

#### reserved / deferred

- `structured_data`：**Skill 3 拥有**，Skill 2 只写 refs
- `sustainability_esg`：**移出 Skill 2**，如原始 filing 含 ESG 材料，仅在 `exhibits_and_material_contracts` 中建立引用并在 `event.yaml.tags` 增加 `esg_available`

### 11.2 每个 bucket 服务的研究问题

| bucket | 服务的问题 |
|---|---|
| `event_overview` | 发生了什么、何时发生、证据在哪 |
| `press_release` | 市场叙事、短期预期、管理层 framing |
| `presentation_slides` | KPI、分部、战略口径、投资者沟通素材 |
| `financial_statements` | 收入/利润/现金流/资产负债的核心结构 |
| `notes_and_accounting` | 会计政策、估计、重述、或有事项、利润质量 |
| `mdna_operating_review` | 增长驱动、利润变化、经营解释 |
| `risk_factors` | 折现率、情景权重、下行风险 |
| `business_and_strategy` | 商业模式、分部结构、竞争位置、护城河线索 |
| `governance_and_compensation` | 激励一致性、治理约束、资本配置质量 |
| `capital_structure_and_liquidity` | 杠杆、再融资、稀释、回购、分红、流动性 |
| `mna_and_integration` | 并购经济学、协同、整合、交易条款 |
| `restructuring_and_impairment` | 一次性 vs 结构性恶化、利润质量、margin reset |
| `legal_and_regulatory` | 罚款、许可、诉讼、经营约束 |
| `exhibits_and_material_contracts` | 原始合同与材料入口，保证可审计性 |

### 11.3 Bucket 设计原则

1. bucket **不 mirror topic family**
2. bucket **只回答下游估值研究问题**
3. default bucket plan 由 **`subtypes_present` 的 union** 推导
4. `event_overview` 与 `exhibits_and_material_contracts` 始终存在
5. bucket_manifest 必须区分状态：

| 状态 | 含义 |
|---|---|
| `present` | 已完整产出 |
| `partial` | 有内容但不完整 |
| `ref_only` | 只有引用，无提取内容 |
| `not_applicable` | 此 event 不需要此 bucket |
| `missing_expected` | 预期存在但未能产出 |

### 11.4 subtype → default bucket plan

#### cycle / periodic

**`annual_report`**
- required: `event_overview`, `financial_statements`, `notes_and_accounting`, `mdna_operating_review`, `risk_factors`, `business_and_strategy`, `exhibits_and_material_contracts`
- optional: `capital_structure_and_liquidity`, `press_release`, `presentation_slides`

**`quarterly_report` / `interim_financial_report`**
- required: `event_overview`, `financial_statements`, `notes_and_accounting`, `mdna_operating_review`, `capital_structure_and_liquidity`, `exhibits_and_material_contracts`
- optional: `risk_factors`, `press_release`, `presentation_slides`

**`earnings_results_announcement` / `guidance_update`**
- required: `event_overview`, `press_release`, `exhibits_and_material_contracts`
- optional: `presentation_slides`, `mdna_operating_review`, `financial_statements`

**`investor_presentation`**
- required: `event_overview`, `presentation_slides`, `exhibits_and_material_contracts`
- optional: `press_release`

#### transaction / balance sheet

**`mna_announcement` / `mna_proxy_material`**
- required: `event_overview`, `mna_and_integration`, `exhibits_and_material_contracts`
- optional: `press_release`, `capital_structure_and_liquidity`, `legal_and_regulatory`, `financial_statements`

**`financing_offering` / `credit_agreement_or_refinancing` / `debt_pricing_or_completion`**
- required: `event_overview`, `capital_structure_and_liquidity`, `exhibits_and_material_contracts`
- optional: `press_release`, `presentation_slides`, `risk_factors`

**`default_or_covenant_breach` / `bankruptcy_or_receivership`**
- required: `event_overview`, `capital_structure_and_liquidity`, `risk_factors`, `exhibits_and_material_contracts`
- optional: `legal_and_regulatory`, `press_release`

#### accounting / quality

**`auditor_change`**
- required: `event_overview`, `notes_and_accounting`, `governance_and_compensation`, `exhibits_and_material_contracts`

**`non_reliance_or_restatement`**
- required: `event_overview`, `notes_and_accounting`, `risk_factors`, `exhibits_and_material_contracts`

**`impairment_charge` / `restructuring_program`**
- required: `event_overview`, `restructuring_and_impairment`, `exhibits_and_material_contracts`
- optional: `press_release`, `mdna_operating_review`, `capital_structure_and_liquidity`

#### governance / shareholder

**`director_or_officer_change` / `charter_or_bylaw_change` / `compensation_or_equity_award`**
- required: `event_overview`, `governance_and_compensation`, `exhibits_and_material_contracts`

**`annual_meeting_proxy` / `proxy_supplement` / `meeting_vote_results`**
- required: `event_overview`, `governance_and_compensation`, `exhibits_and_material_contracts`
- optional: `press_release`

#### capital / equity mechanics

**`share_repurchase_update` / `dividend_announcement` / `stock_split_or_rights_change` / `monthly_return`**
- required: `event_overview`, `capital_structure_and_liquidity`, `exhibits_and_material_contracts`
- optional: `press_release`

#### legal / catch-all

**`listing_status_or_noncompliance` / `legal_or_regulatory_matter`**
- required: `event_overview`, `legal_and_regulatory`, `exhibits_and_material_contracts`
- optional: `risk_factors`, `press_release`

**`other_material_event`**
- required: `event_overview`, `exhibits_and_material_contracts`
- optional: one best-fit bucket only（不得伪造内容完整性）

#### cycle event 的 union 推导

当一个 `cycle` event 吸收多个 subtype（例如先是 `earnings_results_announcement`，后续又吸收 `quarterly_report` 和 `investor_presentation`），default bucket plan 应为所有 `subtypes_present` 的 bucket 并集。

---

## 12. Bucket 抽取规则（详细版）

### 12.1 source document catalog

每个 event 必须先建立 `source_document_catalog`，来源于 raw `meta.yaml: documents[]`，至少包含：`filename`, `doc_type`, `description`, `category`, `bytes`, `path`, `sha256`

### 12.2 Document selection 统一规则

```python
def select_source_docs(bucket_name, catalog, filing_context):
    candidates = score_docs(catalog, bucket_name, filing_context)
    chosen = top_k(candidates, k=bucket_policy[bucket_name].max_docs)
    return chosen
```

评分因子：`doc_type` 精确命中 > `description`/title 命中 > `category` > `bytes`（防选空壳）> `mime_type`（HTML/TXT > PDF）

### 12.3 财报事件 Buckets 抽取

#### `financial_statements` / `notes_and_accounting`

1. 若 `xbrl.has_xbrl=true`：数字解析交给 Skill 3；Skill 2 只写 `financial_statements/narrative.md` + `notes_and_accounting/notes.md` + raw refs
2. 对 40-F / FPI 6-K interim：选 exhibits 中 description 命中 `financial statements`, `interim report`, `unaudited condensed consolidated`, `audited financial statements`；多份命中按 `bytes` 与 `signal_strength` 选前 1~2 份

#### `mdna_operating_review`

1. exhibits 命中：`management's discussion`, `MD&A`, `operating and financial review`, `OFR`
2. 否则 primary doc 按表单 heading：
   - 10-K：`Item 7`
   - 10-Q：`Part I Item 2`（**显式排除 `Part II Item 2`**）
   - 20-F：`Item 5`
   - 40-F：优先 EX-99.*
3. 6-K interim：EX-99.* 优先，primary 兜底

#### `risk_factors`

- 10-K：`Item 1A`
- 10-Q：`Part II Item 1A`
- 20-F：`Item 3.D`
- 40-F：优先 AIF / annual report attachment / EX-99.*

#### `business_and_strategy`

- 10-K：`Item 1`
- 20-F：`Item 4`
- 10-Q：**通常没有 Business（现实，不是漏抓）**
- 40-F：优先 AIF / annual report attachment / EX-99.*

#### `governance_and_compensation`

- 财报事件目录里通常只放 ref
- 主内容由 proxy / meeting event 承担

### 12.4 非财报事件 Buckets 抽取

每个非财报 event **至少**产出：`event_overview/overview.md` + `event_overview/timeline.json` + `exhibits_and_material_contracts/exhibits_index.json`

然后按 primary_topic 补充对应 buckets：

#### `earnings_market_communication`

- 来源优先级：EX-99.* earnings release > earnings presentation > 8-K/6-K primary doc
- 抽取重点：本期关键指标（收入/利润/EPS/segment/cash）、指引变化、核心驱动解释

#### `transaction_balance_sheet`（M&A）

- 来源优先级：EX-2.* merger/acquisition agreement > DEFM14A/proxy > EX-99.* press release > 8-K primary
- 抽取重点：交易对手/标的/对价/支付方式、估值口径/closing conditions/termination fee、管理层 rationale/协同

#### `transaction_balance_sheet`（融资）

- 来源优先级：EX-10.* credit agreement/indenture > 8-K Item 1.01/2.03/3.02 > 6-K financing exhibits > press release
- 抽取重点：融资类型/金额/到期日/利率、抵押/担保/优先级/转换条款、covenant/用途/refinancing

#### `transaction_balance_sheet`（违约/破产）

- 来源优先级：8-K Item 2.04 / Item 1.03 > waiver/amendment/forbearance > lender notices
- 抽取重点：breached covenant/default 类型、涉及金额/工具、加速/豁免/宽限期、对流动性/持续经营影响

#### `accounting_quality`

- 来源优先级：8-K Item 4.01/4.02 > auditor letter > press release
- 抽取重点：auditor change/non-reliance/restatement 的性质、影响期间/范围、原因类型、remediation plan

#### `accounting_quality`（减值/重组）

- 来源优先级：8-K Item 2.05/2.06 > EX-99.* restructuring announcement > management commentary
- 抽取重点：现金/非现金 charges、涉及业务/资产/地区/人数、预计完成时间/savings、对利润质量/现金流影响

#### `governance_shareholder`（高管/治理）

- 来源优先级：8-K Item 5.02/5.03 > employment/separation/charter amendment > 6-K board appointment
- 抽取重点：谁变动/何时生效/职位/原因、compensation/severance 关键条款、章程/细则/投票权修改

#### `governance_shareholder`（proxy/meeting）

- 来源优先级：DEF14A/DEFM14A/DEFA14A > 8-K Item 5.07 > 6-K AGM notice/vote results
- 抽取重点：meeting_date/record_date、proposals/board recommendation、vote results 与通过/否决

#### `capital_equity_mechanics`

- 来源优先级：buyback/dividend/split announcement > 8-K Item 3.01/3.03 > 6-K monthly return
- 抽取重点：回购授权/已执行规模/均价、分红金额/除权日/支付日、拆股合股/股本变化

#### `legal_catchall`

- 来源优先级：settlement/consent/agency letter > 8-K Item 8.01 / 6-K legal > press release
- 抽取重点：对手方/监管机构、案由/进度/金额、对经营/现金/声誉影响

#### `other_material_event`

- 最多补一个 best-fit bucket，不伪装成"完整解析"
- 抽取重点：发生了什么、为什么可能重要、证据在哪、为什么未能更精确归类

### 12.5 PDF/图片处理规则

- 不强制 OCR
- 放引用 + 可选纯文本提取
- `parse_status` 标 `partial`，写 gap
- 若 filing 命中 `presentation_signal` 且 deck 为 PDF：必须下载 PDF 原件并保留 ref

---

## 13. Raw Ingest 规则

### 13.1 `full_download` / `attach_only` 默认下载

- `index.json`
- `{accession}-index.html`
- `submission/{accession}.txt`
- `primary_document`
- 全部非 XBRL exhibits
- 全部 XBRL 包（若 `has_xbrl=true`）
- 其他附件按类型决定（图片可只留 metadata）

### 13.2 `index_only` 最小 scaffold

- `meta.yaml`
- `manifest.yaml`
- `index/index.json`
- filing index page

### 13.3 完整性

- 若 raw 下载不完整：
  - `manifest.yaml.completeness` 显式标注缺口
  - 事件仍可继续 materialize 已有内容
  - overall `status=partial`

---

## 14. 输入参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | today | 窗口终点 |
| `lookback_years` | int | - | 10 | init 回溯 |
| `overlap_days` | int | - | 2 | maintenance overlap |
| `force_refresh` | bool | - | false | 强制重建索引/重下 |
| `proxy_preliminary_policy` | enum | - | `index_until_definitive` | 初步 proxy 的处理策略 |

> 注意：不再有 `download_policy`、`vmf_score_threshold`、`vmf_annual_budget` 参数。

---

## 15. Hard 依赖

- `company/{TICKER}/company.yaml`
  - 必须至少有：`ticker`、`cik`、`fiscal_year_end`

---

## 16. 输出

### raw layer

- `raw/sec/accessions/{accession}/meta.yaml`
- `raw/sec/accessions/{accession}/manifest.yaml`
- `raw/sec/accessions/{accession}/index/*`
- `raw/sec/accessions/{accession}/submission/{accession}.txt`
- `raw/sec/accessions/{accession}/documents/*`
- `raw/sec/accessions/{accession}/exhibits/*`
- `raw/sec/accessions/{accession}/xbrl/*`
- `raw/sec/accessions/{accession}/other/*`

### events layer

- `events/sec/ingest_state.yaml`
- `events/sec/filings_index.parquet`
- `events/sec/events_index.parquet`
- `events/sec/events/{event_id}/event.yaml`
- `events/sec/events/{event_id}/raw_refs.json`
- `events/sec/events/{event_id}/bucket_manifest.json`
- `events/sec/events/{event_id}/{bucket}/...`

### current/gaps

- `current/gaps/artifacts_state.yaml`
- `current/gaps/missing_data.yaml`（如有缺口）
- `current/gaps/questions.jsonl`（仅当需要显式提问时）

---

## 17. 内部步骤

### Step 0 - 初始化 + 身份检查

1. 确保目录结构存在
2. 读取 `company.yaml`
3. 验证 `cik`、`fiscal_year_end`
4. 推断 `issuer_type`：`domestic | fpi`
5. 读取历史 `ingest_state.yaml` / `filings_index.parquet`

### Step 1 - Filing discovery + scope decision

1. 按 `issuer_type` 加载 discovery 白名单
2. 在 `[fetch_start, fetch_end]` 取 filings metadata
3. 对每个 filing：
   - 拉 index page / document catalog
   - 构建 signal_bundle
   - 判断 `excluded / index_only / full_download / attach_only`
4. 先写入 staging `filings_index`

### Step 2 - Raw ingest

1. 对 `full_download` / `attach_only` 的 accession 下载 raw
2. 对 `index_only` 落最小 scaffold
3. 解析 `index.json` 与 `{accession}-index.html`
4. 生成 `meta.yaml` + `manifest.yaml`
5. 记录 `xbrl` 完整性状态

### Step 3 - Final classification

1. 用完整 raw 信号刷新 classification
2. 得到最终 `filing_action` / `primary_subtype` / `primary_topic` / `secondary_topics` / `evidence`
3. 若分类器升级导致 action 变化，可在 maintenance 中重新 materialize

### Step 4 - Event grouping

1. 基于 subtype 生成 `group_key`
2. 把相关 filings 归并到 event
3. 判定 primary filing 与 related filings
4. amendments / supplements / attachments 默认 attach
5. 计算 event-level rollup（`primary_subtype`, `subtypes_present`）

### Step 5 - Bucket materialization

1. 建立 `source_document_catalog`
2. 按 `subtypes_present` 的 union 确定 bucket plan
3. 先写 `event_overview` 与 `exhibits_index`
4. 再按规则写对应 buckets
5. 写 `event.yaml`、`raw_refs.json`、`bucket_manifest.json`
6. 如某 bucket 无法抽取：不伪造文件，在 `bucket_manifest.json` 标 `missing_expected` / `partial`，写入 `missing_data.yaml`

### Step 6 - 更新索引与 current/gaps

1. 更新 `events/sec/filings_index.parquet`
2. 更新 `events/sec/events_index.parquet`
3. 更新 `events/sec/ingest_state.yaml`
4. 更新 `current/gaps/artifacts_state.yaml`
5. 如有新增缺口，更新 `current/gaps/missing_data.yaml`

### 模式判断逻辑

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
```

补充规则：
- `init`：做完整 backfill
- `maintenance`：允许 overlap 重扫，借此修复 amendment、late attachment、分类器升级后的再归档
- 若 `classifier_version` 升级且 `force_refresh=false`：可以只重做 Step 3~5，raw 不必全部重下

---

## 18. Schema 定义

### 18.1 `ingest_state.yaml`

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

> 注意：不再有 `vmf_version`、`sixk_classifier_version` 字段。

### 18.2 `filings_index.parquet` Schema

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
| `primary_subtype` | string | Y | 分类结果 |
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

> 注意：不再有 `vmf_triggered`、`vmf_score`、`bucket`（periodic_core/event_stream）、`sixk_class` 字段。

### 18.3 `events_index.parquet` Schema

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

> 注意：不再有 `category`、`subtype`（旧语义）字段。`primary_subtype` 与 `primary_topic` 是新的主分类字段。

### 18.4 `event.yaml`

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

> 注意：不再有 `category`（旧 12 类）、`importance_hint` 字段。

---

## 19. `result.yaml` components

```yaml
skill: sec-ingest-and-materialize-events
ticker: ABC
run_id: 20260105_143012
as_of: 2026-01-04
timezone: America/New_York

status: ok | partial | blocked | skipped | error

requires:
  hard:
    - company/{TICKER}/company.yaml

missing: []
warnings: []

outputs:
  - events/sec/filings_index.parquet
  - events/sec/events_index.parquet
  - events/sec/ingest_state.yaml

components:
  discovery:
    issuer_type: domestic|fpi
    forms_scanned: []
    totals:
      filings_seen: 0
      excluded: 0
      index_only: 0
      full_download: 0
      attach_only: 0

  sec_ingest:
    mode: init|maintenance
    window: {start: "...", end: "..."}
    totals:
      accessions_targeted: 0
      accessions_downloaded: 0
      accessions_incomplete: 0
      xbrl_packages_ok: 0
    warnings: []
    errors: []

  classification:
    classifier_version: "topic7_subtype_v1"
    taxonomy_version: "topic7_v1"
    totals_by_topic:
      periodic_core: 0
      earnings_market_communication: 0
      transaction_balance_sheet: 0
      accounting_quality: 0
      governance_shareholder: 0
      capital_equity_mechanics: 0
      legal_catchall: 0
    unresolved: 0

  events_materialize:
    totals:
      events_upserted: 0
      cycle_events: 0
      meeting_events: 0
      transaction_events: 0
      calendar_period_events: 0
      filing_events: 0
    bucket_coverage:
      event_overview: 1.0
      press_release: 0.0
      presentation_slides: 0.0
      financial_statements: 0.0
      notes_and_accounting: 0.0
      mdna_operating_review: 0.0
      risk_factors: 0.0
      business_and_strategy: 0.0
      governance_and_compensation: 0.0
      capital_structure_and_liquidity: 0.0
      mna_and_integration: 0.0
      restructuring_and_impairment: 0.0
      legal_and_regulatory: 0.0
      exhibits_and_material_contracts: 1.0
```

---

## 20. 状态判定

### skipped

- 目标窗口内 filings metadata 未变化，且 classifier_version 未变化，且已有 event 产物完整

### partial

任一成立即可：
- 任一 accession raw 下载不完整
- 财报事件 `period_end` / `fiscal_period` 无法稳定识别
- meeting event `meeting_date` 无法识别
- 非财报事件关键 bucket 缺失，但最小 event_overview 已产出
- PDF / image-only material 未 OCR，只保留 raw ref
- 分类器只能给出 `other_material_event`，但证据不足以更细分

### blocked

- `company.yaml` 缺 `cik`
- SEC metadata 拉取失败且本地无可用 `filings_index.parquet`
- 工作目录不可写

### error

- 运行时异常，且无法写出最小 `result.yaml`

---

## 21. Definition of Done

Skill 2 算"做成"，不是看抓了多少文件，而是看**能否稳定把不同 form language 规整为统一 events**。最小验收标准：

1. **Domestic annual / quarterly**
   - 10-K / 10-Q 能稳定落为 `periodic_core` + `annual_report` / `quarterly_report`
   - 财报 buckets 至少覆盖：`financial_statements`, `mdna_operating_review`, `risk_factors`（若该 form 有）

2. **Domestic current reports**
   - 8-K Item 2.02 → `earnings_market_communication` + `earnings_results_announcement`
   - 8-K Item 5.02 → `governance_shareholder` + `director_or_officer_change`
   - DEF14A + 8-K Item 5.07 → 同一个 `meeting` event

3. **FPI annual / interim**
   - 20-F / 40-F → `periodic_core` + `annual_report`
   - 6-K `period + results + fs_signal` → `periodic_core` + `interim_financial_report`
   - 6-K `period + results` 但无完整报表包 → `earnings_market_communication` + `earnings_results_announcement`

4. **FPI 事件流**
   - `Monthly Return` / `Share Repurchase Update` → `capital_equity_mechanics`
   - 债券发行 / pricing / completion → `transaction_balance_sheet`
   - AGM notice / proxy / vote result → `governance_shareholder`

5. **归并验证**
   - 10-Q + 8-K Item 2.02 → 同一个 `cycle` event
   - DEF14A + vote results → 同一个 `meeting` event
   - 20-F + annual report attachment 6-K → 同一个 `cycle` event

6. **索引层与事件层一致**
   - `filings_index.parquet` 中每个下载过的 accession 都能映射到 event 或 attach target
   - `events_index.parquet` 中每个 event 都有 `event_overview` 与 `exhibits_index`

7. **无 VMF 残留**
   - 所有 discovery whitelist 内的 filing 都得到处理（不存在因"预算不够"被 index_only 的情况）
   - event_id 不编码 category/topic/subtype

---

## 22. 可扩展性

这套方案的目标不是"完美理解 SEC 的全部 form 宇宙"，而是：

- 对绝大多数**美股经营性公司**都能工作
- 同时覆盖 **Domestic + FPI**
- 把最重要、最常见、最估值相关的披露流规整干净

如果未来要继续扩：

- `sustainability_esg` bucket
- merger proxy / tender offer 更深 form family
- registration statement side-channel linking
- 更细的 legal / regulatory subtype
- 更细的 transaction clustering
- Forms 3/4/5 ownership tracking（独立 skill）

都可以在**不改 raw/events/current 架构**的前提下往里加。分类器升级时只需要：
- 更新 `subtype_registry` + `classifier_rulepack`
- 重跑 events 层（raw 不需重下）
- event_id 保持稳定
