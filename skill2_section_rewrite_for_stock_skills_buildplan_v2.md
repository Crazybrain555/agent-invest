### Skill 2: `sec-ingest-and-materialize-events`

> **替代旧 Skill2 `collect-company-facts`**：raw ingest + events materialize  
> **本节是 Skill 2 的权威规格**：当本节与第五章（taxonomy）、第六章（buckets）、第十章（下载策略）存在表述冲突时，以本节为准。

**设计目标（v2.2）**

Skill 2 要解决的不是“把 SEC 文件抓下来”，而是把 **面向估值有材料性的 filing universe** 规整成可重复消费的事件层。  
适用对象是：**在美国上市、按 Exchange Act 披露的经营性公司**，同时覆盖：

- **Domestic issuer**：10-K / 10-Q / 8-K / proxy family
- **FPI（foreign private issuer）**：20-F / 40-F / 6-K

本 Skill 的设计必须满足两个现实：

1. **Domestic 与 FPI 的 form 语法不同，但下游分析不能感知这种差异**；
2. **一个“研究事件”可能由多个 filings 共同构成**，例如：
   - 10-K + 10-K/A
   - DEF14A + 8-K Item 5.07
   - 20-F + 同周期 6-K annual report attachment
   - 6-K results announcement + 6-K interim report

因此 Skill 2 的统一流程必须是：

```text
filing discovery
  -> filing scope decision（纳入/排除/仅索引/附着）
  -> raw ingest
  -> signal extraction
  -> subtype classification
  -> category mapping
  -> event grouping
  -> bucket materialization
  -> filings_index / events_index 更新
```

---

#### 2.1 职责边界（严格）

Skill 2 负责：

1. **下载并落盘 raw as-filed 证据**（不可变、可追溯）
2. **解析 filing index page / document catalog**，建立 `meta.yaml` 与 `manifest.yaml`
3. **决定 filing 是否进入研究宇宙**：
   - `excluded`
   - `index_only`
   - `full_download`
   - `attach_only`
4. **把 filing 分类为 subtype 与 taxonomy category**
5. **把一个或多个 filings 归并为 event**
6. **对 event 产出 canonical buckets**
7. **维护 events/sec/filings_index.parquet 与 events/sec/events_index.parquet**

Skill 2 **不负责**：

- 事实级 XBRL 解析（Skill 3 负责）
- OCR 作为默认路径（仅保留引用与 `partial` 标记，不强制 OCR）
- 投资结论、打分、估值参数推断（后续 Skills 负责）

---

#### 2.2 Phase 1 Bucket 收敛

为避免 Skill 2 首版过重，**Skill 2 Phase 1 采用收敛后的 bucket 集**：

- 保留 14 个由 Skill 2 直接 materialize 的内容 buckets：
  - `event_overview`
  - `press_release`
  - `presentation_slides`
  - `financial_statements`
  - `notes_and_accounting`
  - `mdna_operating_review`
  - `risk_factors`
  - `business_and_strategy`
  - `governance_and_compensation`
  - `capital_structure_and_liquidity`
  - `mna_and_integration`
  - `restructuring_and_impairment`
  - `legal_and_regulatory`
  - `exhibits_and_material_contracts`
- `structured_data` 保留为 Skill 3 专属出口，不由 Skill 2 写事实级内容
- `sustainability_esg` **从 Skill 2 Phase 1 中剔除**：
  - 如原始 filing 含 ESG / sustainability / climate report，仅在 `exhibits_and_material_contracts` 中建立引用
  - 在 `event.yaml.tags` 增加 `esg_available`
  - 后续如有需要，再由独立 enhancement 补上 `sustainability_esg`

---

#### 2.3 输入参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | today | 窗口终点 |
| `lookback_years` | int | - | 10 | init 回溯 |
| `overlap_days` | int | - | 2 | maintenance overlap |
| `force_refresh` | bool | - | false | 强制重建索引/重下 |
| `download_policy` | enum | - | `periodic_full__events_vmf` | 周期性全量；事件按 VMF / 规则 |
| `vmf_score_threshold` | int | - | 8 | 事件流评分阈值 |
| `vmf_annual_budget` | int | - | 20 | 每自然年评分事件预算 |
| `proxy_preliminary_policy` | enum | - | `index_until_definitive` | 初步 proxy 的处理策略 |

---

#### 2.4 Hard 依赖

- `company/{TICKER}/company.yaml`
  - 必须至少有：`ticker`、`cik`、`fiscal_year_end`

---

#### 2.5 输出（必须）

**raw layer**

- `raw/sec/accessions/{accession}/meta.yaml`
- `raw/sec/accessions/{accession}/manifest.yaml`
- `raw/sec/accessions/{accession}/index/*`
- `raw/sec/accessions/{accession}/submission/{accession}.txt`
- `raw/sec/accessions/{accession}/documents/*`
- `raw/sec/accessions/{accession}/exhibits/*`
- `raw/sec/accessions/{accession}/xbrl/*`
- `raw/sec/accessions/{accession}/other/*`

**events layer**

- `events/sec/ingest_state.yaml`
- `events/sec/filings_index.parquet`
- `events/sec/events_index.parquet`
- `events/sec/events/{event_id}/event.yaml`
- `events/sec/events/{event_id}/raw_refs.json`
- `events/sec/events/{event_id}/bucket_manifest.json`
- `events/sec/events/{event_id}/{bucket}/...`

**current/gaps**

