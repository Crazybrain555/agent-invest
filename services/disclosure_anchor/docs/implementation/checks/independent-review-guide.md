---
id: disclosure_anchor_independent_review_guide
project: disclosure_anchor
title: 独立审查大清单（code review + 表字段 review 开工指南）
status: living
created_at: 2026-07-07
---

# 独立审查大清单

**用法**：新 session 的独立审查按本清单开工。立场 = "L2 拿这些数据能不能直接用"，
不是"代码能不能跑"。每一节先跑 SQL/命令取证，再下判断；每个 finding 带
file:line 或行级 SQL 证据。历史经验：**逐案打地鼠不如类扫描**——本清单的
SQL 都是"抓一整类"的写法。

## 0. 审查前置

新增审查对象（round12-16）：`docs/architecture/data-dictionary.md`（逐列核对与实库
一致）；词表文件 note_key_map r18（173 键、391 标签）/
class_map(r7,31类)/facet_map r1/config/processing_policy r4（0016 起分类视图现算、round21
单一处理面+级联覆盖，design/classification-facets-and-derived-views.md）；已撤销的
event_key_map 不得作为当前审查对象，现状勘误见 retrieval 设计文档 §6.3。

## 0.1 原审查前置

- 读：`docs/architecture/service-purpose.md`（canonical 契约）→
  `docs/implementation/design/*.md`（watchlist/检索两份决议）→
  `docs/implementation/milestones/09-production-readiness.md`（背账）→
  05 里程碑 §8.5（规则包 ub-2026.07-2..-53 修订史）。
- 环境：`set -a; source ~/.config/agent-invest/disclosure_anchor/worker.env; set +a`；
  DB 只读直连 psql 或 dbhub。
- 语料状态：`make track-status`；确认单一规则代
  （`SELECT DISTINCT builder_rules_version FROM ... WHERE is_active`）。

## 0.2 可见 Pro/Fable 外部审查交付

当 ChatGPT/Claude 的仓库连接器不可用、读不到私有历史，或需要把本地未跟踪的质量证据
一起交给 reviewer 时，不要只粘贴零散代码。建立可复现的 review packet，并把交付事实写入
`docs/agent/HANDOFF.md`：

1. 在 `/Users/zhang/Downloads/buddle_code/<review-id>/` 集中准备完整 tracked source snapshot、
   verified Git bundle、当前 staged/unstaged binary patch、全部 in-scope untracked 文件、审查提示、
   SHA manifest，以及 current/held-out 行级报告和 reset/replay receipt。准备阶段可在该目录更新；
   一旦上传即冻结该版本，后续字节变化另建 versioned review-id，禁止覆盖已发 packet。禁止放入
   `.env`、凭据、数据库 URL、HANDOFF/RUNTIME 私有状态、raw PDF 或 AgentSSD artifact tree。
2. 附件过大时拆为 `source.zip`、`review-evidence.zip`、`review-history.zip`。通过用户指定的
   Codex App `@Browser` 自有 file/binary upload 接口上传，不切换 Chrome、Computer Use 或 Mac
   原生 file picker。ChatGPT 可能把附件改成 UUID 文件名，必须在 prompt 中列出“网页文件名 →
   本地逻辑名 → SHA-256”的映射；附件未全部可见前不得发送审查 prompt。
3. prompt 同时给出公开仓库 URL 与 exact commit，要求 reviewer 先读附件，缺文件时只按该
   commit 从 Web 补读。公开 URL 是缺口补读，不替代附件内的未跟踪证据或精确 commit 绑定。
4. HANDOFF 记录 exact commit、每个附件 SHA、conversation/session URL、最终 verdict、经本地
   exact bytes 验证的 findings 与采取的修复。acknowledgement、`thinking`、中断、额度耗尽或
   旧 artifact 的结论都不是 verdict；不得据此重复上传、重复清库或重复外部动作。
5. reviewer 只读；其 finding 是待验证 claim，不是自动改代码的授权。P0/P1 先在当前 tree、
   public view 和代表性行上复现，再做最小通用修复；P2 必须说明是否值得当前阶段处理。
