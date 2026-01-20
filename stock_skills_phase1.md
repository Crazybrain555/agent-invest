下面我按你“**现在的 MCP 配置已经能用**”这个前提，把 **Codex Skills 的最佳搭建模式**（我去查了官方 Codex skills 文档 + 一些踩坑点）和你这条 **Phase 1 核心估值链 5 个 Skill**，做成**逐个 Skill 的搭建方式**（目录怎么放、SKILL.md 怎么写、脚本怎么分层、用哪些 MCP 工具、怎么判定 skipped/blocked/partial、怎么测试）。

---

# 0) 先把“Codex 里搭 Skill 的最佳模式”对齐到你的体系

你现在的规划（`result.yaml` / `needs.yaml` / runs→current 原子替换 / evidence ledger）本质上是“可审计流水线”。在 Codex Skills 机制里，最佳实践是：

## 0.1 Progressive disclosure（渐进加载）= 你的“规定产物契约”最佳载体

Codex 启动时**只加载每个 Skill 的 `name + description + SKILL.md 路径`**，只有当该 Skill 被显式/隐式触发时，才会把 SKILL.md 正文加载进上下文。([OpenAI Developers][1])
所以：

* **把“触发条件”和“输出契约”写进 description/正文**，能让 Codex 更稳定选中正确 Skill
* 把大块 schema / 模板 / 例子放到 `references/` 或 `assets/`，需要时再读，避免上下文污染([OpenAI Developers][1])

## 0.2 SKILL.md Frontmatter 必须“干净”

官方要求：

* `name`：非空、≤100 字符、**单行**
* `description`：非空、≤500 字符、**单行**
  不满足会导致 Skill 不加载。([OpenAI Developers][1])
  另外有个真实踩坑：description 里出现 `something: something` 这种未加引号的冒号，会被 YAML 解析成非法，从而直接跳过加载。([GitHub][2])
  **结论：description 里尽量别写冒号；或者强制用引号包起来。**

## 0.3 Skill 放哪里（建议 repo-scope）

Codex 会从这些位置扫描 Skill（并有覆盖优先级）：repo 的 `.codex/skills`、用户的 `~/.codex/skills` 等。([OpenAI Developers][3])
你这套研究系统明显是“工程化资产”，建议：

* 放到你的主工程仓库：`<repo_root>/.codex/skills/company_research/<skill>/SKILL.md`
* 这样全团队/多机同步最舒服（git 管起来）

## 0.4 你现在的 MCP 配置里，能用的关键工具映射

你给的 config 里，Phase 1 用得到的主要工具：

* `sec_edgar_mcp`：能 **ticker→CIK**、拉 filings、拉财报、甚至抽 XBRL 概念等（工具列表里明确有 `get_cik_by_ticker`、`get_recent_filings`、`get_financials`、`discover_xbrl_concepts`、`get_xbrl_concepts` 等）。([Docker Hub][4])
* `trading_mcp`：`get_fundamental_stock_metrics`（价格/股本/EV 等）
* `gdelt` + `rss`：新闻
* `openalex`/`pubmed`/`arxiv`：论文/技术资料
* `fs`：写 `/home/help/mcp/work`（刚好你的研究根目录规划就在这个树下）
* `search`：DuckDuckGo/网页抽取/PDF 解析/工具信息查询（我强烈建议你在每个 Skill 的脚本里加一个“tool schema 自检”，防止工具参数变动）

> GPT‑5.2 在 MCP 环境下的“多工具、结构化输出”能力是被官方明确强调的；写 Skill 指令时**把输出 schema、缺失字段处理写死**会显著更稳。([OpenAI Cookbook][5])

---

# 1) Phase 1 的“第一阶段”统一脚手架（建议你先做一次）

下面 5 个 Skill 我都会按同一套脚手架来写，这样你写起来非常快：

## 1.1 放置位置建议

假设你的主仓库是 `/mnt/d/python_project/my-quant-project`（你 config 里已 trusted），那么：

```
/mnt/d/python_project/my-quant-project/
  .codex/skills/company_research/
    company-foundation/
    collect-company-facts/
    extract-xbrl-timeseries/
    recast-economic-statements/
    valuation-and-margin-of-safety/
```

运行产物根目录按你规划保持：

```
/home/help/mcp/work/company_research/
```

## 1.2 建一个共享 runtime（可选但很值）

建议放一个共享库（不算“Skill 互相调用”，只是复用代码）：

