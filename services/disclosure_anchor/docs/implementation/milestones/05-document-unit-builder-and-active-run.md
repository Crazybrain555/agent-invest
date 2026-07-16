---
id: disclosure_anchor_milestone_05_document-unit-builder-and-active-run
project: disclosure_anchor
title: document_unit builder 与 active run
status: complete
created_at: 2026-06-26
updated_at: 2026-07-07
depends_on: milestone 04R（必须先完成）
delivers_to: milestone 06
---

# Milestone 05: document_unit builder 与 active run

从 NormalizedIR v2 生成 L2-ready `document_unit`，完成载体规范化（carrier normalization，
顶层协议 §3.5）、质量标记、unit snapshot、active run 原子发布和 change_event。
本文件是实施级规格：算法、事务边界、哈希定义、事件语义全部定死，实施 agent 不另做设计决策。

## 1. 前置依赖（04R 交付，开工前核对）

- NormalizedIR **v2**：parser 中立 `kind ∈ {text, heading, table, image, equation,
  page_furniture, unknown}`、`heading_level`、结构化 `table: {headers, rows, merged_cells?}`（04R-R5）；
- `ops.outbox_event` 的 `change_kind`/`subject_kind`/`subject_ref` 列 + 事件工厂（04R-R1/R3）；
- `document.status` = public availability 枚举 + `processing_run.unit_build_status`（04R-D4/B1）；
- `document_unit.query_projection_hash` 列、0007 索引与视图信封列；
- 既有不变量：`uq_processing_run_one_active_per_document`、`uq_document_unit_run_order`、
  `document_units_relpath` 全链、`write_jsonl_atomic`。

## 2. 本 milestone 锁定的契约决策

- **U1 同 run 构建（04R-D8）**：builder 作用于一个 `status='succeeded'` 的 parse run，
  unit 挂在同一 `processing_run_id` 下；`run_kind='rebuild_units'` 已于 2026-07-06 实现（用户裁决：规则迭代不重跑 MinerU）——`use_cases/rebuild_units.py` 复制最近 succeeded parse run 的解析出处与 artifact 引用生成 succeeded 重切 run，CLI `rebuild-units` 串 build+publish，实测全语料重切 5 秒（对比全解析 ~40 分钟）。
- **U2 三哈希分层（unit 级；实现为 domain service `domain/services/unit_hashing.py`；
  canonical_json = `json.dumps(sort_keys=True, ensure_ascii=False,
  separators=(",", ":"))`）**。跨进程稳定性测试形态定死：新建
  `tests/fixtures/unit_hashing/golden_hashes.json`——固定 4 组输入（text/table/qa/mixed 各一，
  含中文、历史 nullable semantic_key 兼容例及 mixed part 注解）与期望三哈希 hex，`tests/unit/test_unit_hashing.py`
  重算断言相等（期望值固化在文件里即保证跨进程/跨版本稳定）：

```text
content_hash          = "sha256:" + sha256(canonical_json({payload_kind, content_payload}))
                        纯内容身份。不含 title/heading_path/order_index/semantic_key(s)/
                        applicability/quality/artifact_locator；mixed 另排除 semantic_type、part order、
                        local_heading/heading_path/applicability/quality_status/artifact_locator 等规则与位置注解
query_projection_hash = "sha256:" + sha256(canonical_json({payload_kind, title,
                        heading_path, semantic_key, semantic_keys, quality_status,
                        applicability[, mixed_part_annotations]}))
                        public 查询投影身份：这些字段不是内容，但 L2 按它们检索/路由，
                        规则升级改变投影时必须可产生事件（落 document_unit 列，0007）
structure_hash        = "sha256:" + sha256(canonical_json({payload_kind, heading_path,
                        order_index}))
                        文档内结构位置身份，仅结构变化 → observed
```

- **U3 聚合哈希（run 级）**：
  `run.content_hash_aggregate = "sha256:" + sha256("\n".join(sorted(所有 unit content_hash，含重复)))`
  （排序但保留重复计数 → 与顺序无关、对 multiset 敏感）；
  `run.structure_hash = "sha256:" + sha256("\n".join(按 order_index 排列的 unit structure_hash))`。
  判"内容没变"只看 `content_hash_aggregate`，**绝不用 normalized_ir 的 artifact_hash**
  （IR 顶层含 created_at，每次运行必变）。
- **U4 asset_id 生成**：`du_` + ULID（`ids.new_asset_id()`），系统生成、不编码业务含义；
  跨 run 不承诺同 ID（身份由 content_hash 表达）。
- **U5 事件模型：multiset + 稳定配对 diff（发布事务内产出，全部走事件工厂；
  公告内容会重复出现，set diff 会丢重复项，必须按 multiset 计数）**：

```text
第一层 multiset：old/new 各按 key=(payload_kind, content_hash) 计数（Counter）
第二层稳定配对：同 key 内先按相同 query_projection_hash 做 multiset 精确配对；剩余 old/new
  再各按 (order_index, asset_id) 排序后逐个配对，避免重复内容仅因位置漂移产生假投影事件
  未配对 new   → document_unit_created（materialized）
                 payload {new_asset_id, new_processing_run_id, content_hash, payload_kind,
                          new_order_index, new_heading_path}
  未配对 old   → document_unit_removed（materialized）
                 payload {old_asset_id, old_processing_run_id, content_hash, payload_kind,
                          old_order_index, old_heading_path}
                 —— 必须携带 old_asset_id：L2 持有的是旧 asset_id，只给 content_hash 无法撤销
  配对且 query_projection_hash 不同
               → document_unit_projection_changed（materialized）
                 payload {old_asset_id, new_asset_id, content_hash,
                          old/new_query_projection_hash, changed_fields[]}
                 changed_fields 词表固定 = {title, heading_path, semantic_key, semantic_keys,
                 quality_status, applicability, mixed_part_annotations} 的子集，逐字段比较旧/新值得出（payload_kind 在配对
                 key 中不可能变）
  配对且投影相同 → 不发事件（内容与投影都没变；旧 asset_id 永远可解析，L2 引用不失效）
processing_run_published  每次发布 1 条：
  materialized ⇔ content_hash_aggregate 变化（或首发）；否则 observed（协议 §2.8）
  仅结构变化（structure_hash 变、content/projection 均同）→ observed，不发 unit 级事件
subject_kind/subject_ref 定死：created→(document_unit, new_asset_id)；
  removed→(document_unit, old_asset_id)；projection_changed→(document_unit, new_asset_id)；
  published→(processing_run, processing_run_id)
processing_run_published payload = {previous_processing_run_id|null, content_hash_aggregate,
  structure_hash, unit_count, created_count, removed_count, projection_changed_count
  [, allow_empty_reason]}
事件写入顺序固定（seq 即消费序）：removed（按 old order_index）→ created（按 new
  order_index）→ projection_changed（按 new order_index）→ 最后 processing_run_published
```

  首次发布：全部 unit 走 document_unit_created。旧 quality_status_changed 事件并入
  projection_changed（quality 是投影字段之一，changed_fields 标明）。
- **U6 builder 规则版本可归因（协议 §2.6 rule_bundle_ref）**：新迁移
  `0008_unit_builder_provenance`：`processing_run` 加 `builder_rules_version varchar(32)`，
  `processing_runs_v1` 跟进。builder 规则表（噪声/保留/切分/semantic_key）集中在
  `adapters/unit_builder/rules.py`，模块级 `RULES_VERSION = "ub-2026.07-1"`，规则变更必须升版。
  列值语义定死：`processing_run.builder_rules_version` 恒等于 `rules.RULES_VERSION` 字符串本身；
  `cn_a_v1` 只是 rules.py 内 `HEADING_RULESET_ID` 常量，**不拼接进列值**；变更任何规则
  （含 heading ruleset）必须升 RULES_VERSION。0008 迁移规格定死：新建
  `src/…/migrations/versions/0008_unit_builder_provenance.py`，
  revision="0008_unit_builder_provenance"、down_revision="0007_envelope_and_feed_hardening"；
  upgrade = ALTER TABLE core.processing_run ADD COLUMN builder_rules_version varchar(32) NULL
  （历史 run 保持 NULL，unit_build 成功时回写）+ processing_runs_v1 drop+create 在 0007 形状
  末尾追加该列；downgrade = 还原 0007 视图形状后 DROP COLUMN；往返核验按 04R §6.2 同款命令
  （upgrade head → downgrade 0007 → upgrade head，真库与临时库各一遍，权限测试全绿）。
