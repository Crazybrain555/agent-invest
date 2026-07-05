# asset_intake provider 框架讨论稿 v0.1

状态:讨论稿(M-C 在 P2 后暂停的产物)。待外部评审(GPT-Pro)与用户拍板后升级为定稿;
之后的 P3–P6 以定稿为准。本文只讨论 provider 接入与数据集契约,不重开信封/表结构已定的部分。

## 1. 已固定的地基(不在本次讨论范围)

- 信封与表(M-C P2 已落库,协议 §3.2/§3.9/§2.8):`intake_core.data_asset`(稳定键列化 + jsonb
  payload,`(provider, dedup_key)` 唯一)、`source_access`(查空/出错也留痕)、`processing_run`、
  `intake_ops.outbox_event`(observed/materialized 两语义)。
- 共享信封核 `packages/envelope_kernel`(字段模型、kind 矩阵、asset:// URI、data_asset.v1 schema)。
- provider 端口 v0.1 签名(`DatasetProvider.fetch(DatasetRequest) -> DatasetResult`),注册层只依赖
  端口:开 run → fetch → source_access → (查空止步 | 建资产) → outbox。
- 硬边界:凭证只走环境变量;真实 provider 调用只在显式 opt-in smoke;LLM 不进登记层。

## 2. 新前提(2026-07-05 用户输入)

1. **通道未定**:tushare 与 Wind 理想情况都走 SQL(阿里云 RDS SQL Server,Wind 终端导出 50 张表);
   但也可能走 endpoint + 参数。确认需要一段时间;确认前用 `Reckoner/api test` 的材料做测试。
   **框架必须做到换通道不返工**(不改表、不改契约、不改注册层)。
2. **范围收窄**:首发只做两个数据集——
   a. 复权 OHLCV 日行情(高开低收,估值用);
   b. 三大表财务指标 + 业绩预告/快报指标(补充 disclosure_anchor 的财报数据,降低图表解析量)。
3. **text-to-query 的定位**:标准化数据的自由问数本质是 text-to-SQL / text-to-params,靠 LLM,
   且依赖数据表字典。这是真实需求,但见 §5——首发两个数据集不需要它。

## 3. 关键解耦:dataset_key 用语义命名,provider 只是 provenance

P3 端口 v0.1 草案里 dataset_key 写作 `tushare.daily`——**本文提议废弃该写法**。dataset_key 绑定
provider 会让"换通道/换 provider"污染 source_access 历史与去重键。改为:

```text
dataset_key = 语义数据集名(与 provider 无关):
  cn_equity.eod_quote          # 复权 OHLCV 日行情
  cn_equity.fin_statement      # 三大表财务指标
  cn_equity.fin_preannounce    # 业绩预告/快报指标

provider / adapter / adapter_version = 信封 provenance 字段(§3.2 D 组),
  记录这一次实际由谁、经什么通道取得。
```

同一 dataset_key 未来可由 tushare API、Wind RDS SQL 甚至双源供给;消费方按语义键检索,
按 provenance 区分来源;对账(双源同数据集)是 L2/后续里程碑的事,L1 只如实登记。

## 4. 分层提案

```text
调用方(人 / agent / 未来 L6 调度)
  │  语义请求:dataset_key + 语义 query_params(如 security、date range、report_period)
  ▼
[dataset_registry 字典]  Git 管理(本仓库 tracked),per dataset 一份条目:
  语义字段定义 → 各 provider 的物理映射(API 名+参数名 / 表名+列名+SQL 模板)、
  单位、复权口径、期间口径、主体标识映射(ts_code / wind code)、必填 query_params
  ▼
[provider adapter(实现 DatasetProvider 端口)]
  HTTP API 型:dataset_key+params → API 调用(参考 stock-tushare数据mcp 的限流/缓存设计)
  SQL 直查型:dataset_key+params → 白名单只读 SQL 模板渲染(参考 aliyun-wind-rds 的
    query_cookbook;只读账号、超时、行数上限)
  ▼
DatasetResult(records、returned_fields、provider_as_of、locator、raw_asset_ref、ScopeHints)
  ▼
[确定性登记层(已定,不随通道变)] processing_run + source_access + data_asset + outbox
```

要点:

1. **registry 是字典,adapter 是执行器**:字段映射、口径、模板都在 registry 条目里(数据),
   adapter 代码只做"读条目 → 发请求/渲染 SQL → 规整返回"(逻辑)。换通道 = registry 条目加一段
   物理映射 + 换 adapter 实现,端口与登记层零改动。
2. registry 落点:协议 §3.7 已有钩子——"dataset 的字段契约由 dataset_registry(Git 管理的 L1
   adapter 配置清单)声明,DB 只记实际发生的访问"。形态建议 `services/asset_intake/registry/
   <dataset_key>.yaml`,contract 测试校验条目结构。
3. **SQL 安全边界**(SQL 直查型):只读账号、模板白名单(禁自由拼接)、参数化渲染、超时与行数上限、
   语句摘要进 locator。

## 5. LLM / text-to-query 的位置:登记层外

- 首发两个数据集的 query_params 完全可枚举(证券代码 + 日期区间 / 报告期),**用确定性模板,
  不需要 LLM**。
- 自由问数(text-to-SQL / text-to-params)是未来的"查询规划器(query planner)"组件:输入自然语言,
  输出合法的 DatasetRequest(受 registry 字典约束、白名单校验)。它属于调用方一侧,产物经同一端口
  进入登记层;LLM 参与记 producer_action/rule_bundle(协议 §2.6),登记层本身保持确定性(§15.35:
  能确定性完成的不交给 LLM)。
- 因此字典的第一用户是 adapter(字段映射),第二用户才是未来的 query planner(语义检索)。
  字典条目从第一天就写"语义字段说明",为后者铺路,但 planner 本体不在 M-C 范围。

## 6. 首发两个数据集的契约草案

### 6.1 cn_equity.eod_quote(复权 OHLCV 日行情)

- query_params:`security`(如 `000001.SZ`)、`start_date`、`end_date`、`adj`(复权口径,见开放问题 Q4)。
- payload.records 语义字段:trade_date、open、high、low、close、volume、amount、(adj_factor)。
- scope:subject_candidates=[`security:{code}`];event_time=区间末日;published_at≈trade_date(收盘后可得)。
- **技术风险(开放问题 Q4)**:前复权价随未来除权除息全量重算——同一查询不同日期取会得到不同数字,
  content_hash 必变,资产会"每天全新"。草案倾向:落库存**不复权 + adj_factor**(或后复权),
  前复权由消费方现算;待评审。

### 6.2 cn_equity.fin_statement / cn_equity.fin_preannounce(三大表 + 预告/快报)

- query_params:`security`、`report_period`(如 2025A/2026Q1)、`statement`(income/balance/cashflow,
  仅 fin_statement)。
- payload.records:registry 字典声明的指标字段(单位、口径注明);预告类含区间值(min/max)与预告类型。
- scope:report_period 直接入列;material_type = `financial_statement` / `earnings_preannouncement`;
  published_at = 披露日(provider 提供);与 disclosure_anchor 的对账锚点是 (security, report_period)。
- 价值:与披露 PDF 同报告期数据互补,L2 可用它替代大量表格图表解析。

## 7. 开放问题(待 GPT-Pro 评审与用户拍板)

- Q1 dataset_key 语义命名与粒度(§3 提案)是否成立;预告/快报独立 dataset_key 还是并入 fin_statement。
- Q2 registry 字典形态:YAML per dataset(提案)vs 单文件 vs 入库;条目 schema 该长什么样;
  50 张 Wind 表的字典维护成本怎么控(先只写用到的表/字段,按需增补?)。
- Q3 双 provider 同一 dataset_key 的策略:M-C 单 provider;将来双源并存时去重键、优先级、对账放哪层。
- Q4 复权数据的存储口径(§6.1):不复权+因子 / 后复权 / 前复权,幂等与 as-of 语义怎么最干净。
- Q5 SQL 直查型的白名单模板机制与只读安全边界,有无更好的工程实践。
- Q6 增量策略:行情类按日增量、财务类按披露事件增量,source_access/checkpoint 怎么配合。
- Q7 端口 v0.1 签名在以上前提下有无需要现在就改的破绽(改签名要趁 P3 未写)。
