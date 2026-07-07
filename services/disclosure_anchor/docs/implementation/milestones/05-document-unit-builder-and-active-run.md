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
  `tests/fixtures/unit_hashing/golden_hashes.json`——固定 3 组输入（text/table/qa 各一，
  含中文与 null semantic_key）及期望三哈希 hex，`tests/unit/test_unit_hashing.py`
  重算断言相等（期望值固化在文件里即保证跨进程/跨版本稳定）：

```text
content_hash          = "sha256:" + sha256(canonical_json({payload_kind, payload}))
                        纯内容身份。不含 title/heading_path/order_index/semantic_key/
                        artifact_locator——标题识别与切分规则升级不得伪装成内容变化
query_projection_hash = "sha256:" + sha256(canonical_json({payload_kind, title,
                        heading_path, semantic_key, quality_status}))
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
第二层稳定配对：同 key 的 old/new units 各按 (order_index, asset_id) 排序后逐个配对
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
                 changed_fields 词表固定 = {title, heading_path, semantic_key,
                 quality_status} 的子集，逐字段比较旧/新值得出（payload_kind 在配对
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
缺失或不可读）/ `IR_CONTRACT_TOO_OLD`（contract_version 非 normalized_ir.v2，指引重新 parse）/
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

**S4 qa builder**（2026-07-05 评审修订：qa 识别对**全部 filing_type** 生效，稳定性三判据 +
needs_review 兜底防误报——问询函回复等问答密集文档同样受益；实证 16+ 真实文件年报/审计/
季报零误报。原"投关/说明会触发"限定只保留在 S2 的 qa_heading_mode——该模式下编号标题
不入 heading 树，防问句累积成 heading_path）：

```text
问题起始：^\s*(问题|问|Q\d*|投资者提问|提问)\s*\d*\s*[：:] 或
          ^\s*\d+[、.．]\s*.{2,}[？?]\s*$（编号 + 问号结尾）
回答起始：^\s*(答|回复|公司回复|A\d*)\s*[：:]
边界规则：回答 = 回答起始行（或问题行之后）到下一问题起始之间的全部文本
来源：text 元素序列 + 结构化表格 cell 内的多行文本（ir_activity 样本的问答嵌在
      "投资者关系活动主要内容介绍" cell 里——按行拆后套同一规则）
产出：每个 Q&A 对一个 qa unit，payload = {question, answer, raw_text}
      （与 golden fixtures 契约一致）
"边界不稳"闭合判据（命中任一 → 整块存 text + needs_review，不硬拆）：
  (a) 出现回答起始行但其前无未配对的问题起始；
  (b) 问题起始后直到下一问题起始/块尾无任何非空文本（有问无答）；
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

**S7 semantic_key + quality_status**：semantic_key 由规则表给出（未命中 = null，禁止自由
发明）。首版规则表定死（rules.py `SEMANTIC_KEY_RULES`，按序首个命中；匹配对象 = title +
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
   快照行顶层键集 = {artifact_locator, asset_id, content_hash, document_id, heading_path,
   order_index, payload, payload_kind, quality_status, semantic_key, title}
   （structure_hash/query_projection_hash 只在 DB 列，不进快照）。
   build 统计落点定死：ArtifactStore.write_json_atomic 写快照同目录 `build_stats.v1.json`
   （键：generated_by_kind / dropped_by_kind / dropped_unknown_by_raw_kind /
   skipped_sections / merged_tables / needs_review_count / unusable_count），
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
契约（键集断言定死，防两个真相源打架）：快照每行**顶层键集** == S8 步骤 2 的 11 键集合
（与 phase00 fixture 顶层一致）；**payload 内层键集按 kind 断言**：text={text}（image 壳=
{image_ref, caption, context}）、qa={question, answer, raw_text}、table={caption, unit,
headers, rows, notes}（table_parse_failed 时={caption, raw_html, notes}）。
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