- **U7 retrieval/search projection 边界（概念定案，实现留给 06R）**：`document_unit` 是
  证据锚点（durable semantic retrieval anchor：可追溯、不可变、按业务结构切分）；检索发现层
  是其上的**派生投影**，不是新的核心对象——字段族为 heading_path_text / display_subtitle /
  search_text / controlled_keywords / extractive_keywords /（后置）LLM summary。硬边界：
  投影不进 content_hash 与 query_projection_hash、不替代 payload、不作为证据或 claim、
  不新增 chunk / table_cell / embedding 核心对象。投影全部可由已持久化数据
  （payload + title + heading_path + semantic_key + document 元数据）确定性再生，因此
  **本 milestone 不在 build 时生成 projection artifact**：检索规则（retrieval_rules_version）
  会独立于切分规则演进，耦合进 builder 会把可再生的发现层写进不可变 run 快照、放大
  builder_rules_version churn。06R 以派生层（视图或独立投影作业）+ PostgreSQL FTS/pg_trgm
  实现 `GET /v1/search/units`；LLM summary/keywords 若引入，必须版本化记录
  model/prompt/hash，且其重建只触发投影更新，**不得产生 materialized 事件或 L3 fact
  invalidation**（acceptance A38–A40）。

## 3. Builder 流水线（`application/use_cases/build_units.py`）

输入：`document_id`（找其最新 succeeded parse run）或显式 `processing_run_id`。
前置校验错误码闭集（沿用 04R 结构化错误 JSON 形状，stage='build_units'，retryable=false）：
`RUN_NOT_FOUND`（run 不存在）/ `RUN_NOT_SUCCEEDED` / `IR_MISSING`（normalized_ir_relpath
缺失或不可读）/ `IR_HASH_MISMATCH`（derived IR 与 run 记录的 artifact hash 不一致）/
`IR_CONTRACT_TOO_OLD`（contract_version 非 normalized_ir.v2，指引重新 parse）/
`IR_TABLE_RECONCILIATION_INVALID`（聚合表 locator 诊断、元素或跨对象计数不可能）/
`IR_TABLE_RECONCILIATION_ALGORITHM_MISMATCH`（聚合表 locator 算法版本不兼容，必须重新 parse）/
`UNITS_ALREADY_BUILT`（该 run 已有 unit——unit 不可变，重建走新 run）。

按序七个阶段（每阶段独立纯函数，输入输出可单测）：

**S1 噪声抑制与非文本元素处置**：丢弃 `kind='page_furniture'`。文本清理封闭为两条规则：
剥离 Unicode 类别 Cc 字符（\n\t 除外）；整行匹配 `^[\s\-—―=_·•\*~～]{3,}$` 的纯分隔线行删除。
水印首版不设独立规则（页级重复已由 page_furniture 承担），rules.py 留 `NOISE_LINE_PATTERNS`
空扩展位。**任何丢弃都进 build 统计（按 kind 分桶计数），不允许静默消失**。
`image`：不无条件丢弃——判定"有语境"= caption 非空，或紧邻标题（该元素之前最近的非
page_furniture 元素是 kind='heading' 且同页）；有语境（股权结构图、组织架构图等实质图）
→ 生成 `text` unit，payload `{"image_ref": ..., "caption": ..., "context": 邻近标题}`、
quality_status='needs_review'。**image_ref 存跨 run 稳定的内容寻址图片名**（MinerU 输出的
images/<sha256>.jpg 文件名；若非哈希命名则以图片 bytes 的 sha256 自算），**绝不存
artifact_locator**——run 级路径进 payload 会使 content_hash 每次重解析必变，击穿"内容未变
→ 0 unit 事件"；run 内定位只放 unit.artifact_locator 列（U2 已排除在 content_hash 外）。
无语境的装饰图 → 丢弃入统计。
`equation`：并入所在 text 流（MinerU 输出 latex 文本）。
`unknown`（mapper 未映射的 raw type，raw_kind 原样保留）：有可读文本 → 并入所在 text 流并置
quality_status='needs_review'；无可读内容 → 丢弃入统计（按 raw_kind 分桶，便于发现 parser
升级引入的新类型，统计键 dropped_unknown_by_raw_kind）。页眉重复出现的「重要提示」四字
属 page_furniture 噪声，不因含关键词而每页生成 unit（有反向测试）。

**S2 heading tree**：对 `kind='heading'` 与文本形态像标题的 `kind='text'` 元素建层级：

```text
主信号（正则，按优先级给层级）：
  L1: ^第[一二三四五六七八九十百]+[节章]        （第八节 财务报告）
  L2: ^[一二三四五六七八九十]+、               （一、主营业务分析）
  L3: ^（[一二三四五六七八九十]+）              （（一）收入构成）
  L4: ^\d+([.、．]|\s)                         （1. / 1、）
  L5: ^[①②③④⑤⑥⑦⑧⑨⑩]                        （带圈编号，最低层级）
kind='text' 的标题候选资格（三条同时满足，kind='heading' 元素不受限）：
         单行（不含换行）、去首尾空白后 ≤ 40 字符、不以 。；，, 结尾
         ——防"一、"开头的整段正文被误判成标题
固定板块词直判（无编号也是标题，层级 = L1）：重要提示 / 释义 / 目录 / 备查文件
辅助信号：IR heading_level——对 `kind='heading'` 元素（parser 已判定为标题）**允许在正则
         未命中时单独定级**（clamp 1..5；2026-07-05 评审修订：否则无编号标题如"公司简介"
         会整体降级为 text、丢失真实结构）；对 `kind='text'` 元素仍禁止单独定级
         （必须正则命中 + 候选资格三条件）
排除规则：以？/?结尾、或匹配 qa 起始模式（见 S4）的行绝不是标题——
         防 Phase00 的"问句累积成 heading_path"回归（有专门回归测试）
深度上限 4；入树时若将处于第 5 层（heading_path 长度将超 4），该元素按普通 text 处理，
         不建层级不进 heading_path（L5 圈号只在当前深度 ≤3 时成为下一层）；
heading_path = 祖先标题原文列表（保留编号前缀，与 golden fixtures 一致）
规则集按市场/语言版本化（E8）：本期实现 `heading_rules=cn_a_v1`，作为 builder_rules_version
的组成部分；hk/us 文档接入时新增 ruleset，不改 cn_a_v1
```

**S3 text 切分（service-purpose §8.1 优先级）**：同一最深标题下的连续 text 元素合并为一个
`text` unit；显式编号条目多且长时按条目拆分；长而无内部结构的小节保持单 unit（§8.2，
不做字符数/token 切分）。`title` = 最近标题文本。

**S4 qa builder**（qa 识别对全部 filing_type 运行，但边界强度分层）：投关记录/业绩说明会
允许“编号问句 + 无显式答标签的连续正文”；其他 filing_type 必须同时出现明确 `答/回复/A:`
才生成 QA，避免把年报里的审计职责、声明或判断条件误拆成问答。原“投关/说明会触发”还限定
S2 的 qa_heading_mode——该模式下编号标题不入 heading 树，防问句累积成 heading_path；所有
模式仍使用下述稳定性判据与 needs_review 兜底：

```text
问题起始：^\s*(问题|问|Q\d*|投资者提问|提问)\s*\d*\s*[：:] 或
          ^\s*\d+[、.．]\s*.{2,}[？?]\s*$（编号 + 问号结尾）
回答起始：^\s*(答|回复|公司回复|A\d*)\s*[：:]
边界规则：回答 = 回答起始行（或问题行之后）到下一问题起始之间的全部文本
来源：text 元素序列 + 结构化表格 cell 内的多行文本（ir_activity 样本的问答嵌在
      "投资者关系活动主要内容介绍" cell 里——按行拆后套同一规则）
产出：每个 Q&A 对一个 qa unit，payload = {question, answer, raw_text}
      （与 golden fixtures 契约一致）
前缀/尾段保全：首个问题前的表单元数据、会议说明或上一问尾段单独保留为 text，不阻断其后
完整 QA；若至少已有一个完整 QA，块尾因分页/跨表只剩“有问无答”的截断问题，则保留已闭合
QA，并把尾段单独存为 text + needs_review。结构化表格本体始终保留，QA 只是其检索投影。
"边界不稳"闭合判据（命中任一 → 除上述可证明的块尾截断外，整块存 text + needs_review）：
  (a) 出现回答起始行但其前无未配对的问题起始；
  (b) 问题起始后到下一问题起始无任何非空文本，或首个/唯一问题到块尾仍有问无答；
  (c) 同一问题区间内出现 ≥2 个回答起始行
全部模式用 re.match，不启用 IGNORECASE（Q/A 仅匹配大写）
```

**S5 table builder**：一个 IR table 元素 → 一个 `table` unit。payload：

