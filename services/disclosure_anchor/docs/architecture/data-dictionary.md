---
id: disclosure_anchor_data_dictionary
project: disclosure_anchor
title: 数据字典（表/列/枚举/关系）
status: living
created_at: 2026-07-07
authority: 语义冲突时以 service-purpose.md 为准；列集数量以 contract-checklist.md §2 为准
---

# 数据字典

**维护约定**：新增列/枚举值/词表键时同步本文件（与迁移同一个提交）。
模式：`disclosure_core`（业务写入）/ `disclosure_public`（唯一读契约 *_v1 视图）/
`disclosure_ops`（队列视图 + outbox + alembic_version）。角色：owner（迁移）、
app（读写 core/ops）、reader / future_l2_reader（只读 public 视图）。

## 1. 表关系（FK 图）

```text
company 1←n company_identifier        company 1←n security
company 1←1 tracked_company（盯盘配置） security 1←n document（经 company_id+security_id）
document 1←n processing_run（每文档唯一 is_active=true）
processing_run 1←n document_unit      document 1←n source_access（获取记录）
company 1←n source_checkpoint（scope_key = company_id:p_info3015）
provider_category（字典表，无 FK；经 document.provider_metadata.raw_category 段关联）
outbox_event（ops；subject_ref 弱引用 document/unit/run）
```

## 2. disclosure_core 逐表

### company（公司主体）
| 列 | 类型 | 含义 / 约定 |
|---|---|---|
| company_id | vc64 PK | `co_`+ULID |
| legal_name | text NOT NULL | 法定名；离线入池时为 `PENDING_LEGAL_NAME code.exchange` 占位，首次成功同步自动升级为真名 |
| unified_social_credit_code | vc32 UNIQUE NULL | USCC 强键；NULL=尚未从 profile 获得 |
| created_at / updated_at | timestamptz | |

### company_identifier（标识账本）
| 列 | 含义 |
|---|---|
| scheme | 闭集：uscc / lei / sec_cik / hk_cr / cninfo_org_id |
| raw_value / normalized_value | 原值 / 归一值（大写去空白）；强键 scheme+normalized 在 active 上唯一 |
| status | active / retired / **contested**（主体冲突标记，resolver 置位后 commit 再抛——改名解卡是 09 背账项） |
| source_access_id | 证据来源（可空=人工/初始化） |

### security（证券）
security_id PK；company_id FK；`security_code+exchange` 定位并唯一。写入口统一
`strip + uppercase exchange`；0022 以 CHECK 保证库存键已是规范形态，并校验大陆六位码
与 SSE/SZSE/BSE 前缀一致（上海 B 股仍属 SSE）。无法从代码前缀可靠判断时必须显式给 exchange，
不允许悄悄落 SZSE。

### tracked_company（盯盘配置——唯一人工输入的落库形态）
| 列 | 含义 |
|---|---|
| status | active / paused（暂停=改字段不删行；真源是 config/watchlist.csv，`make track` 对账） |
| lookback | jsonb `{"days": N}`；覆盖默认初始回补 1095 天（级联：空=继承 env） |
| process_classes | jsonb 数组（class 键，0018 由 filing_categories 改名改义）；**按公司覆盖全局处理策略**（替换式，非空数组整体替换；NULL/空数组 `[]` 均继承 config/processing_policy.json）；登记永远全量 |
| sync_frequency | 闭集 hourly/daily/weekly；NULL=全局 DISCLOSURE_SYNC_INTERVAL_SECONDS |

### document（披露文件版本）
| 列 | 含义 |
|---|---|
| status | 公开可消费态：registered → parsed \| parse_failed →（发布后）published；published 永不降级 |
| ~~filing_type~~ | **0017 删除**（表列——规则派生值不是事实）。视图现算：class 词表码 argmax，无码时标题关键词规则（rule_set='title'），仍无命中='other'；未知枚举值按 other 消费（前向兼容） |
| ~~disclosure_topics~~ | **0016 删除**（表列）。视图现算：class 词表全部命中类集合（jsonb 数组）；无码通道 NULL |
| report_period | `YYYY(A|Q1-4)`；按 code + title_topic priority argmax 后的主类推导，定期报告必填，临时公告可 NULL，不因标题含“季度报告”等子串伪造 |
| raw_file_relpath/raw_file_hash | 相对路径+sha256；原始 PDF 不可变只追加 |
| provider_metadata | jsonb：raw_category（F006V 原串）、category_names（中文分类名数组）、file_signature 等；CNInfo F005N/adjunctSize 原值只作不透明 provider 签名提示，不推断单位，也不作为解析上限 |
| supersedes/correction_of_document_id | 版本链（修订/更正） |

