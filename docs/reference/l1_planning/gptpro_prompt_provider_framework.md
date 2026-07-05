# GPT-Pro 咨询 prompt:asset_intake provider 框架评审

(把本文件内容作为提问正文,连同 zip 一起提供给 GPT-Pro。)

---

你是一位资深数据平台架构师,请评审一个投研数据接入服务的 provider 框架设计。

## 背景

我在建一个个人投研预测引擎(单机、单人使用、PostgreSQL,分层架构 L1–L6,详见附件协议文档)。
L1 是来源资产层,统一出口是 `data_asset` 信封。现有两个 L1 服务:disclosure_anchor(披露 PDF,
已上线)和 asset_intake(结构化数据登记,建到一半暂停,等这次评审)。

asset_intake 的信封表、查空记录、outbox change feed、角色权限已落库(见附件代码,这部分**不改**)。
暂停在 provider adapter 层,因为出现三个不确定性:

1. 数据通道未定:tushare 与 Wind 数据理想情况都走 SQL(同事的阿里云 RDS SQL Server,Wind 导出
   50 张表),但也可能走 HTTP API(tushare 官方)。确认要几周;期间用附件里同事写的两个 skill
   (aliyun-wind-rds、stock-tushare数据mcp)做测试参考。**很可能中途换通道,框架必须让换通道
   不返工。**
2. 标准化数据的自由查询本质是 text-to-SQL / text-to-params,要靠 LLM + 数据表字典,字典维护
   有成本,我担心设计不好会很麻烦。
3. 首发范围收窄为两个数据集:复权 OHLCV 日行情(估值用)、三大表财务指标含业绩预告/快报
   (补充披露 PDF 的财报数据,减少图表解析)。

## 附件说明(zip 内)

- `GPTPRO-PROMPT.md`:本文件。
- `00-framework/asset_intake_provider_framework_v0.1.md`:**待评审的框架讨论稿(核心评审对象)**,
  含分层提案、dataset_key 语义命名、registry 字典、LLM 定位、两个数据集契约草案、开放问题 Q1–Q7。
- `01-protocol/`:引擎顶层协议 v0.7(重点 §2 统一范式、§3 L1 层、§15 工程取舍、§16 硬边界)。
- `02-repo-context/`:仓库根与两个组件的 AGENTS.md(工程规矩)。
- `03-plan-snapshot/`:当前任务的持久计划/状态快照(了解进度用)。
- `04-code/`:asset_intake 服务与 envelope_kernel 共享包的完整代码(P1/P2 已完成部分,含
  provider 端口 v0.1 签名所在的计划描述;信封表结构见 db/models.py 与 migration 0001)。
- `05-reference-skills/`:同事写的两个 skill(只读参考:aliyun-wind-rds 是 SQL 直查 Wind RDS 的
  脚本集+表结构速查,stock-tushare数据mcp 是 tushare 的 MCP 封装,其限流/缓存/工具索引设计
  可作 adapter 实现参考;凭证文件已剔除)。

## 请你输出

1. **逐条回答讨论稿 §7 的开放问题 Q1–Q7**,每条给明确推荐 + 理由 + 被否方案的致命点。
2. **框架总评**:分层提案(§4)和 dataset_key 语义命名(§3)能否支撑"通道未定、中途会换"的现实;
   指出讨论稿里你认为错误或过度设计的地方。
3. **registry 字典条目 schema 的具体建议**(给出一个 YAML 条目示例,覆盖:语义字段、单位、口径、
   tushare 与 Wind RDS 两套物理映射、SQL 模板白名单的表达方式)。
4. **两个首发数据集的落地清单**:按"确认走 SQL"与"确认走 API"两种情形,分别列出需要写的
   registry 条目、adapter 代码边界、测试策略(fixture 怎么造)。
5. **风险清单**:尤其是复权数据的幂等/as-of 问题(Q4)和字典维护成本失控问题,各给一个最小
   缓解方案。

约束:遵守协议 §2.1 扩展字段纪律(默认不新增顶层对象、不改信封核心)、§15.35(能确定性完成的
不交给 LLM)、§16 硬边界。已落库的表结构与信封契约视为冻结,除非你能论证不改就无法满足需求。
回答用中文,直接给结论和方案,不要泛泛的最佳实践综述。