```text
{
  "caption":  table_caption 列表原样,
  "unit":     从 caption / 表头 / 前一文本元素按规则识别（"单位：元/万元/千元"），识别不到为 null,
  "headers":  IR 结构化 headers,
  "rows":     IR 结构化 rows,
  "notes":    table_footnote 列表原样
}
title = caption 首项或最近标题。**payload.headers 的来源（表头提升规则，04R-R5.2 定死
IR headers 只含 `<th>` 证据、MinerU 下通常为空）**：跨页合并**完成后**，IR headers 非空
（th 证据）→ 直接采用；为空 → 将合并后网格的首行提升为 payload.headers（先合并后提升，
防续页首行被错标；KV 形态首行被提升属可接受粗糙，数据完整保留在 payload）。
merged_cells 保留 row_span/col_span；空单元格、"-"、"—"、"不适用" 原样区分不归一。
单位说明与脚注**绝不作为噪声丢弃**（脚注常含追溯调整/会计政策，红线）。
IR 带 table_parse_failed → payload 落 {caption, raw_html, notes} 并
quality_status='needs_review'。跨页表合并：相邻 table 元素间无非噪声元素、列数相同、
后表无独立 caption → 合并 rows，**识别并删除续页重复表头行**（与首表首行——即待提升的
表头候选行——逐 cell str.strip() 相等的行不得当数据行），payload 记
`merge_reason='continued_table'` 与 page span（artifact_locator）；合并失败标 needs_review，
不阻塞其他 unit（不确定即不合并）。"列数相同"定死：列数 = len(rows[0])（IR headers 非空时
= len(headers)）；前后表该值相等才允许合并。**空表元素在合并判定中视为噪声**（headers 与
rows 均空且 table_html 为空——2026-07-05 实测这是 MinerU 在续页页首表头带产出的无内容碎片，
真实年报 75/473、审计报告 55/256）：跳过它、不阻断其前后真实表段的相邻性，计入 build 统计
（dropped_by_kind.table_empty），不生成 unit。
```

**S6 保留/跳过（service-purpose §9 + 红线）**：规则表驱动，首版跳过规则封闭定死：
`SKIP_SECTION_TITLES = {"释义", "目录", "备查文件"}`（标题去空白后精确匹配，板块整体跳过）；
空表 = rows 为空或全部 cell strip 后为空；封面/签章/页码类已由 page_furniture 通道处理；
service-purpose §9.2 其余类目首版**一律保留**（拿不准 → 保留），扩充跳过词表必须升
RULES_VERSION。**「重要提示」「风险提示」标题的板块必须生成 unit**，不得按标签跳过
（协议 §3.5，有专门测试）。跳过项记入 build 统计（S8 报告），不写 DB。

**S7 semantic_key + quality_status（首版历史规则；由 §8.5 ub-2026.07-26 取代）**：
semantic_key 由规则表给出（首版未命中 = null，禁止自由发明；当前未命中使用真实通用键
`document_content`，新产物 scalar/array 均非空）。首版规则表定死
（rules.py `SEMANTIC_KEY_RULES`，按序首个命中；匹配对象 = title +
heading_path 末两级 + table caption 首项，qa 另加 question）：

```text
receivable_aging       含"应收账款" 且 含"账龄|坏账"
inventory_breakdown    含"存货" 且 含"分类|构成|跌价"
goodwill_impairment    含"商誉"
revenue_breakdown      含"分行业|分产品|分地区|营业收入构成"
guarantee              含"担保"
related_party          含"关联交易|关联方"
shareholder_structure  含"股东|股本|股份变动" 且 含"结构|变动|情况|前10名|前十名"
tariff_exposure        含"关税"（不限 filing_type）
前 7 项限 filing_type ∈ {annual_report, semiannual_report, quarterly_report, inquiry_reply}
```

quality_status：结构完整 → ok；表解析失败/QA 边界不稳/跨页合并失败 → needs_review；
unusable 判据定死：主文本（text 取 payload["text"]；qa 取 question+answer；table 取全部
cell 拼接）去空白后为空 → unusable；(Unicode 类别 C*（\n\t\r 除外）+ U+FFFD) 字符数 /
总字符数 > 0.30 → unusable（常量 `GIBBERISH_RATIO_MAX = 0.30` 定义在 rules.py）。

**S8 快照与落库（FS 与 DB 不是一个原子事务，顺序与失败策略定死，B9）**：

```text
1. 内存完成 S1–S7，计算全部哈希
2. 快照写临时路径 → fsync → 原子 rename 到 document_units_snapshot_relpath →
   校验（基准定死）：重新打开快照文件，行数 == len(units) 且重算 sha256 ==
   ArtifactWriteResult.artifact_hash；任一不符按 ARTIFACT_WRITE_FAILED 处理。
   快照行顶层键集 = {applicability, artifact_locator, asset_id, content_hash, document_id,
   heading_path, order_index, page_no, payload, payload_kind, quality_status, semantic_key,
   semantic_keys, title}
   （structure_hash/query_projection_hash 只在 DB 列，不进快照）。
   build 统计落点定死：ArtifactStore.write_json_atomic 写快照同目录 `build_stats.v1.json`；
   顶层键集由 `BuildStats.as_dict()` 与 fixture contract 守护（当前 33 键，覆盖生成/丢弃、标题与
   表格恢复、native-text/QA 恢复、公告号合并去重、needs_review/unusable），
   CLI 同时打印该 JSON 到 stdout；不加 DB 列
3. 开 DB 事务：DocumentUnitRepository.add_many（新增仓储方法，含 list_by_processing_run、
   list_by_document_active）；order_index 从 1 开始步长 1 全文档递增（与 golden fixtures
   一致）；回写 run 的 document_units_relpath / content_hash_aggregate / structure_hash /
   builder_rules_version / unit_build_status='succeeded' / unit_built_at；commit
失败策略：
  FS 写入/rename 失败 → 不碰 DB；unit_build_status='failed'，
    unit_build_error={error_code: ARTIFACT_WRITE_FAILED, ...}，attempt_count+1
  FS 成功后 DB 失败 → DB 回滚；快照成为合法 orphan（doctor 深检报告，可清理/复用），
    unit_build_status='failed' 记录于重试路径
  DB 成功但快照缺失 → 严重一致性错误，doctor FAIL
```

build 失败不改 document.status（保持 parsed/published），重试由 worker 按
unit_build_attempt_count 门槛驱动（08）。

## 4. 发布事务（`application/use_cases/publish_run.py`；对应 04R §1 第 5 条
"失败 run 不扰动 active run"的既有检查点，发布必须是单事务原子操作）

单 UoW 事务，顺序固定：

```text
1. SELECT document FOR UPDATE（新增 DocumentRepository.get_for_update）
2. 读上一 active run 全部 unit 的**完整行**
   （DocumentUnitRepository.list_by_processing_run(document.current_processing_run_id)——
   U5 配对 diff 与 changed_fields 逐字段比较需要 title/semantic_key/quality_status）
3. 旧 active run：is_active=false（先 flush——partial unique index 要求先清后置）
4. 新 run：is_active=true
5. document.current_processing_run_id = 新 run；document.status = 'published'
6. 按 U5 计算 diff，经事件工厂写全部 outbox 事件（processing_run_published + unit 级）
7. commit。任何一步失败 → 整体回滚，旧 active run 不变（既有检查点保持）
```

前置校验：run 属于该 document、`status='succeeded'`、`unit_build_status='succeeded'`
（空 run 拒绝发布，除非显式 `allow_empty=true`——原因写入 processing_run_published 事件
payload 的 allow_empty_reason 字段）。幂等判据定死：run.is_active 为 true 且
document.current_processing_run_id == 该 run id → 直接返回已发布结果、不写任何事件
（集成测试断言二次 publish 后 ops.outbox_event 行数不变）。
发布是 document.status 进入/保持 `published` 的唯一途径（04R-D4）。

## 5. CLI 与 Make 入口

`src/disclosure_anchor/cli/pipeline.py` 为**本 milestone 新建模块**（仓库现只有 cli/db.py 与
cli/doctor.py）；下列 make 目标均为 Makefile 新增：

```bash
python -m disclosure_anchor.cli.pipeline register --file <pdf> --provider cninfo \
  --security-code <code> --exchange <szse|sse> --filing-type <D7 词表值> \
  --title <str> --announcement-date <YYYY-MM-DD> \
  [--report-period 2025A] [--provider-document-id <id>]   # 薄封装 RegisterLocalPdf
python -m disclosure_anchor.cli.pipeline parse --document-id <id>          # 触发 ParseDocument
python -m disclosure_anchor.cli.pipeline build-units --document-id <id>    # S1–S8
python -m disclosure_anchor.cli.pipeline publish --processing-run-id <id> \
  [--allow-empty --reason "<text>"]                        # §4（--allow-empty 时 --reason 必填）
python -m disclosure_anchor.cli.pipeline process --document-id <id>        # parse→build→publish 串行
make register FILE=<pdf> … / make build-units DOC=<id> / make publish RUN=<id> / make process DOC=<id>
```

