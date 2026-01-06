下面给你一套**“估值为中心、证据可追溯、可持续迭代”**的公司研究 Skill 体系规划。它的目标不是“找便宜”，而是把每家公司拆成可维护的模块，让每个 Skill 都在回答同一件事：

> **这家公司在保守、可验证的假设下，值多少钱？为什么市场会把它定价成现在这样？价值回归/价值增长靠什么实现？**

我会按你现有 MCP 能力（trading_mcp / sec_edgar_mcp / search+fetch / rss / gdelt / openalex+arxiv+pubmed+crossref / fs）来设计，尽量做到：

* 每个 Skill **分工明确**、**输入输出结构化**、**可重复运行**；
* 输出既能做“定量估值底座”，也能堆出“定性护城河/管理层/成长性证据链”；
* 目录结构以 company 为主体，同时保留历史快照；
* 后续你可以用一个总编排脚本/总 Skill 逐个 ticker 跑起来。

---

## 一、总体研究框架：估值栈（Valuation Stack）

你描述的“烟蒂”和“错杀复利”我建议不要拆成两套方法，而是当作**同一估值体系的两种“价值实现路径”权重不同**：

* **经营价值（Operating Value）**：未来经营现金流/利润能力 + 成长性（含确定性折扣）
* **退出/收购/清算价值（Exit/Asset Value）**：资产可兑现价值 + 品牌/渠道/专利/客户关系等在并购下可能体现的溢价 + 催化剂路径 这个暂时不加以考虑，因为这个越来越不常见，所以经营价值是这个框架的主要方式。

于是你的研究就可以固定成一条“从底到顶”的链条，每层都能产出结构化结果：

1. **事实底座（Facts）**

* filings（10-K/10-Q/8-K/DEF14A；外国发行人/ADR 常用 20-F/6-K）+ XBRL 时间序列
* 最新 market data（price、market cap、EV、share count 的口径）
* 关键事件（新闻、诉讼、监管、产品、并购、融资）

> **管理层视角/驱动因素 & 投资者问答（Q&A）在哪里找？（给后续 Skill 做“证据链入口”）**
>
> * **10-K / 10-Q**：核心在 **MD&A**（10-K Item 7；10-Q Item 2），以及 Business/Risk Factors 等叙述段落。
> * **20-F / 6-K（如 BABA）**：20-F 里对应的是 **Item 5 “Operating and Financial Review and Prospects”**（功能上≈MD&A）；6-K 常附 HKEX/新闻稿/演示材料（EX-99.x）。
> * **业绩沟通材料**：很多公司会把 press release / investor presentation 作为 **8-K（Item 2.02/7.01）或 6-K（EX-99.x）** 的附件发布；但 **Q&A** 更常见的是公司 IR 站点发布的 **earnings call transcript（常为 PDF）**，SEC 里不一定会单独附。
> * **工具映射（你现有 MCP）**：`sec_edgar_mcp` 拉 filings；`search+fetch` 找/抓 IR 页面；`search.parse_pdf` 把 transcript PDF 变成可检索文本（后续可按 “Q&A/Operator/Question” 切段）。
>
> 示例（BABA）：
> * SEC 20-F（年报）：https://www.sec.gov/Archives/edgar/data/1577552/0000950170-25-090161-index.html
> * SEC 6-K（Sep 2025 业绩公告，EX-99.1）：https://www.sec.gov/Archives/edgar/data/1577552/000110465925115949/0001104659-25-115949-index.html
> * IR Quarterly Results（含 Webcast/Presentation/Transcript）：https://www.alibabagroup.com/en-US/ir-financial-reports-quarterly-results

2. **真实财务（Truth Accounting）**

* 识别会计噪音：一次性项目、计提、SBC、租赁、商誉减值、收入确认、关联交易等
* 形成“**可解释的 Normalized 三表**”和“Owner Earnings/FCF/ROIC/经营杠杆”的稳定口径
* 同时做“**反作弊/红旗**”：应收、存货、现金流匹配、审计意见、重述、控制缺陷等

3. **商业质量（Business Quality）**

* 商业模式 + 单位经济模型（如果能抽到）
* 护城河证据：定价权/切换成本/网络效应/规模成本/品牌/渠道/监管牌照等
* 竞争格局与替代威胁（来自 filings + web + 行业资料）

4. **成长与资本配置（Growth & Allocation）**

* 增长来源：市场增长 + 份额提升 + 新产品 + 提价 + 并购整合
* 管理层资本配置：回购/分红/M&A/再投资的历史与纪律
* 激励机制（proxy）是否对齐长期价值

5. **误定价假说（Mispricing Hypothesis）**

* 市场在担心什么？（短期/局部/可修复 vs 长期/结构性/不可逆）
* 这些担心能否被证伪/证实？需要哪些证据？
* 关注不足/信息偏差在哪里？（coverage 低、叙事偏见、复杂业务、短期事件冲击等）

6. **价值实现路径（Pathways & Catalysts）**

* 经营改善（利润率/周转/价格/产能/渠道）
* 周期回归（库存/商品价格/利率/需求）
* 资产兑现（剥离/出售/清算）
* 公司行为（回购、私有化、并购）
* 外部变量（监管放松、诉讼落地等）

7. **估值输出（Valuation Output）**

* 给出区间（Bear/Base/Bull）+ 关键假设表
* 输出 Intrinsic Value vs Market Cap 的**安全边际**
* 记录“**我愿意为不确定性付出的折扣**”来源（护城河强弱、周期、治理、财务可信度）

> 每个 Skill 只做其中一块，但最终都要“回写”到一个统一的 `value_state.yaml`（你最终的估值底座）。

---

## 二、目录结构与落盘策略：以公司为主体 + current/runs 双层架构

你希望"以公司为主体，不太按时间"，同时又能保存历史。最佳做法是 **current + snapshots（runs）** 两层：

* `current/`：永远存放"当前结论"和最新结构化数据（便于总 Skill 直接读取）
* `runs/<run_id>/`：每次跑技能的快照（方便追溯、回测、审计、对比改动）

建议根目录放在 FS MCP 可写范围内（你 fs 只允许 `/home/help/mcp/work`），例如：

```
/home/help/mcp/work/company_research/
  registry.jsonl                         # 全局运行注册表（每个skill每次run追加一行）
  company/
    {TICKER}/
      company.yaml                       # 稳定档案：CIK/行业/交易所/FY等
      latest.json                        # 指向最新run_id + current摘要
      current/                           # "当前态"：所有下游优先读这里
        # --- 市场与财务底座 ---
        market_snapshot.yaml             # 最新市值/价格/股本口径（as_of）
        filings_index.yaml               # 你抓了哪些 filings（accession、日期、类型）
        financials_xbrl.parquet          # XBRL原始时间序列
        xbrl_mapping.yaml                # tag映射与口径说明
        financials_normalized.parquet    # Normalized 三表 + 核心口径
        normalization_adjustments.yaml   # 每一项调整可追溯
        redflags.yaml                    # 财务红旗/反作弊结论 + 证据链接

        # --- 商业质量与成长 ---
        profile.yaml                     # 业务/分部/竞争对手/管理层摘要
        competitors.yaml                 # 竞争对手清单
        moat.yaml                        # 护城河与竞争证据
        growth.yaml                      # 成长性拆解与证据
        growth_kpi.yaml                  # 可跟踪KPI
        management.yaml                  # 管理层、激励、资本配置
        allocation_history.csv           # 回购/分红/M&A/股权激励时间线

        # --- 新闻与研究资料 ---
        news_digest.yaml                 # 事件线：时间—事件—标签—来源
        papers_digest.yaml               # 技术论文/专利摘要

        # --- 误定价与价值实现 ---
        mispricing.yaml                  # 误定价假说清单 + 证伪路径
        catalysts.yaml                   # 催化剂与价值实现路径
        risk_register.yaml               # 风险清单

        # --- 估值输出 ---
        valuation.yaml                   # 估值区间、假设、敏感性、MOS
        valuation_model.csv              # 预测表、FCF、折现等
        value_state.yaml                 # 估值底座总表（给总Skill用）

        # --- 综合输出 ---
        thesis.md                        # 研究员视角主文
        questions.jsonl                  # 未解之谜/待验证事项
        evidence.jsonl                   # 证据账本：每条结论引用哪些来源
        artifacts_state.yaml             # 可选：每个artifact的更新时间/来源/版本

      raw/                               # 原始材料
        sec/
          {accession}/...                # 原始 filings（html/pdf/txt/xbrl）
        news/
          news.jsonl                     # 原始新闻条目（gdelt/rss）
        web/
          ...                            # 关键网页抓取快照
        papers/
          papers.jsonl                   # 学术/技术资料
      runs/
        {run_id}/
          meta.yaml                      # 本次 run 的输入、工具调用、缓存命中、输出文件列表
          result.yaml                    # Skill运行结果与状态
          needs.yaml                     # 仅当blocked时存在
          outputs/                       # 本次 run 产物（可选）
      logs/                              # 可选
```