```
/mnt/d/python_project/my-quant-project/company_research_runtime/
  __init__.py
  paths.py              # 统一算 current/runs/raw 路径
  atomic_io.py          # atomic write yaml/jsonl/parquet
  runlog.py             # 写 meta.yaml/result.yaml/needs.yaml
  artifacts_state.py    # 更新 artifacts_state.yaml
  evidence.py           # append evidence.jsonl / questions.jsonl
  hashing.py            # 文件 hash / input fingerprint（做 skipped 判定）
```

所有 skill 的 `scripts/run.py` 都调用它即可。

---

# 2) Skill 1 — `company-foundation` 搭建方式（逐步落地）

## 2.1 这个 Skill 在 Codex 里怎么“最好触发”

**触发句式**要写进 description（让 Codex 自动选中更稳）：

* “初始化某个 ticker 的研究目录 / 需要 company.yaml / 需要 market snapshot / 更新 shares 和 EV”
  这类 prompt 一出现，Codex 就应该选中它。([OpenAI Developers][1])

## 2.2 目录与文件

```
.codex/skills/company_research/company-foundation/
  SKILL.md
  scripts/
    run.py
  references/
    schemas.md          # 可选：把 company.yaml / market_snapshot.yaml schema 放这里
```

## 2.3 SKILL.md（建议模板）

> 注意：description 单行，避免冒号，必要时加引号。([OpenAI Developers][1])

```md
---
name: company-foundation
description: "Initialize a ticker research folder and write company.yaml plus market_snapshot.yaml when user asks to start coverage or refresh shares price EV."
version: v0.1
---

# company-foundation

## What this skill does
- Create folder tree for /home/help/mcp/work/company_research/company/{TICKER}/
- Resolve identity (ticker -> CIK, company name, exchange, FY end, currency)
- Fetch market snapshot (price, shares outstanding, float if available, market cap, EV if possible)
- Write runs/{run_id}/ then atomically promote to current/

## Tools to use (MCP)
- sec_edgar_mcp: get_cik_by_ticker, get_company_info
- trading_mcp: get_fundamental_stock_metrics
- fs: write files under /home/help/mcp/work
- search.get_tool_info (optional sanity check)

## Inputs
ticker (required), as_of (optional), force_refresh (optional)

## Hard dependencies
None

## Outputs
- company/{ticker}/company.yaml
- company/{ticker}/current/market_snapshot.yaml
- company/{ticker}/current/artifacts_state.yaml
- company/{ticker}/runs/{run_id}/meta.yaml
- company/{ticker}/runs/{run_id}/result.yaml
- Append evidence.jsonl
```

（这里把 tool 名写出来，是为了让 Codex 在执行时“更像 runbook”——官方也建议 tool 使用规则写清楚。([OpenAI Cookbook][5])）

## 2.4 scripts/run.py 的搭建要点（你照着写就行）

核心逻辑按你的协议：

1. **路径与 run_id**

* `run_id = YYYYMMDD_HHMMSS`（建议带 timezone 记录到 meta.yaml）
* `base = /home/help/mcp/work/company_research/company/{ticker}`

2. **查漏补缺 / skipped**

* 如果 `company.yaml` 存在且 cik 不空且未 `force_refresh` → identity `skipped`
* 如果 `market_snapshot.yaml` 的 `as_of == as_of` 且字段齐 → market `skipped`

3. **身份解析：优先 sec_edgar_mcp**

* `sec_edgar_mcp.get_cik_by_ticker(ticker)` ([Docker Hub][4])
* `sec_edgar_mcp.get_company_info(identifier=ticker or cik)` ([Docker Hub][4])
* fallback（极少用）：直接抓 SEC 的 `company_tickers.json`（SEC 官方列了这个数据文件）([Securities and Exchange Commission][6])

4. **市场快照：trading_mcp**

* `trading_mcp.get_fundamental_stock_metrics(ticker)`（参数你用 `search.get_tool_info` 先确认一遍）
* 计算 market_cap / EV：能拿到就直接用；拿不到就置 null（但文件必须写齐 key）

5. **写 result.yaml**

* `ok | skipped | partial | blocked` 按你定义
* 若完全拿不到 identity 或 market，才 `blocked`

6. **promote 到 current**

* 先写 `runs/{run_id}/outputs/...`
* ok/partial 后原子替换到 `current/`

## 2.5 最小验收（Definition of Done）

