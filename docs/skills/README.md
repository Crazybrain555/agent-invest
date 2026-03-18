# Company Research Skills 总览

> 9 个 Skills 按顺序执行，逐个实现。详细规格见各 skill 文档。

## 实施状态

| # | Skill | 文档 | 状态 | 职责 | 对"利润×质量"贡献 |
|---|-------|------|------|------|------------------|
| 1 | `company-foundation` | [skill1](specs/skill1-company-foundation.md) | 已实现 | 身份 + 市场口径（含 shares） | 估值分母/每股化基座 |
| 2 | `sec-ingest-and-materialize-events` | [skill2](specs/skill2-sec-ingest-and-materialize-events.md) | 开发中 | raw ingest + events materialize（subtype + 7 topic families，确定性策略栈） | 证据池 + 事件数据库 |
| 3 | `xbrl-parse-financial-report-events` | [skill3](specs/skill3-xbrl-parse-financial-report-events.md) | 待开发 | per-event XBRL 解析 + 全局 atlas | 利润事实底座 |
| 4 | `recast-economic-statements` | [skill4](specs/skill4-recast-economic-statements.md) | 待开发 | 经济三表 + 核心指标 | Owner Earnings / ROIC |
| 5 | `profit-quality-and-risk` | [skill5](specs/skill5-profit-quality-and-risk.md) | 规划中 | 财报质量/操纵风险/利润可持续性 | 质量系数与情景下界 |
| 6 | `growth-driver-explorer` | [skill6](specs/skill6-growth-driver-explorer.md) | 规划中 | 增长来源与 ROIIC/生命周期 | 未来利润路径 |
| 7 | `moat-inferencer` | [skill7](specs/skill7-moat-inferencer.md) | 规划中 | 护城河 → 优势期 → 质量系数映射 | 质量系数主体 |
| 8 | `valuation-and-margin-of-safety` | [skill8](specs/skill8-valuation-and-margin-of-safety.md) | 待开发 | 估值区间 + MOS + 敏感性 | 输出 IV vs 市场 |
| 9 | `cross-examination-audit` | [skill9](specs/skill9-cross-examination-audit.md) | 规划中 | 反问审计：找矛盾/遗漏/为什么便宜 | 提高确定性，防大错 |

## 执行顺序

```
company-foundation → sec-ingest → xbrl-parse → recast-economic → profit-quality → growth-driver → moat → valuation → cross-exam
```

## 状态说明

- **已实现**：有可运行的 scripts/run.py
- **开发中**：正在实现
- **待开发**：有详细规格，尚未开始编码
- **规划中**：有概要规格，待展开

## 相关文档

- [总规划](MASTER_PLAN.md) — 核心公式、设计原则、目录结构、产物 schema
- [SEC/XBRL 技术参考](references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md)