- `current/gaps/artifacts_state.yaml`
- `current/gaps/missing_data.yaml`（如有缺口）
- `current/gaps/questions.jsonl`（仅当需要显式提问时）

---

#### 2.6 Filing 纳入 / 排除边界（通用方案）

Skill 2 不是“抓所有 SEC filing”；它只处理 **对公司研究与估值有较高材料性的 filing universe**。

##### 2.6.1 适用发行人范围

默认适用：

- Exchange Act reporting operating companies
- US-listed domestic issuers
- US-listed foreign private issuers（FPI）

默认不作为主目标：

- ETF / mutual fund / closed-end fund / BDC 专属 form family
- ABS / trust / SPV / structured product issuers
- 只做注册声明、几乎无经营披露的壳体

若发行人 form family 明显不属于经营性公司常规模型，Skill 2 可：

- `status=partial`，并在 `missing_data.yaml` 写 `issuer_type_not_targeted`
- 或由 orchestrator 在上游做 universe 过滤

##### 2.6.2 Discovery 白名单

**Domestic issuer discovery forms**

- `10-K`, `10-K/A`
- `10-Q`, `10-Q/A`
- `8-K`, `8-K/A`
- `DEF14A`, `DEFA14A`, `DEFM14A`, `DEFR14A`
- `PRE14A`, `PREM14A`（默认仅索引，见下文）

**FPI discovery forms**

- `20-F`, `20-F/A`
- `40-F`, `40-F/A`
- `6-K`, `6-K/A`

##### 2.6.3 默认排除的 form families

以下 forms **默认排除，不进入 Skill 2 研究宇宙**：

- Insider / beneficial ownership：`3`, `4`, `5`
- Ownership reports：`SC 13D`, `SC 13D/A`, `SC 13G`, `SC 13G/A`
- Securities Act registration / prospectus：`S-1`, `S-3`, `S-4`, `S-8`, `F-1`, `F-3`, `F-4`, `424B*`, `497*`
- Transfer / resale / notices：`144`, `144/A`
- Misc / admin：`UPLOAD`, `CORRESP`, `EFFECT`, `RW`, `POS AM`
- Proxy plumbing but low research value：`PX14A6G`
- Fund-specific / structured-product families：`N-*`, `ABS-*`, `10-D`, `N-CSR`, `N-CSRS`

##### 2.6.4 四种 filing 处理动作

每个 filing 必须先被赋予一个 `filing_action`（实现时可写入 `filings_index` 的扩展列，若暂不扩列，也必须体现在内部逻辑中）：

| filing_action | 含义 | 典型情形 |
|---|---|---|
| `excluded` | 直接排除 | Forms 3/4/5、13D/G、424B* |
| `index_only` | 仅记录 metadata，不下载正文 | VMF 未通过的低材料性 8-K / 6-K；初步 proxy |
| `full_download` | 下载 raw 全套，并可单独形成 event | 10-K/10-Q/20-F/40-F、命中材料性规则的 8-K/6-K、DEF14A |
| `attach_only` | 下载 raw，但不单独成为新 event，而是附着到已有 event | 10-K/A、20-F 同周期 annual report attachment 6-K、proxy supplement、earnings deck supplement |

##### 2.6.5 PRE / amendment 的处理原则

- `10-K/A`, `10-Q/A`, `20-F/A`, `40-F/A`, `8-K/A`, `6-K/A`：
  - 若能匹配已有 base event，则 `attach_only`
  - 若匹配失败，则 `full_download` + 新建 event，并打 `amendment_orphan`
- `PRE14A`, `PREM14A`：
  - 默认 `index_only`
  - 若 45 天内未出现 definitive proxy，升级为 `full_download`
  - 若明确为重大并购代理材料，可直接 `full_download`
- `DEFA14A` / proxy supplement：
  - 若能匹配 meeting event，则 `attach_only`
  - 若不能匹配，则 `full_download`

---

#### 2.7 模式判断逻辑

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

补充规则：

- `init`：做完整 backfill
- `maintenance`：允许 overlap 重扫，借此修复 amendment、late attachment、分类器升级后的再归档
- 若 `classifier_version` 升级且 `force_refresh=false`：
  - 可以只重做 `Step 2~4`（分类 / 分组 / buckets），raw 不必全部重下

---

#### 2.8 统一分类架构：先 subtype，后 category

Skill 2 的关键升级是：**不要直接从 filing 跳到 taxonomy category**。  
必须先识别 **subtype**，再把 subtype 映射为 12 个 category。

原因：

- `6-K` 不是一个语义，而是一整个容器
- `8-K` 也常常一份 filing 同时覆盖多个 item
- 相同主 category 里的处理差异，往往由 subtype 决定

##### 2.8.1 分类输入信号（统一抽取）

对每个 filing，Skill 2 必须先构建 `signal_bundle`，来源按强度排序如下：

1. **form family**（10-K / 8-K / 6-K / proxy）
2. **8-K items**（若有）
3. **document catalog**（doc_type / description / filename / category）
4. **filing title / SEC metadata description**
5. **primary document headings**（仅轻量解析，不做深度全文 NLP）
6. **exhibit title / opening paragraphs**（可选轻量提取）

统一信号组至少包括：

- `period_signal`
- `fs_signal`
- `results_signal`
- `guidance_signal`
- `presentation_signal`
- `proxy_signal`
- `vote_results_signal`
- `management_change_signal`
- `auditor_signal`
- `restatement_signal`
- `default_signal`
- `impairment_signal`
- `restructuring_signal`
- `mna_signal`
- `financing_signal`
- `capital_return_signal`
- `legal_signal`
- `monthly_return_signal`
- `share_repurchase_update_signal`
- `annual_report_attachment_signal`
- `interim_report_signal`