6. reviewer 思考期间不得修改被审字节、重生引用证据、追加 prompt 或并行实施。若 target 已变化，
   原回答只可记为 stale evidence，必须用新 packet 重审。`thinking`、半截回答、超时、中断或额度
   错误都不是 verdict。完整答复、本地验证和必要复审全部关闭且 HANDOFF 已记 SHA/结论后，才将
   该 exact review-id packet 移入废纸篓；不得递归删除共享 `buddle_code` 根或其他 active packet。
7. “live 与 source replay 零差异”必须由仓库内可复跑的只读脚本生成。receipt 至少列明比较字段、
   source/replay 身份、live generation 标记，并给双方规范化行集的独立聚合哈希；只有
   `mismatch_count=0`、没有字段清单/聚合哈希/生成脚本的 JSON 不是可证伪的验收证据。脚本必须从
   同一份已读取字节同时解析 replay 和计算 receipt SHA，禁止比较后再次按路径读取；replay 每个
   比较字段必须显式存在，route arrays 必须是数组。只有 live public-view 的
   `semantic_keys/section_keys NULL` 可按声明归一化为 `[]`；哈希字段必须是 lowercase hex SHA-256。
   live 查询范围必须从 replay 的完整 provider-document 集合机械派生，并读取这些 provider 的全部
   active Units（不得只按 expected identity join，也不得无条件扫描其他 provider）；receipt 绑定排序后的
   provider 集合哈希、各 active run ID 与同一只读 repeatable-read snapshot，额外/缺失 unit_index 均失败。

## 1. 表字段 review（逐表逐列，问四个问题）

对 `disclosure_core` 每张表的每一列问：①语义是否唯一清楚（与 service-purpose §5-§7
对得上）？②空值合法吗（NULL=什么意思，是 SQL NULL 不是 JSON null）？③谁写它、
何时变（不可变列被 update 过吗）？④L2 按它筛选时有索引吗？

重点核对项：
- company/company_identifier/security：USCC 唯一、identifier 有 source_access、
  exchange 全大写、无 PENDING_LEGAL_NAME 残留（有=该公司从未成功同步过）。
- tracked_company：运行时以 DB 为权威，逐行检查状态/覆盖/生效值；config/watchlist.csv 只是
  导入/快照文件。库/文件差异必须报告并解释，未经明确 prune/import 指令不得以“零漂移”为目标
  自动改任一侧；
  lookback/filing_categories/sync_frequency 三列有值时 worker 真在用（queries.sync_due）。
- document：表列 filing_type 仅为注册兜底；**消费面以视图为准**——filing_type=
  class 词表 argmax（30 类）、disclosure_topics=命中集合、三维拆解列与
  provider_metadata.raw_category 一致（抽 5 行手工对 class_map.json）；
  规则质量环：`scripts/audit_unmapped_codes.py` 必须输出 none；
  doctor 的 classification rules 检查必须 pass（词表版本一致）；
  report_period 定期报告必填；
  status 生命周期（published 不降级）。
- document_unit：见 §2 切分审查。三哈希列非空且 content_hash 不含检索派生字段。
- processing_run：每文档 active 唯一；无 status='running' 孤儿（stale reclaim 工作）；
  builder_rules_version 单一。
- source_access/source_checkpoint：失败也有行（profile/index 双路径）；cursor 含
  window_end/window_start/synced_at；无凭据泄漏（query_params 无 token/secret）。
- outbox_event：projection_changed 的 changed_fields 非空；seq 单调。

## 2. 切分质量审查（投资经理视角 + 类扫描 SQL）

固定验收包（应全零；round3 review 文档尾部有完整 SQL）：
A 议案标题误挂 / B 首单元从第三章起 / C 空 heading_path / D 标记行进标题 /
E payload 带 applicability。