* 随便选一个 ticker（比如 AAPL），跑完后必须有：

  * `company.yaml` 含 cik
  * `current/market_snapshot.yaml` 含 price 与 shares_outstanding（允许 float/EV 为 null）
  * `runs/{run_id}/result.yaml` status=ok/partial/skipped

---

# 3) Skill 2 — `collect-company-facts` 搭建方式

这个 Skill 的关键是：**把“证据池”做成稳定、增量、可追溯**。

## 3.1 目录与文件

```
.codex/skills/company_research/collect-company-facts/
  SKILL.md
  scripts/
    run.py
  references/              # 可选：预留（Phase1 不使用）
```

## 3.2 SKILL.md（要点）

* description 明确：仅拉 SEC filings，并写 `filings_index.yaml`（可选：events_index 作为候选事件池）
* body 里写清：filings 要哪些 form；lookback；增量逻辑；“已有 accession 不重复下载”

```md
---
name: collect-company-facts
description: "Collect SEC filings for a ticker and write filings_index.yaml under current/."
version: v0.1
---
...
```

## 3.3 scripts/run.py 的搭建要点（按你 MCP 工具最省力的路线）

### (A) SEC filings：尽量用 sec_edgar_mcp 的“filings / sections / financials”能力

你这套 `sec_edgar_mcp` 很可能就是社区的 SEC EDGAR MCP Server（工具表里有 `get_recent_filings`、`get_filing_content`、`get_filing_sections`、`get_financials` 等）。([Docker Hub][4])

**推荐实现**（先跑通最小闭环）：

1. 读取 `company.yaml`，拿到 `cik`（hard 依赖缺就 blocked）
2. 用 `sec_edgar_mcp.get_recent_filings(identifier=ticker, form_type="10-K", days=...)` + 10-Q/8-K/DEF14A 组合，拼成 filings 列表([Docker Hub][4])

   * 你要“回溯 10 年”，就别只靠 days；可以按“循环 years 或分页”做（工具是否支持要看 tool schema）
3. 生成/更新 `current/filings_index.yaml`
4. raw 下载策略（两档）：

   * **档 1（最快上线）**：先不下载全量原文，只用 `get_filing_content` 或 `get_filing_sections` 把关键段落落盘到 `raw/sec/{accession}/`（html 或 txt）([Docker Hub][4])
   * **档 2（完全符合你 spec）**：用 accession 去 SEC Archives 拉 `index.json` + XBRL 文件落盘（你可以在脚本里用 `fetch` MCP 工具做下载）

> 这里我建议你先用档 1 把整条链跑通（Phase 1 的目标是先有可复算 IV），然后再升级到档 2。

### (B) News / Papers（Phase1 暂停）

按当前阶段目标（先把估值链跑通），News / Papers 建议抽离为独立信息服务/数据库（embedding + LLM rerank）并通过 MCP 查询；本 Skill 暂不产出相关 artifacts。

## 3.4 blocked / needs.yaml 规则（照你 v2）

* 缺 `company.yaml` 或缺 `cik` → blocked，needs 指向 producer `company-foundation`
* SEC 列表完全拉不到（工具不可用/频控/网络）→ blocked

---

# 4) Skill 3 — `extract-xbrl-timeseries` 搭建方式（你 Phase 1 的关键瓶颈）

这是最“工程重”的 Skill，我建议你用 **两阶段实现**：

* **v0.1（Phase 1 可用版）**：先把 `facts.parquet` 做出来 + 做一个“浅树”（root→line items）保证 recast 能跑
* **v0.2（完整 Atlas）**：再补 presentation/calculation linkbase 生成真实 nodes/edges/paths

这样你不会卡在 XBRL 解析细节上。

## 4.1 目录与文件

```
.codex/skills/company_research/extract-xbrl-timeseries/
  SKILL.md
  scripts/
    run.py
    build_atlas_minimal.py     # v0.1
    build_atlas_full.py        # v0.2 可后续加
  references/
    atlas_schema.md
```

## 4.2 你现成 MCP 能帮你少走弯路的点

`sec_edgar_mcp` 工具表里有：

* `discover_xbrl_concepts`（列出 filing 内全部 XBRL 概念，含公司特有概念）([Docker Hub][4])
* `get_xbrl_concepts`（从指定 filing 抽取指定概念的事实）([Docker Hub][4])
* `get_financials`（直接拿财报表）([Docker Hub][4])

**Phase 1 建议（v0.1）**：用 `get_financials`/`get_company_facts`（如果输出里含 period）先落 `facts.parquet`，树先做浅的。
等你有时间再用本地 XBRL 解析（Arelle）把树补齐。