##### 2.8.2 通用 subtype 列表

Skill 2 必须至少支持以下 subtype：

**Periodic / core**

- `annual_report`
- `quarterly_report`
- `interim_financial_report`
- `annual_report_attachment`

**Earnings / market communication**

- `earnings_results_announcement`
- `guidance_update`
- `investor_presentation`

**Transaction / balance sheet**

- `mna_announcement`
- `mna_proxy_material`
- `financing_offering`
- `debt_pricing_or_completion`
- `credit_agreement_or_refinancing`
- `default_or_covenant_breach`

**Accounting / quality**

- `auditor_change`
- `non_reliance_or_restatement`
- `impairment_charge`
- `restructuring_program`

**Governance / shareholder**

- `director_or_officer_change`
- `charter_or_bylaw_change`
- `compensation_or_equity_award`
- `annual_meeting_proxy`
- `proxy_supplement`
- `meeting_vote_results`

**Capital / equity mechanics**

- `share_repurchase_update`
- `dividend_announcement`
- `stock_split_or_rights_change`
- `monthly_return`

**Legal / catch-all**

- `legal_or_regulatory_matter`
- `other_material_event`

##### 2.8.3 subtype -> category 映射

| subtype | category |
|---|---|
| `annual_report`, `quarterly_report`, `interim_financial_report`, `annual_report_attachment` | `financial_report` |
| `earnings_results_announcement`, `guidance_update`, `investor_presentation` | `earnings_release_guidance` |
| `mna_announcement`, `mna_proxy_material` | `mna` |
| `financing_offering`, `debt_pricing_or_completion`, `credit_agreement_or_refinancing` | `financing_liquidity` |
| `default_or_covenant_breach` | `default_covenant` |
| `auditor_change`, `non_reliance_or_restatement` | `auditor_restatement` |
| `impairment_charge`, `restructuring_program` | `impairment_restructuring` |
| `director_or_officer_change`, `charter_or_bylaw_change`, `compensation_or_equity_award` | `governance_management` |
| `share_repurchase_update`, `dividend_announcement`, `stock_split_or_rights_change`, `monthly_return` | `capital_return_equity` |
| `legal_or_regulatory_matter` | `legal_regulatory` |
| `annual_meeting_proxy`, `proxy_supplement`, `meeting_vote_results` | `shareholder_meeting_proxy` |
| `other_material_event` | `other_material` |

---

#### 2.9 完整分类器伪代码 / 决策树

```python
def classify_filing(filing, company, catalog, sec_metadata):
    form = filing["form"]
    signals = build_signal_bundle(filing, catalog, sec_metadata)

    # 0) hard exclusion
    if form_matches_excluded_family(form):
        return Classification(action="excluded")

    # 1) direct form-family classification
    if form in {"10-K", "10-K/A"}:
        return Classification(
            action="full_download" if form == "10-K" else "attach_or_full",
            subtype="annual_report",
            category="financial_report",
        )

    if form in {"10-Q", "10-Q/A"}:
        return Classification(
            action="full_download" if form == "10-Q" else "attach_or_full",
            subtype="quarterly_report",
            category="financial_report",
        )

    if form in {"20-F", "20-F/A", "40-F", "40-F/A"}:
        return Classification(
            action="full_download" if not form.endswith("/A") else "attach_or_full",
            subtype="annual_report",
            category="financial_report",
        )

    # 2) proxy family
    if form in {"DEF14A", "DEFA14A", "DEFM14A", "DEFR14A", "PRE14A", "PREM14A"}:
        return classify_proxy_family(form, signals)

    # 3) current reports - domestic 8-K
    if form in {"8-K", "8-K/A"}:
        return classify_8k(form, signals)

    # 4) current reports - FPI 6-K
    if form in {"6-K", "6-K/A"}:
        return classify_6k(form, signals)

    # 5) fallback
    return Classification(action="excluded")
```

##### 2.9.1 `classify_proxy_family()`

```python
def classify_proxy_family(form, signals):
    if form in {"PRE14A", "PREM14A"} and preliminary_policy == "index_until_definitive":
        return Classification(action="index_only", subtype="annual_meeting_proxy", category="shareholder_meeting_proxy")

    if signals.mna_signal or form in {"DEFM14A", "PREM14A"}:
        subtype = "mna_proxy_material"
        category = "mna"
    else:
        subtype = "annual_meeting_proxy" if signals.proxy_signal else "proxy_supplement"
        category = "shareholder_meeting_proxy"

    if form in {"DEFA14A", "DEFR14A"}:
        action = "attach_only"
    else:
        action = "full_download"

    return Classification(action=action, subtype=subtype, category=category)
```

##### 2.9.2 `classify_8k()`

