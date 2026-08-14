---
id: disclosure_anchor_design_classification_facets
project: disclosure_anchor
title: 分类三维拆解 + 衍生分类下沉视图（0016 方案）
status: adopted-and-implemented (0016, 2026-07-08)
created_at: 2026-07-07
---

# 分类三维拆解 + 衍生分类下沉视图

用户裁决方向（2026-07-07）：document 表只放数据源事实；衍生分类（filing_type
粗桶、disclosure_topics 白名单）不该物化在核心表里——物化的代价已被证实
（词表升版→存量陈旧→需要 reclassify 补偿工具）。本方案落这个方向。

## 1. 数据验证（全库 91 份带 F006V 文档）

- **段数不固定**：3 段 69 份（主体+市场+内容），1 段 4 份（仅内容码，IR 调研
  记录），4 段 11 份、5 段 7 份（多内容码：如 限制出售股份上市+股权激励）。
- **三个维度混在一串**：`01010503||010112||010301` = 上市公司董事会（发布
  主体）+ 深市公司公告（市场）+ 年度报告（内容）。维度可从巨潮自己的分类树
  机械判定：0101（发表公告机构）支下 = 主体/市场（其中 010112/010113 类
  "X市公告"码 = 市场，其余 = 主体）；其他根支 = 内容。
- **每份公告 ≥1 个内容码**（零内容码文档 = 0）；内容码 0..n 多值。
- 巨潮字典 disclosure_core.provider_category（2135 行，parent_category_code
  父子树）已整表在库。

## 2. 业界形态（2026-07-07 调研）

| 系统 | 形态 |
|---|---|
| 东方财富（akshare stock_notice_report） | **单值 7 类粗桶**（财务报告/融资/风险提示/信息变更/重大事项/资产重组/持股变动），供应商侧降维 |
| Wind 公告/舆情 | **多标签 + 重要度**，逐条打标签可按类别/重要度筛（≈我们的 topics+白名单层） |
| SEC EDGAR | form_type **单值粗桶**（10-K/8-K）+ 8-K item codes **多值事件维**——粗桶+多值事件的双层与本服务同构 |
| 巨潮官网自身检索 | category 参数只用**内容支**单选（年报/半年报…），主体/市场当过滤维 |

结论：粗桶（单值枚举）+ 多值内容标签的双层是行业通行形态；差别只在物化位置。
本方案把两层全部下沉到视图现算，表里只留事实。

**用户裁决（2026-07-08，港股+A股双市场）**：
1. topic 词表**按 HKEX Tier 2（上市规则 Appendix 24）为锚扩容**——它是离
   A 股最近的官方两级体系，且用户做港股，未来 HKEX 通道的 Tier 2 可直接映射
   进同一套 topics；A 股特色类（问询回复等）保留自定键。
2. 每个 topic/event 键挂 `std_refs` 交叉引用（caev / hkex_t2 / sec），
   **只放词表 JSON 文件**，不进库表不进契约。
3. L2 契约暴露 CAEV 列：暂缓（视图加列便宜，有跨市场需求再加）。

## 3. 目标 schema（迁移 0016）

### 3.1 新表 disclosure_core.classification_rule（词表的库内加载副本）

| 列 | 类型 | 说明 |
|---|---|---|
| rule_set | vc16 | **'class' / 'facet' 两种**（2026-07-08 定案：filing_type 无独立规则行） |
| prefix | vc16 | F006V 码前缀（'010301'、'0113'、'010112'…） |
| value | vc32 | 'annual_report' / 'dividend' / 'market'… |
| priority | int | class 行=该类在主分类阶梯的档位（同类各前缀行同值，loader 从词表校验）；facet 行=长前缀优先 |
| version | vc16 | 与源 JSON 词表版本一致（整表同版本） |

**单一映射、两种输出**（回应"filing_type 和 topic 是不是一样的"——是同一张映射）：
`disclosure_topics` = 文档全部 class 命中的**集合**；`filing_type` = 同一命中集里
**priority 最高的那一个**（argmax）。定期报告/业绩类也进同一张 map——
年报 topics=["annual_report"]，"定期报告 topics 为 NULL"的特例消失，
topics 成为完备分类（NULL 仅剩无码通道）。词表 JSON 形态：
`"dividend": {"prefixes":["0113"], "priority":60, "std_refs":{...}}`。

- 真源仍是仓内 versioned JSON（filing_type_map.json / topic_map.json / 新增 facet_map.json——
  **落地勘误 2026-07-16：`topic_map.json` 已并入统一的
  `adapters/sources/cninfo/class_map.json`（31 类，单张 class 映射同时派生 disclosure_topics 与
  filing_type）；`filing_type_map.json`（标题 title/title_topic 规则）与 `facet_map.json`
  （F006V market/publisher 维度判定）仍各自独立在库，未被 class_map 取代**）；
  `make load-rules` = 事务内 TRUNCATE+INSERT。
