---
id: disclosure_anchor_notes_process_classes_review
title: 全局处理策略 process/register_only 分界审计（31 类逐类）
date: 2026-07-12
updated_at: 2026-07-13
status: adopted-and-implemented
implementation_commits: [040b50f, d715f59, 33a2927]
authority: historical decision and validation evidence; current behavior is defined by tracked config, migrations, contracts, and tests
inputs: live DB 只读取证（13 家公司 1236 份登记文档）+ 巨潮官网对照 + 两路对标调研（美股 SEC 工具与研究 / A 股卖方实务）
---

> **阅读与权威。** §0–§4 是实施前审计，若与“落地记录与修正”冲突，以后者为准。
> 当前运行语义以 `config/processing_policy.json`、
> `src/disclosure_anchor/adapters/sources/cninfo/filing_type_map.json`、迁移 0021、
> data dictionary 和测试为准；本文保存用户裁决、取证、误伤核验与清理 RCA，不是运行配置。
>
> **最终落地。** P0+P1、carrier gate 和 title topic 主体见 `040b50f`；review fixes 见
> `d715f59`；title noise gate 见 `33a2927`。数据库清理数字是 2026-07-13 的历史操作证据，
> 不是可复现的当前库存。

> **重要修正（2026-07-12 晚，实现阶段发现）：本文 §0 的 F1 结论已撤回，
> 部分成本数字口径有误——先读文末"落地记录与修正"再引用正文数字。**

# 处理策略分界审计（processing_policy.json 2026-07-r1）

判断标准（协议 v0.8 §0.4 原则一/二/四）：一个类该不该 process，唯一问题是
"它对预测/解释/复盘净利润有没有信息量"。既有裁决不动：register 永远全量；
process=下载+解析一个动作；三年财报+重要公告是底线；默认值只服务一般公司，
特殊行业走 per-company process_classes 覆盖。

## 0. 三个先于逐类结论的结构性发现

**F1（语料口径警告）：登记并非全量，零条 ≠ 无信息。** 000001 官网三年 258 条 vs
库内 106 条（≈41%）；全库 1236 条中 0 条标题含"决议/减持/增持/质押/诉讼/仲裁/
审计/社会责任"——这些每家公司必发。同步代码无类过滤、无分页截断证据（单窗最高 56 条，
分布平滑），缺口来自 p_info3015 接口本身的目录覆盖面。10 个零条类（operating_data、
performance_flash、share_pledge、litigation、share_buyback、delisting_risk、
ipo_listing、rights_issue、bond_notice、esg）的"零"主要是上游没给，不是公司没发。
→ 采集层跟进项（web 通道补索引），不在本审计动作范围，但下方所有体量数字按此打折解读。

**F2（边界现状不咬合）：register_only 目前一份文档都没拦住。** 放行语义是"任一
F006V 段命中生效类集合"（worker/queries.py `_download_scope_sql`），而巨潮多段
编码几乎总带一个 process 类段：全库 0 条文档不命中 process 集合；11 个 register_only
类的 sole-reason 边际数全部为 0。即 92 份律师意见书/受托管理报告、68 份激励解锁
公告**现在就在下载+解析**。真正的 GPU/解析浪费杠杆在词表 ANY-hit 语义与程序性
载体共码（见 F3 跟进项），不在策略文件边界；同理，本次建议的移入类回补成本≈0。

**F3（operating_data 是死词表）：A 股真实经营数据流走的是 012305，不是 010309。**
万科月度销售简报、茅台主要经营数据公告、长江电力发电量公告全部被巨潮编成
012305"经营环境重大变化"→ 落入 risk_alert（现属 process，所以**底线未破**——
这些文档正被解析）。010309 前缀在 1236 条语料中零次出现。含义：①risk_alert
绝不能移出 process；②operating_data 移入 process 今天零成本，但要真正接住
月度数据还需词表跟进（给 operating_data 类补标题规则或前缀，class_map 升版 +
make load-rules，独立于本策略文件）。

## 1. 对标结论摘要

