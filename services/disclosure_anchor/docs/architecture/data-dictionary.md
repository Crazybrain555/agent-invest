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
security_id PK；company_id FK；security_code+exchange 定位（exchange 全大写 SSE/SZSE）；board 可空；status active。

### tracked_company（盯盘配置——唯一人工输入的落库形态）
| 列 | 含义 |
|---|---|
| status | active / paused（暂停=改字段不删行；真源是 config/watchlist.csv，`make track` 对账） |
| lookback | jsonb `{"days": N}`；覆盖默认初始回补 1095 天 |
| filing_categories | jsonb 数组（F006V 前缀）；同步侧过滤；NULL=全量 |
| sync_frequency | 闭集 hourly/daily/weekly；NULL=全局 DISCLOSURE_SYNC_INTERVAL_SECONDS |

### document（披露文件版本）
| 列 | 含义 |
|---|---|
| status | 公开可消费态：registered → parsed \| parse_failed →（发布后）published；published 永不降级 |
| filing_type | **一级粗桶**闭集 9 值：annual/semiannual/quarterly_report, performance_forecast/flash, investor_relations, performance_briefing, inquiry_reply, other（新值=契约升版） |
| disclosure_topics | **二级分类** jsonb 数组（0014）；F006V→topic_map.json 派生，12 topic；web 通道无 F006V→NULL；GIN 部分索引 |
| report_period | `YYYY(A|Q1-4)`；定期报告必填，临时公告可 NULL，不伪造 |
| raw_file_relpath/raw_file_hash | 相对路径+sha256；原始 PDF 不可变只追加 |
| provider_metadata | jsonb：raw_category（F006V 原串）、category_names（中文分类名数组）、file_signature、oversized 标记等 |
| supersedes/correction_of_document_id | 版本链（修订/更正） |

### processing_run（处理运行=action_log 特化）
| 列 | 含义 |
|---|---|
| run_kind | parse / rebuild_units（复用解析产物只重切，5 秒级） |
| status | running / succeeded / failed；stale running 由 worker 按阈值回收 |
| is_active | 每文档唯一 true（发布原子切换） |
| builder_rules_version | 恒等于 rules.RULES_VERSION（当前 ub-2026.07-15）；单一代=同版本 |
| content_hash_aggregate / structure_hash | run 级聚合（U3）；"内容没变"只看前者 |
| parser_* / *_relpath / artifact_hash | 解析出处与产物引用（相对路径） |
| unit_build_status/attempt_count/error | 构建生命周期；error 为结构化 {stage,error_code,retryable} |

### document_unit（L2 消费的最小可寻址单元）
| 列 | 含义 |
|---|---|
| asset_id | `du_`+ULID；跨 run 不承诺同 ID，身份=content_hash |
| payload_kind | 闭集 text / table / qa / **mixed**（业务块，payload=semantic_type+有序 parts） |
| heading_path | jsonb 1-4 级**多级标题**（必填非空；GIN jsonb_path_ops 精确包含）。可检索形态=视图列 heading_path_text |
| title | 叶子显示名（单值；多级在 heading_path，决策记录见 retrieval 设计文档 §4.5） |
| semantic_key | 单值路由键（规则命中优先，否则回落词表键）；btree 索引 |
| semantic_keys | jsonb 数组=规则键∪词表键（note_key_map 142 键）；GIN(jsonb_ops) 支持 `? / ?|`——**多值标签的正确 PG 检索形态**（`semantic_keys ? 'dividend'` 走位图索引） |
| payload | 纯内容（**禁**任何规则派生字段——进 content_hash，U2） |
| content_hash / query_projection_hash / structure_hash | 三哈希分层（U2）；projection 含 title/heading/semantic_key(s)/quality/applicability |
| quality_status | ok / needs_review / unusable（乱码率>30%） |
| applicability | vc16 CHECK：applicable / not_applicable / NULL（√适用声明列化；见 §5 讨论） |
| page_no | 定位列（artifact_locator 首页码） |
| artifact_locator | jsonb（order_index/page_no/bbox/merge 信息）；JSONB(none_as_null) |

