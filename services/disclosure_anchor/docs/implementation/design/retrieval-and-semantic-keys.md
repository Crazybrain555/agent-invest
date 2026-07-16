---
id: disclosure_anchor_design_retrieval_semantic_keys
project: disclosure_anchor
title: 非 embedding 检索数据面：多级标题、关键词与 semantic_key 附注词表（设计评审）
status: partially-adopted (词表与数组过滤已实施；06R 投影待立项)
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
| A3 | 需要多级标题？ | **有公开 1-4 级 breadcrumb + 最深 title，但深层中间节未全量公开**；06R 需显式投影完整路径/上下文 |
| A4 | semantic_key 明显少了 | **对，是词表覆盖缺口**：附注小节 95/112 双 NULL；且附注标题是法定受控词表，可系统性补齐 |

## 2. 现状取证（file:line + live DB，2026-07-07）

- **多级标题分两个形态**：公开 `heading_path` 是每个 unit 的必填 1-4 级 breadcrumb，
  `title` 保留最深叶子；S2 内部 `structural_path` 保留完整树，供分组和语义键派生，但不入当前
  public contract。历史 379-unit 快照的 379/379 只能证明 breadcrumb 非空，不能证明深层中间节
  无损可见；mixed parts 另用 `local_heading` 保留组内相对路径。
- **缺的仍是检索面**：0015 起 public 视图有派生 `heading_path_text`，但它只线性化最多 4 级数组；
  `ix_document_unit_heading_path`（GIN jsonb_path_ops）主要支持结构精确包含，没有对 title + path + payload
  建统一分词/子串/模糊索引。
- **semantic_key 机理**：通用规则读 title + 公开路径末级 + caption/question；附注词表键另沿完整
  `structural_path` 自深向浅继承，所以受控中间节即使超过公开 4 级，仍会进 `semantic_keys`。
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

## 4.5 多级标题/多 title 的决策记录（round10 补记——此前只在 §1/§5 隐含，未显式讨论，用户指正）

**问题**：unit 只有一个 `title` 列，是否需要"多级标题/多个 title"？

**决策：不加多个 title 列，理由如下；但补一个立即可用的检索形态。**

1. 不加 `title_1..title_N` 固定多列：深度可变，`title` 作为最深叶子显示名（或表名/议案名）
   保持单值是对的。但原“数据早已完整”的表述经全语料复核已修正：公开 `heading_path` 只投影
   前 4 级，`title` 只补最深一级，两者之间的深层中间节可能仅存在 build-time
   `structural_path`。mixed parts 的 `local_heading` 可保留组内相对路径，但不能等价为全文档完整路径列。
2. 用户真正缺的是**可检索形态**：heading_path 是 jsonb，只能整串精确匹配、没法关键词
   grep。0015 起 `document_units_v1` 新增派生列 **`heading_path_text`**
   （"第八节 财务报告 > 七、合并财务报表项目注释 > 77、现金流量表补充资料"）——
   视图内派生、不入库、不进哈希；今天就能 `LIKE '%现金流量%'`。06R 必须再线性化
   title + breadcrumb + payload，并显式解决完整路径投影，不能只对这个有损 breadcrumb 建 FTS。
3. 若 06R 后仍有"按层级过滤"需求（只查第 2 级为 X 的单元），heading_path 的 jsonb
   下标访问（`heading_path->>1`）已经支持，无需 schema 变更。

## 4.6 三 facet 标签架构（round12 调研定案）

业界共识（RavenPack/AlphaSense/SmarTag/8-K/DuEE-fin 全部如此）：**facet 分立，不混词表**。
本服务三 facet：

| facet | 字段 | 词表 | 规模 | 回答的问题 |
|---|---|---|---|---|
| 主题（文档级） | document.disclosure_topics | topic_map.json | 12 | 这份公告属于哪类事务 |
| 章节（单元级） | semantic_keys 中的 section 键 | note_key_map.json | 173 | 这个单元是报告的哪个标准部位 |
| **事件（文档级→单元）** | semantic_keys 中的 event 键 | event_key_map.json | 35 | **发生了什么公司行为** |

事件词表 = DuEE-fin(13) ∪ CCKS篇章级(9) ∪ FewFC(10) ∪ CFinDEE(22) 并集去重，
并补入交易所标题可直接验证的经营数据、业绩预告、业绩快报、关联交易四类（当前 35 键），
对标 SEC 8-K item 的监管锚定思路；方向成对键分立（增持/减持——投资检索里方向即查询词）；
从公告标题派生（标题是交易所格式指引规范文本），并入该文档全部单元 semantic_keys。