- 美股（edgartools/py-sec-edgar + Lerman & Livnat 2010 等）：深解析"业绩释放链"
  （定期报告 XBRL、8-K Item 2.02、earnings call、guidance）与"盈利质量警报"
  （4.02 重述/2.06 减值/5.02 高管变动）；月度经营数据历史上是卖方 nowcasting 核心；
  Form 4/13D 中等（信号型）；治理/会议/债券/ESG 只登记。
- A 股卖方实务：必精读=定期报告、业绩预告/快报、**月度经营数据（强披露行业等同
  小型财报）**、重组/再融资/激励（考核目标=隐性指引）/减值/更正、重大合同、
  问询函回复；字段化监控=质押、减持、担保、诉讼、风险提示；只登记=治理、债券、
  ESG、中介报告；投资者关系记录表居中但有预测价值（机构调研文献）。

## 2. 31 类逐类判定表

体量=disclosure_topics 计数（sole=仅因该类而 process 的边际数）。信息量=对净利润
预测/解释/复盘。建议列：保持 | 移入 process | 移出。

| 类 | 现属 | 体量(sole) | 抽样标题结论 | 信息量 | 建议 + 一句依据 |
|---|---|---|---|---|---|
| annual_report | process | 86(86) | 年报，9317 units，解析主力 | 高 | 保持——三年财报底线本体 |
| semiannual_report | process | 80(80) | 半年报，7874 units | 高 | 保持——同上 |
| quarterly_report | process | 86(86) | 季报 | 高 | 保持——同上 |
| performance_forecast | process | 33(33) | 业绩预告+全部业绩快报（快报被 012111 前缀吸收） | 高 | 保持——预告/快报直接驱动预测修正 |
| performance_flash | process | 0(0) | 死词表：快报实际落 performance_forecast | 高(名义) | 保持——零成本占位，词表修正后接住 |
| operating_data | register_only | 0(0) | 死词表（F3）：真实经营数据在 risk_alert 里 | **高** | **移入 process（P0）**——月度经营数据是预测核心输入（两路对标一致），今天零成本 |
| investor_relations | process | 170(151) | 调研记录表/交流会实录，910 units | 中高 | 保持——A 股的 earnings-call 等价物，问答含产能/订单口径；机构调研文献证明预测价值 |
| performance_briefing | process | 19(0) | 业绩说明会/交流会实录，与 IR 全重叠 | 高 | 保持——earnings call 在美股是一等信息源 |
| inquiry_regulatory | process | 3(1) | 语料仅自查/整改报告（真问询函上游缺，F1） | 高 | 保持——问询函回复是排雷关键文本（《管理世界》2019 等） |
| restructuring_assets | process | 79(71) | 套保/产业基金/子公司增资/CMBS，真重组少 | 中高 | 保持——真 M&A/减值测试直接改利润基数 |
| dividend | process | 126(126) | 分配方案/实施公告 | 中 | 保持——payout 与特别分红入现金质量与复盘 |
| additional_issuance | process | 9(9) | 几乎全是长电督导/核查意见（共码噪声） | 中 | 保持——真增发预案=摊薄，体量小留着 |
| convertible_bond | process | 102(101) | 能辉转债回售/下修/赎回流程件 | 中 | 保持——下修/赎回改摊薄与财务费用；多为小文档成本低 |
| rights_issue | process | 0(0) | 无语料（13 家未配股） | 中 | 保持——触发即重大摊薄 |
| share_buyback | process | 0(0) | 真回购报告书 0 条（上游缺，F1；53 条"回购"全是激励注销） | 中 | 保持——回购注销影响 EPS 分母 |
| major_contract | process | 17(16) | 能辉中标/储能框架合同 | 高 | 保持——小市值公司订单=收入预测直接输入 |
| related_party | process | 96(87) | 万科深铁借款系列+日常关联预计 | 中高 | 保持——困境公司股东输血=生存与利息费用信号 |
| risk_alert | process | 51(51) | **茅台经营数据/长电发电量/万科销售简报/会计政策变更/异动** | **高** | 保持——事实上的经营数据通道（F3），移出即破"重要公告底线" |
| delisting_risk | process | 0(0) | 无语料 | 高 | 保持——触发即持续经营假设变更 |
| equity_incentive | process | 299(296) | 草案/考核办法（高）被大量解锁/归属/行权程序件+共码法律意见书稀释 | 中高 | 保持——考核目标=隐性盈利指引+费用摊销压利润；噪声治理走词表（§4-B3） |
| correction_supplement | process | 13(10) | 年报补充公告（高）混股东会通知更正（低） | 中高 | 保持——更正/重述是盈利质量强信号（10-K/A 对标），体量小 |
| meeting_resolution | register_only | 10(0) | 核查意见/会议通知更正，无真决议（上游缺） | 低 | 保持——议案实质另有实体类承载 |
| governance_rules | register_only | 4(0) | 考核管理办法/独董声明（考核目标本体在草案=equity_incentive） | 低 | 保持——治理章程类两路对标均只登记 |
| intermediary_report | register_only | 92(0) | 全是律师意见书/受托管理/跟踪评级/督导意见 | 低 | 保持——纯程序载体；注意其 92 条现经共码已在解析（F2） |
| equity_share_change | register_only | 68(0) | 全是激励解锁/限售流通/回购注销伴生件；真减持增持 0 条（上游缺） | 低（现语料） | 保持——减持/质押类是字段监控项不是全文精读项（卖方实务）；Form 4 属信号型非利润表输入 |
| share_pledge | register_only | 0(0) | 无语料（上游缺） | 低-中 | 保持——阈值告警型风险监控，元数据够用 |
| litigation | register_only | 0(0) | 无语料（上游缺） | 中 | 保持（P2 候选）——或有负债个案冲击大但难量化；财报附注已在解析覆盖存量诉讼；语料出现体量后复审 |
| financing | register_only | 25(0) | 万科深铁借款（已经 related_party 共码解析）+募投延期/变更 | 中 | **移入 process（P1）**——募投进度改产能预测、借款改利息费用；边际成本 0 |
| ipo_listing | register_only | 0(0) | 无语料（13 家均过发行期） | 低 | 保持——注册期文件对存量公司预测无输入 |
| bond_notice | register_only | 0(0) | 无语料（上游缺；银行债券公告本就另渠道） | 低 | 保持——付息/兑付程序件，财务费用锚在财报 |
| esg | register_only | 0(0) | 无语料（上游缺） | 低 | 保持——两路对标均只登记 |