## 4.3 v0.1 最小 Atlas 具体怎么做

目标：产出这 5 个文件（即使 tree 很浅）：

* `current/xbrl_atlas/periods.yaml`
* `nodes.parquet`
* `edges.parquet`
* `facts.parquet`
* `paths.parquet`

### v0.1 数据来源路线（推荐）

对每个 `accession`：

1. 用 `sec_edgar_mcp.get_financials(identifier=ticker, statement_type="all")` 拿三表 line items + 值([Docker Hub][4])

2. 把返回结构展开成事实长表：

   * `statement_type`（IS/BS/CF）
   * `label`
   * `concept`（如果返回有 tag；没有就用稳定 hash：`synthetic:{slug(label)}`）
   * `value`
   * `unit` / `decimals`（没有就 null）
   * `period_end`（从 filing/statement header 推断；推断不了就先用 filings_index 的 period_end）
   * `accession`

3. nodes/edges/paths（浅树）：

   * 每个 statement_type 一个 root node（depth=0）
   * 每个 line item 一个 node（depth=1，order=返回顺序）
   * edges：root→item
   * paths：`"{statement_type}/{label}"`

4. periods.yaml：

   * `period_end -> accession`（你可以只保留最近一个 accession 代表该 period）

### v0.1 的好处

* recast（Skill 4）马上能跑
* 你后续升级到 v0.2 时，不会改下游接口，只是 nodes/edges/paths 更真实

## 4.4 blocked / partial

* blocked：缺 `filings_index.yaml` 或 raw/sec 缺失（如果你选择从 raw 解析）
* partial：某个 period 取不到财报/缺字段 → 仍产出，但写 warnings + questions

---

# 5) Skill 4 — `recast-economic-statements` 搭建方式（Phase 1 的“经济利润口径”落地）

这个 Skill 你只要抓住一句话：

> **把 Skill 3 的“事实长表”映射到你的经济三表，并把 mapping 写进 recast_policy 以便可追溯。**

## 5.1 目录与文件

```
.codex/skills/company_research/recast-economic-statements/
  SKILL.md
  scripts/
    run.py
    recast_policy_default.yaml     # 作为 assets 或 references
    recast.py
  references/
    mapping_heuristics.md
```

## 5.2 v0.1 实现策略（强烈建议）

你现在 Skill 3 的 v0.1 Atlas 很可能只有 label + value（concept 可能不稳定）。所以重铸先走 **label-based + 少量 concept 优先** 的策略：

### (A) 先做 3 个“必出”指标（Phase 1 够用）

* `owner_earnings`
* `maintenance_capex`
* `fcf`

实现上，最稳的做法是：

* CFO：从 CF 里找 “Net cash provided by operating activities”/“Cash flows from operating activities” 类 label
* Capex：从 CFI 里找 “Capital expenditures”/“Payments for property and equipment”
* maintenance capex：`depr_floor`（你 policy 里已有）
* owner earnings：`CFO - maintenance_capex ± normalized_wc`（Phase 1 可以先把 normalized_wc 设 0，后续再补）

### (B) NOPAT/ROIC（Phase 1 可先简化）

Phase 1 如果你想快速拿出“估值底座”，NOPAT/ROIC 可以先做简版：

* EBIT：从 IS 找 “Operating income”/“Income from operations”
* 税率：用 `Income tax expense / Pretax income`（做 clip 到 [0, 0.35]）
* NOPAT = EBIT*(1-tax_rate)
* Invested Capital：用 BS 的 “Total assets - Cash - Non‑interest‑bearing current liabilities” 近似（能找到哪些 line items 看事实表）

### (C) 把“你选择了哪个 line item”写进 recast_policy

这是你体系里最关键的“可追溯性”。
每次 selector 命中 label，要记录：

* `chosen_labels` 或 `chosen_concepts`
* `rationale`
* `fallback_used`（如果没命中用什么兜底）

## 5.3 blocked / partial

* blocked：atlas 缺失（nodes/edges/facts/periods 任一个缺就 blocked）
* partial：找不到 capex 或 CFO，只能降级估计（写 warnings + question）

---

# 6) Skill 5 — `valuation-and-margin-of-safety`（Phase 1 版本）搭建方式

这里必须强调一个“工程现实”：