```python
def classify_8k(form, signals):
    # hard / primary topics first
    if signals.item_4_01 or signals.item_4_02 or signals.auditor_signal or signals.restatement_signal:
        return full_or_attach(form, "non_reliance_or_restatement", "auditor_restatement")

    if signals.item_2_04 or signals.default_signal:
        return full_or_attach(form, "default_or_covenant_breach", "default_covenant")

    if signals.item_2_05 or signals.item_2_06 or signals.impairment_signal or signals.restructuring_signal:
        return full_or_attach(form, "restructuring_program", "impairment_restructuring")

    if signals.item_2_01 or signals.mna_signal or signals.has_ex2:
        return full_or_attach(form, "mna_announcement", "mna")

    if signals.item_1_01 or signals.item_2_03 or signals.item_3_02 or signals.financing_signal:
        return full_or_attach(form, "credit_agreement_or_refinancing", "financing_liquidity")

    if signals.item_2_02 or signals.results_signal or signals.guidance_signal:
        return full_or_attach(form, "earnings_results_announcement", "earnings_release_guidance")

    # meeting / governance should be below more material balance-sheet/accounting events
    if signals.item_5_07 or signals.vote_results_signal:
        return full_or_attach(form, "meeting_vote_results", "shareholder_meeting_proxy")

    if signals.item_5_02 or signals.item_5_03 or signals.management_change_signal:
        return full_or_attach(form, "director_or_officer_change", "governance_management")

    if signals.item_3_01 or signals.item_3_03 or signals.capital_return_signal:
        return full_or_attach(form, "stock_split_or_rights_change", "capital_return_equity")

    if signals.legal_signal:
        return full_or_attach(form, "legal_or_regulatory_matter", "legal_regulatory")

    if vmf_passed(signals):
        return Classification(action="full_download", subtype="other_material_event", category="other_material")

    return Classification(action="index_only", subtype="other_material_event", category="other_material")
```

##### 2.9.3 `classify_6k()`

```python
def classify_6k(form, signals):
    # annual attachments: 20-F / 40-F cycle sidecar
    if signals.annual_report_attachment_signal:
        return Classification(action="attach_only", subtype="annual_report_attachment", category="financial_report")

    # full interim package: period + results + fs signal
    if signals.period_signal and signals.results_signal and signals.fs_signal:
        return Classification(action="full_download", subtype="interim_financial_report", category="financial_report")

    # results announcement only: period + results but no financial statements package
    if signals.period_signal and signals.results_signal:
        return Classification(action="full_download", subtype="earnings_results_announcement", category="earnings_release_guidance")

    if signals.interim_report_signal and signals.fs_signal:
        return Classification(action="full_download", subtype="interim_financial_report", category="financial_report")

    if signals.monthly_return_signal:
        return Classification(action="full_download", subtype="monthly_return", category="capital_return_equity")

    if signals.share_repurchase_update_signal:
        return Classification(action="full_download", subtype="share_repurchase_update", category="capital_return_equity")

    if signals.proxy_signal or signals.vote_results_signal:
        return Classification(action="full_download", subtype="annual_meeting_proxy", category="shareholder_meeting_proxy")

    if signals.financing_signal:
        return Classification(action="full_download", subtype="debt_pricing_or_completion", category="financing_liquidity")

    if signals.mna_signal:
        return Classification(action="full_download", subtype="mna_announcement", category="mna")

    if signals.auditor_signal or signals.restatement_signal:
        return Classification(action="full_download", subtype="non_reliance_or_restatement", category="auditor_restatement")

    if signals.impairment_signal or signals.restructuring_signal:
        return Classification(action="full_download", subtype="restructuring_program", category="impairment_restructuring")

    if signals.management_change_signal:
        return Classification(action="full_download", subtype="director_or_officer_change", category="governance_management")

    if signals.legal_signal:
        return Classification(action="full_download", subtype="legal_or_regulatory_matter", category="legal_regulatory")

    if vmf_passed(signals):
        return Classification(action="full_download", subtype="other_material_event", category="other_material")

    return Classification(action="index_only", subtype="other_material_event", category="other_material")
```

---

#### 2.10 多信号冲突时的优先级规则

同一 filing 可能命中多个 signals。Skill 2 必须输出：

- `primary_subtype`
- `primary_category`
- `secondary_topics`（list）
- `classification_evidence`（list）

##### 2.10.1 信号强度优先级

当多个候选 subtype 冲突时，先看证据强度：

```text
8-K item / definitive form family
  > exhibit doc_type
  > exhibit description/title
  > SEC filing title/metadata
  > primary text heading
  > generic keyword
```

##### 2.10.2 主题优先级（primary topic precedence）

若信号强度相同，则按以下顺序选 `primary_category`：

```text
auditor_restatement
> default_covenant
> impairment_restructuring
> mna
> financing_liquidity
> financial_report
> earnings_release_guidance
> shareholder_meeting_proxy
> governance_management
> capital_return_equity
> legal_regulatory
> other_material
```

设计原因：

- 越靠前，越可能直接改变“利润可置信度 / 生存概率 / 资本结构 / 未来路径”
- `shareholder_meeting_proxy` 高于 `governance_management`，是为了把 proxy + vote-results 串成一个 event
- 未被选为 primary 的其他命中主题，必须保留到 `secondary_topics`

##### 2.10.3 无法匹配时的 fallback

- form 在白名单内，但未命中任何 subtype：
  - 若 VMF 通过 → `other_material` + `full_download`
  - 若 VMF 不通过 → `other_material` + `index_only`
- 若 `period_end` / `meeting_date` / `transaction_key` 无法识别：
  - 不得阻塞 raw ingest
  - event_id 回退到 `filed_at + accession_suffix`
  - 同时写 `missing_data.yaml`

---

#### 2.11 Event 归并规则（比单 filing 分类更重要）

Skill 2 的最小正确单位不是 filing，而是 **event**。

##### 2.11.1 event grouping keys

Skill 2 必须按以下优先顺序生成 `group_key`：

1. `financial_report`
   - `group_key = period_end + fiscal_period`
2. `shareholder_meeting_proxy`
   - `group_key = meeting_date`