## 3. 建议变更清单

**P0（明显错配，必须动）**
- `operating_data`：register_only → process。依据：月度/季度经营数据是净利润预测
  核心高频输入（美股 nowcasting 传统 + A 股强披露行业等同小型财报），放在"只登记"
  与 §0.4 原则一直接冲突。成本：现库 0 条回补、0 下载 0 解析；未来强披露公司约
  +12~36 条/家/年（小文档，GPU 均值 73s/条 → 每家每年 <45 GPU 分钟）。注意：
  要实际接住数据还需 F3 词表跟进，本条先把策略意图摆正，防止词表修好后月度数据
  反被策略拦掉。

**P1（建议动）**
- `financing`：register_only → process。依据：募投项目延期/变更改产能与收入
  节奏，股东借款/担保改利息费用与生存判断（万科深铁系列是活例）。成本：现库
  25 条全部已因共码在解析，边际 0；未来独立编码的 financing 文档年增量估计
  <10 条/家（小文档）。

**P2（可选，暂不动）**
- `litigation`：等采集层缺口（F1）修复、语料出现真实体量后复审是否移入。
  现在移入是零成本空转，但类内容未经语料验证，不符合本审计的取证方法。

**明确审视过、结论为不动的边界候选**：investor_relations（151 条 sole、解析
成本第二大，但它是 A 股 earnings-call 等价物，对标两路都判高值——留）；
performance_briefing（高值，留）；correction_supplement（体量小+重述信号强，留）；
risk_alert（它就是经营数据通道，留）；equity_share_change / share_pledge
（字段监控级，不进全文解析——留 register_only）。**没有发现应移出 process 的类**：
表面上的浪费（中介报告、程序性激励件被解析）由共码 ANY-hit 造成，移动任何类都
治不了它（见 §4-B3）。

