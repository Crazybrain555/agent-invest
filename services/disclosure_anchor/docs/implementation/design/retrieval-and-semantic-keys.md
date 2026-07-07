---
id: disclosure_anchor_design_retrieval_semantic_keys
project: disclosure_anchor
title: 非 embedding 检索数据面：多级标题、关键词与 semantic_key 附注词表（设计评审）
status: partially-adopted (词表已实施 2026-07-07；06R 投影待立项)
created_at: 2026-07-07
inputs: 用户 round8 提问 + 2026-07-07 调研（EDGAR EFTS/AlphaSense/PG 中文 FTS/编报规则第15号/XBRL）+ 本仓只读取证
decides_for: 06R（检索投影里程碑，规格待编写）+ semantic_key 词表升版
---

# 非 embedding 检索数据面设计评审

**背景**：用户明确不做 embedding，L2 召回走"关键词 + 语义相关"。他判断需要
(a) 关键词字段、(b) 多级标题，并指出 semantic_key 明显偏少（贴出附注科目大片 NULL：
其他综合收益/研发支出/政府补助/公允价值披露/股份支付/租赁/承诺及或有事项…）。
本文档判定这三个判断的对错、给业界依据、给 06R 最小投影规格草案与 semantic_key
词表方案。**本轮不改代码。**

## 1. 问题清单（共 4 条）

| # | 问题 | 判定（先说结论） |
|---|---|---|
| A1 | 不做 embedding，关键词+语义检索的数据面要补什么？ | 补的是 **06R 投影层**（早有概念边界，未实现），不动切分层 |
| A2 | 需要关键词字段？ | **对，但形态是受控词表（semantic_key 扩容），不是自由抽取词**（抽取词近乎冗余，见 §3） |
| A3 | 需要多级标题？ | **数据已经有了**（heading_path 1-4 级，379/379 覆盖）；缺的是把它放进可打分的检索文本 |
| A4 | semantic_key 明显少了 | **对，是词表覆盖缺口**：附注小节 95/112 双 NULL；且附注标题是法定受控词表，可系统性补齐 |

## 2. 现状取证（file:line + live DB，2026-07-07）

- **多级标题已存在**：heading_path 是每个 unit 的必填 1-4 级数组（S2 深度封顶
  `builder.py:257-261`；无标题单元有合成锚，live 直方图 深度1=34 / 2=101 / 3=209 /
  4=35，共 379，零空、零超深）；mixed parts 另带 local_heading 相对路径
  （`builder.py:1095-1115`）。
- **缺的是检索面**：public 视图无 heading_path_text/search_text；唯一索引是
  `ix_document_unit_heading_path`（GIN jsonb_path_ops，`0007:347-354`）——只支持
  `@>` 整串精确包含（必须一字不差写出"七、合并财务报表项目注释"），不支持
  子串/分词/模糊；payload 正文完全无索引。
- **semantic_key 机理**：匹配文本 = title + heading_path 末两级 + 首个表 caption
  (+qa 的 question)（`builder.py:1202-1223`）；现 25 条规则/23 个键。
  "八、研发支出"NULL 的原因：rd_investment 要求 研发+（投入|费用|人员），
  "支出"不在表内；"其他综合收益"NULL：词表根本没有这个概念。
- **缺口规模**：附注（含"合并财务报表项目注释"各变体）112 个 active 单元中
  **95 个 semantic_key/semantic_keys 双 NULL**（~85%）。

## 3. 业界调研结论

1. **纯词法检索是披露检索的现役主流**：SEC EDGAR 全文检索（EFTS）就是无 embedding
   的 Elasticsearch 词法检索 + 结构化过滤（sec.gov/edgar/search/efts-faq.html）。
   AlphaSense 式"语义感"= 关键词 + **同义词扩展** + 受控节税 taxonomy 过滤 + 多因子
   重排——没有用户可见的向量层。结论：不做 embedding 完全站得住，前提是把词法面做对。
2. **面包屑必须进被打分文本**："contextual chunk headers"是命名实践——标题链拼进
   索引文本可显著降低 BM25 侧检索失败率（Anthropic contextual retrieval 同向证据）。
   只把 heading_path 留在元数据列，打分器永远看不见它。
3. **PG 中文 FTS 的承重决策是分词质量**：zhparser / pg_jieba + **领域自定义词典**
   优于 bigram（Wikimedia 中文分析器评测）；短查询兜底可加 pg_bigm/pg_trgm 双通道。
   字段加权是成熟模式：`setweight`（A=title，B=面包屑，C=正文，D=标签）拼进单个
   tsvector + 单个 GIN。
4. **自由抽取关键词（TF-IDF/TextRank/YAKE）对召回几乎无增益**——抽出来的词本来就在
   正文里；无 embedding 栈里**唯一能买到同义/复述召回的是受控词表 + 查询侧同义词映射**
   （中文 BM25 复述查询 Recall@50 实测 0.49-0.70，缺口正是靠这个补）。