### processing_run（处理运行=action_log 特化）
| 列 | 含义 |
|---|---|
| run_kind | parse / rebuild_units（复用解析产物只重切，5 秒级） |
| status | running / succeeded / failed；stale running 由 worker 按阈值回收 |
| is_active | 每文档唯一 true（发布原子切换） |
| artifact_owner_processing_run_id | 实际拥有 parser artifact 与 primary parse artifact 字节的根 parse run；parse=self，rebuild 传播根 owner，不能从路径文本反推 |
| builder_rules_version | 新 writer 当前恒等于 `provider_unit.v23`；历史 run 保留原规则版本，不能回写 |
| parser_target_identity | 产生该 run 的完整 parser target（backend/method/language/runtime bundle identity）；不从零散 parser_* 列反推 |
| search_projection_error | 当前 retrieval_rules_version 的确定性、非重试检索投影终态；delta 不空转，full 可显式重试，成功替换时同事务清空 |
| content_hash_aggregate / structure_hash | run 级聚合（U3）；"内容没变"只看前者 |
| parser_* / normalized_ir_relpath / provider_document_relpath / artifact_hash | 解析出处与 primary artifact 引用；parse/rebuild 必须精确选择一个输出 arm。新 writer 只写私有 `provider_document_relpath`，历史 v4 只读 `normalized_ir_relpath`；`artifact_hash` 是所选 primary artifact 的精确字节哈希，public view 不暴露路径 |
| unit_build_status/attempt_count/error | 构建生命周期；error 为结构化 {stage,error_code,retryable} |
| semantic_route_receipts_relpath / contract_version / hash | v2 收据的显式私有路径、闭合版本与精确字节哈希；历史 v1 行只保留 hash 并按旧 sibling 规则回放 |
| semantic_adjudication_status | `not_required` / `complete_primary` / `complete_backup` / `degraded_unavailable` / `failed_closed`；parse 成功不能掩盖 Unit 构建终态 |
| semantic_degraded_unit_count / semantic_failover_group_count / semantic_adjudication_summary | provider 链降级与切换的可审计计数和闭合摘要；health/doctor/`unit_build_terminal_v1` 的真源 |