## 4. 建议终稿与生效机制

**processing_policy.json 建议终稿**（version 升号；process +operating_data
+financing = 22 类，register_only 9 类）：

```json
{
  "version": "2026-07-r2",
  "_about": "处理策略（级联层2，design/classification-facets-and-derived-views.md v3）。process=下载+解析（一个动作，round20 用户裁决：不存在下载不解析）；register_only=只登记元数据（可见可查，零下载/解析成本）。按公司覆盖=watchlist.csv 的 process_classes 列（替换式，空=继承本文件）。增类=从 register_only 挪到 process，历史已登记文档自动回补下载；删类=挪回，队列即刻静默，已有数据不动。类名必须 ⊆ class_map.json（make config-check 校验，worker 启动 fail-closed）。2026-07-12 审计（docs/implementation/reviews/process-classes-review-2026-07-12.md）：+operating_data（月度经营数据=预测核心输入）、+financing（募投进度/借款利息）。",
  "process": [
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "performance_forecast",
    "performance_flash",
    "operating_data",
    "investor_relations",
    "performance_briefing",
    "inquiry_regulatory",
    "restructuring_assets",
    "dividend",
    "additional_issuance",
    "convertible_bond",
    "rights_issue",
    "share_buyback",
    "major_contract",
    "related_party",
    "financing",
    "risk_alert",
    "delisting_risk",
    "equity_incentive",
    "correction_supplement"
  ],
  "register_only": [
    "meeting_resolution",
    "governance_rules",
    "intermediary_report",
    "equity_share_change",
    "share_pledge",
    "litigation",
    "ipo_listing",
    "bond_notice",
    "esg"
  ]
}
```

（若只采纳 P0 不采纳 P1：把 "financing" 从 process 移回 register_only 即可，
其余不变。）

**生效机制**（config/README.md + round21 机制）：改文件前跑 `make config-check`；
无需命令，下次 worker 启动生效。增类自动回补：历史已登记元数据重新落入下载队列
（放行谓词对 classification_rule 现算），无需重同步；删类即刻静默、已有数据不动。
本次两个移入类现库边际回补=0 条（F2），风险极低。

**采集层/词表跟进项（不改本文件，另行裁决）**
- B1（采集，最高优先）：p_info3015 目录覆盖 ≈41%，整类缺失决议/减持/质押/诉讼/
  ESG/真回购——"register 永远全量"的意图在 API 通道上不成立。候选：web 通道
  （hisAnnouncement）补索引同步。
- B2（词表）：给 operating_data 补真实前缀/标题规则（销售简报/经营数据/发电量/
  月度报告——现全被 012305 编入 risk_alert）；class_map 升版 + make load-rules。
- B3（词表/放行语义，成本项）：ANY-hit 共码使 92 份中介报告与大量激励程序件
  进入解析（equity_incentive sole 296 条 ≈ 6 GPU 时）。若未来解析成本敏感，
  候选：argmax(filing_type) 放行或程序载体负向规则。当前吞吐下可接受，不建议现在动。
- B4（下游）：investor_relations 910 units 中 887 条 semantic_key 为空，
  调研问答的口径提取还没被切分规则语义化——高值类的价值兑现依赖后续单元规则。

---

## 落地记录与修正（2026-07-12 晚，用户裁决后实现）

用户裁决：P0+P1 采纳；③共码 ANY-hit 必须改（L2 只收高质量数据）；②词表归位做；
①按"补登记"意图处理。落地如下。

### 修正一：F1 撤回——登记（候选层）覆盖是全的