5. **附注标题→规范键是监管背书的成熟做法**：财政部《企业会计准则通用分类标准》与
   沪深 XBRL 定期报告分类标准，正是把每项附注标记为唯一 taxonomy 元素（text block）。
   我们给附注小节派生 semantic_key 与 XBRL 同构。

## 4. semantic_key 附注词表方案（A4 的系统性修法）

**法定封闭集**：《公开发行证券的公司信息披露编报规则第 15 号——财务报告的一般规定》
（2023 修订，证监会公告〔2023〕64 号）第三章逐条枚举附注必须披露的项目——
资产 23 款 + 负债 15 款 + 权益 8 款 + 损益 13 款 + 现金流 7 款 + 16 个专节。
科目名与财会〔2019〕6 号报表行项目一致，真实年报附注标题几乎逐字复用（样本验证：
附注"五、1~54"按此顺序）。

- **词表规模**：~80-90 键 = 章节级 ~20（编制基础/重要会计政策/税项/研发支出/合并范围
  变更/在其他主体中的权益/政府补助/金融工具风险/公允价值披露/关联方/股份支付/
  承诺及或有事项/日后事项/其他重要事项/母公司注释/补充资料/外币项目/租赁…）
  + 项目级 ~60-70（货币资金/交易性金融资产/应收票据/应收账款/…/其他综合收益/
  专项储备/盈余公积/…/信用减值损失/非经常性损益）。
- **匹配策略三级**：精确名 → 别名表 → 包含式（标题含唯一科目名）。标题是监管标准化
  文本，预期前两级覆盖绝大多数。
- **必须处理的三类噪声**：① 版本漂移（2019 前旧准则：可供出售金融资产→
  其他权益工具投资/其他债权投资 等，别名表吸收）；② 合并标题（"应收票据及应收账款"
  "营业收入和营业成本"——支持复合命中多键）；③ 金融行业变体（发放贷款及垫款/
  吸收存款等追加科目）+ 自定义小节兜底。
- **实现位置**：与 filing_type_map.json 同模式的 versioned 词表 JSON
  （note_key_map.json），规则包升版触发（rebuild-units 5 秒级，重建成本可忽略）。
  现有 25 条通用规则保留（担保/回购/议案这些跨文档类型的键不属于附注词表）。

## 5. 06R 最小检索投影规格草案（A1-A3 的答案）

U7 边界（05 §2，原文摘录）已锁定：投影是**派生层**，字段族
heading_path_text / display_subtitle / search_text / controlled_keywords /
extractive_keywords /（后置）summary；不进 content_hash、不替代 payload、
不作证据、不引入 chunk/embedding；全部可由已持久化数据确定性再生。

结合调研，最小落地形态（视图或投影作业 + 一张投影表）：

| 字段 | 来源 | 索引 | 权重 |
|---|---|---|---|
| title | 已有 | ↓ 并入 search_tsv | A |
| heading_path_text | heading_path join " > "（含 mixed parts local_heading） | ↓ 并入 | B |
| body_text | payload 线性化（text + 表头/单元格 + qa 问答） | ↓ 并入 | C |
| semantic_keys | 已有列（§4 扩容后） | GIN(array) 精确过滤 + 并入 tsv | D |
| search_tsv | 上四者 setweight 拼接，zhparser/pg_jieba + 财务领域词典 | GIN | — |
| （可选兜底） | pg_bigm 双通道应对短查询/新词 | GIN(bigram) | — |

查询侧配套：受控词表的同义词映射（"分红"→dividend，"坏账"→receivable_aging），
scope 过滤沿既有列（filing_type/report_period/heading_path @>/applicability/page_no）。

**明确不做**（重申 U7 红线 + 本轮调研加强）：不加自由抽取关键词字段（无召回增益）、
不做 embedding/向量库、投影重建不产生 materialized 事件。

## 6. 排期（更新 2026-07-07）

§4 附注词表已实施（用户授权）：note_key_map.json 95 键 + 三级匹配（剥编号→精确→
别名→最长名包含），builder 在定期报告类文档对 title/末级标题派生 note key 并入
semantic_keys（scalar 在规则未命中时回落 note key）。实测：附注双 NULL 95→37
（余 37 为"其他说明/明细情况"类无科目语义标题，按设计兜底不强配），全库 keyed
36%→56.5%。规则包 ub-2026.07-11。06R 投影按 §5 表待立项。

## 6.1 原排期建议（存档）

1. **S**：semantic_key 附注词表（note_key_map.json + 三级匹配 + 别名表）——
   独立于 06R，先把 A4 的 85% NULL 补掉；规则包升版 + 5 秒重建即可验证。
2. **M**：06R 投影层按 §5 表实施（需要先拍板：zhparser vs pg_jieba，是否加 pg_bigm）。
3. 我的顺序建议：词表先行（L2 立刻可用受控路由），投影随 06R 正式立项。