### 写入规则（避免 current 被写坏）

* Skill **先写 runs/{run_id}/outputs/**（或直接写 runs/{run_id}/xxx）
* 成功（ok/partial）后，再把关键产物 **拷贝/原子替换** 到 `current/`
* `latest.json` 只在成功后更新
* 每次 run 追加 `registry.jsonl`，方便全局审计

### 为什么要 `evidence.jsonl`

你后续会非常依赖“能否复核”。我建议任何 Skill 输出的结论，都配套写入 evidence：

**一条 evidence record** 大致长这样（jsonl 一行一条）：

```json
{
  "as_of": "2026-01-04",
  "ticker": "XXX",
  "skill": "financial_redflags",
  "claim_id": "AR_turnover_abnormal_1",
  "claim": "应收周转恶化主要来自某业务线账期拉长，而非收入虚增",
  "confidence": 0.65,
  "sources": [
    {"type": "sec_filing", "form": "10-K", "accession": "...", "path": "raw/sec/.../10k.html", "anchor": "Note X / AR"},
    {"type": "xbrl_fact", "tag": "AccountsReceivableNetCurrent", "path": "current/financials_normalized.parquet", "range": "2019-2025"}
  ]
}
```

这会极大降低“研究系统越做越像黑箱”的风险。

---

## 三、Skill 体系概览

按"采集 → 结构化 → 分析 → 综合"分层，共 15 个 Skill（详细规格见后文"3) Skill 规格"）：

| Layer | Skill | 一句话职责 | 主要输出 |
|-------|-------|-----------|---------|
| **0 初始化** | `init-company-dossier` | 建立公司目录与基础档案 | `company.yaml`, `latest.json` |
| | `resolve-company-identity` | 解析 ticker → CIK/公司名 | 更新 `company.yaml` |
| | `update-market-snapshot` | 刷新市场口径（价格/市值/EV） | `market_snapshot.yaml` |
| **1 事实采集** | `fetch-sec-filings` | 拉取 SEC filings + XBRL | `filings_index.yaml`, `raw/sec/` |
| | `collect-news-events` | 收集新闻事件 + 分类标签 | `news_digest.yaml` |
| | `collect-technical-papers` | 收集技术论文/专利（按需） | `papers_digest.yaml` |
| **2 财务底座** | `extract-xbrl-timeseries` | XBRL → 标准化时间序列 | `financials_xbrl.parquet` |
| | `normalize-financials` | 会计口径 → 经济口径 | `financials_normalized.parquet` |
| | `financial-redflags` | 财务反作弊/红旗扫描 | `redflags.yaml` |
| **3 商业质量** | `business-profile-and-segments` | 公司画像与分部 | `profile.yaml`, `competitors.yaml` |
| | `moat-analysis` | 护城河证据矩阵 | `moat.yaml` |
| | `growth-engine` | 成长性拆解与 KPI | `growth.yaml`, `growth_kpi.yaml` |
| | `management-and-capital-allocation` | 管理层资本配置轨迹 | `management.yaml`, `allocation_history.csv` |
| **4 估值决策** | `mispricing-hypotheses` | 误定价假说 + 证伪路径 | `mispricing.yaml` |
| | `catalysts-and-pathways` | 价值实现路径与催化剂 | `catalysts.yaml` |
| | `risk-register` | 风险清单 + 监控指标 | `risk_register.yaml` |
| | `valuation-engine` | 估值区间（Bear/Base/Bull） | `valuation.yaml`, `value_state.yaml` |
| **5 综合输出** | `synthesize-final-report` | 汇总成可读报告 | `thesis.md` |

> 命名统一用 `verb-noun` 的 kebab-case 风格。详细的输入参数、依赖关系、内部步骤、触发策略见"3) Skill 规格"。

---

## 四、统一“估值底座”数据模型：`value_state.yaml`

这是你后续总编排最需要的一个文件：不管跑多少 Skill，最后都应该把关键结论落到同一张“总表”，方便筛选“显著低于估值”的公司。

建议 `value_state.yaml` 至少包含：

```yaml
ticker: XXX
as_of: 2026-01-04
market:
  price: ...
  market_cap: ...
  enterprise_value: ...
  shares_out: ...
valuation:
  intrinsic_value_equity_range:
    bear: ...
    base: ...
    bull: ...
  intrinsic_value_per_share_range:
    bear: ...
    base: ...
    bull: ...
  margin_of_safety_base: ...   # (IV_base - market_cap) / market_cap
  key_assumptions:
    revenue_cagr_5y: ...
    normalized_margin: ...
    reinvestment_rate: ...
    discount_rate: ...
    terminal_multiple_or_growth: ...
quality:
  moat_strength: 0..5
  management_score: 0..5
  accounting_trust: 0..5
  confidence: 0..1             # “我对估值区间可靠性的信心”
thesis:
  one_liner: "市场在担心..., 但证据显示..."
  main_drivers:
    - ...
  main_risks:
    - ...
  catalysts:
    - ...
open_questions_count: 12
links:
  thesis_md: current/thesis.md
  valuation_yaml: current/valuation.yaml
  evidence_jsonl: current/evidence.jsonl
```

这份文件相当于“你最后要拿来下单前看的那张卡片”。

---

## 五、总编排（你后面要写的大 Skill/脚本）的建议流程

你之后的 orchestrator（循环调用）可以非常清晰：

1. 读取 `pool.csv`（你已经有）拿到 tickers
2. 对每个 ticker 按顺序执行（共 18 个 Skill）：

   **Layer 0：初始化**
   * `init-company-dossier`
   * `resolve-company-identity`
   * `update-market-snapshot`

   **Layer 1：事实采集**
   * `fetch-sec-filings`
   * `collect-news-events`
   * `collect-technical-papers`（按需，医药/科技类）

   **Layer 2：财务底座**
   * `extract-xbrl-timeseries`
   * `normalize-financials`
   * `financial-redflags`

   **Layer 3：商业质量**
   * `business-profile-and-segments`
   * `moat-analysis`
   * `growth-engine`
   * `management-and-capital-allocation`

   **Layer 4：估值决策**
   * `mispricing-hypotheses`
   * `catalysts-and-pathways`
   * `risk-register`
   * `valuation-engine`

   **Layer 5：综合输出**
   * `synthesize-final-report`

3. 结束后汇总所有 `value_state.yaml` 到全局表 `company_research/value_summary.csv`
4. 按 `margin_of_safety_base`、`confidence`、`accounting_trust` 做"可投候选排序"

> **关于落点归属**：
> * 造假/红旗 → `redflags.yaml` + `risk_register.yaml`
> * 催化剂 → `catalysts.yaml`
> * 它们可由多个 Skill 共同贡献，但最终汇总到这些文件。

---

## 六、先做 Demo 的最小可用集合（MVP）

为了尽快跑通闭环（从池子 → 研究 → 估值 → 报告），建议分两阶段实现：

### 第一阶段：核心估值链（9 个 Skill）

| 顺序 | Skill | 为什么必需 |
|------|-------|-----------|
| 1 | `init-company-dossier` | 建立目录结构和基础档案 |
| 2 | `resolve-company-identity` | 解析 CIK，后续 SEC 拉取依赖 |
| 3 | `update-market-snapshot` | 获取当前价格/市值，估值必需 |
| 4 | `fetch-sec-filings` | 拉取 10-K/10-Q/8-K 原文和 XBRL |
| 5 | `extract-xbrl-timeseries` | 抽取财务时间序列 |
| 6 | `normalize-financials` | 会计口径 → 经济口径 |
| 7 | `financial-redflags` | 财务反作弊扫描 |
| 8 | `valuation-engine` | 输出估值区间和 value_state |
| 9 | `synthesize-final-report` | 汇总成可读报告 |

**第一阶段产出**：
* 可复算的估值区间（Bear/Base/Bull）
* 财务可信度/红旗摘要
* 可读报告草稿（thesis.md）
* 全局总表筛选"显著低估且可信度较高"的标的

### 第二阶段：补齐分析能力（9 个 Skill）

| Skill | 提升能力 |
|-------|---------|
| `business-profile-and-segments` | 公司画像，理解业务 |
| `moat-analysis` | 护城河证据，区分价值陷阱 |
| `growth-engine` | 成长性拆解，验证增长假设 |
| `management-and-capital-allocation` | 管理层轨迹，资本配置纪律 |
| `collect-news-events` | 新闻事件线，补近期变化 |
| `mispricing-hypotheses` | 误定价假说，明确投资逻辑 |
| `catalysts-and-pathways` | 价值实现路径，明确触发条件 |
| `risk-register` | 风险清单，完善估值折扣 |
| `collect-technical-papers` | 技术论文（按需，医药/科技类） |

**第二阶段产出**：显著提升"错杀 vs 价值陷阱"的分辨能力。

---

## 七、你这套体系的“关键工程原则”（强烈建议坚持）

1. **所有结论必须指向证据**（evidence ledger）
   否则系统规模一大就会变成“写得像真的”。

2. **把“估值假设”当成一等公民**
   估值不是结果，是“假设集合”。假设要能追踪变化。

3. **current 永远可用，runs 永远可追溯**
   你想“以公司为主体、不太按时间”，用 current 解决；
   你想“可复盘可审计”，用 runs 解决。

4. **Skill 输出尽量结构化**（yaml/json/parquet/csv）
   报告是给人看的；结构化是给系统迭代和编排用的。

---
下面我按你提的两点，把体系“工程化”得更明确：**Skill 不隐式调用 Skill**，但能通过“缺失信息触发协议”把缺口显式抛给编排器（总 Skill / 脚本），由编排器决定下一步跑哪个 Skill 来补齐，然后再回到原 Skill 继续推进。

我会给出：

1. **Skill 关联/触发机制（协议）**：怎么做到“可触发补信息”但不互相调用
2. **统一目录与统一数据契约（Artifacts/Schema）**：每个 Skill 该读什么、写什么
3. **每个 Skill 的详细规格**：职责边界、输入输出、依赖、内部步骤、缺失信息触发策略、落盘结构、刷新/缓存策略
4. **编排器建议算法**：怎么利用“needs”把流程跑通，避免循环/黑箱

> 你说“细节以后慢慢补充”，完全 OK。我这里把“框架与接口”先一次性搭好——后面你补规则不会推翻结构。

---

## 1) Skill 关联/触发机制：显式依赖 + needs（不隐式调用）

### 1.1 核心原则

* **每个 Skill 是一个“可重复运行的模块”**：输入（参数 + 规定的文件）→ 输出（规定的文件 + meta/result）
* Skill **不去调用另一个 Skill**（避免隐式耦合、难追踪）
* 但 Skill 可以在运行时发现缺口，然后：

  * **要么降级输出（partial）**：把缺口写进 `questions.jsonl` / `meta.yaml`
  * **要么阻塞（blocked）**：写出一个明确的 `needs.yaml`，告诉编排器“缺什么、谁能产出、优先级、原因”
* 编排器读取 `needs.yaml`，决定下一步跑哪些 Skill 补齐，再回来重跑原 Skill（或继续下一个）

### 1.2 统一的“Skill Result 协议”（强烈建议）

每个 Skill 跑完，都会在本次 run 目录落一个 `result.yaml`（或把同样字段写进 `meta.yaml`，二选一即可）。建议结构：

```yaml
skill: normalize-financials
ticker: ABC
run_id: 20260105_143012
as_of: 2026-01-04
timezone: America/New_York

status: ok        # ok | partial | blocked | skipped | error

requires:
  hard:
    - current/financials_xbrl.parquet
  soft:
    - current/filings_index.yaml

missing: []       # hard missing 才会 blocked
warnings:
  - "缺少最近一季现金流表XBRL标签，已用替代tag映射"

outputs:
  - current/financials_normalized.parquet
  - current/normalization_adjustments.yaml

next_suggested_skills:   # 不是强制，只是建议
  - financial-redflags
```

当 **blocked** 时，必须额外写 `needs.yaml`（或在 result/meta 内嵌 needs 字段）：

```yaml
blocked_by:
  - artifact: current/financials_xbrl.parquet
    producer_skill: extract-xbrl-timeseries
    reason: "估值与归一化需要XBRL时间序列"

suggested_plan:
  - extract-xbrl-timeseries
  - normalize-financials
priority: high
```

> 这套协议就是你要的“Skill 可关联、可触发补信息”，同时保持 Skill 之间不互相调用。

### 1.3 Hard vs Soft 依赖

* **Hard 依赖缺失 → blocked**（不允许胡算）
* **Soft 依赖缺失 → partial**（允许先输出一个版本，但要显式降级：降低 confidence、写 questions、标注假设）

---

## 2) 统一数据契约：核心 artifacts 的最小 schema（后续你扩展不破坏）

你后续会不断加字段，所以我只定“最小可用字段 + 扩展方式”。

### 2.1 company.yaml（身份与静态信息）

```yaml
ticker: ABC
company_name: "ABC Inc."
cik: "0000123456"
exchange: "NYSE"
sic: "1234"
fiscal_year_end: "12-31"
currency: "USD"
source:
  sec: true
  manual_notes: ""
```

### 2.2 market_snapshot.yaml（估值必需口径）

```yaml
as_of: 2026-01-04
price: 12.34
shares_out: 100000000
market_cap: 1234000000
enterprise_value: 1500000000   # 若能算
net_debt: 266000000            # 若能算
source: "trading_mcp.get_fundamental_stock_metrics"
```

### 2.3 filings_index.yaml（你抓了什么、在哪里）

```yaml
as_of: 2026-01-04
filings:
  - form: "10-K"
    filed_at: "2025-02-20"
    period_end: "2024-12-31"
    accession: "0000123456-25-000123"
    has_xbrl: true
    local_dir: "raw/sec/0000123456-25-000123/"
  - form: "10-Q"
    filed_at: "2025-11-05"
    period_end: "2025-09-30"
    accession: "..."
```

### 2.4 financials_xbrl.parquet（建议“长表”为主，灵活）

最小字段建议：

* `period_end`（日期）
* `form`（10-K/10-Q）
* `statement`（IS/BS/CF）
* `tag`（XBRL tag）
* `value`
* `unit`
* `accession`

（后面你再加 `segment`, `dimensions`, `is_restated` 等）

### 2.5 financials_normalized.parquet（你的“真实财务”底座）

建议至少产出以下“核心宽表列”（每期一行）：

* `revenue`
* `gross_profit`
* `operating_income`
* `net_income`
* `cfo`
* `capex`
* `fcf`
* `sbc`
* `lease_adjustment`（可选）
* `one_off_items`（可选）

### 2.6 normalization_adjustments.yaml（所有调整可追溯）

```yaml
policy_version: "v0.1"
adjustments:
  - period_end: "2025-12-31"
    item: "SBC"
    action: "add_back_partial"
    amount: 12300000
    rationale: "用于Owner Earnings口径"
    source_ref: "raw/sec/.../10k.html#note_sbc"
```

### 2.7 questions.jsonl（未解谜团：可持续迭代的关键）

一行一个问题：

```json
{"id":"Q_20260105_001","created_at":"2026-01-05","skill":"moat-analysis","priority":"high","question":"客户集中度是否来自单一合同？续约条款是什么？","status":"open","related_artifacts":["current/profile.yaml"],"notes":""}
```

### 2.8 evidence.jsonl（结论—证据对齐账本）

一行一个“结论”：

```json
{"id":"E_20260105_010","created_at":"2026-01-05","skill":"financial-redflags","claim":"应收增长显著快于收入，但主要来自并购并表","confidence":0.6,"sources":[{"type":"sec","accession":"...","path":"raw/sec/.../10k.html","anchor":"MD&A"},{"type":"data","path":"current/financials_normalized.parquet","fields":["revenue","ar"]}]}
```

---

## 3) Skill 规格：逐个把边界、依赖、步骤、触发策略写清楚

下面每个 Skill 我都按同一模板描述：

* **职责边界**
* **输入参数**
* **Hard/Soft 依赖（读哪些文件）**
* **输出（写哪些文件）**
* **内部步骤（框架级）**
* **缺失信息触发策略（blocked/partial + needs）**
* **刷新/缓存策略（框架级）**

---

### Layer 0：初始化与市场口径

## Skill: `init-company-dossier`

**职责边界**

* 创建公司目录结构
* 建立 `company.yaml`（至少 ticker + cik 尝试解析）
* 初始化 `current/questions.jsonl`、`current/evidence.jsonl` 空文件（如果不存在）

**输入参数**

* `ticker`（必需）
* `as_of`（默认当天）
* `force_refresh=false`

**依赖**

* Hard：无
* Soft：无（但可尝试从 SEC/网页解析 company name、CIK）

**输出**

* `company/{ticker}/company.yaml`
* `company/{ticker}/latest.json`（初始）
* `company/{ticker}/current/questions.jsonl`
* `company/{ticker}/current/evidence.jsonl`
* `runs/{run_id}/meta.yaml` + `result.yaml`

**内部步骤**

1. 创建目录树：`current/ raw/ runs/ logs/`
2. 尝试解析 CIK/公司名（可以先空着，后续补）
3. 写 company.yaml（可逐步完善）
4. 记录 meta/result

**缺失信息触发策略**

* 若无法解析 CIK：`status: partial`，并在 `questions.jsonl` 写入 `“CIK 未解析，需 resolve-company-identity”`
* 同时在 `result.yaml` 的 `next_suggested_skills` 建议 `resolve-company-identity`

**刷新策略**

* 不覆盖用户手工补充字段；只补缺失字段（merge）

---

## Skill: `resolve-company-identity`（建议新增，小而关键）

> 这是为了让后续 SEC 拉取稳定，不把“CIK 不知道”散落到各处。

**职责边界**

* ticker → CIK / 公司法定名称 / 交易所 / SIC 等
* 把身份信息补齐到 `company.yaml`

**输入参数**

* `ticker`, `as_of`, `force_refresh=false`

**依赖**

* Hard：`company.yaml`（若无则 blocked，建议先 init）
* Soft：无

**输出**

* 更新 `company.yaml`（补齐 cik/company_name 等）
* 记录 evidence（来源：SEC 或可信网页）

**缺失触发策略**

* 如果 SEC/网页都无法找到：`blocked` 并在 needs 里写 `manual_intervention`（这类极少，但要有出口）

**刷新策略**

* 身份信息通常稳定；默认不刷新，除非 force_refresh

---

## Skill: `update-market-snapshot`

**职责边界**

* 刷新市场口径：price、shares_out、market_cap、EV（能算就算）
* 这是估值层的“行情底座”，独立成 Skill 便于日更

**输入参数**

* `ticker`, `as_of`, `force_refresh=false`

**依赖**

* Hard：`company.yaml`（至少 ticker）
* Soft：无

**输出**

* `current/market_snapshot.yaml`

**内部步骤**

1. 调用 `trading_mcp.get_fundamental_stock_metrics` 获取价格、股本、市值等
2. 若能从 metrics 里拿到债务/现金则计算 EV；拿不到则留空并写 warnings
3. 写 market_snapshot.yaml + meta/result

**缺失触发策略**

* 若 trading_mcp 返回缺字段：`status: partial`，并写入 questions：“EV 口径缺 debt/cash”

**刷新策略**

* 默认：若 `as_of` 相同且文件存在 → `skipped`
* 建议日更（as_of 每天）

---

### Layer 1：Filings / 新闻 / 论文采集

## Skill: `fetch-sec-filings`

**职责边界**

* 拉取指定 forms 的 filings 原文（html/txt/pdf + xbrl）
* 建立 `filings_index.yaml`
* 只做“采集与索引”，不做财务分析

**输入参数**

* `ticker`
* `forms`（默认 `[10-K, 10-Q, 8-K, DEF 14A]`）
* `lookback_years=10`
* `as_of`
* `force_refresh=false`

**依赖**

* Hard：`company.yaml`（最好有 cik；至少 ticker）
* Soft：无

**输出**

* `raw/sec/{accession}/...`（原文 + xbrl）
* `current/filings_index.yaml`

**内部步骤（框架）**

1. 确认 cik：若 company.yaml 无 cik → 尝试轻量解析（或直接 blocked + needs resolve-company-identity）
2. 拉取 filings 列表，按 form 与 lookback 过滤
3. 对每个 accession：

   * 下载原文到 `raw/sec/{accession}/`
   * 若有 XBRL，保存到同目录
4. 生成/更新 `filings_index.yaml`
5. meta/result + registry

**缺失信息触发策略**

* 无 cik → `blocked`，needs 指向 `resolve-company-identity`
* 某些 filing 没有 xbrl（常见于老文件/特定表单）：

  * `status: partial`
  * `filings_index.yaml` 标注 `has_xbrl: false`
  * 后续 `extract-xbrl-timeseries` 对缺口做降级或触发“文本解析”（可后续新增 Skill）

**刷新策略**

* 10-Q/8-K：可设 staleness 7 天
* 10-K/DEF14A：staleness 90~365 天
* `force_refresh` 时重拉

---

## Skill: `collect-news-events`

**职责边界**

* 收集新闻 + 去重 + 初步标签（事件类型/正负面/时间线）
* 可选：抓取少量关键网页正文快照，便于证据留存
* 不做深度观点，只做“可用素材池 + 结构化事件线”

**输入参数**

* `ticker`
* `lookback_days=180`
* `as_of`
* `force_refresh=false`

**依赖**

* Hard：`company.yaml`
* Soft：`current/profile.yaml`（有公司名/产品名可提高召回）

**输出**

* `raw/news/news.jsonl`（原始条目）
* `raw/web/...`（关键正文快照，可选）
* `current/news_digest.yaml`（事件线：时间—事件—标签—来源）

**内部步骤**

1. 组合查询词：ticker + company_name（若有）+ 核心产品词（若 profile 有）
2. 用 gdelt/rss 拉取候选
3. 去重（按 URL、标题hash、发布时间窗口）
4. 事件标签（粗粒度即可）：诉讼/监管/产品/并购/财务/事故/裁员/融资等
5. 选取 Top-K 影响大的新闻抓正文快照（可选）
6. 写 digest + evidence（至少对重大事件）

**缺失信息触发策略**

* 若 company_name 缺失导致召回差：`partial` + needs `resolve-company-identity` 或 `business-profile-and-segments`
* 若新闻源不可用：`partial`，并写 questions：“news 数据源不可用，本次跳过”

**刷新策略**

* staleness：1~3 天（新闻时效强）

---

## Skill: `collect-technical-papers`（按需）

**职责边界**

* 为技术/医药/材料等公司建立论文/技术资料池
* 目的：支撑护城河/成长性/产品有效性证据链
* 不做估值，只做资料整理与主题摘要

**输入参数**

* `ticker`
* `keywords`（可空：则从 profile 抽取）
* `as_of`
* `force_refresh=false`

**依赖**

* Hard：`company.yaml`
* Soft：`current/profile.yaml`（产品/技术关键词）

**输出**

* `raw/papers/papers.jsonl`
* `current/papers_digest.yaml`
* `questions.jsonl`（把不确定点转成可研究问题）

**缺失触发策略**

* keywords 缺失且 profile 不存在 → `blocked` needs `business-profile-and-segments`

**刷新策略**

* staleness：30~90 天（论文不需要日更）

---

### Layer 2：XBRL 抽取与真实财务（核心底座）

## Skill: `extract-xbrl-timeseries`

**职责边界**

* 从 filings 的 XBRL 抽取时间序列事实
* 目标是得到可计算的基础财务数据集（先不做归一化）

**输入参数**

* `ticker`
* `as_of`
* `force_refresh=false`

**依赖**

* Hard：`current/filings_index.yaml`（知道有哪些 accession）
* Soft：`company.yaml`

**输出**

* `current/financials_xbrl.parquet`
* `current/xbrl_mapping.yaml`（tag 映射/口径注释，先很简单也行）

**内部步骤**

1. 读取 filings_index，挑选最近 N 年的 10-K/10-Q（优先有 xbrl 的）
2. 抽取核心 tag 集（先做一份默认 tag 列表，后续你慢慢扩）
3. 合并成时间序列（按 period_end 对齐）
4. 生成 mapping（记录每个核心字段使用了哪些 tags，fallback 是什么）
5. 输出并记录 evidence：accession + tag

**缺失信息触发策略**

* filings_index 不存在 → `blocked` needs `fetch-sec-filings`
* 大量缺 tag/对不齐 → `partial`

  * mapping 里标注 fallback
  * questions 写：哪些字段缺失需要手工映射或文本解析

**刷新策略**

* 有新 filing（filings_index 更新）则更新；否则 `skipped`

---

## Skill: `normalize-financials`

**职责边界**

* 把会计口径转换为“经济口径/可估值口径”
* 输出 normalized 三表与 adjustments 账本
* 这里不需要一次做完“格雷厄姆一本书”的细节；先把“调整框架 + 可扩展策略位”立住

**输入参数**

* `ticker`
* `as_of`
* `policy_version="v0.1"`（你的调整策略版本号）
* `force_refresh=false`

**依赖**

* Hard：`current/financials_xbrl.parquet`
* Soft：`current/filings_index.yaml`（用于定位脚注证据）
* Soft：`raw/sec/...`（要提取脚注时）

**输出**

* `current/financials_normalized.parquet`
* `current/normalization_adjustments.yaml`

**内部步骤（框架化）**

1. 读取 xbrl 时间序列，构建“基础宽表”（revenue、op income、cfo、capex…）
2. 应用调整策略（policy）——先定义成“可插拔的规则列表”：

   * 一次性项目（restructuring、impairment、litigation 等）归类到 `one_off_items`
   * SBC 单独列出（决定是否 add-back 由 policy 控制）
   * 租赁（可选）：把 lease payment 近似拆成利息/折旧（先留策略位）
   * 并购相关费用（可选）
3. 生成关键估值口径：

   * `fcf = cfo - capex`（最朴素版本）
   * `owner_earnings`（后续你补）
4. 写出 adjustments.yaml：每条调整“调了什么、多少、为什么、证据在哪”
5. 产出 normalized 数据

**缺失信息触发策略**

* financials_xbrl 不存在 → `blocked` needs `extract-xbrl-timeseries`
* 某字段缺失（例如 capex tag 取不到）：

  * `partial`，并写 warnings + questions（“capex 缺失导致 fcf 不可靠”）
  * valuation-engine 会据此降低 confidence 或阻塞（你可定义：FCF 必须可靠才算 DCF）

**刷新策略**

* policy_version 变化 → 强制重跑
* filings 有更新 → 重跑
* 否则 `skipped`

---

## Skill: `financial-redflags`

**职责边界**

* 财务反作弊/红旗扫描与解释假设（不是打分，而是“疑点→证据→解释→待验证”）
* 直接服务于：**估值折扣（风险）**与“价值陷阱识别”

**输入参数**

* `ticker`, `as_of`, `force_refresh=false`

**依赖**

* Hard：`current/financials_normalized.parquet`
* Soft：`current/financials_xbrl.parquet`
* Soft：`raw/sec/`（审计意见、重述、风险因素）

**输出**

* `current/redflags.yaml`（红旗清单）
* 向 `current/questions.jsonl` 追加若干问题
* 向 `current/evidence.jsonl` 写关键红旗证据

**内部步骤（框架）**

1. 生成一组“红旗候选指标”（先少量、可扩展）：

   * 应收/收入增速背离、DSO 异常
   * 存货周转恶化与减值风险
   * 利润与 CFO 长期背离
   * 频繁一次性项目“常态化”
   * 股本稀释/SBC 异常
2. 对每个红旗：

   * 触发阈值（先粗略）
   * 可能解释（业务/并购/会计政策）
   * 证据指针（哪个 filing/哪个表）
   * 需要验证的问题（写 questions）
3. 输出 redflags.yaml（带 severity、confidence）

**缺失信息触发策略**

* normalized 不存在 → `blocked` needs `normalize-financials`
* 缺少脚注文本证据 → `partial`，记录“需要补 filings 原文抓取/定位”

**刷新策略**

* 财报更新时季度更新；否则 `skipped`

---

### Layer 3：商业模式/护城河/成长/管理层

## Skill: `business-profile-and-segments`

**职责边界**

* 生成公司画像：业务、分部、收入来源、客户/渠道、地理分布、成本结构线索
* 输出结构化 profile，供 moat/growth/mispricing 用
* 注意：不要求对所有行业都很细，先“通用骨架 + 行业可插拔字段”

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/filings_index.yaml`（至少有 10-K/10-Q）
* Soft：`raw/sec/...`（10-K Business/MD&A）
* Soft：`current/news_digest.yaml`（用于补近期变化）

**输出**

* `current/profile.yaml`
* `current/competitors.yaml`（可以先很粗）
* evidence/questions

**内部步骤**

1. 从 10-K/10-Q 抽取：主营、分部、客户集中度、供应链、季节性、关键风险
2. 形成 profile.yaml 的固定字段（先不深）：

   * business_summary
   * segments（列表：名称、收入占比（若能）、毛利线索（若能））
   * customers（集中度、合同性质线索）
   * distribution（直销/渠道/订阅…）
   * cost_structure_notes
3. 竞争对手：从 filings + web 粗提（先列表）
4. 输出并写 evidence（引用 filing 部分）

**缺失信息触发策略**

* filings_index 不存在 → `blocked` needs `fetch-sec-filings`
* raw/sec 缺 10-K → `partial`（用 10-Q 先凑骨架），并在 needs 建议补 10-K

**刷新策略**

* 年更为主；遇到业务大变动/并购则更新

---

## Skill: `moat-analysis`

**职责边界**

* 护城河证据矩阵：来源—证据—可持续性—风险
* 不追求一次写完；核心是“结构化 + 可补证据”

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/profile.yaml`
* Soft：`current/papers_digest.yaml`
* Soft：`current/news_digest.yaml`

**输出**

* `current/moat.yaml`
* `current/moat_evidence_matrix.csv`（可选但很有用）
* questions/evidence

**内部步骤（框架）**

1. 按护城河类型建骨架：

   * 定价权、切换成本、网络效应、规模成本、品牌、渠道/分销、监管牌照、数据/模型优势等
2. 每类护城河填：

   * 现有证据（来自 filings、客户结构、毛利稳定性、续费率线索、行业资料）
   * 反证/风险（竞争/替代/监管变化）
   * 证据缺口 → 写 questions
3. 产出 moat 强度（0..5）与 confidence（0..1）

**缺失信息触发策略**

* profile 缺失 → `blocked` needs `business-profile-and-segments`
* 技术型公司缺 papers → `partial` + needs `collect-technical-papers`（如果判断“技术证据是关键”）

**刷新策略**

* 半年/年更；行业剧变时更新

---

## Skill: `growth-engine`

**职责边界**

* 拆解成长：来自哪里、持续性如何、确定性折扣
* 产出增长 KPI 框架（便于你长期跟踪）

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/profile.yaml`
* Soft：`current/news_digest.yaml`
* Soft：`current/moat.yaml`

**输出**

* `current/growth.yaml`
* `current/growth_kpi.yaml`（或 csv）
* questions/evidence

**内部步骤**

1. 定义增长拆解维度：

   * 市场增长（行业/宏观）
   * 份额提升（竞争力/渠道）
   * 新产品/新地区
   * 提价（定价权）
2. 明确关键 KPI（行业因子，先占位）：

   * 订单/积压、装机量、门店数、ARPU、续费率、渗透率等
3. 给出“增长确定性折扣”的理由（证据不足要写 questions）

**缺失触发策略**

* profile 缺失 → blocked
* moat 缺失不阻塞：`partial`，但 growth 的“提价/份额”部分降级

**刷新策略**

* 季度更新 KPI；叙事半年更新

---

## Skill: `management-and-capital-allocation`

**职责边界**

* 管理层与资本配置：不是主观评价，而是“可验证的轨迹”
* 输出回购/分红/M&A/SBC 等历史线，支持估值与风险折扣

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/filings_index.yaml`
* Soft：`raw/sec/...`（DEF14A、10-K）
* Soft：`current/financials_normalized.parquet`

**输出**

* `current/management.yaml`
* `current/allocation_history.csv`
* evidence/questions

**内部步骤**

1. 从 DEF14A 抽取：高管、薪酬结构、激励指标（占位即可）
2. 从 10-K/10-Q 抽取：回购授权、分红政策、债务与资本结构变化
3. 生成 allocation_history.csv（时间线）
4. 形成 management.yaml（资本配置纪律的证据）

**缺失触发策略**

* 缺 DEF14A 不阻塞：`partial`，needs 建议补 DEF14A
* financials 缺失：仍可跑轨迹，但对“价值创造”分析降级

**刷新策略**

* 年更 + 大事件更新

---

### Layer 4：误定价/催化剂/风险/估值

## Skill: `mispricing-hypotheses`

**职责边界**

* 把“市场担心A、我认为B”写成可检验命题
* 输出：假说清单 + 证伪路径 + 关键观测指标 + 证据缺口

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/profile.yaml`
* Soft：`current/news_digest.yaml`
* Soft：`current/redflags.yaml`
* Soft：`current/growth.yaml`, `current/moat.yaml`

**输出**

* `current/mispricing.yaml`
* questions/evidence

**内部步骤**

1. 形成“市场叙事候选”：来自新闻/风险因素/财报波动
2. 对每条叙事写反命题：

   * 可修复 vs 不可逆
   * 局部 vs 结构性
3. 写证伪路径（最关键）：

   * 哪些指标/事件会证明你错
   * 哪些证据缺失需要补
4. 输出假说优先级（你后续研究资源分配依据）

**缺失触发策略**

* profile 缺失 → blocked
* news 缺失不阻塞，但会显著降级：`partial` + needs `collect-news-events`

**刷新策略**

* 月更/事件驱动更新

---

## Skill: `catalysts-and-pathways`

**职责边界**

* 把“价值实现”从愿望变成路径：触发条件、时间窗、失败方式、监控指标

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/mispricing.yaml`（建议 hard，因为催化剂要服务假说）
* Soft：`current/news_digest.yaml`
* Soft：`current/management.yaml`

**输出**

* `current/catalysts.yaml`
* evidence/questions

**内部步骤**

1. 对每条 mispricing 假说列出可能的兑现路径：

   * 经营改善、周期回归、资产处置、并购私有化、监管落地等
2. 给出“可观察的里程碑”与“失败判据”
3. 输出概率/时间窗（可先粗略占位）

**缺失触发策略**

* mispricing 缺失 → `blocked` needs `mispricing-hypotheses`
* news 缺失 → `partial`，但路径可信度降级

**刷新策略**

* 事件驱动更新

---

## Skill: `risk-register`

**职责边界**

* 风险清单可操作化：触发条件、影响链路、监控指标、对估值的折扣落点
* 与 redflags 区分：redflags 偏“财务可信度”，risk-register 偏“全局风险资产负债表”

**输入参数**

* `ticker`, `as_of`

**依赖**

* Hard：`current/profile.yaml`
* Soft：`current/redflags.yaml`
* Soft：`current/news_digest.yaml`
* Soft：`raw/sec/...`（风险因素）

**输出**

* `current/risk_register.yaml`
* evidence/questions

**内部步骤**

1. 风险分类骨架：

   * 业务/竞争、财务/流动性、治理/激励、监管/诉讼、周期/宏观、技术/产品
2. 每条风险写：

   * 触发条件
   * 影响路径（影响利润率/现金流/资本成本/终值）
   * 监控指标
   * 在 valuation 里对应哪条折扣（例如提高折现率、降低终值倍数、降低增长等）
3. 输出 top risks

**缺失触发策略**

* profile 缺失 → blocked
* filings 原文缺失 → `partial`（风险因素无法引用），needs 建议补 `fetch-sec-filings`

**刷新策略**

* 季度更新

---

## Skill: `valuation-engine`

**职责边界**

* 产出可复算的估值区间（Bear/Base/Bull）
* 同时允许两条腿：Operating（经营）与 Exit/Asset（退出/并购/清算），但写在同一 valuation.yaml
* **强制把关键假设显式化**（你最在意的）

**输入参数**

* `ticker`, `as_of`
* `model_type`（可选：`epv|dcf|multiple|hybrid`，默认 hybrid）
* `force_refresh=false`

**依赖**

* Hard：

  * `current/market_snapshot.yaml`
  * `current/financials_normalized.parquet`
* Soft：

  * `current/growth.yaml`
  * `current/moat.yaml`
  * `current/risk_register.yaml`
  * `current/redflags.yaml`
  * `trading_mcp.compare_stock_valuations`（同业对比辅助）

**输出**

* `current/valuation.yaml`
* `current/valuation_model.csv`（或 parquet）
* `current/value_state.yaml`（给全局汇总用，最关键！）
* evidence/questions（假设来源）

**内部步骤（框架）**

1. 读取 market_snapshot 与 normalized 财务，计算当前基线：

   * 收入、利润率、FCF、资本结构、ROIC 线索等
2. 构建三情景假设表（先少量字段）：

   * 增长（来自 growth，没有就用保守默认并标注）
   * 利润率回归/改善路径
   * 再投资率/capex 强度
   * 折现率（与风险挂钩）
   * 终值（倍数或长期增长）
3. 输出估值区间：

   * Operating IV（EPV/DCF）
   * Exit/Asset IV（先占位：SOTP/净资产折扣/并购倍数）
4. 生成 `value_state.yaml`：

   * IV range、MOS、confidence、关键风险、开放问题数量

**缺失触发策略（非常关键）**

* market_snapshot 缺失 → `blocked` needs `update-market-snapshot`
* normalized 缺失 → `blocked` needs `normalize-financials`（而 normalize-financials 会反向 needs xbrl 等）
* growth/moat 缺失不阻塞，但必须：

  * `status: partial`
  * `value_state.confidence` 下调
  * 在 valuation.yaml 明确“哪些假设用默认值/保守值”

**刷新策略**

* market_snapshot 是日级口径：as_of 变化则重算
* 财报更新/normalized 更新则重算
* 否则 `skipped`

---

### Layer 5：综合输出

## Skill: `synthesize-final-report`

**职责边界**

* 把 current/ 的结构化产物编成“研究员可读”的 thesis/report
* 这里不要求文采，强调：**假设—证据—结论一致**、能快速定位证据与开放问题

**输入参数**

* `ticker`, `as_of`
* `format=md`（可选 pdf 以后再加）

**依赖**

* Hard：`current/value_state.yaml`、`current/valuation.yaml`
* Soft：`profile/moat/growth/management/news/mispricing/catalysts/risk/redflags`

**输出**

* `current/thesis.md`
* 可选：`current/final_report.md`
* evidence（报告本身不一定写 evidence，但要引用 evidence id）

**内部步骤（框架）**

1. 生成固定章节：

   * 一句话结论（MOS + why）
   * 业务概览（profile）
   * 误定价假说（mispricing）
   * 价值实现路径（catalysts）
   * 估值（估值区间 + 最敏感假设）
   * 风险与反证（risk + redflags）
   * 开放问题（questions）
2. 把每个关键结论后面挂上“证据指针”（链接到 raw 路径或 evidence id）
3. 输出 md

**缺失触发策略**

* value_state/valuation 缺失 → blocked needs `valuation-engine`
* 其他缺失 → `partial`，报告照样出，但空章节要显式标注“待补”

**刷新策略**

* 估值更新或假说更新时重写报告

---

## 4) 编排器（总 Skill / 脚本）如何利用 needs 跑通闭环

你想要的是：**缺啥补啥、补完继续**。推荐一个简单稳健的调度逻辑：

### 4.1 调度输入

* 你的股票池：
  `/home/help/mcp/work/stock_pools/us_value_mispricing_v1/runs/20260104_131512/pool.csv`

### 4.2 调度算法（框架）

对每个 ticker：

1. 先跑一个"计划列表"（按 Layer 顺序）：

   * **L0**: `init-company-dossier` → `resolve-company-identity` → `update-market-snapshot`
   * **L1**: `fetch-sec-filings` → `collect-news-events` → `collect-technical-papers`
   * **L2**: `extract-xbrl-timeseries` → `normalize-financials` → `financial-redflags`
   * **L3**: `business-profile-and-segments` → `moat-analysis` → `growth-engine` → `management-and-capital-allocation`
   * **L4**: `mispricing-hypotheses` → `catalysts-and-pathways` → `risk-register` → `valuation-engine`
   * **L5**: `synthesize-final-report`
2. 每跑一个 skill，读取它的 `result.yaml`：

   * `ok/partial/skipped`：继续
   * `blocked`：读取 `needs.yaml`，把 `producer_skill` 插到队列**前面**（或优先级更高的位置），然后继续跑
   * `error`：记录并跳过或重试（你可设重试次数）
3. 做一个“防循环”机制：

   * 同一 skill 同一 ticker 同一 as_of 连续 blocked 超过 N 次 → 标记 `manual_required`
4. 最后把 `current/value_state.yaml` 汇总到全局 `value_summary.csv`

### 4.3 为什么这套比"技能互相调用"更好

* 依赖关系全部显式化（needs）
* 日后你加新 Skill 不会引起隐式调用链爆炸
* 每次失败点可定位（blocked by 什么 artifact）
* 可审计、可复盘、可并行（以后）

---

## 5) SKILL.md 写作模板（你后续照抄就能批量生成）

你说“写不写 SKILL.md 都行”，但我建议至少统一骨架。模板如下（每个 Skill 都能套）：

```md
---
name: <skill-name>
description: <一句话：做什么，为估值服务的哪一层>
inputs:
  - ticker: string (required)
  - as_of: YYYY-MM-DD (default today)
  - force_refresh: bool (default false)
requires:
  hard:
    - <artifact-path>
  soft:
    - <artifact-path>
provides:
  - <artifact-path>
status_codes:
  - ok, partial, blocked, skipped, error
missing_info_strategy:
  - if hard missing -> write needs.yaml and status=blocked
  - if soft missing -> continue, write questions, status=partial
---

# Workflow
1. <step>
2. <step>
...

# Output schemas
- <file>: <minimal schema bullets>

# Refresh / caching
- <staleness rules>
```

---

## 6) 落地结论

### Skill 关联与触发

✅ 用 **result/status + needs.yaml** 做"显式触发"，编排器来调度补信息。

* 不会出现 Skill 隐式调用 Skill 的黑箱
* 缺信息时不会硬算
* 依赖链可追溯、可审计、可维护

### 边界/依赖/步骤清晰

✅ 每个 Skill 的"接口与行为"框架已定：

* 后续只需逐步填充"规则细节"（会计调整策略、行业 profile 模板、护城河证据类型等）
* 不需要推翻目录或流程

> MVP 实现路径见前文"六、先做 Demo 的最小可用集合"。



''''''
我需要修正整个skills的方向因为我的思路慢慢清晰，和上面已经有所不同，我提出几个建议，可能修改比较大。 你要根据我的想法去优化，从而更有扩展性，更方便后续的优化:
1.* init-company-dossier * resolve-company-identity 甚至update-market-snapshot这几个部分我觉得可以合并成一个skill，就是以后执行就先看看有没有已经存在的路径或者文件，如果有了，就看是不是最新的要不要补全，如果是最新的不需要补，就跳过，不执行就好，简而言之就是查漏补缺，其他的取数，数据采集，都遵循这个逻辑就好。
2.* update-market-snapshot 这个可以不动，最好再加一个股票数量(Shares Outstanding)，如果Shares Float有的话，方便后续以后算每股收益。其实我认为 * init-company-dossier * resolve-company-identity * update-market-snapshot都可以合并成一个，用查漏补缺的方式去提升效率就好
3.**Layer 1：事实采集** * fetch-sec-filings * collect-news-events * collect-technical-papers（按需，医药/科技类） 这个我认为没有问题，数据采集就是我要后面慢慢验证，慢慢完善就好。当然我认为数据采集可以变成一个skill没必要写这么多个。同样，查漏补缺方式提升效率也不错。已经有的就不下载，没有的跟新，下载，去提升效率。
4. **Layer 2：财务底座** * extract-xbrl-timeseries * normalize-financials * financial-redflags **Layer 3：商业质量** * business-profile-and-segments * moat-analysis * growth-engine * management-and-capital-allocation **Layer 4：估值决策** * mispricing-hypotheses * catalysts-and-pathways * risk-register * valuation-engine **Layer 5：综合输出** * synthesize-final-report 这些我认为需要修改，整体上这些skills多且混乱。
4.1 具体修改的指导思想：
    后续就按照我这个思想去丰富，现在分析的流派众多，纷繁复杂。我觉得估值的核心还是没有变，当然这是我个人的观点，现在的这个框架也会带有我的个人色彩，这我觉得没有关系。 我觉得估值的核心观点就是 股价=每股利润*质量系数  或者 估值=利润*质量系数
    所以一切的核心就是围绕这两个因素去展开，也就是怎么看利润，怎么看质量系数。
    
    首先 我认为利润主要看的 未来的持久（这个很难）的经济利润（或者是Owner Earnings，这要和财务利润分开看），质量系数我觉得是对这个未来利润的确定性的一个系数，当然有人认为是公司的成长或者质量，我觉得都有道理，这些其实都指向了未来稳定的成长的利润，尤其是如果利润本事就是包含了未来的考虑因素的时候。

    然后就是财务报表、产品信息、分析师、护城河、公司文化等等，这些都是对成长性，长久利润，做预判的依据，最后都要归因到这个因素的。也就是所有的分析也好，都要围绕着利润，未来利润，以及未来利润的确定性这个方向来。从而做出投资判断。比如资产负债表，我反映的时资产的来源，也就是我看到资产负债表的状况看得到以前经营的实际情况，同时，我的资产的roic，我资产的置换成本，我持有的各类资产的盈利潜力，现金应对风险，拥有的潜在资源进行投资的情况，都能反映到我公司未来的利润和未来的利润的稳定性。

4.2 具体我的改进内容。 
    4.2.1 目录结构与落盘策略也需要改进，xbrl_mapping.yaml # tag映射与口径说明 这个是不需要的。后续我会说明，其他的比如allocation_history.csv，mispricing.yaml等等，可以更具skills的变化而相应变化
    4.2.2 **Layer 2：财务底座** * extract-xbrl-timeseries * normalize-financials * financial-redflags 这三个 我觉得extract-xbrl-timeseries可以留着（但要优化，我等下跟你说怎么回事），另外两个写的都有问题，normalize-financials本质方向是好的，就是说你要把会计利润，变成Owner Earnings，包括去调整其他的三大表，能够更好反映投资者利益和实际经营情况。
    但是tag 集是非常愚蠢的，financial-redflags也非常愚蠢，因为你做这些有一个假设，就是有一个大而全的框架能涵盖所有公司所有市场，还能一直保持不变，这个假设不成立。但是如果你所有的都围绕着未来持续的利润和成长性的确定性这两个点的话，其实不同的tag并不是重要的。
    所以我建议这个部分，可以有两个skills。extract-xbrl-timeseries可以保持，但是要求我给你的要求，就是，你能把几百页的财务报表，进行整理，能够非常详细的把每一个财务数据的来源能够溯源，方便后续的整理和分析。  extract-xbrl-timeseries能产出的是三大表的在时间窗口内的情况（最好十年），并且能够更好地，更清晰的能够获取各个财务信息。 总之，我能通过你的信息，很快能够给出三个非常复杂的图标，一个是利润表的树状图，最左边或者最底下是Comprehensive income（综合收益） = Net income + OCI，然后Net income 继续能够分解成各个项目，甚至分公司，子公司，等等组成这个Comprehensive income你都算进去
    现金流量表也是一样，最底下都是Net increase (decrease) in cash and cash equivalents，上面是变化，每个季度或者年报你都能很快做到。 资产负债表也是，能够第一是知道资产、负债的组成，还能知道各个期间变化的关系，你能很快做出这样的变化图。你不一定要做出这个图，但是我指的是这个作图的能力，代表后续你能很方便反映财务信息。

    后续的一个skill是 ：三表重铸与核心指标（经济报表层）
    也就是你根据你extract-xbrl-timeseries分解出来的全部的经济数据，重构一个你觉得能够反映owener enrings的三大表，后续的预测的利润也是这个经济利润。
    operating vs financing 拆分
    NOPAT、Invested Capital、ROIC、FCF、Owner Earnings（含maintenance capex估计）
    
    4.2.3当你重构三大表之后，先做一个分析过去和当前财务状况分析的skill，也就是基于当前的财务状况，有哪些未来利润是清晰的，有哪些未来利润是不清楚的。 还有就是公司的盈利风险和盈利质量分析，这个核心也就是从当前的情况，预测企业未来3-5年的经济利润的风险。 可以参考 下面的方法去判断风险，但是核心是通过当前的情况，去做出初步的预测未来净利润*质量系数的预测：
    Sloan式应计质量、现金流对账
    SSRN
    Piotroski F-score（更偏“价值股里分强弱”）
    Beneish M-score、Dechow misstatement F-score + Financial Shenanigans 规则库

    4.2.4 接下来就是成长性的进一步探索的skill 也就是除了财报外，结合财报的内容，有哪些可能增长的点。
    方法很多，数据来源可以是研究报告（现在还没有mcp），新闻，重构后的财报，论文，等等，分析的比如：
    增长来自：销量/价格/产品结构？还是会计口径/并购？
    再投资率、ROIIC、生命周期阶段（可参考 Damodaran 的生命周期视角作为模板）

    4.2.5  护城河推断器skill
    这个主要是质量系数，当然也可以通过这个去调整未来的利润。
    这个可以参考：
    Morningstar 五类 moat 来源识别 + 证据标准Morningstar
    Porter 五力做行业压力与结构约束Harvard Business School
    Greenwald 用“进入壁垒/局部竞争”把 moat 落地成可检验命题
    Mauboussin 给“优势持续周期/价值创造持续性”的组织方式

    4.2.6估值与安全边际的skill
    通过综合上面的内容去计算估值

    这个可以参考的时 McKinsey DCF/价值驱动因素体系McKinsey & Company
    Penman/会计估值视角（如残余收益/报表驱动的估值拆解）
    输出：估值区间、关键敏感参数、下行保护来源（安全边际）

    4.2.7反问和审计skill
    对比：管理层叙事（MD&A/风险）vs 数字变化
    找矛盾：比如说“需求强劲”但应收/库存/退货条款恶化
    找问题，反问，寻找可能的思路漏洞，**What did I miss?（我遗漏了什么）** —— Munger 的反向思维 + 避免大错
    **Why is it cheap?（为什么会被低估）**
    Who is running it?（谁在经营它）等，不断地去挑战，完善，寻找可能的思考盲区来增加预测的确定下。


    也就是我最后希望精简到
    skill1:
     init-company-dossier * resolve-company-identity update-market-snapshot 到一个是company的基础信息
    
    skill2：
    数据采集 * fetch-sec-filings * collect-news-events * collect-technical-papers 合并在一起

    skill3：extract-xbrl-timeseries的改进版本

    skill4： 三表重铸与核心指标

    skill5： 基于财报，发现风险，预测未来利润的，提供财报证据的skill

    skill6：成长性的进一步探索的skill

    skill7： 护城河推断器skill
 
    skill8： 估值与安全边际的skill

    skill9： 反问和审计skill

    就这个9个skill 你把soft的要求去掉，都是硬要求，然后input 参数格式等等其他的模版，我觉得还行不需要变化，你根据我上面的想法，去写一个新的完整的Skill 体系规划吧
