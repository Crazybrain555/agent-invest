---
id: disclosure_anchor_design_classification_facets
project: disclosure_anchor
title: 分类三维拆解 + 衍生分类下沉视图（0016 方案）
status: proposed
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

## 3. 目标 schema（迁移 0016）

### 3.1 新表 disclosure_core.classification_rule（词表的库内加载副本）

| 列 | 类型 | 说明 |
|---|---|---|
| rule_set | vc16 | 'filing_type' / 'topic' / 'facet' |
| prefix | vc16 | F006V 码前缀（'0103'、'011513'、'010112'…） |
| value | vc32 | 'annual_report' / 'dividend' / 'market'… |
| priority | int | filing_type 单值取最高优先命中；facet 长前缀优先 |
| version | vc16 | 与源 JSON 词表版本一致（整表同版本） |

- 真源仍是仓内 versioned JSON（filing_type_map.json / topic_map.json /
  新增 facet_map.json）；`make load-rules` = 事务内 TRUNCATE+INSERT。
- doctor 增加校验：库内 version == 文件 version（漂移报警）。
- **词表升版 = 改文件 + make load-rules，全库分类即刻生效**；
  scripts/reclassify_documents.py 删除（补偿工具不再需要）。

### 3.2 document 表

- `disclosure_topics` 列 + GIN 索引：**删除**（视图现算）。
- `filing_type` 列：**保留但语义收窄** = 注册时兜底分类（web/本地通道无
  F006V，只能靠标题规则，属观察期事实，SQL 无法再生）。注册代码不变。
- F006V 原串留在 provider_metadata.raw_category（现状，事实已在）。

### 3.3 视图新增/改造列（documents_v1 与 document_units_v1 同步）

| 视图列 | 形态 | 样例（真实数据） |
|---|---|---|
| publisher_categories | jsonb 数组 | `[{"code":"01010503","name":"上市公司董事会"}]`；律所公告为 `[{"code":"01010901","name":"律师事务所"}]` |
| market | text（0..1） | `"深市公司公告"` / `"沪市主板公告"` / NULL（IR 记录无市场码） |
| content_categories | jsonb 数组（≥1） | 年报：`[{"code":"010301","name":"年度报告"}]`；激励解禁：`[{"code":"011307","name":"限制出售股份上市"},{"code":"012325","name":"股权激励"}]` |
| filing_type | text（现算优先） | `COALESCE(规则表按 priority 首命中, d.filing_type)`——API 通道永远跟随当前词表，web 通道回落注册值 |
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

## 4. 取舍与不做

- **GIN 索引丢失**：全库按 topic 聚合会变慢——L2 场景以过滤为主，可接受；
  阈值见 §3.4。
- **filing_type 双来源**必须写进数据字典：视图值 = 码派生优先、注册值兜底；
  document 表列 ≠ 视图列（表列仅兜底语义）。
- 不给主体/市场维做白名单策略（纯事实透传）；解析范围策略仍只挂内容维。
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