3. `earnings_release_guidance`
   - `group_key = cycle_key`（若能识别 period / quarter）
   - 否则 `filed_at + accession_suffix`
4. `mna` / `financing_liquidity`
   - `group_key = normalized_transaction_key`（deal/instrument/target/counterparty）
   - 若失败，退回 `filed_at + accession_suffix`
5. 其他 category
   - 默认一 filing 一 event，amendment 附着到 base event

##### 2.11.2 event_id 生成规则（v2.2 推荐）

```python
def generate_event_id(category, filing, inferred):
    if category == "financial_report":
        period_end = inferred.period_end or filing.get("report_date") or filing["filed_at"]
        fiscal_period = inferred.fiscal_period or infer_fiscal_period(period_end, company_fye)
        return f"sec_fr_{period_end}_{fiscal_period}"

    if category == "shareholder_meeting_proxy" and inferred.meeting_date:
        return f"sec_shm_{inferred.meeting_date}"

    if category == "earnings_release_guidance" and inferred.cycle_key:
        return f"sec_erg_{inferred.cycle_key}_{filing['filed_at']}"

    if inferred.transaction_key:
        return f"sec_{category[:8]}_{slugify(inferred.transaction_key)}_{filing['filed_at']}"

    acc_suffix = filing["accession"].replace("-", "")[-6:]
    return f"sec_{category[:8]}_{filing['filed_at']}_{acc_suffix}"
```

##### 2.11.3 attach-only 归并规则

以下情形优先 `attach_only`，而不是新建 event：

- `10-K/A`, `10-Q/A`, `20-F/A`, `40-F/A` → attach 到对应 period event
- `8-K/A`, `6-K/A` → attach 到 base event
- 与同周期 20-F / 40-F 强相关的 annual report attachment 6-K → attach 到 FY `financial_report` event
- `DEFA14A`, `DEFR14A`, `meeting_vote_results` → attach 到 meeting event
- 同一 earnings cycle 的 deck / call slides / supplemental PR → attach 到同一 earnings event

---

#### 2.12 财报事件 Buckets 映射（权威版）

财报事件依然是 Skill 2 的第一优先级，以下规则为权威版。

##### 2.12.1 source document catalog

每个 event 必须先建立 `source_document_catalog`，来源于 raw `meta.yaml: documents[]`，至少包含：

- `filename`
- `doc_type`
- `description`
- `category`
- `bytes`
- `path`
- `sha256`
- `signal_tags`（由 Skill 2 生成，可选）

##### 2.12.2 `financial_statements` / `notes_and_accounting`

优先级：

1. 若 `xbrl.has_xbrl=true`：
   - 数字解析交给 Skill 3
   - Skill 2 只写：
     - `financial_statements/narrative.md`
     - `notes_and_accounting/notes.md`
     - 对应 raw refs
2. 对 40-F / FPI 6-K interim：
   - exhibits 中 description / title 命中：
     - `financial statements`
     - `interim report`
     - `unaudited condensed consolidated`
     - `audited financial statements`
   - 多份命中按 `bytes` 与 `signal_strength` 选前 1~2 份

##### 2.12.3 `mdna_operating_review`

优先级：

1. exhibits 命中：
   - `management's discussion`
   - `MD&A`
   - `operating and financial review`
   - `OFR`
2. 否则 primary doc 按表单 heading：
   - 10-K：`Item 7`
   - 10-Q：`Part I Item 2`（显式排除 `Part II Item 2`）
   - 20-F：`Item 5`
   - 40-F：优先 EX-99.*
3. 6-K interim：EX-99.* 优先，primary 兜底

##### 2.12.4 `risk_factors`

- 10-K：`Item 1A`
- 10-Q：`Part II Item 1A`
- 20-F：`Item 3.D`
- 40-F：优先 AIF / annual report attachment / EX-99.*

##### 2.12.5 `business_and_strategy`

- 10-K：`Item 1`
- 20-F：`Item 4`
- 10-Q：通常不强制生成
- 40-F：优先 AIF / annual report attachment / EX-99.*

##### 2.12.6 `governance_and_compensation`

- 财报事件目录里通常只放 ref
- 主内容由 proxy / meeting event 承担，不强制复制

---

#### 2.13 非财报事件 Buckets 抽取方案（完整版本）

每个非财报 event **至少**产出：

- `event_overview/overview.md`
- `event_overview/timeline.json`
- `exhibits_and_material_contracts/exhibits_index.json`

然后按 `primary_category` 补充对应 buckets。以下为权威规则。

##### 2.13.1 `earnings_release_guidance`

**必选 buckets**

- `event_overview`
- `press_release`
- `exhibits_and_material_contracts`

**条件 buckets**

- `presentation_slides`：若 deck / investor presentation / earnings call slides 存在
- `mdna_operating_review`：若 results announcement 中有充分经营解读
- `financial_statements`：仅当 exhibit 含成体系的财务表，而不足以升格为 `financial_report`

**来源优先级**

1. EX-99.* earnings release / results announcement
2. earnings presentation / shareholder letter / quarterly update
3. 8-K Item 2.02 primary doc 或 6-K primary doc

**抽取重点**

- 本期关键指标（收入 / 利润 / EPS / segment / cash）
- 指引变化（若有）
- 管理层给出的核心驱动解释
- raw refs 指向 results release / deck

##### 2.13.2 `mna`

**必选 buckets**