process 是 08 worker 的单文档执行单元。三个验收样本（tmp/sample_filings 下，与 Phase00
三类样本一致）register 参数表定死（provider 均为 cninfo，provider_document_id = 文件名去后缀，
announcement_date 取文件名/文首日期）：

```text
002484 2025年年度报告            security_code=002484 exchange=szse
                                 filing_type=annual_report  report_period=2025A
000333 投资者关系活动记录表       security_code=000333 exchange=szse
                                 filing_type=investor_relations  report_period 省略
002484 股票交易异常波动公告       security_code=002484 exchange=szse
                                 filing_type=other  report_period 省略
```

## 6. 检查点

- 年报样本（002484 2025A）：可取经营分析 `text` unit；应收账款账龄 `table` unit 的
  headers/rows/unit 完整，semantic_key='receivable_aging'。
- 投关样本（000333）：≥30 个 qa unit，question/answer 绑定正确（对照 golden fixtures）。
- payload 存快照本身的断言定死：任取新发布 run 的一个 asset_id，core.document_unit.payload
  与快照 jsonl 同 asset_id 行的 payload JSON 相等（json.loads 后 ==），且 payload 内不含指向
  快照文件的引用字段；heading_path 无问句污染（回归测试）。
- 重跑/改内容两条的操作程序定死（集成测试实现，不经 MinerU）：为同一 document 直接构造两个
  status='succeeded' 的 run，分别写入 IR fixture A 与 fixture B（B 仅修改一个 text 元素的
  文本），各自 build+publish 后断言事件；"内容未变"场景 = 两个 run 用同一 IR fixture：
  - 内容未变：`processing_run_published` 为 observed，0 条 unit 级事件。
  - 改一个 unit：恰好 1 removed + 1 created（各带 old/new asset_id），其余不动。
- 文档含两个 content_hash 相同的重复 unit、删除其一：恰好 1 removed（multiset 计数正确）。
- 仅规则升级改变 semantic_key/title：只发 projection_changed，content 级事件为 0。
- 已 published 文档重解析失败：document.status='published' 且
  `SELECT count(*) FROM disclosure_public.document_units_v1 WHERE document_id='<id>'`
  与失败前相同（active run 投影仍可读；B1 回归测试）。
- 发布中途失败注入点定死：monkeypatch OutboxRepository.add 使写 processing_run_published 时
  raise（步骤 6 末尾失败）；断言旧 run.is_active 仍 true、current_processing_run_id 与
  status 不变、ops.outbox_event 无新行。
- 「重要提示」板块存在于 unit 中且含退市风险文本（红线测试）；页眉重复「重要提示」不生成
  逐页 unit（反向红线测试）。
- 带 caption 的股权结构图生成 needs_review 文本壳 unit；表格脚注含「追溯调整」被保留。
- 跨页表续页重复表头被剔除、merge_reason 记录正确。
- 0008 迁移真库 + 临时库往返通过；`document_units_v1` 行可按 15 scope keys 检索。

## 7. 测试要求

单测：S1–S7 每阶段纯函数测试（heading 排除与 cn_a_v1 规则、qa 中文模式边界、跨页合并与
重复表头、U2 三哈希稳定性与字段归属、semantic_key 规则表、红线保留与反向保留、image 文本壳）；
U5 multiset 配对 diff（重复 hash、仅投影变化、仅结构变化三分支）；事件 subject_kind/change_kind。
集成（DB-gated）：build→publish 全链、幂等重发布、发布失败回滚、事件可从 change_events_v1
读回且 subject_ref/change_kind 正确（期望值 = U5 定死的取值表）。
契约（键集断言定死，防两个真相源打架）：快照每行**顶层键集** == S8 步骤 2 的 14 键集合
（与 phase00 fixture 顶层一致）；**payload 内层键集按 kind 断言**：text={text}（image 壳=
{image_ref, caption, context}）、qa={question, answer, raw_text}、table={caption, unit,
headers, rows, notes}（table_parse_failed 时={caption, raw_html, notes}）；mixed 至少为
{semantic_type, parts}，parts 保留有序 kind/content 与局部标题/适用性/质量注解。
phase00 fixtures 的 payload 内层是 v1 历史形态（{format,page_no,text} 等），
**不作为 05 payload 契约来源**，payload 内层不与 fixture 比较。

## 8. Definition of Done

- 三个本地样本 raw → parse → build → publish 全链跑通，§6 检查点全过；
- `make test` no-DB 与 live-DB 双绿；acceptance-matrix A19/A20/A21 置 pass。

## 8.5 实施后修订（2026-07-06，rule bundle ub-2026.07-2）

用户裁决 + 协议 §3.5 核对后新增两条封闭规则（fixtures 已随规则重生成为真 golden，
再生成脚本 scripts/regen_phase00_fixtures.py 现在同时产出 document_units.v1.jsonl）：

- **封面前言噪音（cover prelude）**：文档存在结构性一级标题（第X节 或 重要提示/释义/目录/
  备查文件）时，首个一级标题之前的 heading/text 元素不生成 unit 也不进标题树，计数入
  build_stats.dropped_cover_prelude（D9：不静默消失）。理由：封面的公司名/标题/日期/代码
  已是 document 元数据（§3.6 继承），unit 重复即纯噪音。短公告无节结构 → 规则不激活，零误伤。
- **适用性声明标志（applicability）**：text unit 内出现"√适用 □不适用"/"□适用 √不适用"
  声明行（勾选框实测含 MinerU 的 U+F052 私有区字形；允许粘连在标题行尾，行尾锚定匹配）时，
  payload 增加可选键 `applicability: applicable|not_applicable`。"不适用"=该节声明豁免，
  是信息不是噪音，保留原文并结构化供 L2 过滤（江海年报实测 38 applicable + 97 not_applicable）。

### ub-2026.07-3（2026-07-06 切分审计追加）

第三方审计（真实江海年报 908 units）发现 21% 的 text 单元是独立成行的"单位：元/股"声明——
该信息 S5 已从元素流提取进表格 payload.unit，text 单元属纯冗余。新规则：S3 输出时丢弃
**整体恰为**单位声明的 text 单元（`^单位[：:]\S{1,8}$`，与正文合并出现则保留），
计数入 build_stats.dropped_unit_declarations。实测 908→712；"无/不适用/□是 否"类
声明照旧保留（有信息量）。

### ub-2026.07-4（2026-07-06 用户数据审查后定案）

用户裁决三条（数据库直查发现）：
1. **applicability 是一等列不是 payload 键**：payload 只放原文；新列
   `document_unit.applicability`（0010 迁移，CHECK + 部分索引，视图/API/契约同步升级），
   并纳入 query_projection_hash 输入（U3 修订）。
2. **√适用 声明行不得独立成 unit**（"用完直接往下走"）：S3 行级剥离——单位声明行一律剥除；
   首行为纯标记行 → 剥除并记 applicability（not_applicable 保留该行为 unit 原文，因为它就是
   该节的全部披露；applicable 且无剩余正文 → 占位下沉，标志落到同节紧随的下一个 unit 上，
   无后继时保留"适用"兜底）；短标签+次行标记的复合单元只打标不改文（原文中心）。
3. 计数：stripped_marker_lines 进 build_stats；实测年报 712 → 688。

同日数据复审追加（同属 ub-2026.07-4）：
4. **applicable 下沉放宽为前缀匹配**：声明后紧随的单元若进入子标题（heading_path 以声明节为
   前缀）仍属该节内容，照常下沉（实测收掉"十六、募集资金"的孤行"适用"；无后继才保留兜底）。
5. **标记行禁止进标题树**：MinerU 偶发给标记行标 text_level>=1，_heading_level_for 对
   纯标记行返回 None（实测年报有 1 处 "□适用 √不适用" 曾成为 heading_path 分量与 title）。
6a. **page_no 列**（用户裁决 2026-07-06）：document_unit 增加 page_no 列（源自 artifact_locator
   的首页码，0010 迁移；视图/API/契约同列），定位与审查的一等筛选参数；不入任何哈希（provenance）。
6b. **category_names 存证**：候选与 document.provider_metadata 增加 category_names
   （adapter 用 p_info3005 解码 F006V 的中文分类名数组；web 通道为 null）——filing_type
   保持 9 类契约粒度，原始分类语义完整可查。
