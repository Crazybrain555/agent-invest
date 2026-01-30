# 公司研究 Skills 体系规划 v2

> **核心公式：估值 = 利润 × 质量系数**
>
> 一切分析围绕两个因素展开：**未来可持续的经济利润（Owner Earnings）** 和 **对这个利润的确定性系数（Quality Coefficient）**

---

## 一、北极星：利润 × 质量系数

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

---

## 三、目录结构：以公司为主体 + current/runs

```
/home/help/mcp/work/company_research/
  registry.jsonl                           # 全局运行注册表
  value_summary.csv                        # 全局估值汇总（由编排器生成）
  company/
    {TICKER}/
      company.yaml                         # 身份与静态信息
      latest.json                          # 指向最新 run_id + current 摘要
      current/                             # "当前态"：所有下游优先读这里
        artifacts_state.yaml               # 产物状态（用于查漏补缺判断）
        questions.jsonl                    # 未解之谜/待验证事项
        evidence.jsonl                     # 证据账本

        # --- 市场口径 ---
        market_snapshot.yaml

        # --- SEC 证据池 ---
        filings_index.yaml                 # SEC filings 元数据索引（含VMF筛选字段）
        filings_index.parquet              # 分析层（同schema）

        # --- 报表图谱（替代固定 tag 集）---
        xbrl_atlas/
          periods.yaml                     # 每个 period 用哪个 accession
          nodes.parquet                    # 报表树节点
          edges.parquet                    # 报表树边
          facts.parquet                    # 所有报表事实（长表）
          paths.parquet                    # 可视化路径表

        # --- 经济重铸层 ---
        economic/
          recast_policy.yaml               # 重铸规则与映射账本
          economic_statements.parquet      # 经济三表
          core_metrics.parquet             # NOPAT/IC/ROIC/FCF/Owner Earnings

        # --- 诊断层 ---
        diagnostics/
          profit_quality.yaml              # 利润质量
          profit_risk_forecast.yaml        # 利润风险预测
          growth_drivers.yaml              # 成长性驱动
          moat.yaml                        # 护城河
          quality_coefficient.yaml         # 质量系数
          audit.yaml                       # 审计/反问

        # --- 估值输出 ---
        valuation/
          valuation.yaml                   # 估值假设与结果
          valuation_model.csv              # 预测表/模型明细
          value_state.yaml                 # 估值底座总表（给编排器用）
          investment_memo.md               # 投资备忘录

      raw/                                 # 原始材料层（不可变、可追溯）
        sec/                               # SEC filings（每个 accession 一个目录）
          {accession}/
            meta.yaml                      # 元数据（含VMF筛选信息）
            manifest.yaml                  # 下载清单 + hash + 完整性标记
            primary_document.html          # 主文档
            primary_document.txt           # 纯文本版
            sections/                      # 关键段落
              mdna.md
              risk_factors.md
              business.md
            xbrl/                          # XBRL 包（周期性filing）
              *.xml
              *.xsd
            exhibits/                      # 高价值附件（VMF筛选）
              exhibit_99_1.html            # 新闻稿/业绩公告
              exhibit_10_1.html            # 重大合同
              exhibit_2_1.html             # 并购协议
        web/...                            # 网页快照（未来扩展）

      runs/{run_id}/                       # 历史快照
        meta.yaml                          # 本次 run 的输入、调用、缓存命中
        result.yaml                        # Skill 运行结果
        needs.yaml                         # 仅 blocked 时存在
        outputs/...                        # 本次 run 产物（可选）

      logs/                                # 可选
```

### 写入规则