- `event_overview`
- `mna_and_integration`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`
- `financial_statements`（若有 pro forma / target FS）
- `capital_structure_and_liquidity`（若交易融资条款重要）
- `legal_and_regulatory`（若审批/诉讼为关键 closing condition）

**来源优先级**

1. EX-2.* merger agreement / plan of merger / asset purchase agreement
2. DEFM14A / proxy materials / tender materials（若在 discovery universe）
3. EX-99.* press release
4. 8-K Item 2.01 / 1.01 primary text

**抽取重点**

- 交易对手、标的、对价、支付方式
- 估值口径、closing conditions、termination fee
- 管理层 rationale、协同、整合里程碑
- 如有 pro forma，记录 refs，不做深度重算

##### 2.13.3 `financing_liquidity`

**必选 buckets**

- `event_overview`
- `capital_structure_and_liquidity`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`
- `presentation_slides`
- `risk_factors`（若条款显著抬升风险）

**来源优先级**

1. EX-10.* credit agreement / indenture / note purchase agreement
2. 8-K Item 1.01 / 2.03 / 3.02
3. 6-K financing announcement / pricing / completion exhibits
4. press release

**抽取重点**

- 融资类型、金额、币种、到期日、利率/票息
- 抵押、担保、优先级、转换/交换条款
- covenant、用途、refinancing 关系
- completion / pricing / closing 的时间线

##### 2.13.4 `default_covenant`

**必选 buckets**

- `event_overview`
- `capital_structure_and_liquidity`
- `risk_factors`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`
- `legal_and_regulatory`（若触发破产/诉讼/交叉违约）

**来源优先级**

1. 8-K Item 2.04
2. waiver / amendment / forbearance agreements
3. lender notices / press release

**抽取重点**

- breached covenant / default 类型
- 涉及债务金额与受影响工具
- 加速、豁免、宽限期、修订条款
- 对流动性和持续经营的直接影响

##### 2.13.5 `auditor_restatement`

**必选 buckets**

- `event_overview`
- `notes_and_accounting`
- `risk_factors`
- `exhibits_and_material_contracts`

**条件 buckets**

- `governance_and_compensation`（若审计委员会动作重要）
- `press_release`

**来源优先级**

1. 8-K Item 4.01 / 4.02
2. auditor letter / audit committee communication
3. press release / investor FAQ

**抽取重点**

- auditor change / non-reliance / restatement 的性质
- 影响期间与财务报表范围
- 原因类型（收入确认、税、分类、内控缺陷等）
- remediation plan 与后续修正路径

##### 2.13.6 `impairment_restructuring`

**必选 buckets**

- `event_overview`
- `restructuring_and_impairment`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`
- `mdna_operating_review`
- `capital_structure_and_liquidity`

**来源优先级**

1. 8-K Item 2.05 / 2.06
2. EX-99.* restructuring announcement / plant closure / workforce reduction
3. management commentary

**抽取重点**

- 现金/非现金 charges
- 涉及业务/资产/地区/人数
- 预计完成时间与后续 savings
- 对利润质量与现金流的影响入口

##### 2.13.7 `governance_management`

**必选 buckets**

- `event_overview`
- `governance_and_compensation`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`
- `capital_structure_and_liquidity`（若涉及可转债/股权激励稀释）

**来源优先级**

1. 8-K Item 5.02 / 5.03
2. employment agreement / separation agreement / award agreement / charter amendment
3. 6-K board appointment / grant of awards / board meeting notice

**抽取重点**

- 谁变动、何时生效、职位与原因
- compensation / severance / equity award 关键条款
- 董事会 / 管理层治理变化
- 章程/细则/投票权修改

##### 2.13.8 `capital_return_equity`

**必选 buckets**

- `event_overview`
- `capital_structure_and_liquidity`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`

**来源优先级**

1. buyback / dividend / split announcement exhibits
2. 8-K Item 3.01 / 3.03
3. 6-K monthly return / share repurchase update

**抽取重点**

- 回购授权、已执行规模、平均价格
- 分红金额、除权/支付日
- 拆股、合股、股东权利变更
- 股本变化 / treasury share / issued share movement

##### 2.13.9 `legal_regulatory`

**必选 buckets**

- `event_overview`
- `legal_and_regulatory`
- `exhibits_and_material_contracts`

**条件 buckets**

- `risk_factors`
- `press_release`

**来源优先级**

1. settlement agreement / consent decree / agency letter / court filing exhibit
2. 8-K Item 8.01 / 6-K legal announcement
3. press release

**抽取重点**

- 对手方 / 监管机构 / 法院
- 案由、进度、金额、限制性义务
- 对经营、现金、声誉、许可的影响

##### 2.13.10 `shareholder_meeting_proxy`

**必选 buckets**

- `event_overview`
- `governance_and_compensation`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`

**来源优先级**

1. DEF14A / DEFM14A / DEFA14A / DEFR14A
2. 8-K Item 5.07 vote results
3. 6-K AGM notice / proxy statement / proxy form / vote results

**抽取重点**

- meeting_date / record_date / meeting_type
- proposals / board recommendation / voting thresholds
- vote results 与通过/否决结论
- compensation / governance proposal refs

##### 2.13.11 `other_material`

**必选 buckets**

- `event_overview`
- `exhibits_and_material_contracts`

**条件 buckets**

- `press_release`
- 根据弱信号补一个最贴近的单桶，但不得伪造内容完整性

**来源优先级**

1. primary doc
2. EX-99.*
3. related contracts / notices

**抽取重点**

- 发生了什么
- 为什么可能重要
- 证据在哪
- 为什么未能更精确归类

---

#### 2.14 Document selection 统一规则

每个 bucket 的 materialization 都走统一流程：

```python
def select_source_docs(bucket_name, catalog, filing_context):
    candidates = score_docs(catalog, bucket_name, filing_context)
    chosen = top_k(candidates, k=bucket_policy[bucket_name].max_docs)
    return chosen