实现阶段发现注册（document 行）只发生在下载之后："登记面"是 source_access 候选
快照（3859 条待下载候选），documents_v1 天生只含已下载文档。候选层关键词普查：
决议 638 / 审计报告 90 / ESG 30 / 减持 28 / 增持 18 / 回购报告书 5 / 质押 4 /
诉讼 2——全都在。官网 258 vs 文档 106 的差距是 round20"登记全量、下载核心"的
设计行为，不是 p_info3015 覆盖窄。**web 通道补目录（原跟进项 B1）无需做**；
零条类的复审依据改为候选层计数（litigation 候选层仅 2-3 条，维持 register_only
的判断反而更稳）。①的实际含义已经满足：元数据都在库里（source_access），
仅"查询面"是 ops 视图而非 public 视图——若要 public 候选视图另立跟进项。

### 修正二：本文 §2/§3 体量与成本口径

正文表格的"体量"列是**已下载文档**口径。候选层真实体量（下载队列同款类命中
统计）：governance_rules 962 / meeting_resolution 757 / intermediary_report 387 /
equity_share_change 308 / bond_notice 180 / financing 152 / esg 48 / share_pledge 4 /
litigation 3。因此"financing 移入边际成本 0"有误：真实边际 = **152 条候选**
（其中 10 条带 0129 被 carrier 门拦下）；2026-07-12 23:23 的 worker 轮已按 r2
策略实际下载 ~142 条（documents 1236→1380，financing 25→167），内容为万科
担保/贷款、授信额度、募集资金专项报告等——与批准语义一致。operating_data
候选层同样 0 条码命中（死前缀坐实），移入零回补。

### 落地清单（全部完成，gates 绿）

1. **processing_policy.json 2026-07-r2**：process 20→22（+operating_data
   +financing），register_only 11→9；config-check OK。
2. **③ carrier 门**（worker/queries.py）：新增 CARRIER_CLASSES=
   ("intermediary_report",)；下载/解析共用谓词 `_processing_scope_sql` ——
   资格 =（码命中 ∪ title_topic 标题命中）∈ 生效集合，守卫 = 不存在"落在
   生效集合之外的 carrier 命中"（有码走 class 规则、无码走 title 规则）。
   按公司覆盖可显式把 intermediary_report 加回。已知残留：无 0129 码的
   监事会/薪酬委"核查意见"（现库 30 条）仍会解析——信任 provider 码的
   代价，量小接受。
3. **② operating_data 归位**：filing_type_map.json 2026-07-r5 新增
   topic_rules（销售简报/情况简报/经营数据/发电量完成情况/生产经营情况
   → operating_data），loader 落 rule_set='title_topic'（priority=类优先级
   95）；同版 rules 顶部新增 intermediary_report carrier 词（法律意见书/
   核查意见/受托管理事务报告/跟踪评级/验资报告/持续督导/独立财务顾问报告
   ——审计报告一词因"年度报告及审计报告"合并标题遮蔽风险刻意不收）。
   迁移 0021 重建 documents/units 两视图：分类 = class 码命中 ∪ title_topic
   标题命中（并集进 topics 与 argmax），列集不变；downgrade 恢复 0017 形态，
   往返核验通过。现网效果：30 份经营数据公告 filing_type 归位
   operating_data（万科 18 简报 + 茅台经营数据 + 长电发电量），topics 同时
   保留 risk_alert；另捞出 8 条历史码盲区候选（长电发电量公告，原 argmax
   =other 从未入下载队列）进入回补。
4. 测试：+5（下载 carrier/topic 门、解析 carrier 门、视图并集推导有码/无码、
   规则包 topic_rules 与顺序约束）；agent-check 392 绿；live-DB 19/19 绿；
   0021 迁移 downgrade/upgrade 往返绿。
5. 文档同步：cninfo AGENTS.md（词表规则）、db/postgres AGENTS.md（0021 速记）、
   data-dictionary §4（r5/r2 版本）、config/README.md（carrier 例外）。

### 存量污染处置（实施时待用户裁决；后续已执行，见下文）

carrier 门只管未来。存量：92 份 intermediary 文档已下载，其中 77 份已发布、
317 个 units 在 public 视图里。清理有一个硬约束：**"published 永不降级"是
05 钉死的状态机不变量**，且 document_published 事件已进 change feed——
物理删 units/降级 status 会打破两者。备选：
- A（无损，推荐）：不动存量；未来 L2 摄取端按 `disclosure_topics ?
  'intermediary_report'` 过滤（一个 WHERE 条件）。本次已让该标记在视图里
  100% 可判定。