### document_unit（L2 消费的最小可寻址单元）
| 列 | 含义 |
|---|---|
| asset_id | `du_`+ULID；跨 run 不承诺同 ID，身份=content_hash |
| payload_kind | 闭集 text / table / qa（历史只读）/ **mixed**。新 writer：单一正文块提升为 text，单一逻辑表 owner 提升为 table；多块或视觉块使用 mixed。parts 只存浅内容字段；精确 provider type 留在 ProviderDocument，粗粒度 owner/evidence kind 只留 locator |
| heading_path | jsonb 完整**源标题路径**（有 heading 时非空；GIN jsonb_path_ops 精确包含）。可检索形态=视图列 heading_path_text；路径来自 typed heading/outline 结构，不来自普通 caption 或 taxonomy；唯一的强编号 table-caption 例外见下一行 |
| title | 只取已接受 source heading 的叶节点，并与 heading_path 末项相等；无可靠 heading 时为 NULL。登记文档标题只留 document scope。普通 caption、单位、脚注不得填入 title；仅当 Provider 将缺失的强根编号标题并入唯一 `table_caption` 时，locator v3 起以 source index + payload ordinal 将该 caption 作为 source heading，且从 table payload 移出以避免重复。locator v4 还可用 `continuation_fragments` 绑定同页几何闭合的换行标题尾部；每个 fragment 仍对应原 source occurrence |
| semantic_keys | 可选的受控 Unit **直接主题**路由 JSONB 有序集合（1..8 或 SQL NULL），GIN 支持 any/all recall；0047 已删除冗余私有 scalar。mixed/长 Unit 可保留多个独立粗主题。只有 `body_status=content` 的答案载体可获得 direct route；`heading_only` / `empty` 内部保持 NULL，标题仍由 title/heading_path/section_keys 提供导航。direct 表示 Unit 自身有 source-bound 主题证据，不表示陈述为真、已实现、本期、无条件或可采信；历史、风险、预测、计划、条件、因果、否定、无发生与不适用由 L2 解释，不能抹掉已成立的 L1 主题。有内容 Unit 的唯一精确标题可确定性落键；正文定量 route 只对 versioned positive key allowlist、受控 label 紧邻数值（可经闭合 connector）或定期指标的明确方向结果锁定；严格 typed table field/header 与等价正文产生同一 allowlisted粗主题，普通 table text/data cell 只作 lexical/candidate。内部 `role_anchor` 保存 forecast period/range/comparison/basis/risk 等精确披露角色，可与独立粗主题并存，但一个角色中的数字不得制造另一个角色；`exclusive_container` 仅用于目录、完整报表/整表等机械载体。其余标题/正文/表格只生成至多 8 个 source-bound 候选；provider 链必须逐候选返回闭合布尔裁决，不能造 key、决定边界或判断事实性。Document filing type + authoritative disclosure topics 只开放 scope；provider content categories 只作 facet/context。证据不足时不以 `document_content` 冒充语义；public v1 将内部 NULL 投影为 `[]`。Build 冻结 receipt，Publish 只重放不调模型 |
| section_keys | 可选的受控**结构位置**路由，与直接主题分离。从已接受 heading_path 的根到叶精确匹配显式结构容器：定期报告使用 `context_container`，事件公告只开放少量命中 filing_type/authoritative disclosure_topics scope 的 `section_container`。一个 versioned exact composite heading 可显式映射多个结构键（如“公司治理、环境和社会”→governance + environment_social）；不做 contains/similarity。无模型、无 Document 类别直接传播；heading-only Unit 可从自身 hash-bound accepted heading path 获得精确结构位置，没有可匹配 heading 的真空 Unit 内部保持 NULL。完整链可让 L2 按“管理层讨论/财务报告/认购方法/交易风险”等章节批量召回，又不与 Unit 直接主题竞争。非空时为 JSONB 数组、GIN、API any/all；0040 起 public v1 的空集合统一为 `[]`；变化进入 query_projection_hash |
| payload | ProviderDocument 的 source-bound 浅投影：顶层 text 只保存 `{text}`；顶层 table 保存原始 `table_body` HTML 与 caption/footnote 数组；mixed 只保存有序浅内容 fields，不重复 `provider_type`/kind/semantic_type。精确 source type 与粗 owner kind 分别在 ProviderDocument/locator。视觉 part 的 `content_artifacts` 仅含 hash/size/media，使视觉内容进入 content hash；路径、raw JSON、表格 crop 不进入 payload。仅当同一不可变 PDF、同一 MinerU text bbox 中 native text 通过闭合规则证明是 MinerU 漏失的完整数字核心，或同样 source-bound 的窄 identifier 证明数字与至多一个开引号（v1），或恰好一处 `=` 与至少一个完整数字 atom（v2）同时漏失时，才允许把 native PDF text 投影为 payload并记录双侧 hash；其余空白逐字相等，只忽略实际删除的数字/`=` atom 位点所消费的 ASCII 空格/Tab。ProviderDocument 仍保留原 MinerU 文字。L1 不解析 grid、不修复 cell、不用 middle HTML 覆盖 content-list owner；table/native 数字序列异常只产生 `source_pdf_native_table_quality.v1` finding；source-only text omission 若至少漏一个完整数字 atom、又不满足 repair，只产生 `source_pdf_native_text_quality.v1/native_text_omission`；其 proof 只忽略处在矩形首尾或直接紧邻数字 atom 的单个 ASCII 空格/Tab atom，其他正文空白逐字一致。两者均置 `needs_review`，不改对应 payload |
| content_hash / query_projection_hash / structure_hash | 三哈希分层（U2）；content 绑定 payload（含视觉内容 digest），query 绑定 title/heading/完整直接主题 routes/section routes/quality/applicability，structure 绑定 kind/path/order。locator/page/provider identity 不混入哈希，发布前由 fresh ProviderDocument admission + deterministic rebuild 精确复核；旧快照兼容 lead 只从 `semantic_keys[0]` 派生，不是现行 DB 列 |
| quality_status | ok / needs_review / unusable（乱码率>30%） |
| applicability | vc16 CHECK：applicable / not_applicable / NULL；只列化当前叶标题自有 selector，或第一个实质/视觉 part 之前 declaration-only leading part 的受控成对勾选。普通 paragraph 不因整句匹配变成标题或 prompt role；实质、visual/table carrier 之后以及嵌套 child 的 selector 不提升为整个 Unit 状态；不跨 Unit 继承（见 §5 讨论） |
| page_no | 定位列（artifact_locator 首页码） |
| artifact_locator | 新 writer 为闭合的 `provider_unit_locator.v9`：保留 v8 的 ProviderDocument hash、source heading block + payload ordinal、parts、逻辑表 owner/physical segment、evidence/search bindings、`continuation_fragments`、native-PDF reconciliation 与 quality findings，不凭普通 paragraph 的整句词面发明标题；只新增 finding-only 的完整 token omission、截断后仍至少两位的单数字末位截断和 cell-scoped 畸形数字分组证据，不改 payload。历史 v1-v8 继续按各自 vocabulary 只读；v7 可解码其历史 `statutory_template` placement，v8/v9 不发出，v8 也不得声明 v9 quality kind；v1-v3 不得声明 v4 才引入的 `unit_title_fragment` search destination，v1-v6 不得声明 v7 placement。跨页关系只接受 MinerU merge-on 的 typed owner/stub assertion；上一页表尾 exact `page_footnote` 只可作为 physical boundary，下一页 leading footnote 仍阻断；不按相似度猜、不复制 HTML、不存 raw JSON/path；JSONB(none_as_null) |