```

评分因子：

- `doc_type` 精确命中
- `description` / title 命中
- `category`（documents / exhibits / xbrl / other）
- `bytes`（防止选到空壳封面）
- `mime_type`（html/text/pdf）
- `form-aware boosts`（例如 10-K 的 `Item 1A`、6-K 的 `Exhibit 99.1`）

默认策略：

- HTML / TXT 优先于 PDF
- 结构化标题优先于图片附件
- 有明确 exhibit 描述的附件优先于 primary shell document

---

#### 2.15 Raw ingest 规则（细化版）

对于 `full_download` / `attach_only` 的 filing，默认下载：

- `index.json`
- `{accession}-index.html`
- `submission/{accession}.txt`
- `primary_document`
- 全部非 XBRL exhibits
- 全部 XBRL 包（若 `has_xbrl=true`）
- 其他附件按预算与类型决定（图片可只留 metadata）

补充规则：

- 若 filing 命中 `presentation_signal` 且 deck 为 PDF：必须下载 PDF 原件并保留 ref
- 若 filing 仅为 `index_only`：
  - 不下载正文
  - 但 `filings_index.parquet` 必须保留 form / filed_at / accession / title / items / VMF 等 metadata
- 若 raw 下载不完整：
  - `manifest.yaml.completeness` 显式标注缺口
  - 事件仍可继续 materialize 已有内容
  - overall `status=partial`

---

#### 2.16 VMF（Valuation Materiality Filter）权威版

VMF **只用于事件流 current reports**，不用于 periodic core。  
即：

- 不对 `10-K / 10-Q / 20-F / 40-F / definitive proxy` 用 VMF
- 只对 `8-K / 8-K/A / 6-K / 6-K/A / preliminary proxy` 的下载动作做 VMF 控制

##### 2.16.1 periodic core

**Domestic**

- `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `DEF14A`, `DEFM14A` → `full_download`
- `DEFA14A`, `DEFR14A` → `attach_only`（若能匹配 meeting event）
- `PRE14A`, `PREM14A` → 默认 `index_only`

**FPI**

- `20-F`, `20-F/A`, `40-F`, `40-F/A` → `full_download`
- `6-K` 中经分类器识别为：
  - `interim_financial_report`
  - `annual_report_attachment`
  - `annual_meeting_proxy`
  → 不受 VMF 限制，直接 `full_download` / `attach_only`

##### 2.16.2 事件流硬触发（不限预算）

命中以下任一规则，直接 `full_download`：

- `8-K Item 2.02 / 4.01 / 4.02 / 2.04 / 2.05 / 2.06 / 2.01 / 5.07`
- exhibit 描述命中：
  - `earnings release`
  - `results announcement`
  - `interim report`
  - `investor presentation`
  - `share repurchase update`
  - `monthly return`
  - `credit agreement`
  - `indenture`
  - `merger agreement`
  - `auditor letter`
- 标题/摘要命中高材料性词：
  - `restatement`
  - `material weakness`
  - `default`
  - `covenant`
  - `bankruptcy`
  - `impairment`
  - `restructuring`
  - `guidance`
  - `earnings`
  - `results`
  - `offering`
  - `pricing`
  - `completion`
  - `repurchase`

##### 2.16.3 评分筛选（预算约束）

对未命中硬触发的 current reports，按以下权重计分：

| 维度 | 示例关键词 | 权重 |
|------|-----------|------|
| 生存/流动性/再融资 | liquidity, refinancing, credit facility, covenant | 5 |
| 利润/预期/指引 | earnings, results, guidance, outlook, margin | 4 |
| 会计可信度 | restatement, auditor, material weakness | 4 |
| 资产质量/重组 | impairment, restructuring, exit, disposal | 3 |
| 并购/业务组合 | acquisition, merger, disposition, divestiture | 3 |
| 治理/激励 | ceo, director, appointment, award | 2 |
| 资本回报 | repurchase, dividend, split, monthly return | 2 |
| 法律/监管 | investigation, settlement, doj, sec, litigation | 2 |

规则：

- `score >= vmf_score_threshold` → `full_download`
- 否则 `index_only`
- 每自然年最多下载 `vmf_annual_budget` 个纯评分事件；**硬触发不占预算**

---

#### 2.17 Step-by-step 内部步骤（完整实现版）

##### Step 0 - 初始化 + 身份检查

1. 确保目录结构存在
2. 读取 `company.yaml`
3. 验证 `cik`、`fiscal_year_end`
4. 推断 `issuer_type`：`domestic | fpi`
5. 读取历史 `ingest_state.yaml` / `filings_index.parquet`

##### Step 1 - Filing discovery + scope decision

1. 按 `issuer_type` 加载 discovery 白名单
2. 在 `[fetch_start, fetch_end]` 取 filings metadata
3. 对每个 filing：
   - 判断 `excluded / index_only / full_download / attach_only`
   - 记录 preliminary VMF 信息、items、title、form family
4. 先写入 staging `filings_index`

##### Step 2 - Raw ingest

1. 对 `full_download` / `attach_only` 的 accession 下载 raw
2. 解析 `index.json` 与 `{accession}-index.html`
3. 生成 `meta.yaml`
4. 生成 `manifest.yaml`
5. 记录 `xbrl` 完整性状态