类扫描（每轮规则变更后必跑）：
```sql
-- 微型孤儿单元：<25 字独立 text 单元全清单（人工逐行判：声明变体该剥、
-- 标签碎片该丢、实质一句话事实该留）
SELECT provider_document_id, heading_path->>-1, payload->>'text'
FROM disclosure_public.document_units_v1
WHERE is_active_run AND payload_kind='text'
  AND length(coalesce(payload->>'text','')) BETWEEN 1 AND 25;
-- title 空值
SELECT count(*) FROM disclosure_public.document_units_v1 WHERE is_active_run AND title IS NULL;
-- 声明残留（单位/保证/适用 三族）
SELECT count(*) FROM disclosure_public.document_units_v1
WHERE is_active_run AND payload->>'text' ~ '单位(均)?[为是]?[：:]\S{1,12}$';
-- 附注点分子项失怙：N.M / N.M.K 标题的 heading_path 必须保留最近的 N、父级。
-- 先列出所有点分标题供人工判读；不能使用 `(?!\d)`，它会把真正的点分编号
-- 排除在探针之外，也不能只以固定三层路径作唯一签名。
SELECT provider_document_id, title, heading_path FROM disclosure_public.document_units_v1
WHERE is_active_run AND title ~ '^\d{1,3}([.．]\d{1,3})+'
ORDER BY provider_document_id, order_index;
-- 过碎审计：filing_type='other' 且 units>=10 且总字数<8000 的文档需逐一给理由
-- 过粗审计：单 unit chars>15000 的抽查其 parts 是否同主题
```
Provider 原生完整性对账环（**DB 内类扫描看不见“进入 provider projection 前”的丢失**）：

1. `make doctor-full` 必须逐个验证 source PDF、provider-document artifact、Unit snapshot 和
   active semantic receipt 的 owner/hash；任何缺失或漂移均失败。
2. 先用 `make generate-current-source-replay EVALUATION=<新 evaluation> RECEIPT=<新 source receipt>
   SOURCE_REVISION=<不可变源码版本>` 在单一 `REPEATABLE READ READ ONLY` 事务中遍历全部 active
   generation。该命令必须重新读取 immutable source PDF、ProviderDocument/bundle 与 hash-bound
   semantic receipts，走生产 publication guard 重建 `provider_unit` 并逐字段核对 private Unit；
   receipt 同时绑定当前源码树、taxonomy/router/builder、每份 source/artifact/receipt hash 与 DB snapshot。
   evidence 文件只允许新建，不覆盖旧收据。
3. 再用 `make audit-live-unit-replay REPLAY=<上一步 evaluation> OUTPUT=<新 live 收据>
   SOURCE_REVISION=<同一源码版本>` 对 replay 中每个 provider 的全部 active Units 做第二个
   repeatable-read public-view 对账；字段清单、provider 集合、active run、双方聚合哈希及额外/缺失
   索引都必须闭合。
4. source replay 证明的是“冻结 provider projection → live Unit”的完整性，不能单独证明 PDF
   从未被 MinerU 漏读。对 `source_quality_findings`、表格和可疑标题页必须回到同一 hash-bound
   PDF 渲染目检；新增 provider 形态要加入冻结代表样本和确定性回归，不得引用已退役的
   NormalizedIR heading/boilerplate 脚本，也不得把跨文档频率启发式重新写回 L1。

目检协议：随机抽 ≥5 份不同 filing_type 的文档，`pdftoppm` 渲染对应页
（page_no 列可定位），逐单元对照 PDF 判断：边界是否业务完整、标题归属是否正确、
表格是否整存、mixed parts 顺序/local_heading 是否与版面一致。

## 3. code review 分层清单

- **domain**：实体无 IO；枚举闭集走契约升版；错误分型 retryable 语义正确
  （尤其 quota_exhausted=请求内 fail-fast + 下轮可重试）。
- **provider unit builder**：薄的 source-bound 投影带显式版本；禁止恢复业务 taxonomy
  rules、文档短语补丁或第二证明图；
  builder 纯函数无 IO；词表 JSON（note_key_map/class_map/facet_map/parse_scope）
  与代码读取键一致；内容哈希纯净性——payload 不得含任何规则派生值（U2）。
- **worker**：队列谓词只在 queries.py；批次上限/背压/熔断路径有测试；
  单项异常隔离不破轮。
- **sources/cninfo**：凭据只从 env；query_params 持久化前剔除 token；
  429/透传错误分型；两通道（API/web）候选形状一致性。
- **api**：读端点只出 public 视图列；错误 envelope 无堆栈无绝对路径；
  admin 默认不挂载；DERIVED 白名单=document_unit.{asset_uri,evidence_refs} +
  source_ref.{evidence_refs}。