6c. **测试轮数据纪律**（用户指示）：每轮新测试用 `make wipe-test-data WIPE=YES` 全清业务数据
   （DB TRUNCATE + 磁盘 cninfo 归档/derived/quarantine；_phase00 fixture 产物保留），
   不得新老数据混跑；投产前同样全清。历史 run 保留策略（retention）为投产后议题。

6. **公告眉头 KV 行剥离**：`证券/股票/债券/[ABH]股 + 代码/简称：值` 整行剥除
   （值即 document/security 元数据，同"单位：元"逻辑），计数 stripped_header_lines；
   **公告编号行保留**——provider 公告编号是元数据里没有的独有信息。正文中
   "被担保人证券代码"等前缀不同的行不受影响。实测年报 908 → 687（-2 起累计）。

### ub-2026.07-5（2026-07-06 phase008 综合审查后：unit 边界升级为业务语义块）

审查依据 `docs/implementation/reviews/phase008-db-comprehensive-audit-round3.md`（P0#1-#4）；
用户裁决：**payload kind 不决定 unit 边界，一个业务块内 text/table 混合是常态**。新增
`payload_kind='mixed'`（0011 迁移扩 CHECK；payload = semantic_type + 有序 parts，
part 形状复用各 kind payload schema；见 service-purpose §6.5）。S8 语义分组阶段：

1. **议案分组**（P0#1 股东会决议实证）：`\d+.议案名称：` 锚点起一个 proposal unit
   （semantic_type=meeting_proposal），审议结果+表决表格+会议决定同体；正文中段出现的下一项
   议案标题就地切开并重新归属（修复"第 4 项议案挂在第 3 项 heading 下"的结构归属错误）。
2. **短公告坍缩**：可坍缩 filing_type（other/performance_forecast/performance_flash）且
   正文 ≤ SHORT_DOC_CONTENT_CHARS(8000) → 一个 document 级 mixed unit（parts 保留完整
   heading_path）——短公告首先是"一份公告"（P0#1 董事会决议实证）。
3. **长文档业务块分组**：其余文档在"子树内容 ≤ SECTION_GROUP_MAX_CHARS(8000) 的最浅标题
   节点"整体成 unit（semantic_type=section，parts 带 local_heading 局部路径）；超限叶子
   仍整体合并（按 kind 硬拆一个主题=被禁碎片化）；单成员节点保持原 kind 不包壳；qa 单元
   永不并组。单元级 applicability 仅在成员声明一致时置值，冲突为 NULL（parts 承载细节）。
4. **编号行拆分废除**：原"≥3 条全编号行拆成逐条 unit"逻辑删除——编号列表是一个业务块
   （round3 过碎主诉之一）。
5. **封面误删修复**（P0#2）：MinerU 把"第一章 总则"标成 text 时也算首个结构标题
   （复用 heading 门的 text 候选判定），薪酬管理办法第一/二章不再被当封面丢弃。
6. **公告头锚定**（P0#4）：首标题前的眉头残留（公告编号行）挂到合成锚 `公告头信息`，
   不再产生 heading_path=[] 孤儿；全平文档不造假结构。
7. **是/否 checkbox 防线**（P0#3 扩展）：`是 □否` 类 disclosure answer 不是标题也不是
   表名——heading 门与 table caption 双向拦截（is_declaration_line）。
8. **发布投影字段补全**（P1#8）：PROJECTION_FIELDS 补 applicability，
   outbox `document_unit_projection_changed` 的 changed_fields 不再出现空列表审计洞。
9. 配套：0011 迁移同时给 document_units_v1/source_refs_v1 加 is_active_run（P1#7，
   DB 直读方免 join 过滤 active）；0012 provider 分类维表 + document_categories_v1 +
   filing_type_map r3（012001 调研活动→investor_relations，P1#6）。
10. 计数：grouped_proposal_units / grouped_section_units / collapsed_documents /
   anchored_header_units 进 build_stats。

### ub-2026.07-6（2026-07-06 Codex round4 复审后）

复审依据：Codex 对 phase008 round3 修复的独立 DB 终审（no-go 判定的四条 P1，其中
P1#3 "local_heading 字符串化数组" 经 jsonb_typeof 复核为假阳性——`->>` 把数组渲染成
文本后被正则误判，实际 614/614 均为真 JSON 数组，未采纳）。落地三条：

1. **semantic_keys 召回列**（P1#1）：mixed 分组曾吞掉成员的 semantic_key（年报 22 keyed
   → 12）。现在每个 part 计算并携带自己的 semantic_key；单元级新增 `semantic_keys`
   （jsonb 数组 = 自身 key ∪ parts keys，0013 迁移 + GIN 部分索引，视图 36 列 + API/契约
   同步）；纳入 query_projection_hash 与 PROJECTION_FIELDS。单值 semantic_key 保留原语义。
2. **半角括号层级**（P1#2 根因）：MinerU 把审计报告附注标题一律标 heading_level=2，
   半角 `(1)`/`(一)` 不在层级表时落回 level 2 并把科目父节点挤出栈。cn_a_v3：
   L3 增加 `\(一\)` 形态、L5 增加 `\(1\)` 形态。
3. **坍缩单元 title 用登记文档标题**（P1#4）：PDF 内文档名行常被封面剥离，第一章 曾成为
   document 单元 title。builder 增加 document_title 传参（build_units 从 document.title
   注入），坍缩单元 title/heading_path 用它；parts 保留各章 heading_path。
4. 附带（P2#1 部分）：表格纯空行在网格合并后剥除（有 merged_cells 时保守保留，
   计数 dropped_blank_table_rows）；分组标签行治理仍留 P2。

### ub-2026.07-7（2026-07-06 Codex round5 复审 + 用户 PDF 目检授权后）

Codex round5 复现 4 个行级证据（年报 order 142/226、审计报告 order 51/93）；本轮先按用户
指示以投资经理视角肉眼核对样本 PDF（tmp/sample_filings，poppler 渲染 p.110/p.187/审计
p.102）后定案，全部为标题树进栈规则问题，cn_a_v4：

1. **无编号标题就地嵌套**：MinerU 无编号标题（"与回购公司股份相关的会计处理方法"、
   "安全生产费"式子标签）的 heading_level 在真实语料被压平为 2，曾窜到根层并吞掉后继
   兄弟节。现在无编号标题一律嵌到当前栈最深层+1（永不驱逐编号父节）；MinerU 显式
   level 1 的仍信任为顶层（文档题名/重要提示类）。
2. **序号连续性修复**：原文自身编号错乱（年报 p.187 实印"三、（市场风险）"，应为
   （三））导致二级模式驱逐"十二、金融工具风险"。带序号标题若在自身模式层级断序、
   但恰好接续栈上某开放层级的序列（3 接 (一)(二)），则归入该层；序号 1 永远在自身
   层级起新序列。栈条目升级为 (level, title, ordinal)，中文序数解析支持 十/百 位。
3. **[注] 脚注防线**：`[注x]`/`注：` 行是前表脚注，永不成标题（审计报告实证曾升为
   root 级 unit title 并吞掉关联租赁/关键管理人员报酬等兄弟主题）。
4. **表格空行剥离扩展到带 merged_cells 的表**：同步重映射 merged_cells 行索引，
   指向被删行的条目一并去除（round4 只跳过了这类表，Codex round5 复测出 23 行残留）。
5. **责任声明套话剥离（用户授权 2026-07-06）**：固定"（董事会/监事会…）保证…（真实/
   虚假记载）…（重大遗漏/法律责任）"公式句整行剥除，计 dropped_boilerplate_lines；
   正则锚定+关键词齐备，带实质内容的句子不匹配（§3.5 红线）。
6. 已知接受项（用户裁决"一类数据大也没关系"）：大 mixed section（主营业务分析
   25 parts）保持；十二、金融工具风险内部 1、/(一) 层级倒置文档的次级归属不完美
   （修复后不再窜根、保持在 十二 子树内），完美化需字体/样式信息，记 P2。

### ub-2026.07-8（2026-07-06 round6 GO 后的工作流轮）

1. **rebuild_units 快速路径落地**（见 §2 U1）：轻/重测试分层——轻 = agent-check +
   `make rebuild-units`（秒级，规则改动的默认验证路径）；重 = `make process` 全解析
   （仅 parser/MinerU 变更时需要）。
2. **prune-history 测试期工具**：`make prune-history PRUNE=YES` 删除全部非 active
   run + 其 units + 相关 outbox 事件，库中只留当前代（用户裁决：测试期看不清新旧不可接受；
   被删代的 U5 历史回放测试期放弃，投产 retention 另议）。