- **watchlist.csv 的 filing_categories 列改收 topic 键**（如
  `dividend;major_contract`），经规则表解析为 F006V 前缀集下推同步过滤；
  原始 F006V 前缀仍兼容（运维友好：人记得住 topic 键，记不住码）。
  全量登记默认不变（round9 裁决）；按公司收窄是运营者选项。
- doctor 增加校验：库内 version == 文件 version（漂移报警）。
- **词表升版 = 改文件 + make load-rules，全库分类即刻生效**；
  scripts/reclassify_documents.py 删除（补偿工具不再需要）。

### 3.2 document 表

- `disclosure_topics` 列 + GIN 索引：**删除**（0016，视图现算）。
- `filing_type` 列：**0017 删除**（用户裁决：规则派生值不是事实，兜底列违反
  '表=事实'——且实测服务 0 行）。标题关键词规则进 classification_rule
  （rule_set='title'，filing_type_map.json 文件序=优先级，'问询%回复' 表达
  all-match）；视图 filing_type = COALESCE(码 argmax, 标题 argmax, 'other')。
  切分器（build_units）经 mapper.derive_primary_class 用同一词表 Python 求值
  （与视图双求值器，一致性由集成测试钉住）。
- F006V 原串留在 provider_metadata.raw_category（现状，事实已在）。

### 3.3 视图新增/改造列（0016 历史方案）

> 0037 部分取代：三类 provider facets 当前只由 `documents_v1` / `document_categories_v1`
> 暴露，不再重复到 `document_units_v1`。下表保留 0016 最初的分类形态与来源设计。

| 视图列 | 形态 | 样例（真实数据） |
|---|---|---|
| publisher_categories | jsonb 数组 | `[{"code":"01010503","name":"上市公司董事会"}]`；律所公告为 `[{"code":"01010901","name":"律师事务所"}]` |
| market | text（0..1） | `"深市公司公告"` / `"沪市主板公告"` / NULL（IR 记录无市场码） |
| content_categories | jsonb 数组（≥1） | 年报：`[{"code":"010301","name":"年度报告"}]`；激励解禁：`[{"code":"011307","name":"限制出售股份上市"},{"code":"012325","name":"股权激励"}]` |

**裁定：不加 content_branches 列**（2026-07-08 评估）。巨潮原样分类已由
document_categories_v1 完整承载（每文档每码一行，含 category_name 与
parent_category_code，可按树任意钻取）——再加一列 = 同一信息第三个载体，
恰恰复刻"分不清哪列是谁的分类"的混乱。替代保障是质量环（见 §4）。
| filing_type | text（现算优先） | `COALESCE(规则表按 priority 首命中, d.filing_type)`——API 通道永远跟随当前词表，web 通道回落注册值 |

**filing_type 词表扩桶（2026-07-08 定案，回应"92% other 无意义"）**：
单值主分类 = 同一 class 映射按 priority 取 argmax。优先级不是口味，是
**三层原则**（无公认标准可抄——SEC 靠表单结构天然单值、HKEX 多选不定主次、
东财/RavenPack 规则不公开——故显式建阶梯并写进 versioned 词表）：
1. 日历/法定周期类（定期报告、业绩预告/快报）——身份无歧义，最高档；
2. 实质事件类（重组>激励>股份变动>分红>合同>关联>问询>风险>融资>IR）——
   按重大性，锚定交易所上市规则重大性章节与 8-K item 结构的共同取向；
3. 程序载体类（决议、中介报告、治理制度）——载体永远让位于内容
   （激励方案经董事会决议发布，内容是激励不是决议）。
实测影响面：91 份中仅 4 份存在 ≥2 实质类命中（全为激励公告，argmax 给出
equity_incentive 与人工判断一致）；层内微调顺序影响 <5% 文档，且落选类
仍在 topics 集合里不丢失。实测（2026-07-08 真库落地后）：other 92% → **16%**（15/91，全部为巨潮仅打
012399 其它事项码的文档——董事会工作报告/财务决算/债权人通知类，诚实杂项；
映射它会捏造语义，接受）。分布：equity_incentive 17、governance_rules 13、
meeting_resolution 13、intermediary_report 7、dividend 5、performance_briefing 2…。
API 通道补标题关键词兜底（码推导=other 时跑 web 通道同款规则包——巨潮漏码的
业绩说明会类由标题救回）。与 disclosure_topics 的分工 = SEC form type 之于 8-K items：
**单值主分类管 GROUP BY/看板/简单过滤，多值 topics 管检索**（激励解禁公告
primary=equity_incentive，topics 同时含 equity_share_change）。
L2 前向兼容规则写进契约说明：未知枚举值按 other 消费。
| disclosure_topics | jsonb 数组（现算） | `["equity_incentive","equity_share_change"]` |