### classification_rule（0016，词表的库内查询副本）
| 列 | 含义 |
|---|---|
| rule_set | 闭集 class / facet / **title** / **title_topic** / **title_noise**。title 是无码通道 broad fallback；title_topic 对有码/无码都可追加窄主题；title_noise 只可作为路由/复核候选，不能凭标题 pattern 单独跳过已在处理范围内的 PDF；重复件需 provider linkage、内容哈希或明确版本关系证明 |
| prefix / value / priority | F006V 前缀或标题模式 → 类名/维度名；priority=主分类阶梯档位（三层原则）/facet 长前缀优先；match=all 的标题模式以 `%` 编码并由 SQL/Python 同义匹配 |
| version | 与仓内 JSON 词表一致；doctor 校验漂移；`make load-rules` 事务内重载 |

### source_access / source_checkpoint / provider_category
- source_access：每次 provider 访问一行（**失败也留痕**，含 profile 拉取失败）；query_params 已剔除凭据；error 结构化。下载成功快照中的 `result_snapshot.byte_count` 是归档实测字节数，与 `result_hash`/document raw hash 绑定，可作调度成本；provider 大小提示仍保留原值，不冒充实测字节。worker 捕获的同步失败另写 `cninfo:worker_sync_failure` 调度标记，保证失败公司冷却并移到未尝试公司之后。
- source_checkpoint：scope_key=`company_id:p_info3015`；cursor={window_end, window_start, synced_at}（后两个为审计字段，判定只用 window_end 与 updated_at）；每次 cursor update 必须同步刷新 updated_at，避免已同步公司永久 due。
- provider_category：F006V 字典 2135 行（p_info3005 快照 seed）。

### ops.outbox_event
seq 单调；event_kind 闭集（document_registered/observed、processing_run_created/failed/published、document_unit_created/removed/projection_changed）；change_kind=observed/materialized（下游失效只由 materialized 触发）；projection_changed 携带非空 changed_fields。

## 3. disclosure_public 视图（唯一读契约）

- **document_units_v1（39 列，当前且唯一；0041 起公共面不含 scalar，0047 起私有表也只保留 plural）**：core 列 + 派生（is_active_run、heading_path_text 面包屑、
  现算 filing_type/disclosure_topics、
  contract_version、company_ref/security_ref、security_code/exchange、filing_type、
  disclosure_topics、semantic_keys、section_keys、report_period、announcement_date、source_ref、parent_ref、asset_kind、
  observed_at、source_tier、trace_level、raw_file_hash、`body_status`）。不含 Document-only
  `content_categories`；列集权威=contract-checklist §2。0039 已移除并行的 `document_units_v2`。
  其中 `company_ref/security_ref/security_code/exchange` 继承 Document 的 provider 获取/登记范围，
  只可用于 source-scope 取数；它们不是 Unit 正文 content issuer 的 canonical 断言。母公司代发子公司附件
  可产生 source/content 冲突；当前 v1 没有 issuer-attribution assessment，消费者不得把这些列单独作为
  same-subject join 或事实归因键。