- **migrations**：新改动从 0016 起；视图变更=契约变更（列 pin 测试 + checklist 同步 +
  export_contracts 重导）；已应用迁移不改。
- **tests**：新行为必有回归测试；集成测试 tearDown 自清理；对真库敏感的测试
  （LIMIT/日期排序）必须对积压免疫。
- **L2/L3 检索评测**：graded judgment 必须覆盖 full/ablation 的全部实际评测池，未判定结果
  不能默认 grade 0；每个 judgment 同时绑定 `query_projection_hash` 与 answer-bearing
  `content_hash`，source replay 与 live public search 两边都须逐 Unit 相等。仅按
  `(provider_document_id, unit_index)` 或只绑 query hash 的旧 receipt 不能证明数据质量。

## 4. 已知接受项（不要重复开 finding）

- 仅携带巨潮杂项码（012399/352399 其它事项公告）的文档 filing_type='other'
  ——巨潮自己拒绝分类的，映射即捏造语义（audit_unmapped_codes.py 的
  ACCEPTED_MISC_CODES）；标题关键词兜底已在注册层运行。

- 标题对账环接受类（round15）：①文内扉页标题行（首个编号标题之前的封面标题，
  document.title 已承载，脚本自动归类）；②空节（标题至下一标题间无有效内容，
  42 处，脚本自动归类为 empty sections——含 12、应收票据等零余额附注槽位）；
  ③江海年报"（1）在子公司所有者权益份额发生变化的情况说明"：模板反转
  （（1）包裹层在 1. 子项之上，与 cn_a_v6 主流约定相反），内容完整锚定在
  近同名兄弟"1. 在子公司的所有者权益份额发生变化的情况说明"下，检索零损失；
  确定性管线不同时支持两种嵌套方向，接受。
- headers-only 表（表头非空、数据行全空）自 ub-2026.07-18 起**保留**为 table
  单元——表头是原文内容且承载标题路径（分部信息类）；不要报"空表未剔"。

- 同主题大 mixed 单元（主营业务分析 25 parts）——用户裁决可接受。
- 一句话完整附注（2、会计期间 /「详见附注 X」式转指引）——原子事实，独立保留
  （round10 决定，round14 系统分析确认：合并要发明阈值且破坏锚定粒度/跨公司槽位对齐，
  没有赢的维度）。一句话**子项**（发出存货计价方法等）不属此类——cn_a_v6 修复
  科目标题驱逐 bug 后，它们由既有 section 分组自然归进科目 mixed 单元（parts 带
  local_heading，引用粒度不丢）；再出现失怙孤儿子项按 §2 跳级探测处理。
- "详见附注 X"交叉引用单元——真实内容，保留。
- 金融工具风险节内部 1、/(一) 层级倒置的次级归属（不窜根即可）。
- web 兜底通道 disclosure_topics=null、三维拆解列=null（接口无 F006V），filing_type 走标题规则（rule_set='title'）——设计内。
- L1 不追求 semantic-key 机械满覆盖，也禁止用 `document_content`、`other_information` 或
  Document facet 填充占位语义；但当前 writer 会从 Unit 自身标题/正文/表格的闭合证据生成
  `semantic_keys`（直接主题），并从 hash-bound accepted heading path 的精确结构容器生成
  `section_keys`（章节位置）。两者都用英文规范键并对 L2 公布同源 catalog；真实无证据行允许
  NULL，仍由 title/path/body 词法检索承载。审查必须同时防止可恢复的系统性 NULL 和无证据补键。

## 5. 开放背账（review 时核对是否恶化）

见 09 里程碑待办区。数据正确性四大项：checkpoint 空洞、changes feed 并发跳事件、
下载 403 毒化、公司改名卡死。均未修，review 应验证其仍是"未恶化的已知项"。

## 6. 每轮审查的产出格式

P0/P1/P2 分级 findings（行级证据 + 修法建议）+ "已核无发现"清单 + go/no-go 结论。
假阳性教训：jsonb 数组用 `->>` 取出后是文本渲染，判断类型用 `jsonb_typeof`。