3. **责任声明主语收网**：round6 发现"本公司及董事会全体成员保证…"变体漏网（及 在 董事会
   之前打败有序 alternation）——主语改为有界字符类，正负五例验证（鉴证报告"管理层的责任
   是提供真实…"等实质责任段不匹配、正确保留）。
4. Codex round6 P2 挂账：42、其他重要会计政策 单成员组保留叶子 title（可改进为组键
   title + local_heading，不阻塞）；structure_status / 分组标签行 / 金融工具风险次级
   层级完美化 维持开放。

### ub-2026.07-9（2026-07-06 round7：新公司 + 默认参数真实用法试用后）

Codex 以"新增公司、默认参数"的预期生产用法测试，对照真实 PDF 确认三个切分缺陷，全部修复：

1. **第二种议案锚点**（`PROPOSAL_APPROVAL_RE`）：平安银行董事会决议用"一、审议通过了
   《…议案》"句式，原 `议案名称：` 锚点（`PROPOSAL_ANCHOR_RE`）不命中，整场会议坍缩成
   一个 blob；现两种锚点并行，每项"审议通过 + 表决行"独立成 meeting_proposal unit
   （实测该公告 4 项议案恢复）。
2. **表格 caption 参与锚点探测**：招商银行股东会第 8/10 项议案消失——MinerU 把
   "8.议案名称：…"挂成表决表格的 caption，而锚点探测只扫 text 行与标题；caption 现在
   也是锚点探针（实测 1–13 全序列恢复，15 units）。
3. **全平文档锚定到登记文档标题 + 碎裂问答表 needs_review**：美的投关记录表解析为
   全平裸表格文档，曾产出 title=NULL / heading_path=[]；全平文档现以 registry 的
   document.title 为锚。被 MinerU 把整句碎进单元格的问答表 build 期不可恢复，按
   `QA_TABLE_CONTENT_MIN_CHARS` / `QA_TABLE_MARKER_RE` 判定并标 needs_review
   （不再 ok；parser 级修复留账）。
4. **rebuild 源 run 查询放宽**：prune-history 删除被取代的 parse run 后，原 rebuild 源
   查询只认 run_kind='parse'，rebuild-units 断链；现任何携带 normalized_ir_relpath 的
   succeeded run（rebuild_units run 复制该字段，自身即合法源）都可作 rebuild 源，
   provenance 链自愈。

语料（25 docs，含 Codex 新增的平安/招商/美的样本）经快速路径 8s 重建为单一
ub-2026.07-9 代并 prune；门禁全绿。

### ub-2026.07-10 〜 -17（2026-07-07 rounds 9-14 速记；行级证据见对应提交）

- **-10**：年份行误判编号标题修复（序数封顶三位，cn_a_v5）；"金额单位：" 前缀变体剥离；
  semantic 词表扩到投资经理清单（15 规则，覆盖 6%→36%）。
- **-11**：附注科目受控词表 note_key_map r1（95 键，编报规则第15号法定集，
  三级匹配：剥编号→精确→别名→最长名包含）；scalar 未命中规则时回落词表键。
- **-12**：词表 r2（+MD&A/正文章节 26 键=124）；document.disclosure_topics 二级分类
  （0014，topic_map 12 题，F006V 派生）；分层解析谓词切换到 topics。
- **-13**：微型孤儿类防线（裸标签"其他说明："/年份碎片整单元丢弃，计数）；
  公告头锚定单元必带 title；0015 视图派生 heading_path_text 面包屑（38 列）。
- **-14**：单位/币种声明升级为组合文法（前导×系词×套话×币种×量级×动词，
  StudyOnCompany 式；18 真实变体剥/7 近似句留）；离线频率发现环
  （audit_boilerplate_candidates.py）首轮晋级"特此公告。"、二轮晋级公司名参数化
  眉头/称呼/落款族。
- **-15**：词表 r3（+三大报表/治理小节/市值管理等新规节=142 键）；事件 facet
  event_key_map r1（30 键，DuEE-fin∪CCKS∪FewFC∪CFinDEE 并集，标题派生并入全单元）。
- **-16**：词表键祖先继承（title→heading_path 自深向浅逐级取键，泛型叶子继承科目
  祖先）+ 定期报告门控放开（审计报告等 'other' 同样承载附注）；覆盖 67%→97.1%，
  附注 NULL=0；vocab r4（+合并财务报表项目注释等=144 键）。
- **-17**（cn_a_v6，另一 session round14）：HEADING_PATTERNS 拆级——、号科目=4、
  点号子项=5（(?!\d) 防金额误判）、（1）=6、①=7；修复点号子项驱逐 、号科目祖先的
  跳级 bug；附注子项归组进科目 mixed 单元（parts 带 local_heading），note key 继承
  恢复；一句话叶子去留经系统分析定案为保留。

### ub-2026.07-18（2026-07-07 round15：标题吞没对账环抓出的两个 S5/S6 bug）

- 新审计环 scripts/audit_heading_coverage.py（IR heading ↔ 单元标题域对账，
  round14 诊断方法的制度化）；首跑 101 条吞没，收敛到 1 条已记档接受项。
- **S5 续表合并加同路径前置条件**：原判据只看列数，cn_a_v6 让附注标题进栈后
  同构费用表相邻，跨科目误并（审计报告 3. 销售费用→1. 营业收入，标题全域蒸发）；
  跨页真续表页间只有 page_furniture、栈不变，不受影响。
- **S6 headers-only 表不再判空**：MinerU 首行升表头+空数据行剔除后仅剩表头的表
  （分部信息类）是原文内容且承载路径，保留。
- SKIP/FIXED 词表加"备查文件目录"（备查文件的年报变体，显式化既有丢弃语义）。

### ub-2026.07-22（2026-07-15：投关表单跨页正文无损恢复）

- 根因样本 `provider_document_id=1217576500`：官方 PDF 把正文放在跨页外层表格单元格中，
  MinerU 的物理 table 判定正确，但跨页拼接漏掉第 10 问并把其余问答拆成 text/table/footer
  残片。NormalizedIR v2 增加可选 `native_text` shadow（pdfplumber 逐页文本、版本、hash、
  字符统计）；只对标题有“活动记录表/调研记录/业绩说明会问答或实录”证据的**完整页解析**
  生成，且在 MinerU 剩余 timeout 预算内的可终止子进程执行。MinerU 继续是表格、版面和
  locator 真源。
- builder 恢复门为 fail-closed：native 必须有从“一、”起连续章节、章节内真实闭合 QA、
  首章节在 MinerU 载荷中可定位；所有将被替换的 MinerU 正文片段须在保留小数点、百分号、
  正负号等符号后，按顺序逐片段严格存在于 native 文本。任何独有事实、真实多列表、
  image/equation 或不安全附件边界均取消替换并保留原产物；问答序号有缺口时禁止拆成 QA，
  但可在 coverage 已证明不丢 MinerU 片段的前提下保留完整 native section 为 `needs_review`。
- 恢复后按章节生成 2 个正文单元，问答以 question 为 title、章节为 heading_path；PDF
  硬换行只在可检索正文/answer 中消除，`raw_text` 仍保留。真实样本重建结果为 3 table +
  2 text + 10 qa，Q1–Q10 连续且 footer/附件各归正确顶层路径，无“公告头信息”。

### ub-2026.07-23（2026-07-15：真实重解析形态兼容）

- `1217576500` 真实重解析时 MinerU 已把大部分问答输出为 text，但仍把第 10 问答案尾段塞进
  footer table；严格 recovery coverage 又被官方表单纵向标签尾片 `要内容介绍`、Markdown
  的 `\~` 转义和一个在旧 IR 中表现为空的 list 节点拒绝。
- recovery 精确删除纵向标签尾片、比较时把 `\~` 还原为 `~`（波浪号本身仍参与比较），并
  为已落盘的空 unknown 保留与 S1 相同的兼容分支；任何非空 unknown、MinerU 独有事实或
  结构化表仍取消恢复并保留原产物。随后复核证实该空 unknown 的 raw `list_items` 实际有正文，
  因此不能把兼容分支当成根因修复，见 -24。

### ub-2026.07-24（2026-07-15：MinerU list 保真与稳定章节根）

- MinerU 会直接输出 `type=list + list_items:string[]`；旧 mapper 仅复制 `item.text`，把这类
  正文落成空 unknown，违反“未映射类型不得静默消失”边界。mapper 现将至少含一个非空值的
  纯字符串列表按原顺序以换行连接为 `kind=text, raw_kind=list`；空/混合/嵌套形状仍保持
  unknown，交给既有 fail-visible 路径。