- Skill **先写 runs/{run_id}/**
- 成功（ok/partial）后，把关键产物 **原子替换** 到 `current/`
- `latest.json` 只在成功后更新
- 每次 run 追加 `registry.jsonl`

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

# 可选：Skill 自定义扩展字段（推荐用于 ingestion / multi-stage skills）
# 例如 Skill2 会写 components.sec 的子状态，便于编排与排障
components: {}
```

### 4.2 needs.yaml 结构（blocked 时必须）

```yaml
blocked_by:
  - artifact: current/filings_index.yaml
    producer_skill: collect-company-facts
    reason: "缺 filings_index，无法抽取 XBRL 图谱"

suggested_plan:
  - collect-company-facts
  - extract-xbrl-timeseries

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

## 五、核心产物契约（最小 Schema）

### 5.1 company.yaml

```yaml
ticker: ABC
company_name: "ABC Inc."
cik: "0000123456"
exchange: "NYSE"
sic: "1234"
fiscal_year_end: "12-31"
currency: "USD"
```

### 5.2 market_snapshot.yaml

```yaml
as_of: 2026-01-04
currency: USD                 # 当前强制以 USD 输出
price: 12.34
shares_outstanding: 100000000
shares_float: 80000000          # 若可取到，否则 null
market_cap: 1234000000
enterprise_value: 1500000000    # 若能取到/能换算到 USD；否则 null
source: "mixed:alpaca.get_stock_latest_trade+yfinance.get_stock_info"
```

### 5.3 Evidence Index & Digest Files

> **详见「详细 Schema 规范」章节**（本文档后半部分）
>
> Skill 2 产出的核心索引与摘要文件：
> - `filings_index.yaml` + `filings_index.parquet`：SEC filing 索引（含 VMF 筛选字段）
> - `events_index.parquet`：候选事件指针索引（SEC 事件流；供 Phase2 挑选并写 evidence claims）
>
> 所有字段均为**强制字段**（无 optional/recommended）

### 5.4 xbrl_atlas（报表图谱）

**facts.parquet 最小字段：**

| 字段 | 说明 |
|------|------|
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

**paths.parquet（作图关键）：** `node_id`, `period_end`, `statement_type`, `path_str`, `value`, `accession`

### 5.5 economic 重铸层

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

### 5.6 value_state.yaml（估值底座总表）

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
  memo: "current/valuation/investment_memo.md"
  valuation_yaml: "current/valuation/valuation.yaml"
  audit_yaml: "current/diagnostics/audit.yaml"
  evidence_jsonl: "current/evidence.jsonl"
```

### 5.7 questions.jsonl（一行一个问题）

```json
{"id":"Q_20260105_001","created_at":"2026-01-05","skill":"moat-inferencer","priority":"high","question":"客户集中度是否来自单一合同？续约条款是什么？","status":"open","related_artifacts":["current/diagnostics/moat.yaml"],"notes":""}
```

### 5.8 evidence.jsonl（一行一个结论）

```json
{"id":"E_20260105_010","created_at":"2026-01-05","skill":"profit-quality-and-risk","claim":"应收增长显著快于收入，但主要来自并购并表","confidence":0.6,"sources":[{"type":"sec","accession":"...","path":"raw/sec/.../10k.html","anchor":"MD&A"},{"type":"data","path":"current/economic/core_metrics.parquet","fields":["revenue","ar"]}]}
```

---

## 六、九个 Skills 总览

> 注：本 v2 规划目标是 9 个 Skills；目前已实现 5 个（其余为 roadmap）。

| # | Skill | 状态 | 职责 | 对"利润×质量"贡献 |
|---|-------|------|------|------------------|
| 1 | `company-foundation` | 已实现 | 身份 + 市场口径（含 shares） | 估值分母/每股化基座 |
| 2 | `collect-company-facts` | 已实现 | SEC filings（含事件流） | 证据池（SEC） |
| 3 | `extract-xbrl-timeseries` | 已实现 | 报表图谱（树+事实+溯源） | 利润事实底座 |
| 4 | `recast-economic-statements` | 已实现 | 经济三表 + 核心指标 | Owner Earnings / ROIC |
| 5 | `profit-quality-and-risk` | 规划中 | 财报质量/操纵风险/利润可持续性 | 质量系数与情景下界 |
| 6 | `growth-driver-explorer` | 规划中 | 增长来源与 ROIIC/生命周期 | 未来利润路径 |
| 7 | `moat-inferencer` | 规划中 | 护城河 → 优势期 → 质量系数映射 | 质量系数主体 |
| 8 | `valuation-and-margin-of-safety` | 已实现 | 估值区间 + MOS + 敏感性 | 输出 IV vs 市场 |
| 9 | `cross-examination-audit` | 规划中 | 反问审计：找矛盾/遗漏/为什么便宜 | 提高确定性，防大错 |

---

## 七、Skill 详细规格

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
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- 无（这是链条起点）

**输出**

- `company/{ticker}/company.yaml`
- `company/{ticker}/current/market_snapshot.yaml`
- `company/{ticker}/current/artifacts_state.yaml`（更新）
- `runs/{run_id}/meta.yaml`, `result.yaml`

**内部步骤**

1. 确保目录树存在（current/raw/runs）
2. 身份解析：SEC CIK、公司名、FY end、货币等
3. 市场口径：
   - `alpaca` 优先提供 `price`（低频量价数据更稳定）
   - shares / market cap / EV：优先 trading_mcp/SEC，其次 Yahoo 兜底
   - `market_cap` 默认用来源值（如 Yahoo `marketCap`），并用 `price * shares_outstanding` 交叉验证（差异过大才切换到派生值）
   - `enterprise_value` 以 USD 输出；若 ADR 出现 `financialCurrency != USD`（如 BABA 的 CNY），需 FX payload 才能换算
4. 写 evidence（身份来源、市场数据来源）

**查漏补缺规则**

- identity：若 `company.yaml` 已有 cik 且未 `force_refresh` → `skipped`
- market_snapshot：若 `as_of` 相同且文件存在且字段齐全 → `skipped`

**blocked 条件**

- 只有在"外部源完全不可用导致无法生成最小 company.yaml/market_snapshot.yaml"才 `blocked`

---

### Skill 2: `collect-company-facts`

> **证据池采集与维护**：SEC filings（raw + XBRL + 事件流/VMF）

**设计理念**

本 Skill 定位为 **Evidence Ingestion + Maintenance**，输出两层数据资产：

1. **Raw Store**（不可变、可追溯、可回放）：分区存储原始材料
2. **Index & Digest**（可查询、可分析、人读摘要）：为下游分析提供轻量快速层

支持两种运行模式（自动判断）：
- **Init 模式**：目标文件不存在时，执行完整初始化（多年回溯）
- **Maintenance 模式**：目标文件已存在时，执行增量更新（overlap窗口）

---

**职责边界**

- **SEC**：
  - **周期性核心（Periodic Core）**：10年全量下载（必须）
    - Domestic：10-K/10-Q/DEF14A
    - FPI / MJDS（加拿大）：20-F/40-F **+ 6-K（Interim Financials/Results 子集：季度/半年中期业绩材料）**
  - **事件流（Event Stream）**：10年全量索引 + VMF 选择性下载
    - Domestic：8-K/8-K/A
    - FPI：6-K（排除已归入 Periodic Core 的 “Interim Financials/Results” 子集后的剩余 6-K）
  - 发行人类型自动适配（Domestic vs FPI；FPI 不提交 10-Q，中期/季度信息常在 6-K 附件 99.*）
  - News / Papers：Phase1 暂不在本 Skill 内实现（后续计划抽离为独立信息服务/数据库 + MCP 查询）。

---

**输入参数**

#### 核心参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | N | 当天 | 数据截止日（用于 run 标记与窗口终点） |
| `force_refresh` | bool | N | false | 强制重新初始化（忽略已有文件） |

#### 窗口参数（自动模式切换）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `lookback_years` | int | 10 | **Init 模式**：SEC 回溯年数 |
| `overlap_days` | int | **2** | **Maintenance 模式**：重叠回抓天数（用于补齐窗口边界） |

**模式判断逻辑**：
```python
# SEC
if not filings_index.yaml exists or force_refresh:
    mode = "init"  # 用 lookback_years
    fetch_start = as_of - timedelta(days=lookback_years * 365)
else:
    mode = "maintenance"
    last_filed_at = max_date(load_yaml(filings_index.yaml).filings[].filed_at)  # latest filed_at in current index
    fetch_start = last_filed_at - timedelta(days=overlap_days)
fetch_end = as_of
sec_days = (fetch_end - fetch_start).days + 1
```

#### SEC 参数（无 download_policy，改用 VMF）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `vmf_score_threshold` | int | 8 | VMF 打分阈值（>=8 才下载） |
| `vmf_annual_budget` | int | 20 | 每自然年事件下载上限（硬触发不受限） |
| `download_sections` | bool | true | 对已落盘的 `primary_document.html` 本地解析抽取并存储关键 sections（MD&A/Risk Factors/Business 等；best-effort） |

---

**Hard 依赖**

- `company/{ticker}/company.yaml`（必须有 `cik`）

---

**输出**

### SEC 输出

- `current/filings_index.yaml`：契约文件
- `current/filings_index.parquet`：分析层
- `raw/sec/{accession}/`：每个 filing 一个目录
  - `meta.yaml`：filing 元数据
  - `manifest.yaml`：下载清单 + hash + 完整性标记
  - `primary_document.html` / `.txt`：主文档
  - `sections/`：存储关键段落（由 `primary_document` 本地解析抽取，例如 MD&A/Risk Factors）
  - `xbrl/`：as-filed XBRL 文件集（instance + `.xsd` + linkbases；优先不保留 `*-xbrl.zip`）
  - `exhibits/`：高价值附件（99.* / 10.1 / 2.1 等）

### Events Candidates 输出（推荐）

> 说明：这是“候选事件指针池”，不是 evidence claim（结论）。下游分析类 skills 从这里挑事件，再生成 `current/evidence.jsonl` 的结论型记录。

- `current/events_index.parquet`：候选事件索引（`sec:{accession}`），含 `local_dir` 等可追溯指针

### Run metadata

- `runs/{run_id}/meta.yaml`
- `runs/{run_id}/result.yaml`
- `runs/{run_id}/needs.yaml`（仅 blocked 时）

### Artifact Ownership Matrix（产物归属与依赖）

| Artifact | Producer | Consumer（典型） | 用途 |
|---|---|---|---|
| `company/{TICKER}/company.yaml` | Skill1 `company-foundation` | Skill2 `collect-company-facts` | CIK/公司身份（SEC 抓取前置条件） |
| `company/{TICKER}/current/market_snapshot.yaml` | Skill1 `company-foundation` | Skill5 `valuation-and-margin-of-safety` | 市场口径（price/shares/EV 等） |
| `company/{TICKER}/current/filings_index.yaml` + `.parquet` | Skill2 `collect-company-facts` | Skill3 `extract-xbrl-timeseries` / Skill5 `valuation-and-margin-of-safety` | SEC 索引（含 bucket、6-K 分类、VMF、download 状态） |
| `company/{TICKER}/raw/sec/{accession}/...` | Skill2 `collect-company-facts` | Skill3 `extract-xbrl-timeseries` | 原始证据池（可回放/可追溯） |
| `company/{TICKER}/current/events_index.parquet` | Skill2 `collect-company-facts` | Phase2 分析类 skills（growth/audit/moat 等） | 事件候选池（可追溯指针 + 初筛标签；用于后续生成 evidence claims） |
| `company/{TICKER}/current/xbrl_atlas/*` | Skill3 `extract-xbrl-timeseries` | Skill4 `recast-economic-statements` | XBRL 报表图谱与 facts 底座 |
| `company/{TICKER}/current/economic/*` | Skill4 `recast-economic-statements` | Skill5 `valuation-and-margin-of-safety` | 经济三表与核心指标（ROIC/FCF 等） |
| `company/{TICKER}/current/valuation/*` | Skill5 `valuation-and-margin-of-safety` | 下游决策/报告 | 估值输出（value_state 等） |

---

## SEC 下载策略：VMF（Valuation Materiality Filter）

### 0. 发行人类型识别（Domestic vs FPI）

- 不依赖 “CIK 有 FPI 标记” 这类不稳定信号；以近年 filings 的 forms 推断 `issuer_type`
- **Init 首跑且 forms 为空时**：做一次轻量 probe（推荐顺序：试查 `20-F`/`40-F` → `10-Q` → `6-K`，每次只查近 1 年/少量条数），以避免误判为 domestic
- 若出现 `20-F`/`20-F/A`/`40-F`/`40-F/A` → `issuer_type=fpi`
- 若出现 `10-Q`/`10-Q/A` → `issuer_type=domestic`
- 若主要为 `6-K` 且无 `10-Q`/`10-Q/A` → `issuer_type=fpi`（兜底）
- 推断结果写入 `current/filings_index.yaml: issuer_type`

### 1. 周期性核心（Periodic Core）- 10年全量下载

**按发行人类型自动适配**：

| 发行人类型 | 识别方式（forms 推断） | 下载 Forms |
|-----------|----------|-----------|
| Domestic（美国国内）| 近年 filings 存在 10-K/10-Q（且无 20-F） | 10-K, 10-K/A, 10-Q, 10-Q/A, DEF14A |
| FPI（外国私人发行人）| 近年 filings 存在 20-F/40-F 或主要为 6-K 且无 10-Q | 20-F, 20-F/A, 40-F, 40-F/A, 6-K（仅 Interim Financials/Results 子集） |

**下载内容（全部）**：
- `primary_document.html`：永远下载
- `xbrl/`：若 `has_xbrl=true`
- `sections/`：MD&A / Risk Factors / Business Description（由 `primary_document` 本地解析抽取；不依赖 `get_filing_sections`）
- `meta.yaml` + `manifest.yaml`：元数据与完整性追踪
- **FPI 的 6-K（Interim Financials/Results）额外规则**：必须下载 `exhibits/99.*`（结果公告/演示材料/摘要财务报表通常在此承载）

### 2. 事件流（Event Stream）- 全量索引 + VMF 筛选下载

**事件流定义**：
- Domestic：8-K, 8-K/A
- FPI：6-K（排除已归入 Periodic Core 的 “Interim Financials/Results” 子集后的剩余 6-K）

**策略**：
- **索引**：10年全量（所有 accession 都记录到 `filings_index.yaml`）
- **下载**：只下载通过 VMF 筛选的 filings

### 3. 6-K 分类规则（FPI 专用）— 先分类，再进入 Periodic/Event

> FPI 没有 10-Q；季度/半年中期业绩材料通常通过 6-K “furnish”，且核心内容常在 exhibits 99.*。因此 6-K 需要先拆分：
> - 6-K（Interim Financials/Results）→ 归入 Periodic Core（10年全量下载）
> - 6-K（Other events）→ 归入 Event Stream（全量索引 + VMF 选择性下载）

**严格启发式（更严格，避免把非财报 6-K 误判为 periodic）**：
- Period 信号（任一，来自标题/描述或 exhibits 描述）：`three months ended`, `six months ended`, `quarter ended`, `quarter`, `half-year`, `interim report`, `interim financial statements`, `unaudited interim`, `q1/q2/q3/q4`
- Results 信号（任一，来自标题/描述或 exhibits 描述）：`results`, `earnings`, `financial results`, `financial statements`, `interim results`, `unaudited`, `condensed consolidated`
- 判定规则：`(period AND results)`（标题/描述命中或 exhibits 99.* 描述命中均可）
- 明确禁止：仅命中 `guidance/outlook/presentation` 不能判为 6-K-Periodic；这些只能作为“已判为 periodic 后的附带下载内容”

分类输出写入索引（对 form=6-K 必填，可追溯）：
- `bucket`: `periodic_core` | `event_stream`
- `sixk_class`: `interim_results` | `other_event`
- `sixk_reasons`: `list[string]`（命中规则标签，用于 debug）
- `sixk_reasons` 最多保留 10 条（超出截断）
并映射：
- `sixk_class=interim_results` → `bucket=periodic_core` 且 `is_event_stream=false`
- `sixk_class=other_event` → `bucket=event_stream` 且 `is_event_stream=true`（进入 VMF）

#### 验收用例（6-K 分类）

- **季度/半年业绩材料（应归入 `6-K-Periodic`）**：6-K 的标题/附件描述同时出现“期间口径”（例如 `quarter ended` / `three months ended` / `six months ended` / `interim`）与“财务结果/报表”（例如 `results` / `earnings` / `financial statements`），且常伴随 `exhibits 99.*`
- **非财报事项（应归入 `6-K-Event`）**：例如 monthly return、股本变动/治理/合规披露等；应进入 Event Stream 并走 VMF（大多数情况下 `vmf_triggered=false`，仅保留索引）
- **“presentation/guidance”不能单独触发 periodic**：如果仅出现 deck/指引更新但没有明确期间口径/中期报表信号，应归入 `6-K-Event`（Event Stream）

### 4. VMF 三层筛选规则（仅作用于事件流）

> VMF 仅作用于 Event Stream（Domestic: 8-K/8-K/A；FPI: 6-K-Event）。6-K-Periodic 不进入 VMF（已归入 Periodic Core）。

#### 层 1：硬触发（Hard Trigger）— 命中即下载，不受预算限制

**A) 8-K Item 硬触发**（仅 Domestic，若能获取 items）：

| Item | 名称 | 估值材料性 |
|------|------|-----------|
| 2.02 | Results of Operations / Earnings | 直接影响 EPS/指引预期 |
| 4.01 | Auditor Change | 财务质量与可信度 |
| 4.02 | Non-Reliance / Restatement | 财务质量与可信度 |
| 2.04 | Default / Covenant breach | 现金流/折现率/生存概率 |
| 2.06 | Impairments | 盈利质量、资产质量 |
| 2.01 | Acquisition / Disposition | 未来现金流路径改变 |

**B) 附件类型硬触发**（即便无 items 也适用）：

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

对未命中硬触发的 **事件流 filings**（8-K/6-K-Event），执行关键词打分：

| 维度 | 关键词 | 权重 |
|------|--------|------|
| 现金流/融资与生存 | liquidity, refinancing, credit facility, default, covenant | 5 |
| 盈利/EPS/指引 | earnings, results, guidance, outlook, margin | 4 |
| 财务质量/会计可靠性 | restatement, auditor, material weakness | 3 |
| 资产质量与周期拐点 | impairment, restructuring | 3 |
| 并购/剥离 | acquisition, merger, disposition | 2 |

**计算公式**：
```python
score = sum(weight * count for keyword matches in title + description)
if score >= vmf_score_threshold:  # 默认 8
    download = True
```

#### 层 3：年度预算（Annual Budget）— 防止极端公司过载

```python
# 每自然年最多下载 vmf_annual_budget 个事件（默认 20）
# 但硬触发永远不受预算限制
per_year_counts = {}

for filing in event_filings_sorted_by_score_desc:
    year = filing.filed_at.year

    if filing.vmf_hard_triggered:
        download(filing)  # 硬触发必下
    elif per_year_counts.get(year, 0) < vmf_annual_budget:
        download(filing)
        per_year_counts[year] = per_year_counts.get(year, 0) + 1
    else:
        # 超预算，只保留索引
        pass
```

### 5. 事件流 Filing 下载内容

| 内容 | 下载条件 |
|------|---------|
| `primary_document.html` | 永远下载 |
| `exhibits/99.*` | 永远下载（业绩/新闻稿） |
| `exhibits/10.1`（重大合同）| 命中 liquidity/financing/M&A 关键词时 |
| `exhibits/2.1`（并购协议）| 命中 2.01 item 或 M&A 关键词时 |
| 其他附件 | 不下载 |

---

## 详细 Schema 规范

### filings_index.yaml

> 约束：`current/filings_index.yaml` 与 `current/filings_index.parquet` 字段集合必须一致；所有 key 必须存在，但允许值为 `null`（例如 VMF 字段仅对事件流有意义）。

```yaml
# 路径: current/filings_index.yaml
# 编码: UTF-8
# 格式: YAML

as_of: "2026-01-14"                    # ISO date string
issuer_type: "fpi"                     # domestic | fpi
sixk_classifier_version: "strict_period_and_results"
vmf_version: "standard"

window:
  mode: "maintenance"                  # init | maintenance
  start: "2026-01-08"
  end: "2026-01-14"
  overlap_days: 2
  lookback_years: 10

totals:
  fetched: 320                         # 本次窗口从源返回的 filings 数（含重复/旧）
  deduped_new: 12                      # 本次新增 accession 数
  stored_total: 5420                   # 写入后累计 accession 总数
filings:
  # --- FPI 6-K-Periodic（Interim Financials/Results）：进入 Periodic Core，全量下载 ---
  - form: "6-K"                         # string: 10-K, 10-Q, 8-K, DEF14A, 20-F, 40-F, 6-K, etc.
    filed_at: "2024-08-15"              # ISO date string
    period_end: null                    # SEC period_of_report（周期性=财务期末；事件=事件/报告日期；6-K 往往需后续解析）
    accession: "0001104659-24-090102"   # SEC accession number (unique ID)
    has_xbrl: false                     # bool
    local_dir: "raw/sec/0001104659-24-090102/"  # relative path

    is_amendment: false                 # bool: /A 修订版
    primary_doc: "primary_document.html"  # string: 主文档文件名
    filing_url: "https://www.sec.gov/Archives/edgar/data/..."  # string: SEC 原始 URL

    bucket: "periodic_core"             # periodic_core | event_stream
    sixk_class: "interim_results"       # interim_results | other_event | null(非6-K)
    sixk_reasons: ["title:quarter ended", "ex99:interim report"]

    is_event_stream: false              # bool（与 bucket 一致）
    vmf_triggered: null                 # bool|null（仅 event_stream 有意义）
    vmf_hard_triggered: null            # bool|null（仅 event_stream 有意义）
    vmf_reasons: []                     # list[string]
    vmf_score: null                     # int|null（仅 event_stream 有意义）
    items: null                         # list[string]|null（仅 8-K 可解析）

    downloaded: true
    download_level: "primary_plus_exhibits"  # metadata_only | primary | primary_plus_exhibits
    source: "sec_edgar_mcp.get_recent_filings"

  # --- FPI 6-K-Event（Other events）：进入 Event Stream，全量索引 + VMF ---
  - form: "6-K"
    filed_at: "2025-10-09"
    period_end: null
    accession: "0001104659-25-098045"
    has_xbrl: false
    local_dir: "raw/sec/0001104659-25-098045/"

    is_amendment: false
    primary_doc: "primary_document.html"
    filing_url: "https://www.sec.gov/Archives/edgar/data/..."

    bucket: "event_stream"
    sixk_class: "other_event"
    sixk_reasons: ["ex99:monthly return"]

    is_event_stream: true
    vmf_triggered: false
    vmf_hard_triggered: false
    vmf_reasons: []
    vmf_score: 0
    items: null

    downloaded: false
    download_level: "metadata_only"
    source: "sec_edgar_mcp.get_recent_filings"
```

### filings_index.parquet

```
# 路径: current/filings_index.parquet
# 格式: Parquet (pyarrow)
# 压缩: snappy

Column Schema:
| 字段名 | 类型 | Nullable | 说明 |
|--------|------|----------|------|
| form | string | N | 表单类型 |
| filed_at | date | N | 提交日期 |
| period_end | date | Y | SEC period_of_report（周期性=财务期末；事件=事件/报告日期） |
| accession | string | N | SEC accession number（primary key） |
| has_xbrl | bool | N | 是否有 XBRL |
| local_dir | string | N | 本地目录相对路径 |
| is_amendment | bool | N | 是否修订版 |
| primary_doc | string | N | 主文档文件名 |
| filing_url | string | N | SEC 原始 URL |
| bucket | string | N | periodic_core | event_stream |
| sixk_class | string | Y | interim_results | other_event（非6-K为空） |
| sixk_reasons | list[string] | Y | 6-K 分类命中原因（非6-K为空） |
| is_event_stream | bool | N | 是否事件流（与 bucket 一致） |
| vmf_triggered | bool | Y | 是否通过 VMF（仅 event_stream 有意义） |
| vmf_hard_triggered | bool | Y | 是否硬触发（仅 event_stream 有意义） |
| vmf_reasons | list[string] | Y | 触发原因列表 |
| vmf_score | int32 | Y | VMF 打分（仅 event_stream 有意义） |
| items | list[string] | Y | 8-K items（非8-K为空） |
| downloaded | bool | N | 是否已下载 |
| download_level | string | N | 下载级别 |
| source | string | N | 元数据来源（工具/实现版本） |
```

### raw/sec/{accession}/meta.yaml

```yaml
# 路径: raw/sec/{accession}/meta.yaml
# 每个 filing 的元数据

form: "8-K"
filed_at: "2026-01-10"
period_end: "2026-01-10"
accession: "0000123456-26-000015"
cik: "0000123456"
company_name: "Apple Inc."

# SEC 原始信息
filing_url: "https://www.sec.gov/Archives/edgar/data/..."
primary_doc_url: "https://www.sec.gov/Archives/edgar/data/.../d123456d8k.htm"

# XBRL
has_xbrl: false

# 8-K 特有
items: ["2.02", "9.01"]
items_description:
  - item: "2.02"
    description: "Results of Operations and Financial Condition"
  - item: "9.01"
    description: "Financial Statements and Exhibits"

# 附件清单
exhibits:
  - number: "99.1"
    description: "Press Release dated January 10, 2026"
    url: "https://..."
    downloaded: true
  - number: "104"
    description: "Cover Page Interactive Data File"
    url: "https://..."
    downloaded: false

# VMF 筛选结果
vmf:
  triggered: true
  hard_triggered: true
  reasons: ["item_2.02", "exhibit_99.1"]
  score: 12
```

### raw/sec/{accession}/manifest.yaml

```yaml
# 路径: raw/sec/{accession}/manifest.yaml
# 下载清单与完整性标记

downloaded_at: "2026-01-14T10:23:45-05:00"  # ISO datetime with timezone
download_level: "primary_plus_exhibits"     # metadata_only | primary | primary_plus_exhibits

# 文件清单
files:
  primary_document.html:
    exists: true
    bytes: 123456
    sha256_16: "a1b2c3d4e5f6g7h8"

  sections/mdna.md:
    exists: true
    bytes: 45678
    sha256_16: "b2c3d4e5f6g7h8i9"

  sections/risk_factors.md:
    exists: true
    bytes: 34567
    sha256_16: "c3d4e5f6g7h8i9j0"

  exhibits/exhibit_99_1.html:
    exists: true
    bytes: 12345
    sha256_16: "d4e5f6g7h8i9j0k1"

# XBRL（周期性 filing）
xbrl:
  has_xbrl: false
  files: []

# 完整性检查
completeness:
  has_primary_doc: true
  has_sections: true
  has_exhibits: true
  has_xbrl: false
  all_required_present: true
```

### events_index.parquet（候选事件池）

> 说明：这不是 evidence claim（结论），而是“候选事件指针”。后续分析类 skills 从这里挑选事件，写入 `current/evidence.jsonl`（带引用锚点与置信度）。

```
# 路径: current/events_index.parquet
# 格式: Parquet (pyarrow)
# 压缩: snappy

Column Schema:
| 字段名 | 类型 | Nullable | 说明 |
|--------|------|----------|------|
| event_id | string | N | 稳定ID：`sec:{accession}` |
| event_type | string | N | sec |
| occurred_at | timestamp[us, tz=UTC] | Y | sec=filed_at（统一写 UTC timestamp） |
| ticker | string | N | 归属 ticker |
| headline | string | Y | sec=form + 可选简述 |
| tags | list[string] | Y | sec=vmf_reasons/items 等抽象主题 |
| materiality_hint | string | Y | 轻量提示（例如 vmf_triggered_event） |
| score_hint | float32 | Y | Phase1 可选数值提示：sec=vmf_score（可为空） |
| impact_score | float32 | Y | Phase2：事件 materiality/impact score（0-1）；Phase1 站位，默认 NaN |
| source_ref_json | string | Y | JSON：sec={local_dir, filing_url} |
| anchors_json | string | Y | JSON：sec={items, exhibits}（可选） |
```

**内部步骤**

### Step 0 - 初始化 + 身份检查

1. 确保 ticker 目录结构存在
2. 加载 `company.yaml` 并验证 `cik`
3. 若 `cik` 缺失 → `blocked`，写 `needs.yaml` 指向 `company-foundation`
4. 判断 `issuer_type`（domestic vs fpi）

### Step 1 - SEC 管道

#### 1.1 确定运行模式

```python
filings_index_path = current_dir / "filings_index.yaml"
if not filings_index_path.exists() or force_refresh:
    mode = "init"
    fetch_start = as_of - timedelta(days=lookback_years * 365)
else:
    mode = "maintenance"
    last_filed_at = max_date(atomic_io.load_yaml(filings_index_path)["filings"].filed_at)
    fetch_start = last_filed_at - timedelta(days=overlap_days)

fetch_end = as_of
sec_days = (fetch_end - fetch_start).days + 1
```

#### 1.2 获取周期性核心 filings（含 FPI 6-K-Interim）

```python
# 根据 issuer_type 确定 forms
if issuer_type == "domestic":
    periodic_forms = ["10-K", "10-K/A", "10-Q", "10-Q/A", "DEF14A"]
else:  # fpi
    periodic_forms = ["20-F", "20-F/A", "40-F", "40-F/A"]

# 周期性核心：在 [fetch_start, fetch_end] 窗口内拉取并补齐缺口（init=10年；maintenance=间隔天数+overlap）
periodic_filings = []
for form in periodic_forms:
    filings = sec_edgar_mcp.get_recent_filings(
        identifier=cik,
        form_type=form,
        days=sec_days
    )
    periodic_filings.extend(filings)

# 周期性 core：全部下载（已存在则跳过/校验 manifest）
for filing in periodic_filings:
    download_periodic_filing(filing)
```

#### 1.3 获取事件流（Domestic: 8-K；FPI: 6-K-Event）

```python
# 事件流（Event Stream）+ FPI 6-K 拆分
event_filings = []

if issuer_type == "domestic":
    event_forms = ["8-K", "8-K/A"]
    for form in event_forms:
        event_filings.extend(sec_edgar_mcp.get_recent_filings(
            identifier=cik,
            form_type=form,
            days=sec_days,
        ))
else:
    # FPI：6-K 先分类
    sixk_all = sec_edgar_mcp.get_recent_filings(
        identifier=cik,
        form_type="6-K",
        days=sec_days,
    )

    sixk_periodic = []
    sixk_event = []
    for filing in sixk_all:
        sixk_class, reasons = classify_6k_periodic_vs_event(filing)  # strict：period AND results（不允许 guidance/presentation-only）
        filing["sixk_class"] = sixk_class
        filing["sixk_reasons"] = reasons

        if sixk_class == "interim_results":
            filing["bucket"] = "periodic_core"
            filing["is_event_stream"] = False
            sixk_periodic.append(filing)   # 归入 Periodic Core（季度/半年中期业绩材料）
        else:
            filing["bucket"] = "event_stream"
            filing["is_event_stream"] = True
            sixk_event.append(filing)      # 归入 Event Stream（其他事件）

    # 6-K-Periodic：像 10-Q 一样对待（全部下载；exhibits/99.* 必下）
    periodic_filings.extend(sixk_periodic)
    for filing in sixk_periodic:
        download_periodic_filing(filing)

    # 6-K-Event：才进入 VMF
    event_filings = sixk_event

# VMF 筛选（仅事件流）
for filing in event_filings:
    vmf_result = apply_vmf(filing)
    filing["vmf_triggered"] = vmf_result.triggered
    filing["vmf_hard_triggered"] = vmf_result.hard_triggered
    filing["vmf_reasons"] = vmf_result.reasons
    filing["vmf_score"] = vmf_result.score

    if vmf_result.triggered and within_annual_budget(filing):
        download_event_filing(filing)
    else:
        # 只保存元数据，不下载
        filing["downloaded"] = False
        filing["download_level"] = "metadata_only"
```

#### 1.4 更新 filings_index

```python
existing_index = atomic_io.load_yaml(filings_index_path)
existing_filings = existing_index.get("filings", [])

by_accession = {f["accession"]: f for f in existing_filings if f.get("accession")}
existing_accessions = set(by_accession.keys())

fetched = len(periodic_filings) + len(event_filings)
deduped_new = 0

for filing in periodic_filings + event_filings:
    record = normalize_filing_record(filing, issuer_type=issuer_type)
    accession = record["accession"]
    if accession not in existing_accessions:
        deduped_new += 1
    by_accession[accession] = record  # upsert（允许修正历史字段：downloaded/vmf/sixk_class/...）

all_filings = sorted(by_accession.values(), key=lambda f: f["filed_at"], reverse=True)

filings_index_payload = {
    "as_of": str(as_of),
    "issuer_type": issuer_type,
    "sixk_classifier_version": "strict_period_and_results",
    "vmf_version": "standard",
    "window": {
        "mode": mode,
        "start": str(fetch_start),
        "end": str(fetch_end),
        "overlap_days": overlap_days,
        "lookback_years": lookback_years,
    },
    "totals": {
        "fetched": fetched,
        "deduped_new": deduped_new,
        "stored_total": len(all_filings),
    },
    "filings": all_filings,
}

# runs → promote current（index/digest 先写 run 快照，再原子替换 current）
run_outputs_current = run_dir / "outputs" / "current"
atomic_io.atomic_write_yaml(run_outputs_current / "filings_index.yaml", filings_index_payload)
atomic_io.atomic_write_parquet(run_outputs_current / "filings_index.parquet", pd.DataFrame(all_filings))

atomic_io.atomic_write_yaml(filings_index_path, filings_index_payload)
atomic_io.atomic_write_parquet(current_dir / "filings_index.parquet", pd.DataFrame(all_filings))
```

### Step 2 - Events candidates + artifacts_state + result

```python
# 子管道状态（必须写入 result.yaml: components，便于编排/排障）
sec_status = "ok" | "partial" | "blocked" | "skipped" | "error"

# Skill2 是 ingestion：不写 evidence claim（结论），只保证 ledger 文件存在（空文件也可）
evidence.ensure_jsonl(current_dir / "evidence.jsonl")
evidence.ensure_jsonl(current_dir / "questions.jsonl")

# 生成候选事件池（events_index.parquet）：供 Phase2 分析类 skills 挑选并写 evidence claims
events = []

# from SEC event_stream（候选 SEC 事件；通常用 VMF-triggered/downloaded 的 event filings）
for f in all_filings:
    if f.get("bucket") != "event_stream":
        continue
    if not f.get("vmf_triggered"):  # Phase1：只把“值得看/可下载”的事件推入候选池
        continue
    events.append({
        "event_id": f"sec:{f.get('accession')}",
        "event_type": "sec",
        "occurred_at": f.get("filed_at"),
        "ticker": ticker,
        "headline": f"{f.get('form')} {f.get('accession')}",
        "tags": (f.get("vmf_reasons") or []) + (f.get("items") or []),
        "materiality_hint": "vmf_triggered_event",
        "score_hint": f.get("vmf_score"),
        "impact_score": None,  # Phase2: placeholder (default NaN)
        "source_ref_json": json.dumps({"local_dir": f.get("local_dir"), "filing_url": f.get("filing_url")}, ensure_ascii=False, sort_keys=True),
        "anchors_json": json.dumps({"items": f.get("items")}, ensure_ascii=False, sort_keys=True),
    })

events_df = pd.DataFrame(events)
if events_df.empty:
    events_df = pd.DataFrame(columns=[
        "event_id", "event_type", "occurred_at", "ticker", "headline", "tags",
        "materiality_hint", "score_hint", "impact_score", "source_ref_json", "anchors_json",
    ])
else:
    events_df["occurred_at"] = pd.to_datetime(events_df["occurred_at"], utc=True, errors="coerce")

atomic_io.atomic_write_parquet(run_outputs_current / "events_index.parquet", events_df)
atomic_io.atomic_write_parquet(current_dir / "events_index.parquet", events_df)

sec_warnings, sec_errors = [], []

# artifacts_state：尽量与 components.*.status 一致（避免“最后统一 ok”误导）
# - index/digest 若本次落盘（即使内容为空）→ 更新 artifacts_state
# - index.parquet 若 skipped → 不触碰（不更新 artifacts_state）
if sec_status in ["ok", "partial", "skipped"]:
    artifacts_state.update_artifacts_state(ticker, "filings_index.yaml", sec_status, run_id)
    if sec_status in ["ok", "partial"]:
        artifacts_state.update_artifacts_state(ticker, "filings_index.parquet", sec_status, run_id)

# events candidates（派生索引）：落盘成功则记为 ok（即使为空也 ok）
artifacts_state.update_artifacts_state(ticker, "events_index.parquet", "ok", run_id)

# components（写入 result.yaml，编排器可直接用；window/totals 对齐本次实际写入的契约文件）
components = {
    "sec": {
        "status": sec_status,
        "mode": filings_index_payload.get("window", {}).get("mode"),
        "window": filings_index_payload.get("window", {}),
        "totals": filings_index_payload.get("totals", {}),
        "warnings": sec_warnings,
        "errors": sec_errors,
    },
}

# Rollup status（更无歧义，编排器可直接用）
# 1) sec blocked/error → skill blocked/error
# 2) sec partial → skill partial
# 3) sec skipped → skill skipped
# 4) sec ok → skill ok
status = sec_status

# Write result
runlog.write_result(run_dir, ticker, SKILL_NAME, status,
    outputs=outputs, warnings=warnings, as_of=str(as_of),
    components=components)
```

---

**blocked 条件**

- `company.yaml` 缺 `cik` → `blocked`
- SEC 元数据完全不可用 **且** 没有现存 `filings_index.yaml` → `blocked`

**partial 条件**

- SEC 管道部分失败/降级（例如部分 accession 下载失败）→ `partial`

---

**查漏补缺规则（增量策略）**

### SEC

- Init 模式：10年全量（周期性全下，事件流 VMF 筛选下载）
- Maintenance 模式：以 `current/filings_index.yaml` 的最新 `filed_at` 为锚点，回退 `overlap_days` 再抓取到 `as_of`，用于补齐间隔天数 + 防止边界缺失
- 周期性 filing 永远下载；事件 filing 永远 VMF 筛选


---

### Skill 3: `extract-xbrl-timeseries`

> 报表图谱（Statement Atlas）：把报表"整棵树"抽出来，保留溯源链路

**职责边界**

- 从 raw/sec 的 XBRL 抽取：**报表树（nodes/edges）+ 报表事实（facts）+ 可视化路径（paths）**
- 覆盖 10 年（按 filings 可得）
- **不依赖固定 tag 集**：抽取报表上出现的所有 line items
- 每个数字可追溯：accession / fact / context / 单位 / 维度

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `lookback_years` | int | - | 10 | 回溯年数 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/filings_index.yaml`
- `raw/sec/`（对应 accession 的 XBRL 必须存在）

**输出**

- `current/xbrl_atlas/periods.yaml`
- `current/xbrl_atlas/nodes.parquet`
- `current/xbrl_atlas/edges.parquet`
- `current/xbrl_atlas/facts.parquet`
- `current/xbrl_atlas/paths.parquet`
- evidence

**内部步骤**

1. 读取 filings_index，挑选最近 N 年的 10-K/10-Q
2. 对每个 accession 的 XBRL：
   - 抽取 presentation linkbase → nodes/edges
   - 抽取 calculation linkbase → edges（补充 weight）
   - 抽取 instance facts → facts
3. 生成 paths（从根到叶的路径字符串）
4. 合并所有 period，输出

**核心能力目标**

输出应支持快速生成：
- **利润表树状图**：从 Comprehensive Income → Net Income → 各费用项/分部
- **现金流瀑布图**：从 Net change in cash → CFO/CFI/CFF → 细项
- **资产负债表组成 + 期间变化 bridge**

**查漏补缺规则**

- filings_index 未变化（accession 列表未变）且 atlas 已存在 → `skipped`
- 新增 10-Q/10-K → 只对新增 accession 增量抽取并 merge

**partial 条件**

- 某些 period 没 XBRL 或某些 role 无 calculation linkbase → `partial` + questions

**blocked 条件**

- filings_index 缺失或 raw/sec 对应 XBRL 不存在 → `blocked`

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
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `policy_version` | string | - | "default" | 重铸策略标识（rulebook id） |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/xbrl_atlas/nodes.parquet`
- `current/xbrl_atlas/edges.parquet`
- `current/xbrl_atlas/facts.parquet`
- `current/xbrl_atlas/periods.yaml`

**输出**

- `current/economic/recast_policy.yaml`
- `current/economic/economic_statements.parquet`
- `current/economic/core_metrics.parquet`
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

**partial 条件**

- maintenance capex 估计缺关键字段 → 仍产出但 `partial`，把估计方法降级

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

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/economic/economic_statements.parquet`
- `current/economic/core_metrics.parquet`
- `current/filings_index.yaml`
- `raw/sec/`（用于引用审计意见、会计政策、风险因素）

**输出**

- `current/diagnostics/profit_quality.yaml`
- `current/diagnostics/profit_risk_forecast.yaml`
- questions/evidence

**profit_quality.yaml 结构**

```yaml
as_of: 2026-01-04
signals:
  accrual_quality:
    sloan_accrual_ratio: 0.12
    interpretation: "偏高：利润含较多应计成分"
  manipulation_risk:
    beneish_m_score: -1.6
    flags: ["DSRI_up", "AQI_up"]
  value_strength:
    piotroski_f_score: 6
summary:
  profit_backed_by_cash: "medium"
  key_risks:
    - "DSO 上升与收入增长背离"
  confidence: 0.7
```

**profit_risk_forecast.yaml 结构**

```yaml
horizon_years: 5
economic_profit_outlook:
  clear_components:
    - "存量合同续费（证据：xxx）"
  unclear_components:
    - "新增需求是否可持续（证据缺口：yyy）"
risk_to_profit:
  bear_case_drivers:
    - "库存/应收恶化 -> 折扣促销 -> 毛利下滑"
suggested_valuation_adjustments:
  discount_rate_bps: +150
  margin_normalization: -200
```

**查漏补缺规则**

- 若 economic 输出未变且 filings 未新增 → `skipped`

**blocked 条件**

- economic/core 缺失 → `blocked`

---

### Skill 6: `growth-driver-explorer`

> 成长性进一步探索

**职责边界**

- 把增长拆成"能解释"的驱动：
  - 量/价/结构（mix）/地区/新产品
  - 会计口径/并购驱动 vs 内生驱动
- 输出：再投资率、ROIIC、生命周期阶段（Damodaran 模板）
- 给估值提供：未来利润路径的"证据化假设"

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/diagnostics/profit_quality.yaml`
- `current/diagnostics/profit_risk_forecast.yaml`
- `current/filings_index.yaml`
- `raw/sec/`

**输出**

- `current/diagnostics/growth_drivers.yaml`
- questions/evidence

**growth_drivers.yaml 结构**

```yaml
as_of: 2026-01-04
growth_decomposition:
  revenue:
    volume: "unknown"
    price: "supported"
    mix: "likely_positive"
    mna: "material"
reinvestment_and_roiic:
  reinvestment_rate: 0.35
  roiic: 0.22
lifecycle_stage: "mature|growth|decline|turnaround"
assumptions_for_valuation:
  base_revenue_cagr_5y: 0.06
  base_margin_path: "stable_to_slight_up"
```

**blocked 条件**

- 缺任一 hard 产物 → `blocked`

---

### Skill 7: `moat-inferencer`

> 护城河推断器 → 质量系数

**职责边界**

- 用可追溯证据识别护城河来源：
  - 可检验命题（Greenwald）
  - 行业结构压力（Porter 五力）
  - moat 类型（Morningstar 五类）
  - 优势持续周期 CAP（Mauboussin）
- 产出 **quality_coefficient**：把证据映射成估值参数

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/diagnostics/growth_drivers.yaml`
- `current/diagnostics/profit_quality.yaml`
- `current/filings_index.yaml`
- `raw/sec/`

**输出**

- `current/diagnostics/moat.yaml`
- `current/diagnostics/quality_coefficient.yaml`
- evidence/questions

**moat.yaml 结构**

```yaml
moat_sources:
  - type: "switching_costs"
    evidence:
      - ref: "10-K MD&A: retention/renewal"
    durability: "high|medium|low"
  - type: "cost_advantage"
    evidence: []
    durability: "medium"
industry_pressure:
  porter_five_forces:
    rivalry: "high"
    supplier_power: "medium"
    buyer_power: "low"
    substitutes: "medium"
    new_entrants: "low"
cap:
  advantage_period_years_base: 8
  erosion_triggers:
    - "新进入者以价格战切入"
```

**quality_coefficient.yaml 结构**

```yaml
as_of: 2026-01-04
components:
  moat: 4
  financial_quality: 3
  governance_capital_allocation: 3
  balance_sheet_resilience: 4
mapping_to_valuation:
  discount_rate_base: 0.105
  advantage_period_years: 8
  terminal_multiple_base: 15
coefficient_base: 0.72
confidence: 0.65
```

**blocked 条件**

- 任一 hard 缺失 → `blocked`

---

### Skill 8: `valuation-and-margin-of-safety`

> 估值与安全边际

**职责边界**

- 以"经济利润 × 质量系数"组织估值：
  - EPV / Earnings multiple（质量系数 → multiple）
  - DCF（质量系数 → 折现率 + 优势期 + fade）
  - 可选：Residual Income（Penman 风格）
- 输出：bear/base/bull 估值区间、敏感性、下行保护来源

**参考框架**

- McKinsey DCF/价值驱动因素体系
- Penman 会计估值视角（残余收益/报表驱动）

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `model_type` | enum | - | hybrid | epv/dcf/ri/hybrid |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/market_snapshot.yaml`
- `current/economic/core_metrics.parquet`
- `current/economic/economic_statements.parquet`
- `current/diagnostics/profit_risk_forecast.yaml`
- `current/diagnostics/growth_drivers.yaml`
- `current/diagnostics/quality_coefficient.yaml`

**输出**

- `current/valuation/valuation.yaml`
- `current/valuation/valuation_model.csv`
- `current/valuation/value_state.yaml`
- `current/valuation/investment_memo.md`
- evidence

**valuation.yaml 结构**

```yaml
as_of: 2026-01-04
methods_used: ["dcf", "epv"]
assumptions:
  discount_rate:
    bear: 0.12
    base: 0.105
    bull: 0.095
  advantage_period_years:
    bear: 5
    base: 8
    bull: 12
  owner_earnings_margin:
    bear: 0.08
    base: 0.10
    bull: 0.12
results:
  intrinsic_value_per_share:
    bear: 10
    base: 20
    bull: 30
sensitivity:
  keys: ["discount_rate", "advantage_period_years", "owner_earnings", "roiic"]
downside_protection:
  net_cash_per_share: 2.1
  tangible_backstop_notes: "..."
```

**查漏补缺规则**

- market_snapshot 的 as_of 未变且上游输入未变 → `skipped`
- price 更新但模型假设不变 → 只重算 MOS 与 per-share

**blocked 条件**

- 任一 hard 缺失 → `blocked`

---

### Skill 9: `cross-examination-audit`

> 反问和审计

**职责边界**

- 对比：管理层叙事（MD&A/风险因素） vs 数字（经济三表）
- 找矛盾：需求强 → 但库存/应收恶化；"一次性"常态化等
- 输出"反向思维审计清单"：
  - **What did I miss?**（Munger 反向思维）
  - **Why is it cheap?**
  - **Who is running it?**
- 明确：这会如何影响估值参数

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `current/valuation/value_state.yaml`
- `current/valuation/valuation.yaml`
- `current/diagnostics/quality_coefficient.yaml`
- `current/diagnostics/profit_quality.yaml`
- `current/diagnostics/growth_drivers.yaml`
- `current/filings_index.yaml`
- `raw/sec/`

**输出**

- `current/diagnostics/audit.yaml`
- `questions.jsonl`（追加）
- `evidence.jsonl`（追加）
- 可选：`runs/{run_id}/needs.yaml`（若审计要求回到某一步重跑）

**audit.yaml 结构**

```yaml
as_of: 2026-01-04
contradictions:
  - id: "A1"
    claim: "管理层称需求强劲，但 DSO + 库存同时上升"
    evidence_refs: ["E_..."]
    severity: "high"
what_did_i_miss:
  - "是否存在渠道压货/退货条款变化？"
why_is_it_cheap_hypotheses:
  - "行业结构性下行叙事 vs 公司份额韧性证据不足"
who_is_running_it:
  notes: "DEF14A/管理层更迭/激励结构要点"
impact_on_valuation:
  recommended_adjustments:
    discount_rate_bps: +100
    advantage_period_years: -2
  rerun_valuation_recommended: true
```

**blocked 条件**

- 任一 hard 缺失 → `blocked`

---

## 八、编排器流程

### 8.1 固定队列

对每个 ticker 按顺序执行：

```
1. company-foundation
2. collect-company-facts
3. extract-xbrl-timeseries
4. recast-economic-statements
5. profit-quality-and-risk
6. growth-driver-explorer
7. moat-inferencer
8. valuation-and-margin-of-safety
9. cross-examination-audit
```

### 8.2 执行策略

```
for ticker in pool:
    queue = [skill1, skill2, ..., skill9]
    
    while queue:
        skill = queue.pop(0)
        result = run_skill(skill, ticker)
        
        if result.status == "blocked":
            needs = read_needs_yaml()
            producer = needs.blocked_by[0].producer_skill
            
            # 防循环检查
            if retry_count[ticker][skill] > MAX_RETRY:
                mark_manual_required(ticker, skill)
                continue
            
            # 把 producer 插到队列前面
            queue.insert(0, skill)
            queue.insert(0, producer)
            retry_count[ticker][skill] += 1
        
        elif result.status in ["ok", "partial", "skipped"]:
            continue
        
        elif result.status == "error":
            log_error(ticker, skill)
            continue
    
    # 汇总到全局
    collect_value_state(ticker)

# 生成全局排序
generate_value_summary()
```

### 8.3 全局汇总

完成所有 ticker 后，汇总所有 `current/valuation/value_state.yaml` 到：

```
/home/help/mcp/work/company_research/value_summary.csv
```

按 `margin_of_safety_base`、`confidence`、`financial_quality` 排序，筛选"显著低估且可信度高"的标的。

---

## 九、扩展插槽

后续优化落在这 4 个插槽里，不改目录和 Skill 关系：

### 9.1 Atlas 层增强（Skill 3）

- 更好的 statement_type 识别
- 更完整的维度/分部展开
- 期内 bridge / waterfall 直接产出

### 9.2 经济重铸策略（Skill 4）

- maintenance capex 估计方法库（多策略并行、按行业选择）
- operating vs financing 分类规则库
- 公司特化 overrides

### 9.3 质量系数映射（Skill 7/8）

- 把"证据 → 参数"做成显式函数
- 例如：`discount_rate = base + f(financial_quality, leverage, governance, moat)`
- 换流派只换 mapping，不动底座

### 9.4 审计问题库（Skill 9）

- "反问模板"做成 rule library
- 叙事-数字矛盾、现金流异常、一次性常态化、应收/库存组合拳、激励错配等

---

## 十、SKILL.md 写作模板

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
| ... | ... | ... | ... | ... |

## Hard 依赖

- `<path>`
- ...

## 输出

- `<path>`
- ...

## 内部步骤

1. <step>
2. <step>
...

## 查漏补缺规则

- <condition> → skipped
- <condition> → 增量更新

## partial 条件

- <condition>

## blocked 条件

- <condition>

## 输出 Schema

### <file1>
```yaml
# 字段说明
```

### <file2>
...
```

---

## 十一、实施路径

### 第一阶段：核心估值链（5 个 Skill）

| 顺序 | Skill | 产出 |
|------|-------|------|
| 1 | `company-foundation` | 身份 + 市场口径 |
| 2 | `collect-company-facts` | filings + 证据池 |
| 3 | `extract-xbrl-timeseries` | 报表图谱 |
| 4 | `recast-economic-statements` | 经济三表 + Owner Earnings |
| 5 | `valuation-and-margin-of-safety` | 估值区间 + value_state |

**第一阶段产出**：
- 可复算的估值区间（Bear/Base/Bull）
- 全局总表筛选"显著低估"标的

### 第二阶段：分析能力补齐（4 个 Skill）

| Skill | 提升能力 |
|-------|---------|
| `profit-quality-and-risk` | 财务质量/操纵风险 |
| `growth-driver-explorer` | 成长性拆解 |
| `moat-inferencer` | 护城河 → 质量系数 |
| `cross-examination-audit` | 反问审计，防大错 |

**第二阶段产出**：显著提升"错杀 vs 价值陷阱"的分辨能力。
