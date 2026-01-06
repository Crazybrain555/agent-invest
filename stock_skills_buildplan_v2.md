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

        # --- 证据池 ---
        filings_index.yaml
        news_digest.yaml
        papers_digest.yaml

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

      raw/                                 # 原始材料
        sec/{accession}/...                # SEC filings 原文 + XBRL
        news/news.jsonl                    # 原始新闻
        papers/papers.jsonl                # 论文/技术资料
        web/...                            # 网页快照

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
identity_sources:
  - type: sec
    ref: "CIK lookup"
```

### 5.2 market_snapshot.yaml

```yaml
as_of: 2026-01-04
price: 12.34
shares_outstanding: 100000000
shares_float: 80000000          # 若可取到，否则 null
market_cap: 1234000000
enterprise_value: 1500000000    # 若能算
net_debt: 266000000
source: "trading_mcp.get_fundamental_stock_metrics"
```

### 5.3 filings_index.yaml

```yaml
as_of: 2026-01-04
filings:
  - form: "10-K"
    filed_at: "2025-02-20"
    period_end: "2024-12-31"
    accession: "0000123456-25-000123"
    has_xbrl: true
    local_dir: "raw/sec/0000123456-25-000123/"
```

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
policy_version: "v0.1"
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

| # | Skill | 职责 | 对"利润×质量"贡献 |
|---|-------|------|------------------|
| 1 | `company-foundation` | 身份 + 市场口径（含 shares） | 估值分母/每股化基座 |
| 2 | `collect-company-facts` | filings + 新闻 + 论文 | 证据池 |
| 3 | `extract-xbrl-timeseries` | 报表图谱（树+事实+溯源） | 利润事实底座 |
| 4 | `recast-economic-statements` | 经济三表 + 核心指标 | Owner Earnings / ROIC |
| 5 | `profit-quality-and-risk` | 财报质量/操纵风险/利润可持续性 | 质量系数与情景下界 |
| 6 | `growth-driver-explorer` | 增长来源与 ROIIC/生命周期 | 未来利润路径 |
| 7 | `moat-inferencer` | 护城河 → 优势期 → 质量系数映射 | 质量系数主体 |
| 8 | `valuation-and-margin-of-safety` | 估值区间 + MOS + 敏感性 | 输出 IV vs 市场 |
| 9 | `cross-examination-audit` | 反问审计：找矛盾/遗漏/为什么便宜 | 提高确定性，防大错 |

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
3. 市场口径：调用 `trading_mcp` 拿 price、shares outstanding、float（若有）、market cap、债务现金（能算 EV 就算）
4. 写 evidence（身份来源、市场数据来源）

**查漏补缺规则**

- identity：若 `company.yaml` 已有 cik 且未 `force_refresh` → `skipped`
- market_snapshot：若 `as_of` 相同且文件存在且字段齐全 → `skipped`

**blocked 条件**

- 只有在"外部源完全不可用导致无法生成最小 company.yaml/market_snapshot.yaml"才 `blocked`

---

### Skill 2: `collect-company-facts`

> 合并：fetch-sec-filings + collect-news-events + collect-technical-papers

**职责边界**

- 拉 SEC filings 原文 + XBRL（按 form、lookback）
- 拉新闻事件（gdelt/rss）并生成 digest
- 拉论文/技术资料（openalex/arxiv/pubmed），生成 digest
- **增量更新**：已有 accession 不重复下载

**输入参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | ✓ | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `lookback_years` | int | - | 10 | 回溯年数 |
| `lookback_days_news` | int | - | 180 | 新闻回溯天数 |
| `papers_mode` | enum | - | auto | auto/on/off |
| `force_refresh` | bool | - | false | 强制刷新 |

**Hard 依赖**

- `company/{ticker}/company.yaml`（必须有 cik）

**输出**

- `current/filings_index.yaml`
- `raw/sec/{accession}/...`
- `raw/news/news.jsonl`
- `current/news_digest.yaml`
- `raw/papers/papers.jsonl`
- `current/papers_digest.yaml`
- evidence/questions

**内部步骤**

1. SEC：根据 cik 拉 10-K/10-Q/8-K/DEF14A（及 20-F/6-K），建立/更新 filings_index
2. 对新增 accession 下载原文与 XBRL 到 raw/sec
3. 新闻：用 ticker + company_name 组合 query，去重，写 digest
4. 论文：按 mode 决定是否检索；输出 digest（可空但文件必须有）

**查漏补缺规则**

- filings：对比 filings_index 与 SEC 最新列表，仅下载新增
- news：若上次抓取到 `as_of-1` 且无 force_refresh → 只增量抓近几天
- papers：按 staleness（30~90 天）增量

**blocked 条件**

- `company.yaml` 缺 cik 或 SEC 拉不到 filings 列表 → `blocked`

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
| `policy_version` | string | - | "v0.1" | 重铸策略版本 |
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
- `current/news_digest.yaml`
- `current/papers_digest.yaml`
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
- `current/news_digest.yaml`
- `current/papers_digest.yaml`
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

- 对比：管理层叙事（MD&A/风险因素/新闻） vs 数字（经济三表）
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
- `current/news_digest.yaml`
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
version: v0.1
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





##十二 各个skill搭建的具体方法清单