# Company Research Skills 总览

> 这里区分两件事：**当前仓库里实际存在的可运行 skill 资产**，以及 **9-skill 长期目标架构的规格文档**。不要把规格状态误读成“当前代码已经实现”。

## 当前仓库实际状态

当前 `.codex/skills/company_research/` 目录下，实际存在的可运行 skill 只有：

| Asset | 路径 | 当前状态 | 说明 |
|---|---|---|---|
| `collect-company-facts` | `.codex/skills/company_research/collect-company-facts/` | 可运行 | 当前仓库中实际保留的 skill runner，用于收集 SEC filings / `filings_index.yaml` / raw snapshots |

> 说明：本仓库是裁剪后的 skills/workflow 仓库，不是完整实现仓库。很多能力目前只有规格文档，还没有对应的 in-repo `scripts/run.py`。

## 9-Skill 目标架构规格索引

下表用于索引长期目标架构中的 9 个 skill 文档。**这些状态表示的是规格成熟度，不表示当前仓库已经落地了对应代码。**

| # | Skill | 文档 | 规格状态 | 职责 | 对"利润×质量"贡献 |
|---|-------|------|---------|------|------------------|
| 1 | `company-foundation` | [skill1](specs/skill1-company-foundation.md) | 规格已定义 | 身份 + 市场口径（含 shares） | 估值分母/每股化基座 |
| 2 | `sec-ingest-and-materialize-events` | [skill2](specs/skill2-sec-ingest-and-materialize-events.md) | 规格在迭代 | raw ingest + events materialize（subtype + 7 topic families，确定性策略栈） | 证据池 + 事件数据库 |
| 3 | `xbrl-parse-financial-report-events` | [skill3](specs/skill3-xbrl-parse-financial-report-events.md) | 规格已定义 | per-event XBRL 解析 + 全局 atlas | 利润事实底座 |
| 4 | `recast-economic-statements` | [skill4](specs/skill4-recast-economic-statements.md) | 规格已定义 | 经济三表 + 核心指标 | Owner Earnings / ROIC |
| 5 | `profit-quality-and-risk` | [skill5](specs/skill5-profit-quality-and-risk.md) | 规格规划中 | 财报质量/操纵风险/利润可持续性 | 质量系数与情景下界 |
| 6 | `growth-driver-explorer` | [skill6](specs/skill6-growth-driver-explorer.md) | 规格规划中 | 增长来源与 ROIIC/生命周期 | 未来利润路径 |
| 7 | `moat-inferencer` | [skill7](specs/skill7-moat-inferencer.md) | 规格规划中 | 护城河 → 优势期 → 质量系数映射 | 质量系数主体 |
| 8 | `valuation-and-margin-of-safety` | [skill8](specs/skill8-valuation-and-margin-of-safety.md) | 规格已定义 | 估值区间 + MOS + 敏感性 | 输出 IV vs 市场 |
| 9 | `cross-examination-audit` | [skill9](specs/skill9-cross-examination-audit.md) | 规格规划中 | 反问审计：找矛盾/遗漏/为什么便宜 | 提高确定性，防大错 |

## 目标执行顺序

```text
company-foundation → sec-ingest → xbrl-parse → recast-economic → profit-quality → growth-driver → moat → valuation → cross-exam
```

## 相关文档

- [总规划](MASTER_PLAN.md) — 核心公式、设计原则、目录结构、产物 schema
- [SEC/XBRL 技术参考](references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md)
