---
id: disclosure_anchor_milestone_05_document-unit-builder-and-active-run
project: disclosure_anchor
title: document_unit builder 与 active run
status: ready-for-implementation
created_at: 2026-06-26
updated_at: 2026-07-04
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
  unit 挂在同一 `processing_run_id` 下；`run_kind='rebuild_units'` 保留不实现。
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
辅助信号：IR heading_level（有值时作强信号提升置信，但不单独定级）
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

**S4 qa builder（投关记录 / 业绩说明会 filing_type 触发）**：

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

## 9. 明确不做

- 不抽取 claim；不做 table_cell / page-bbox 核心索引；不做 LLM 语义价值判断；
- 不实现 `rebuild_units`；不做 Filing API（06）；不做批量调度（08）；
- 不生成 search projection artifact / 检索字段（U7 边界：06R 派生层，builder 不耦合检索规则）。

## 10. 交付给下一阶段

document_unit 表数据与快照、active run、outbox 事件流（06 的 `GET /v1/changes` 直接消费）、
build/publish/process CLI（08 worker 的执行单元）。

## 11. 常见失败与处理

- 载体规范化误删实质内容：降级规则倾向保留；原文与 parser artifact 可重处理（红线）。
- 表格跨页合并失败：needs_review，不阻塞 text/qa。
- Q&A 边界不稳：保存为 text 或 needs_review，不自由拆。
- 发布竞态：FOR UPDATE + one-active-run 索引兜底；IntegrityError 翻译为领域错误后重查。