调研留给后续的三项（记 09 背账，暂不做）：①173 个 section 键之上加 2 级父分类
（CFinDEE 6 类/FiQA 4×27 模式，>20 键后层级利于查询放宽）；②每键 salience 分
（SmarTag 模式，排序用）；③查询侧同义词映射表（06R 配套）。

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
| semantic_keys | 已有列（§4 扩容后） | GIN(jsonb_ops) 的 `? / ?| / ?&` 精确过滤 + 并入 tsv | D |
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

## 6.2 2026-07-15 真实语料复核与两层上下文决策

- ub-2026.07-26 起，新 builder 产物的 `semantic_key` 与 `semantic_keys` 都非空：优先保留
  规则/词表/事件键，无更窄受控概念时使用真实通用键 `document_content`（scalar 与数组均
  写入）。它只表达“这是可检索的文档证据”，不假装知道具体主题，也不等同 `unknown`；
  数据库列继续可空仅为历史 run 兼容。Filing API 的 `semantic_key` 是单值兼容参数，会同时匹配
  scalar 列或 `semantic_keys` 数组；集合查询使用 comma-list 的 `semantic_keys_any/all`。因此消费者
  不会因空路由状态漏掉普通证据；这仍是结构化 facet
  过滤，不是 06R 全文检索投影。
- 以下 1,371 份统计来自 ub-2026.07-48 的历史离线重放，长度是展开表格后的**字符启发式**，
  不是当前 ub-2026.07-53 的 token 计数或 prompt 上限；v53 将跨页表保持 aggregate，具体数字
  可随重解析变化，但双峰与两层决策不依赖这些精确计数。分层到全量的重放表明，原“最浅且
  子树不超过 8k”会跨显式业务兄弟合并，且
  边界随年度字数跳变。1,371 份 active NormalizedIR 中，1,292 份（94.24%）的可检索正文
  不超过 40,000 **字符**；短文档最大 39,821，长文档最小 103,354，中间没有样本。79 份
  长文档全是年报/半年报，最大 388,948 字符。这是当前 corpus 的双峰形态，不是通用
  tokenizer 阈值。
- 按这个形态，L2 可用四档组包：1,286 份短文档且无超长表，在实际 token 预算允许时整篇
  带入；6 份短文档含超长表，整篇上下文之外为表格建行窗口；37 份长文档按受控章节组包；
  42 份长文档同时按章节与表格行窗口组包。全量产物中 101 个超过 8k 字符的原子 unit 全是
  表格；它们保留完整 table parent 作证据，发现层另建 1.5–3k **实际模型 tokens** 的连续行窗口，
  每窗口重复 caption/表头/标题路径，重叠 1–2 个逻辑行，不拆 merged logical row。
- L2 的硬约束不应写成固定“40k/50k”：必须用最终模型的 tokenizer 对渲染后的 prompt +
  sources 计数，预留输出和工具开销后才判断是否整篇。长文档的 section pack 建议目标为
  12–20k 实际 tokens；多文档联合抽取共享同一总预算。如果某个模型预留后确有 50k
  source-token 预算，短文档可作一个 L2 context pack；这仍不是 L1 切分阈值。
- 这就是“两层”取舍：L1 细 unit 服务精确召回、去重、引用和局部重算；whole-document /
  section/table context pack 服务 L2 联合抽取。检索较弱时在预算内直接带整篇，检索较强时用
  seed + 同父节/前后邻居；不在“永远细切”和“50k 以下永不切”之间二选一。
- 公开 `heading_path` 仍是最多 4 级的 breadcrumb，`title` 是最深叶子；build 时的完整
  `structural_path` 会参与分组和受控语义键派生，但当前不是公开列。因此 06R/L2 不能把
  `heading_path_text` 当成无损全路径：索引文本必须同时线性化 title + breadcrumb + payload，并保留
  整文/section parent 作弱检索兜底。受控中间节仍能通过 `semantic_keys` 找到；未受控的深层任意
  标题若要单独全文搜索，应在 06R 显式增加完整路径投影，不应反向扭曲 L1 切分。
- 06R 仍未立项，L1 API 不新增 `/v1/search/units`、embedding 或 search projection；自然语言
  发现/排序应由 L2 自有检索面或未来正式 06R 承担，证据引用始终回到 L1 asset_id。
