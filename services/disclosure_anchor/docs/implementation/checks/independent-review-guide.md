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
一致）；词表文件 note_key_map r16（173 键、389 标签）/ event_key_map r2（35 事件键）/
class_map(r5,31类)/facet_map/config/processing_policy.json（0016 起分类视图现算、round21 单一处理面+级联覆盖，design/classification-facets-and-derived-views.md）；三 facet 架构（retrieval 设计文档 §4.6）。

## 0.1 原审查前置

- 读：`docs/architecture/service-purpose.md`（canonical 契约）→
  `docs/implementation/design/*.md`（watchlist/检索两份决议）→
  `docs/implementation/milestones/09-production-readiness.md`（背账）→
  05 里程碑 §8.5（规则包 ub-2026.07-2..-52 修订史）。
- 环境：`set -a; source ~/.config/agent-invest/disclosure_anchor/worker.env; set +a`；
  DB 只读直连 psql 或 dbhub。
- 语料状态：`make track-status`；确认单一规则代
  （`SELECT DISTINCT builder_rules_version FROM ... WHERE is_active`）。

## 1. 表字段 review（逐表逐列，问四个问题）

对 `disclosure_core` 每张表的每一列问：①语义是否唯一清楚（与 service-purpose §5-§7
对得上）？②空值合法吗（NULL=什么意思，是 SQL NULL 不是 JSON null）？③谁写它、
何时变（不可变列被 update 过吗）？④L2 按它筛选时有索引吗？

重点核对项：
- company/company_identifier/security：USCC 唯一、identifier 有 source_access、
  exchange 全大写、无 PENDING_LEGAL_NAME 残留（有=该公司从未成功同步过）。
- tracked_company：与 config/watchlist.csv 对账零漂移（`make track` 输出 drift=0）；
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
-- 附注子项失怙（cn_a_v6 驱逐 bug 回归探测）：点号子项直挂 、号章级（3 层且
-- 尾项=title）= 科目父级被吞的签名，判读清单（当前正本语料为 0；宽式
-- 「无 \d、祖先」不可作门——决议议案/审计报告科目层合法直用点号）
SELECT provider_document_id, title, heading_path FROM disclosure_public.document_units_v1
WHERE is_active_run AND title ~ '^\d{1,3}[.．](?!\d)'
  AND jsonb_array_length(heading_path) = 3
  AND heading_path->>1 ~ '^[一二三四五六七八九十]+、' AND heading_path->>2 = title;
-- 过碎审计：filing_type='other' 且 units>=10 且总字数<8000 的文档需逐一给理由
-- 过粗审计：单 unit chars>15000 的抽查其 parts 是否同主题
```
标题吞没对账环（round15 制度化——**DB 内类扫描看不见"从未入库"的丢失**，
本环是唯一能系统抓住该类的手段）：
`PYTHONPATH=src .venv/bin/python scripts/audit_heading_coverage.py`
——逐文档核对 IR 里每个 heading 元素必须出现在单元的 title/heading_path/
parts.local_heading/parts.heading_path/正文行之一；自动分类扉页标题行与空节
（信息性）。预期残余=1（江海年报"（1）在子公司所有者权益份额…"模板反转，
见 §4）；新增 SWALLOWED 即为切分 bug。round15 用它抓出两个真 bug：
S5 续表合并只看列数（cn_a_v6 后同构附注表跨科目误并，3. 销售费用类标题
全域蒸发）与 S6 把 headers-only 表判空丢弃（分部信息类整支路径蒸发）。

变体发现环（业界定式：C4/eDiscovery 频率法，round11 调研落地）：
`PYTHONPATH=src .venv/bin/python scripts/audit_boilerplate_candidates.py`
——跨文档高频短行且未被现有模式族覆盖的 = 候选新套话变体，人工确认后
晋级进 rules 模式族并升 RULES_VERSION；切分时永远只跑确定性模式。

目检协议：随机抽 ≥5 份不同 filing_type 的文档，`pdftoppm` 渲染对应页
（page_no 列可定位），逐单元对照 PDF 判断：边界是否业务完整、标题归属是否正确、
表格是否整存、mixed parts 顺序/local_heading 是否与版面一致。

## 3. code review 分层清单

- **domain**：实体无 IO；枚举闭集走契约升版；错误分型 retryable 语义正确
  （尤其 quota_exhausted=请求内 fail-fast + 下轮可重试）。
- **unit_builder**：规则全部在 rules.py 且版本化（改规则必升 RULES_VERSION）；
  builder 纯函数无 IO；词表 JSON（note_key_map/event_key_map/class_map/facet_map/parse_scope）
  与代码读取键一致；内容哈希纯净性——payload 不得含任何规则派生值（U2）。
- **worker**：队列谓词只在 queries.py；批次上限/背压/熔断路径有测试；
  单项异常隔离不破轮。
- **sources/cninfo**：凭据只从 env；query_params 持久化前剔除 token；
  429/透传错误分型；两通道（API/web）候选形状一致性。
- **api**：读端点只出 public 视图列；错误 envelope 无堆栈无绝对路径；
  admin 默认不挂载；DERIVED 白名单={asset_uri}。
- **migrations**：新改动从 0016 起；视图变更=契约变更（列 pin 测试 + checklist 同步 +
  export_contracts 重导）；已应用迁移不改。
- **tests**：新行为必有回归测试；集成测试 tearDown 自清理；对真库敏感的测试
  （LIMIT/日期排序）必须对积压免疫。

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
- semantic_keys 覆盖纪律（round13 用户裁决"检索靠它，不能少"）：词表键做祖先继承
  （无科目语义的叶子从最近科目祖先取键+章级键），并对全部 filing_type 开放；
  验收口径=年报/审计报告附注 NULL 为 0、全库覆盖 ≥95%；剩余 NULL 仅限公告头存根
  与词表外标题（当前 **12** 个：round13 的 11 项 + round15 救回的文内扉页存根；Codex 验收逐条核过全为接受类），每轮类扫描复核该清单未增长。
- semantic_key 用英文规范键 + 词表文件即中文标签层（XBRL 模式，round13 决策，
  见 data-dictionary §5）；不做中英双写键。

## 5. 开放背账（review 时核对是否恶化）

见 09 里程碑待办区。数据正确性四大项：checkpoint 空洞、changes feed 并发跳事件、
下载 403 毒化、公司改名卡死。均未修，review 应验证其仍是"未恶化的已知项"。

## 6. 每轮审查的产出格式

P0/P1/P2 分级 findings（行级证据 + 修法建议）+ "已核无发现"清单 + go/no-go 结论。
假阳性教训：jsonb 数组用 `->>` 取出后是文本渲染，判断类型用 `jsonb_typeof`。