- B（净化）：一次性删除这 92 份的 document_unit 行 + 状态回退 parsed→
  registered 不可行（发布态不可逆），只能删 units 保留 document 行——
  破不变量，需要用户明确点头 + 单独脚本 + change feed 补偿事件设计。

### 独立评审与修复（2026-07-13 凌晨，多代理对抗评审）

41 代理（5 维审查 × 每发现 2 名验证者对抗核实）：**14 条确认 / 4 条被证伪否决**。
已修复（review-fix 文件集后续已随 `d715f59` 提交）：
- P1 doctor 规则体检盲区：现在同时钉 title/title_topic 版本（doctor.py）。
- P1 测试基座陈旧（0019 起就红）：alembic head 断言 0018→0021；
  PUBLIC_VIEWS 常量补 tracked_companies_v1（schema.py，doctor 视图体检同步受益）。
- P1 程序性：NULLIF 修正后全门禁重跑（agent-check 395 绿 + live 26/26 绿 +
  doctor classification PASS），diff 快照重出。
- P2 derive_primary_class 与 0021 视图分叉：evaluator 纳入 topic_rules
  （BuildUnits 的 filing_type 与视图一致），parity 断言进视图测试与单测。
- P2 词表补口：topic_rules +经营简报/产销快报/产销数据/运营数据（9 词，已重灌）。
- P2 load-rules TRUNCATE 加 SET LOCAL lock_timeout='5s'（防长读者锁排队卡全库）。
- P2 测试补口：解析门空串 raw_category 用例、解析门 title_topic 资格用例、
  公司级 process_classes 覆盖 opt-in/替换语义用例、argmax 反向（码类 100 胜
  topic 95）用例、map_filing_type 行为锁（核查意见→carrier；
  "年度报告及审计报告"不误路由）。
- P2 契约清单 §2 两处表述同步 0021。

确认但不修（记录在案）：
- N009002"对重组的核查意见/问询函"分类名会让注册期快照 filing_type 标签从
  inquiry_regulatory 翻成 intermediary_report——仅快照标签，视图/门/持久化不受
  影响；词表下轮升版时再议。
- policy 回滚可观测性：删类后已下载未解析文档静默滞留 registered 属既定契约，
  但 doctor 无专项计数；排产 09 里程碑再议。
- doctor 抽检发现 1 例存量 raw 文件缺失（000333 IR 2025-04-11，同 hash 2 行
  supersede 对）——与本轮无关的存量数据完整性问题，待单独排查。
被否决的发现（不动）：0021 视图 carrier/topic 标签矛盾说、持续督导双通道不一致说、
DROP VIEW 锁风险说（既有模式）、policy 每轮重读撕裂说（既有行为且有里程碑跟踪）。

提交状态：主体改动已由并行 session 随 `040b50f` 提交（含 NULLIF）；本节修复
（doctor/schema/mapper parity/lock_timeout/词表 9 词/测试/契约清单，10 文件）
已随 `d715f59` 提交。

### 存量清理执行记录（2026-07-13，用户裁决"直接删"）

单事务两阶段（带守卫断言，全部通过）：
- 阶段1（整行删除）：清除 2026-07-12 23:13/23:23 两次 `local:register_pdf`
  操作留下的污染——2 份 pid 被描述性目录名污染的文档（江海 2025 年报重复件
  200 units + 美的 IR 幽灵件 0 units、盘上无文件）、2 条重复公司行、2 条重复
  security 行、2 条 source_access。江海年报正本（pid 1225087169，200 units）完好。
- 阶段2（中介件净化）：92 份 intermediary 文档删 317 units + 94 runs，状态降回
  registered（元数据保留，register 全量不破）；carrier 门保证不会再入解析队列。
- 验证：intermediary units=0；securities/tracked 回到 13/13；pending_build=0、
  pending_publish=3（正常在飞）；pending_parse=104（全是 financing 回补件）。