- documents_v1 / processing_runs_v1 / source_refs_v1 / change_events_v1 / document_categories_v1。
- **tracked_companies_v1（0019+0020，round22）**：股票池读契约——真源是 tracked_company 表
  （watchlist.csv 降级为导入/快照）。视图只暴露 raw 覆盖列（NULL=继承）+ 生命周期事实列
  （legal_name_status pending/resolved、last_synced_at、synced_through——Miniflux
  checked_at 模式）；生效值与 sync_state（never_synced/due/fresh）由
  `GET /v1/tracked-companies` 在 API 层派生（全局 policy/间隔是文件与 env，SQL 看不见）。
- **unit_search_projection_v1（0025+0028，06R 派生检索投影层，U7）**：与 document_unit 1:1，
  全部列（title_text/heading_path_text/{title,path,body,key}_tokens/header_row_candidate/
  built_at/加权 search_tsv）可由已持久化 unit 确定性再生——应用侧 jieba 预分词 + `simple`
  配置 + `setweight` 拼接 + GIN，title/面包屑另建 pg_trgm GIN 子串兜底。物理上不安全的
  body 进入 **unit_body_search_windows_v1** 连续半开 token 窗；parent 公开列集不变，private
  window flag 不暴露。跨窗 AND 按 `(asset_id, query_group)` 聚合，不能跨资产拼命中。
  **unit_search_atoms_v1（0030）**逐行暴露 explicit search target 的 NFKC+casefold 字符串叶子；
  不连接 target/mixed part。仅归一化后长度 ≥3 的 query 走 GIN `LIKE` 候选 + 同 atom `strpos`
  精确复核；1–2 字只走完整 word channel，不承诺任意子串。
  **unit_search_row_atoms_v1（0044）**固定 8 列：`asset_id / row_atom_index /
  table_target_id / source_row_index / row_text / retrieval_rules_version / built_at / row_search_tsv`。
  仅严格三列 `序号/提问内容/回复内容` source table 产生 source-bound 行候选；同一行内可做问题词与
  回答词 AND，跨行不得拼接；`source_row_index` 是原 HTML `<tr>` 的 zero-based 序号。它不改变
  parent Unit、payload、hash 或 locator，也不是新证据；畸形、
  单格问答或 PostgreSQL tsvector 不安全行不生成，parent word/leaf/lossless-window 通道继续保留。
  private parent 的 ready flag + safe-row count/manifest 让 delta 精确发现并修复 child 缺失、额外、
  版本漂移或 0044 降升后被清空的派生状态。
  **派生、非证据**：
  不进 content/query_projection 哈希，重建不产生 outbox 事件；`retrieval_rules_version` 是重建
  幂等键（词典/jieba 升版即全量重建）。CLI `make rebuild-search-projection`（`ALL=YES` 全量），
  worker 每轮 publish 后跑增量。
- **L2 直读纪律**：必须过滤 `is_active_run`（历史 run 行是 U5 审计语义）。

## 3.1 disclosure_ops 运维读面

- `unit_build_terminal_v1`：只列**尚未被后续成功 Unit 代际修复**的 parse 成功但 Unit build 失败，
  或当前未被后续成功代际替代且语义裁决处于
  `degraded_unavailable` / `failed_closed` 的 run；保留 retryable、完整语义终态计数、v2 收据
  身份与 active 标记。它是 build 修复/死信盘点的统一 SQL 入口，不进入公共 Filing 契约。
- worker 对超过 build retry ceiling 的 run 生成去重 dead-letter 告警；health 在重试中 build、
  build dead-letter、active degraded 或 ops 查询失败时顶层为 degraded。修复走显式
  `rebuild-units` 新代际，不自动对同一坏输入紧循环。

## 4. 词表/配置文件索引（versioned，改=升版）

**词表升版纪律（0016 起）**：分类为视图现算，**无存量残留**。升版 = 改 JSON +
`make load-rules`（事务内 TRUNCATE+INSERT，doctor 校验版本一致），全库即刻生效。
质量环：`scripts/audit_unmapped_codes.py`（语料中未被 class/facet 覆盖的内容码
→ 人工晋级）。watchlist filing_categories 收 class 键（经词表展开为前缀）。