- 新 MinerU 形态还带一级文档标题。仅当投关模式遇到 `source=native_text` 且序号为“一、”的
  已证明恢复章节时清空标题栈，使表单元数据、第一/二节正文和第三节问答成为同级业务分支；
  普通标题树、普通 QA 和非 native 元素不受影响。
- 真实 content_list→mapper→builder 重放为 `3 table + 2 text + 10 qa`，6 条经营亮点、Q1–Q10、
  Q10 跨页尾段、footer 与附件全部保留，`needs_review=0`，无 mixed 合并和“公告头信息”。

### ub-2026.07-25（2026-07-15：标题树、报表锚定与两层边界收敛）

- **不是 MinerU 单点故障**：9 份真实 normalized IR 重放证明，主要错因是 builder 把跨页
  缩进当层级、内部标题栈曾受公开 4 级 breadcrumb 上限截断，以及 S8 用 8k 反向寻找最浅
  祖先。MinerU 的原始标题/页码/bbox 足以确定性恢复；不需要为这些缺陷重新执行 OCR。
- **标题树**：内部栈保持完整深度，公开 heading_path 仍投影前 4 级；同编号族连续性优先于
  换页缩进；支持 `3.2/3.9.1` 大纲与无编号银行报告大章；问号、声明、脚注、年份噪声永不
  进栈。财务报表首表与审计责任为兄弟，后续报表互为兄弟，`一、公司简介`式附注重启会
  关闭报表 run。
- **报表页眉恢复**：只有同页、视觉位于表格上方、水平相交且 exact 命中受控报表词表的
  单一 page_furniture 才补为 table caption；0/多候选不猜，显式 caption 优先。银行/公司/
  合并及公司等报表别名进入 note_key_map（当时 r6；当前 r16）；`财务报表`改为 exact-only，避免把“注册会计师
  对财务报表审计的责任”误标为父节。结构树只接受完整受控报表标题（可带审计状态、年度/
  日期前缀和括号/横线“续”），包含“利润表内确认的金额”或多个页码的目录句不得开报表根；
  计数 `recovered_statement_captions` 进入 build_stats。
- **caption 持续边界**：编号且命中受控科目的 table caption 会更新 S2 当前结构栈，使其后的
  caption-less 正文/表格继续归入新科目；不再只在 S8 修正 caption 表本身后回落到旧 sibling。
- **S8 当前规则覆盖 §8.5 的历史第 3 条**：结构决定边界，8k 只作 mixed 主文本硬上限，
  另设 24 parts 硬上限；有多级标题时至少停在二级业务标题，并下钻到最深受控科目，不跨
  科目把母公司附注或管理层整章并成一个 unit。原子 slice 自身超限时保持原 kind，由 L2
  context packaging 处理。商誉模板以“（n）为商誉减值测试的目的”为资产组实例边界。
- **语义空值（当时规则；由下一节 ub-2026.07-26 取代）**：S7 当时输出真实键数组或
  `semantic_keys=[]`，单值仍允许 NULL；当前新产物改为 `document_content` 非空兜底且 scalar
  必须属于 array。多值召回由 Filing API 的 `semantic_keys_any/all` 承担。
- 真实重放：`1217576500` 维持 `3 table + 2 text + 10 qa` 且全单元有
  `investor_communication`；`1222948914` 的 6 个报表页眉恢复，`38. 会计政策变更` 从错误
  44 parts 审计链缩回 3 parts；`1223121668` 的商誉测试从 52-part 单块恢复为 25 个资产组
  实例（每组最多 4 parts）。

### ub-2026.07-26（2026-07-15：跨载体 QA 与非空语义路由）

- `_qa_lines` 不再把 `Q4/P4/V12` 或 `2.0` 型号拆成编号；支持同一表格单元格内的
  `Q:…A:…Q:` / `Q11、…回复:` 显式边界。表格派生 QA 始终要求明确答复标记，并在解析视图
  内去重 MinerU 合并单元格的重复展开，原 table payload 不变。
- QA 状态机以外层题号和显式答复共同定界：长题干、换行题干、Q10 下的 1/2/3/4 子问题均
  保持一个 question；损坏的领先回答可在下一条强问题处重同步，不再因第二个“答”清空整段。
  table→text / text→table 的高置信跨载体问答可恢复为 `needs_review` QA；无法证明完整性的
  截断答案保留原 evidence 并降级，绝不标 `ok`。
- S7 在没有更窄规则/词表/事件键时写入真实通用键 `document_content`；因此新产物的
  `semantic_key` 与 `semantic_keys` 都非空，且 scalar 始终属于 array。数据库列仍可空只为
  历史 run 兼容，不用伪造的 `unknown`。

### ub-2026.07-27 〜 -31（2026-07-15：多公司、多文类真实语料审计收敛）

- **-27〜-30 中间轮次**：连续重放投关、年报、审计报告、债券与普通公告，收敛原生文本
  direct-QA、正式表单门控、声明/报表/附注 sibling 标题树、公告头及页眉噪声、受控 caption
  锚定；note_key_map 在该轮升至 r11（169 键；当前 r16/173 键、389 标签），event_key_map 保持 r2
  （35 键）。这些
  同日版本是语料发现环的可复现中间代，不再作为上线目标代。
- **-31 最终 builder 代**：以最多 3 个物理 carrier、显式 QA 信号、同章节与顺序距离为界，
  在独立 logical view 中恢复 table→heading→text / text→table→text 连续问答；普通正文、业务表、
  footer 和附件均 fail-closed。恢复结果标 `needs_review`，原 table/text evidence 不被删除。
- 无标题投关 QA 固定锚到正式表单段 `投资者关系活动主要内容介绍`，不再把 question 自身当
  breadcrumb；`43.2024年……？` 仅在严格年份题号形态下识别且 raw_text 保留原点号；text-kind
  的“一 持续加强研发投入 / 1 亿元投资额”不凭空格升级为标题，只有受控附注/报表名例外。
- MinerU 3.4 的另一类根因在 parser 输入层处理：当 `content_list` 把后续页 table HTML 聚合到
  前页并留下空 ghost 时，只在同页 bbox 唯一匹配且 logical cell 串接完全等价时，才用同 stem
  `*_model.json` 恢复 page-local HTML；缺失、坏 schema、歧义或不等价全部保留原值，计数写入
  `parser_diagnostics.table_reconciliation`。几何与 cell 等价的正向证明不依赖标题/业务词表；
  受控结构标题只作拒绝不安全恢复的负向护栏，并由专用 table-builder semantics 版本约束。

### ub-2026.07-32 〜 -37（2026-07-15：全语料重放后的跨页与标题树收口）

- **跨页表格根因确认**：MinerU `content_list` 的首表 HTML 会聚合多页，而同 stem
  `model.json` 仍保留逐页表。reconciler 现支持相邻多页链，以 bbox 唯一匹配和未展开的
  logical source cells 精确串接为证明；rowspan/colspan 展开差异不再制造假不等价。MinerU 将
  running header/page number 序列化在跨页 table carrier 之间时，仅允许跨页精确重复且不可能
  恢复为 statement caption/结构标题的 furniture 穿过；普通变列、正文间隔、唯一页眉或标题冲突
  仍 fail-closed，S5 最终表数保持不变。
- **报表与附注边界**：受控科目词表当轮升至 note_key_map r13（172 键；当前 r16/173 键、389 标签）；补充每股收益、长期
  股权投资、共同经营、债券偿还等真实标题别名。模型把母公司利润表末行
  `（二）稀释每股收益(元/股)` 误标标题时，builder 只在“上一页 4 列利润表末两行 + 次页
  三位签字人 + 紧随现金流量表”全证据成立时恢复成表格行。重复页眉、跨碎片附注标题和
  page-furniture statement caption 同样采用页码/bbox/受控词表的窄门，均有负例回归。
- **历史编号树安全化**：银行报告局部热点可暂时关闭 `1.`…`8.` 主章序列；历史重开仅允许
  受控边界标题、active 同号/前号优先、保存父路径必须是当前路径前缀，多个身份候选时拒绝。
  这消除了把现金流补充资料、金融风险附注错误拉回旧会计政策父树的跨父误挂。
- **无标记/跨页 QA**：官方投关表中 `N、问题？ 无标记回答` 只有在完整 form/footer、连续
  `1..N`、完整问号及 mode isolation 成立时才从 native PDF text 恢复；dot 序列可继续到
  顿号/冒号，explicit-Q 模式的答案编号仍不参与外题边界。MinerU evidence fallback 另支持
  “物理页底问句 → 次页单格内至少 3 个连续问答”，以及“断词答案 → 次页顶部纯续文 →
  同页紧邻下一题”的三明治；原 carrier 保留，派生/修正 QA 一律 `needs_review`。