你完整 v2 里 Skill 8 的 hard deps 包含 `profit_risk_forecast/growth_drivers/quality_coefficient`。
但 Phase 1 只做 5 个 Skill，所以你需要一个 **Phase1 版估值 Skill**（v0.1-phase1），**硬依赖只吃到 Skill 4 的输出**，其余假设走默认 policy。
这不违背“hard deps”原则——因为 Phase1 版本的契约本来就不把它们列为 hard。

## 6.1 目录与文件

```
.codex/skills/company_research/valuation-and-margin-of-safety/
  SKILL.md
  scripts/
    run.py
    valuation_policy_phase1.yaml
    model.py
    render_memo.py
  assets/
    investment_memo_template.md
  references/
    valuation_schema.md
```

## 6.2 Phase1 的 Hard 依赖（建议你就这么写）

* `current/market_snapshot.yaml`
* `current/economic/core_metrics.parquet`
* `current/economic/economic_statements.parquet`

## 6.3 Phase1 的估值方法（建议“hybrid 但先做简版”）

你 Phase1 的目标是“能排序筛低估”，不是完美 DCF。建议两条腿：

### (A) EPV / Owner Earnings multiple（最稳、最快）

* Base Owner Earnings = 最近一期或 TTM（从 core_metrics）
* Multiple = 一个默认区间（比如 10x/14x/18x）

  * 这相当于把“质量系数”先粗暴折进 multiple
* Bear/Base/Bull：用不同 multiple + 不同 owner earnings（做 ±10~20%）

### (B) 简化 DCF（可选）

* 5 年显式期：owner earnings 按一个默认增速（比如 0/3%/6%）
* 折现率：默认 9.5/10.5/12
* 终值：用终值倍数（比如 12/15/18）

> 等你 Phase2 有 `quality_coefficient.yaml` 再把这些参数“证据化”。

## 6.4 输出（必须按你 value_state 契约）

* `current/valuation/valuation.yaml`
* `current/valuation/valuation_model.csv`
* `current/valuation/value_state.yaml`
* `current/valuation/investment_memo.md`

特别是 `value_state.yaml`，Phase1 你可以把：

* `quality` 里 components 全设 null 或 0，并把 `confidence` 设低（比如 0.3）
* `margin_of_safety_base` 必须算出来（`IV_base / price` 或 `(IV_base - price)/price` 你选一种固定口径）

---

# 7)（加分但强烈建议）Phase 1 的“测试与回归”怎么搭

官方 Codex docs建议“测试触发 + 验证输出”([OpenAI Developers][1])，你这套流水线建议加一个最小回归脚本：

```
scripts/smoke_test_phase1.py
- 输入 ticker 列表（2~5 个）
- 顺序跑 1~5
- 检查每个 current/ 产物存在且 schema key 齐
- 生成一个临时 value_summary.csv（哪怕只有 2~5 行）
```

这样你以后升级 Skill 3（从浅树到真树）不会把下游悄悄打爆。

---

# 8) 你现在可以直接照这个顺序开始“一个个搭”

按投入产出比，建议你这么写代码：

1. **先写共享 runtime（可选，但你会省很多时间）**
2. Skill1 + Skill2：全是“取数/落盘/增量”
3. Skill3：先 v0.1（浅 atlas）跑通
4. Skill4：先只保证 CFO/capex/owner earnings
5. Skill5：先 EPV/multiple 版把 value_state 跑出来
6. 最后再回头把 Skill3 做成 v0.2（真树）+ Skill4 做成更严谨的 ROIC

---

如果你愿意，我也可以把上面 5 个 Skill 的 **SKILL.md 正文**（含你那套 `result.yaml/needs.yaml` 的硬协议、每步工具调用约束、错误降级策略）给你直接写成“可复制粘贴版”——你只要放到 `.codex/skills/.../SKILL.md` 就能用（而且会特别注意 description 单行 + 不踩 YAML 冒号坑）。([OpenAI Developers][1])

[1]: https://developers.openai.com/codex/skills/create-skill "Create skills"
[2]: https://github.com/openai/codex/issues/8610/linked_closing_reference?reference_location=REPO_ISSUES_INDEX "skill-creator: Invalid YAML in frontmatter · Issue #8609 · openai/codex · GitHub"
[3]: https://developers.openai.com/codex/skills/ "Agent Skills"
[4]: https://hub.docker.com/r/mcp/sec-edgar "mcp/sec-edgar - Docker Image"
[5]: https://cookbook.openai.com/examples/gpt-5/gpt-5-2_prompting_guide "GPT-5.2 Prompting Guide"
[6]: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data?utm_source=chatgpt.com "Accessing EDGAR Data"