##### Step 3 - Signal extraction + subtype/category classification

1. 从 metadata + catalog + headings 提取统一 signals
2. 跑分类器得到：
   - `filing_action`
   - `primary_subtype`
   - `primary_category`
   - `secondary_topics`
   - `classification_evidence`
3. 若分类器升级导致 action 变化，可在 maintenance 中重新 materialize

##### Step 4 - Event grouping

1. 基于 category/subtype 生成 `group_key`
2. 把相关 filings 归并到 event
3. 判定 primary filing 与 related filings
4. amendments / supplements / attachments 默认 attach

##### Step 5 - Bucket materialization

1. 建立 `source_document_catalog`
2. 先写 `event_overview` 与 `exhibits_index`
3. 再按 category 规则写对应 buckets
4. 写 `event.yaml`、`raw_refs.json`、`bucket_manifest.json`
5. 如某 bucket 无法抽取：
   - 不伪造文件
   - 在 `bucket_manifest.json` 标 `missing` / `partial`
   - 写入 `missing_data.yaml`

##### Step 6 - 更新索引与 current/gaps

1. 更新 `events/sec/filings_index.parquet`
2. 更新 `events/sec/events_index.parquet`
3. 更新 `events/sec/ingest_state.yaml`
4. 更新 `current/gaps/artifacts_state.yaml`
5. 如有新增缺口，更新 `current/gaps/missing_data.yaml`

---

#### 2.18 `event.yaml` 必填扩展字段（Skill 2 推荐）

在既有 schema 基础上，Skill 2 推荐增加以下字段（如暂不扩 schema，也应在内部对象中具备同义信息）：

```yaml
classification:
  primary_subtype: "interim_financial_report"
  secondary_topics: ["investor_presentation"]
  evidence:
    - source: "form"
      value: "6-K"
    - source: "exhibit_description"
      value: "Interim Report"
    - source: "title"
      value: "first six months"

grouping:
  group_key: "2025-09-30_H1"
  grouping_basis: "period_end+fiscal_period"
  filing_role: "primary|related|attachment|amendment"
```

---

#### 2.19 `result.yaml components`（权威版）

```yaml
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
    classifier_version: v2.2
    totals:
      financial_report: 0
      earnings_release_guidance: 0
      mna: 0
      financing_liquidity: 0
      default_covenant: 0
      auditor_restatement: 0
      impairment_restructuring: 0
      governance_management: 0
      capital_return_equity: 0
      legal_regulatory: 0
      shareholder_meeting_proxy: 0
      other_material: 0
    unresolved: 0

  events_materialize:
    totals:
      events_upserted: 0
      financial_report_events: 0
      non_financial_events: 0
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

#### 2.20 skipped / partial / blocked / error 判定

##### skipped

- 目标窗口内 filings metadata 未变化，且 classifier_version 未变化，且已有 event 产物完整

##### partial

任一成立即可：

- 任一 accession raw 下载不完整
- 财报事件 `period_end` / `fiscal_period` 无法稳定识别
- meeting event `meeting_date` 无法识别
- 非财报事件关键 bucket 缺失，但最小 event_overview 已产出
- PDF / image-only material 未 OCR，只保留 raw ref
- 分类器只能给出 `other_material`，但证据不足以更细分

##### blocked

- `company.yaml` 缺 `cik`
- SEC metadata 拉取失败且本地无可用 `filings_index.parquet`
- 工作目录不可写

##### error

- 运行时异常，且无法写出最小 `result.yaml`

---

#### 2.21 Definition of Done（Skill 2 验收口径）

Skill 2 算“做成”，不是看抓了多少文件，而是看 **能否稳定把不同 form language 规整为统一 events**。最小验收标准：

1. **Domestic annual / quarterly**
   - 10-K / 10-Q 能稳定落为 `financial_report`
   - 财报 buckets 至少覆盖：`financial_statements`, `mdna_operating_review`, `risk_factors`（若该 form 有）

2. **Domestic current reports**
   - 8-K Item 2.02 → `earnings_release_guidance`
   - 8-K Item 5.02 → `governance_management`
   - DEF14A + 8-K Item 5.07 → 同一个 `shareholder_meeting_proxy` event

3. **FPI annual / interim**
   - 20-F / 40-F → `financial_report`
   - 6-K `period + results + fs_signal` → `financial_report`
   - 6-K `period + results` 但无完整报表包 → `earnings_release_guidance`

4. **FPI 事件流**
   - `Monthly Return` / `Share Repurchase Update` → `capital_return_equity`
   - 债券发行 / pricing / completion → `financing_liquidity`
   - AGM notice / proxy / vote result → `shareholder_meeting_proxy`

5. **索引层与事件层一致**
   - `filings_index.parquet` 中每个下载过的 accession 都能映射到 event 或 attach target
   - `events_index.parquet` 中每个 event 都有 `event_overview` 与 `exhibits_index`

---

#### 2.22 这版 Skill 2 的通用性边界

这套方案的目标不是“完美理解 SEC 的全部 form 宇宙”，而是：

- 对绝大多数 **美股经营性公司** 都能工作
- 同时覆盖 **Domestic + FPI**
- 把最重要、最常见、最估值相关的披露流规整干净

如果未来要继续扩：

- `sustainability_esg`
- merger proxy / tender offer 更深 form family
- registration statement side-channel linking
- 更细的 legal / regulatory subtype
- 更细的 transaction clustering

都可以在 **不改 raw/events/current 架构** 的前提下往里加。