- **真实回放结果**：`1218099701` 从 10/15 恢复为 15/15；`1223071887` Q1/Q11 的
  `美的|系`、`水冷|型` 跨页断词完整拼回；三份复杂发布会中 tokenizer 产生的 20 个裸
  `问题` 只在其后紧接已证明 outer Q 时丢弃，不改变原 question set。当前规则代为
  `ub-2026.07-37`（当前上线目标代见下文 `ub-2026.07-53`）。

### ub-2026.07-38 〜 -52（2026-07-15：全库根因审计与可检索证据闭环）

- **MinerU 聚合表恢复 v3（历史，已由 -53 的 locator-only v4 取代）**：根因是 MinerU `content_list` 把跨页表聚合到首个 carrier、后页留下
  空 ghost，而 model artifact 仍保留逐页表。reconciler 只有在同页 bbox 唯一、逻辑 cell 串接
  精确相等、列宽一致、续页无 `<th>`/caption/footnote 且 carrier 间仅有可证明 furniture 时才恢复；
  受控报表标题只作否决护栏，不能补足正向证据。v3 进一步把统计公式、专用 S5 语义代、
  locator 五字段 bundle、唯一索引、连续页序和有效 bbox 固定为 fail-loud 契约；歧义一律 fail closed。
- **原生文本 shadow 可观测降级**：仅预期 PDF/IO/子进程/剩余预算错误降级为
  `native_text_shadow=unavailable + error_code`；空结果为 `empty`，未知异常继续使 parse 失败。
  投关/业绩表单遇到 empty/unavailable 时保留 MinerU 主证据并标 `needs_review`。
- **标题树与重复根**：补齐受控 sibling/root 的精确重开、带日期财务附注根与跨页补充 furniture；
  内部分组身份始终使用完整 `structural_path`，公开 breadcrumb 仍最多 4 级。mixed part 通过
  `local_heading` 保留被公开深度上限挤出的标题；空格漂移按逐段规范化匹配，不再吞掉
  `37 主要会计政策…`、`16.3.1/16.3.2/16.4` 等可寻址标题。
- **公告号链**：公告号不再形成伪 `公告头信息` 单元；只允许在首屏局部链中穿过严格元数据载体
  合并到首个实质正文，找不到目标时全部原样保留。兼容 `A 股证券代码`、带内部空格的简称/债券
  代码及中文发布日期；正文历史公告号引用不参与合并。统计记录 merged/deduplicated 数量。
- **声明与终端附件边界**：MinerU 把 `标题 √适用 □不适用` 粘成一个 heading 时，先把声明拆为
  独立 marker 再进入既有适用性状态机，标题不再携带勾选噪声；签章之后、以 `附件:` caption
  开始并连续跨页的终端表只在窄证据成立时重锚到文档根，普通正文内附件仍保持所在业务作用域。
- **语义与检索不变量**：所有新 unit 均满足 `semantic_key IS NOT NULL`、`semantic_keys` 为非空
  数组且 scalar 属于 array；无窄键统一为 `document_content`。Filing API 的 scalar 参数保持
  scalar 精确语义，跨 primary/secondary key 召回使用 comma-list 的 any/all 过滤。
- **全量离线重放**：截至 -48 对 1,371 份现有 raw artifact（13 公司、21 个 active 文类）完成
  content_list→reconciler→IR→builder 重放：39,005 units、0 build errors、0 semantic 空值、0 超过
  50k 字符的 unit；264/264 个首屏唯一公告号均可精确搜索且只出现一次。-49 只修复上述 4 个
  local-heading 证据缺口；-50 再收口声明粘连与终端附件边界，并以针对性回归、完整 builder
  回归与 schema contract 验证。
- **-51 长标题局部序列闭环**：全库只有 3 条 41–80 字的 `n）` 真标题被通用 40 字门限
  压成前一叶子；新规则只在外层 `(1)/(2)` 序列已由来源层级和几何证明、当前栈顶为
  `n-1）`、左边距差不超过 8、单行高度不超过 32、与外层顺序距离不超过 12 且无句末/
  KV 标点时恢复叶子。真实 `1225087169` 的财务担保合同因此从错误继承
  `financial_instrument_risk` 收敛为 `guarantee`；两份格力英文年报的同构叶子一并恢复。
  MinerU list 内的 `①/②/2）` 另保持 mapper 字符守恒后的粗粒度文本；不为一句条款新造碎 unit。
- **-52 parser→builder 语义握手（历史版本）**：表恢复证明记录独立的
  `table-builder-semantics.v2`；BuildUnits 对任何带 aggregate-table reconciliation 诊断的 IR
  fail-loud 校验该版本，不匹配即要求重 parse。它不绑定整个 `RULES_VERSION`，普通标题、QA、
  semantic 规则升级仍可走 5 秒级 `rebuild-units`，只有 S5 重并或结构页眉否决语义变化才重解析。

### ub-2026.07-53（2026-07-16：跨页逻辑表 locator-only 与契约闭环）

- **v4 不再物理恢复**：expanded grid 相等并不能证明最终 unit 相等，因为 native form recovery、
  footer 重挂和 S5 边界会观察物理 carrier。`mineru-aggregate-table-locator.v4` 对 proven
  aggregate+ghost group 永远保留原 `table_body` 和 empty ghosts，只在 root 写连续 page span、
  每页 bbox、model indices 与 continuation indices。`1217576500` 的 11 页附件表因此保持一个
  339 行逻辑表；逐页 model 合计 349 行，多出的正好是 10 次重复表头。完整 locator 同时保留
  11 个页面 bbox，不再把证据切成 page-local 碎片。
- **算法边界与跨对象校验**：diagnostics 新增 `locator_only_groups/tables`。BuildUnits 先把
  语法合法的旧 restore/locator algorithm 分类为 `ALGORITHM_MISMATCH`，再校验当前 v4 的统计恒等式、locator root/table
  数、model index 范围/全局唯一、continuation ghost、页序/bbox 与 content table count；
  locator 没有 diagnostics 也 fail loud。v4 只记录 parser provenance，不改变 carrier、unit 或 hash，
  因此不再绑定 S5/RULES_VERSION；旧 v3/v2 IR 仍必须重新 parse，不能仅 rebuild units。
- **full-grid 坐标与 footer**：headerless 表先删除空行并 remap merged spans，再提升首个非空行；
  repeated header 若有 rowspan 跨越删除边界则不抑制。`merged_cells` 始终相对最终
  `[headers, *rows]`；mixed 每个 part 保存自己的 `artifact_locator`。全语料 29,022 张表中
  32 张命中 leading-blank 修复，32/32 坐标有效；投关 footer 判定改读 full grid，单行日期与
  sparse 附件字段不再错挂到“主要交流问题”；日期值必须含数字或中文数字日期结构，
  `月度收入/报告日余额/日均营业收入/当日收盘价` 等业务列不会再触发 footer 重锚。
- **一次性 mixed hash-domain 迁移**：mixed payload 会保存规则/位置注解，但 content hash 只对
  去注解的 canonical content projection 计算；query projection 继续包含这些注解。首次从旧
  whole-payload hash 重建到本版本时，既有 mixed unit 会产生一次 materialized
  `document_unit_removed/created`，这是身份域修正而非 PDF 内容变化。L2 消费者必须按 change
  feed 撤销旧 asset_id、刷新/重放对应 mixed 证据；完成一次迁移后，同内容只改路径/locator
  走 projection change，不再伪造内容变化。

## 9. 明确不做

- 不抽取 claim；不做 table_cell / page-bbox 核心索引；不做 LLM 语义价值判断；
- 不实现 `rebuild_units`（后由 §8.5 ub-2026.07-8 修订案推翻，2026-07-06 已落地）；
  不做 Filing API（本 milestone 范围外，后由 milestone 06 交付）；
  不做批量调度（本 milestone 范围外，后由 milestone 08 交付）；
- 不生成 search projection artifact / 检索字段（U7 边界：06R 派生层，builder 不耦合检索规则；
  06R 为规划中里程碑，规格文档尚未编写）。

## 10. 交付给下一阶段

document_unit 表数据与快照、active run、outbox 事件流（06 的 `GET /v1/changes` 直接消费）、
build/publish/process CLI（08 worker 的执行单元）。

## 11. 常见失败与处理

- 载体规范化误删实质内容：降级规则倾向保留；原文与 parser artifact 可重处理（红线）。
- 表格跨页合并失败：needs_review，不阻塞 text/qa。
- Q&A 边界不稳：保存为 text 或 needs_review，不自由拆。
- 发布竞态：FOR UPDATE + one-active-run 索引兜底；IntegrityError 翻译为领域错误后重查。