| 文件 | 内容 | 当前版本 |
|---|---|---|
| application/contracts/provider_document_envelope.py | 新 writer 的 canonical primary parse artifact codec；必须经独立 PDF 校验与 MinerU bundle 全量重读 admission，codec 本身不是 source trust boundary | provider_document.v1 |
| application/contracts/provider_unit.py + application/services/provider_unit_builder.py | 闭合 Unit locator/search binding 与 deterministic coarse Unit 投影；不含业务 taxonomy、proof graph 或 cell repair | provider_unit.v23 |
| application/contracts/normalized_ir_v4_evidence.py | 冻结历史 v4 evidence manifest 的最小只读 resolver；不得被新 writer import，也不支持 Build/Publish/Rebuild | normalized_ir.v4 read-only |
| adapters/sources/cninfo/class_map.json | **统一 class 词表 31 类**（+correction_supplement 0127 更正件——edgartools amendments 对照；prefixes+priority+zh+std_refs；r6 financing +011711 担保/011713 财务资助、meeting_resolution +01239910；r7 equity_share_change +0115 父级实码） | 2026-07-r7 |
| adapters/sources/cninfo/facet_map.json | F006V 维度判定（market 精确码/publisher 0101） | 2026-07-r1 |
| adapters/sources/cninfo/filing_type_map.json | 无码通道标题关键词兜底（intermediary carrier 词最前，briefing/inquiry 在定期报告前）+ 65 个 title_topic 词补码盲区 + 18 个 title_noise hard pattern。r12 金融复核将 41 个事实 pattern 与 26 个待可靠去重 pattern 移出绝对门；r13 恢复 6 条自我标识副本/序次重复项；r14 补齐业绩预告与股权激励的标准公告标题；r15 在定期报告优先级之后补齐事件更正公告，使 Document filing scope 能约束 Unit semantic candidates且不把年度报告更正降成事件件 | 2026-08-r15 |
| **config/processing_policy.json** | process 20 类=下载+解析；r4 将 equity_share_change 纳入以覆盖当前股数、流通/限售与未来解禁，register_only 11 类=只登记；carrier 类共码不放行，除非其自身在生效集合；按公司覆盖=watchlist process_classes | 2026-07-r4 |
| config/watchlist.csv | 股票池导入/快照文件 + 按公司级联覆盖；运行时真源是 DB `tracked_company`。文件/库行数不等本身不是可自动 prune/import 的授权 | git 即版本 |

运营者旋钮总索引：`config/README.md`（级联模型/命令速查/两类文件边界）。

## 5. 设计讨论记录

**applicability 要不要改 0/1/2（round12 用户提问）**：建议**保持 varchar 枚举**。
理由：①自解释——直接 `WHERE applicability='not_applicable'`，int 码必须回查字典，
运维/审查成本更高（本字典的存在恰恰证明认知负担是真实成本）；②PostgreSQL 惯例是
text+CHECK（int 码是无 CHECK 时代的习惯），存储差异在本规模（<1% 行非空）不可测；
③它已进 public 契约与 query_projection_hash，改类型=契约升版+全量投影事件翻搅，
收益不成比例。若未来引入第三态语义（如 partially_applicable），扩 CHECK 即可。
不同意可推翻——改动路径：0016 迁移+契约升版。

**semantic_keys 里的 key 用英文还是中文（round13 决策：英文规范键 + 词表即中文标签层）**：
键是机器路由标识，ASCII 标识符在 SQL/API/代码中零引号零编码负担；XBRL 正是这个
模式（英文 element name + 中文 standard label），tushare 同理（英文字段+中文文档）。
当前 Provider writer 在 Unit 构建后执行独立的检索路由阶段，不改变切分、payload、标题或
headpath。它使用英文规范 key、中文标准标签的版本化闭集：`semantic_keys` 保存完整有序的
Unit 直接主题；`section_keys` 单独保存可重建的规范化章节位置，不再另存 primary scalar。
低成本模型只能逐项裁决最多 8 个直接主题候选；章节键完全确定性生成。`document_content` 仅是
私有 receipt 的成功 fallback 标记，绝不入库冒充窄语义。0034/0036 恢复两条检索轴，但不复活
历史自由词面、全部祖先传播或公司专例规则堆。