实现骨架（documents_v1 内）：

```sql
LEFT JOIN LATERAL (
  SELECT
    jsonb_agg(...) FILTER (WHERE f.value='publisher') AS publisher_categories,
    min(pc.category_name) FILTER (WHERE f.value='market') AS market,
    jsonb_agg(...) FILTER (WHERE f.value IS NULL)   AS content_categories,
    (SELECT r.value FROM rule r WHERE rule_set='filing_type'
       AND seg LIKE r.prefix||'%' ORDER BY r.priority LIMIT 1) AS derived_filing_type,
    (SELECT jsonb_agg(DISTINCT r.value) FROM rule r WHERE rule_set='topic'
       AND seg LIKE r.prefix||'%') AS disclosure_topics
  FROM unnest(string_to_array(d.provider_metadata->>'raw_category','||')) seg
  JOIN disclosure_core.provider_category pc ON pc.category_code=seg
  LEFT JOIN classification_rule f ON rule_set='facet' AND seg LIKE f.prefix||'%'
) facets ON true
```

（facet 判定：命中 market 前缀 → market；命中 0101 → publisher；否则 content。
长前缀优先由 priority 表达。）

### 3.4 worker 队列谓词

pending_parse 的 scope 判定从 `d.disclosure_topics ?| :core_topics` 改为与视图
同款的规则表 join。语料规模（200 只票 ≈ 数万文档、队列批量 LIMIT 小）下无
索引也毫秒级；若未来涨到百万级再物化回列（可逆决策，记录于此）。

**处理策略最终形态（2026-07-08 round21，取代 round20 的双清单）**：
"下载了就解析"——parse_scope/download_scope 合并为 config/processing_policy.json
（process 20 类含 correction_supplement / register_only 11 类；并集=词表）。
级联：policy=全局默认，watchlist process_classes=按公司替换式覆盖（0018 列改名，
旧同步侧登记过滤删除——登记永远全量，加类自动回补历史）。业界对标：
changedetection.io 三层级联/替换语义/空=继承；HA check_config→make config-check；
terraform plan→make track DRY_RUN=1；changedetection _source→track-status 来源层列；
edgartools amendments=True→correction_supplement 默认 process；
miniflux OPML→真相翻转时 CSV 降级为 import/export（09 占位）。

> 2026-07-26 边界澄清：processing policy 只决定尚未接入文档的调度/获取范围，不是 PDF
> 内容白名单。文档一旦进入 parse scope，其非空 source carriers 必须全量形成可检索证据；
> classification/title/topic 不能决定标题、切分、删除或 table title。不能用扩大分类词表修复
> parser 结构缺陷。

## 4. 取舍与不做

- **GIN 索引丢失**：全库按 topic 聚合会变慢——L2 场景以过滤为主，可接受；
  阈值见 §3.4。
- **filing_type 双来源**必须写进数据字典：视图值 = 码派生优先、注册值兜底；
  document 表列 ≠ 视图列（表列仅兜底语义）。
- 不给主体/市场维做筛选策略（纯事实透传）；调度范围策略仍只挂内容维，且不得下沉为
  文档内部的结构/证据可见性规则。
- 词表扩容（r3，与 0016 同批）：以 HKEX Tier 2 为锚补齐 topics（预计
  14 → ~25：+清盘退市、供股/公开发售、关连交易细分、盈利警告、
  股份合并拆细、主要交易/须予披露交易分级等），每键带 std_refs；
  扩容后 processing-policy 覆盖范围同步复核。
- **规则质量环（替代暴露原样 30 类）**：未映射内容码审计——语料中出现、
  既不命中 filing_type 规则也不命中 topic 规则的内容维码 = 候选缺口，
  列出码+巨潮名+文档数供人工晋级（同套话/标题吞没发现环模式）；进
  评审指南 §2 每轮必跑。词表升版即全库生效（视图现算），无存量残留。
- 事件语义零影响：两级分类都不进任何 hash（0014 时已如此），改现算不触发
  materialized 事件。

## 5. 实施顺序

1. facet_map.json（市场码清单 + 0101 主体判定）+ classification_rule 表
   + make load-rules + doctor 校验（0016 迁移，含视图重建与 grants）。
2. documents_v1 / document_units_v1 加三事实列、filing_type/topics 改现算；
   契约导出与 contract-checklist §2 同步（列数 38 → 41）。
3. pending_parse 谓词切规则表 join；删 document.disclosure_topics 列与索引；
   删 reclassify_documents.py；数据字典/评审指南/验收 prompt 更新。
4. 全链验证：agent-check + live 套件 + 类扫描（topics 覆盖口径改从视图测）。