**local:register_pdf 事故 RCA（防再犯）**：某次验收式操作在 live 库上跑了本地
PDF 注册，且 (a) 把描述性目录名当成 provider_document_id（去重键失配→重复行），
(b) 当时环境 DATA_ROOT 与生产根不一致（DB 记新式路径、AgentSSD 上无文件——
doctor raw-hash FAIL 的根因），(c) resolver 未命中 security→新建重复公司/证券。
防再犯建议（待排产）：①register_local_pdf 入口对 provider_document_id 做形状
校验（cninfo 数字 TEXTID）；②验收/测试一律走 scratch database（tests/AGENTS.md
既有规矩，人工操作也应遵守）；③doctor raw-hash FAIL 已随删除消失，重跑 PASS。

### document_unit 终检（2026-07-13，清理+回补后）

总量 ~23.9k units。高值主体：年报 12361 + 半年报 8373 + 季报 832（三年财报
底线）、IR/业绩说明会 1091、经营数据 30（每份简报 ~1 单元，已归位）、
financing 已解析 38 份 60 units。残留低值面（评估为可接受，≈1.5%）：
- 股权激励程序件家族（解锁/归属/行权/回购注销/监事会核查意见）约 130 docs /
  ~350 units——类内噪声，policy 类粒度分不开；如要进一步净化需标题负向规则
  （新裁决，暂不动）。
- 1 份无 0129 码的可转债受托管理报告（10 units）——信任 provider 码的已知残留。
- 即将解析的 104 份 financing：42 借款/担保/授信（高信号）+ 48 票据/募集资金
  （中）+ 13 募集资金存放专项/鉴证报告（年度模板件，低值但每年每公司仅 ~1 份）。

### 单元内容质检与噪声总闸（2026-07-13，用户裁决"第一阶段不要陈词滥调"）

5 路代理真读 payload（逐桶抽样 + 全库/候选层逐模式误伤核验）→ 52 条 title_noise
负向规则（filing_type_map.json 2026-07-r6 noise_rules 段，rule_set='title_noise'）。
语义：标题命中即绝对不下载不解析（有码无码一致，公司覆盖不能翻——阶段性总闸）；
登记与视图分类不受影响。5 条 needs_review 未采纳：会计政策变更（主动变更可重大影响
利润）、套保授权（额度是敞口信息）、万科贷款担保系列（困境公司信贷通道=信号）、
平银关联额度、转债付息。

清理执行：424 份命中文档（343 份已解析）删 3345 units + 353 runs、降回 registered。
前后对比：活跃单元 ~24.0k → 20,656；equity_incentive 233→13 docs / 647→82 units
（剩草案/摘要/考核办法/授予/终止实施）；英文版副本 11 docs/2606 units 清零（中文
正本全在）；convertible_bond 83→43 docs（重复提示件清零，下修/强赎决定/回售结果
保留）；dividend 126→108（优先股固定票息件/中期分红授权件出清）；risk_alert 21→11
（异动模板出清，经营数据/会计政策变更保留）；财务附注抽查 5/5 数字兑现。
现网即时验证：pending_parse 104→69（噪声候选被门拦下）、agent-check 397 绿、
live 集成 27/27 绿、doctor 分类 PASS（class r5 / facet r1 / title r6）。

**单元级发现（切分规则跟进项，本轮只记录不动，S 规则升版时处理）**：
- 勾选空壳单元（'□适用 √不适用'剥离成独立单元）≈1163 个 + 裸'适用/无'174 个
  ≈ 定期报告桶 5.4%——建议并回章节或丢弃纯勾选 payload。
- 高管任职表按行炸开（人名标题+职务 payload）74+ units。
- 投关记录表：参会花名册 17 单元（~9.5 万字符）可整删；尾部模板 table 103 个中
  73 个夹带上一条问答正文（须先抽回再删，整删丢内容）；例行声明句宜句级清洗；
  问答题号跨页截断错位（两位数题号被拆两半）。
- 微小单元不可按长度一刀切（<120 字符的 19% 里含真实财务数字与有效否定确认）。