### source_access / source_checkpoint / provider_category
- source_access：每次 provider 访问一行（**失败也留痕**，含 profile 拉取失败）；query_params 已剔除凭据；error 结构化。
- source_checkpoint：scope_key=`company_id:p_info3015`；cursor={window_end, window_start, synced_at}（后两个为审计字段，判定只用 window_end 与 updated_at）。
- provider_category：F006V 字典 2135 行（p_info3005 快照 seed）。

### ops.outbox_event
seq 单调；event_kind 闭集（document_registered/observed、processing_run_created/failed/published、document_unit_created/removed/projection_changed）；change_kind=observed/materialized（下游失效只由 materialized 触发）；projection_changed 携带非空 changed_fields。

## 3. disclosure_public 视图（唯一读契约）

- **document_units_v1（38 列）**：core 列 + 派生（is_active_run、heading_path_text 面包屑、
  contract_version、company_ref/security_ref、security_code/exchange、filing_type、
  disclosure_topics、report_period、announcement_date、source_ref、parent_ref、asset_kind、
  observed_at、source_tier、trace_level、raw_file_hash）。列集权威=contract-checklist §2。
- documents_v1 / processing_runs_v1 / source_refs_v1 / change_events_v1 / document_categories_v1。
- **L2 直读纪律**：必须过滤 `is_active_run`（历史 run 行是 U5 审计语义）。

## 4. 词表/配置文件索引（versioned，改=升版）

| 文件 | 内容 | 当前版本 |
|---|---|---|
| adapters/unit_builder/rules.py | 切分/噪声/声明组合文法/语义规则 | RULES_VERSION ub-2026.07-17 |
| adapters/unit_builder/note_key_map.json | 章节词表 **144 键**（section facet；祖先继承+全类型开放） | 2026-07-r4 |
| adapters/unit_builder/event_key_map.json | 事件键 **30 键**（DuEE-fin/CCKS/FewFC/CFinDEE 并集，标题派生） | 2026-07-r1 |
| adapters/sources/cninfo/filing_type_map.json | F006V→filing_type 9 桶 | 2026-07-r3 |
| adapters/sources/cninfo/topic_map.json | F006V→disclosure_topics 12 题 | 2026-07-r1 |
| application/worker/parse_scope.json | 分层解析 core_topics | 2026-07-r2 |
| config/watchlist.csv | 股票池唯一真源 | git 即版本 |

## 5. 设计讨论记录

**applicability 要不要改 0/1/2（round12 用户提问）**：建议**保持 varchar 枚举**。
理由：①自解释——直接 `WHERE applicability='not_applicable'`，int 码必须回查字典，
运维/审查成本更高（本字典的存在恰恰证明认知负担是真实成本）；②PostgreSQL 惯例是
text+CHECK（int 码是无 CHECK 时代的习惯），存储差异在本规模（<1% 行非空）不可测；
③它已进 public 契约与 query_projection_hash，改类型=契约升版+全量投影事件翻搅，
收益不成比例。若未来引入第三态语义（如 partially_applicable），扩 CHECK 即可。
不同意可推翻——改动路径：0016 迁移+契约升版。

**semantic_key 用英文还是中文（round13 决策：英文规范键 + 词表即中文标签层）**：
键是机器路由标识，ASCII 标识符在 SQL/API/代码中零引号零编码负担；XBRL 正是这个
模式（英文 element name + 中文 standard label），tushare 同理（英文字段+中文文档）。
中文层已经存在——note_key_map/event_key_map 的 names/aliases 就是法定中文名，
即双语词典本体；L2 查询侧中文→键的映射用它（06R 同义词表正式化）。不做中英双写键
（同概念两拼写会碎化过滤）。若要行内可见中文，06R 投影的 controlled_keywords 可带
中文标签进 search_text。

**semantic_keys 为什么用 jsonb 数组不用关联表**：多值标签三种形态中，
jsonb+GIN（现状）查询 `? key` 走索引（已 EXPLAIN 验证）、随行读取零 join；
关联表规范化收益仅在"按 key 统计全库"高频时兑现（L2 场景以按文档/按单元过滤为主）；
text[] 与 jsonb 等价但 API/契约序列化不如 jsonb 自然。维持现状。
