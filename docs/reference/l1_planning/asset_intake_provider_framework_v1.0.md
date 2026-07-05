# asset_intake provider 框架定稿 v1.0

状态:定稿(2026-07-06)。取代 v0.1 讨论稿(见 git 历史)。形成过程:v0.1 → 独立答案底稿 →
GPT-Pro 外部评审 → 以协议 v0.7 与已落库代码为准独立取舍(取舍记录见 §9)。P3 起的实现以本文为准;
修改本文需说明与协议 §2.1 扩展纪律、§16 硬边界的一致性。

## 1. 定案清单

- **F1 dataset_key 语义命名,provider 无关**:`cn_equity.eod_quote` / `cn_equity.fin_statement`
  (`statement=income|balance|cashflow|indicator`)/ `cn_equity.earnings_event`
  (`event_type=forecast|express`)。provider/通道只进 provenance(provider、adapter、locator、
  source_access),换通道不改 key、不改下游。
- **F2 业绩预告/快报独立为 earnings_event**:它是披露事件不是期间报表事实,schema(区间值/类型)、
  生命周期(可修正/取消)、material_type 都不同;硬塞 fin_statement 会制造万能表。
- **F3 复权口径**:L1 canonical 只存**不复权 OHLCV + adj_factor**(每行带 row_published_at);
  qfq/hfq 是派生视图(消费端/将来 L3),公式 `qfq(t;basis_date)=raw(t)×factor(t)/factor(basis_date)`,
  且 `basis_date ≤ as_of`(防前视,协议 §2.5)。query_params 保留 `price_adjustment` 轴但枚举只有
  `raw_plus_factor` 一个合法值(口径显式化,不给第二个值)。
- **F4 registry = Git 管理的 dataset 契约 + provider 映射**,落 `services/asset_intake/registry/
  datasets/<dataset_key>.yaml`(协议 §3.7:L1 adapter 配置清单,不属 L4 六册,不入库)。
  条目由服务内 pydantic 模型校验 + contract test 守护。**只登记当前消费的字段**,严禁预先全录
  Wind 50 张表。
- **F5 L1 不跨 provider 合并、不裁决**:同一 dataset_key 多 provider 各自入账
  (`(provider, dedup_key)` 唯一已支持,零改表);对账/采信是 L2 numeric_observation 的事(§3.1)。
- **F6 幂等三态**(见 §6):同 provider 同 semantic_key 同 content_hash → observed(不建资产);
  content_hash 变 → materialized 新资产 + supersede 旧 active;不同 provider 永远独立。
- **F7 SQL 只走白名单模板**:模板存 registry 条目,绑定参数渲染,allowed tables/columns、
  required predicates、max_rows、timeout、deny tokens、禁 SELECT *、只读账号。
- **F8 LLM 不进登记层**:未来自由问数走 text-to-**DatasetRequest**(不是 text-to-SQL),
  经 registry validator 确定性校验后进同一端口;M-C 不实现 planner。
- **F9 adapter 不碰 DB**:provider adapter 只返回 DatasetResult;processing_run / source_access /
  data_asset / outbox 全部由确定性 registrar 写。
- **F10 登记路径无字段投影**:登记永远按 registry 声明的完整语义字段集取数落 payload;
  `requested_fields` 之类的投影属于读侧,不进登记 DatasetRequest(理由见 §9-R1)。

## 2. 分层与写权边界

```text
调用方(人 / agent / 未来 planner / 未来 L6)
  │ DatasetRequest(dataset_key + 语义 query_params)
  ▼
registry(YAML 字典)── validator:key 存在、params 合法、provider 映射存在
  ▼
provider adapter(实现 DatasetProvider 端口;SQL 型 / API 型 / 未来其他)
  │ 只做:参数规整 → 发请求/渲染白名单 SQL → 字段映射与确定性单位换算 → DatasetResult
  ▼
registrar(register_dataset_snapshot,确定性,唯一写 DB 的地方)
  │ processing_run → source_access(ok/empty/error 三态)→ 幂等判定(§6)
  │ → data_asset(经 envelope_kernel 校验)→ outbox(observed/materialized)
  ▼
intake_public.*_v1 视图 → L2(numeric_observation,对账/采信在那边)
```

模块边界(P3 实现):

```text
src/asset_intake/providers/port.py        DatasetRequest / DatasetResult / DatasetProvider / ProviderError
src/asset_intake/providers/registry.py    load_entry / validate_request / resolve_mapping
src/asset_intake/providers/sql_template.py 模板校验、参数绑定、安全规则(F7)
src/asset_intake/providers/<provider>.py  具体 adapter(通道确认后)
src/asset_intake/application/register_dataset.py  registrar
registry/datasets/<dataset_key>.yaml      字典条目
```

## 3. 端口定稿签名

```python
@dataclass(frozen=True)
class DatasetRequest:
    dataset_key: str                    # F1 语义 key
    query_params: dict[str, Any]        # registry 声明的语义参数;规范化后哈希

@dataclass(frozen=True)
class ScopeHints:                       # adapter 补充;scope 派生以 registry scope_mapping 为主
    subject_candidates: list[str] | None = None
    report_period: str | None = None
    event_time: datetime | None = None
    published_at: datetime | None = None   # snapshot 级 = records 内 row_published_at 最大值
    title: str | None = None
    semantic_key: str | None = None

@dataclass(frozen=True)
class DatasetResult:
    records: list[dict[str, Any]]       # 语义字段名;[] = 查空
    returned_fields: list[str]
    provider_as_of: str | None
    locator: str | None                 # 模板 id@版本+参数摘要 / endpoint+参数摘要;严禁含凭证与主机名
    raw_asset_ref: str | None = None
    scope: ScopeHints = ScopeHints()
    warnings: tuple[str, ...] = ()      # 换算/修补说明,进 processing_run.params 留痕
    stats: dict[str, int] | None = None # 行数等,进 source_access.result_count 与日志

class ProviderError(Exception): ...     # 权限/限流/连接 → registrar 记 error,与查空严格分开

class DatasetProvider(Protocol):
    provider_name: str
    adapter_name: str
    adapter_version: str
    source_tier: SourceTier             # 首发均 tier_1
    trace_level: TraceLevel             # 数据服务级 G1
    def fetch(self, request: DatasetRequest) -> DatasetResult: ...
```

相对 v0.1 的变化:+warnings/+stats(评审采纳);scope 派生主责移到 registry scope_mapping,
DatasetResult.scope 降级为补充;分页/流式(fetch_iter)进 roadmap,M-C 不做。

## 4. registry 条目 schema v1(槽位)

```yaml
schema_version: dataset_registry.v1
dataset_key: <semantic key>
dataset_contract_version: 1
status: active | draft | deprecated
semantic_contract:
  asset_kind: dataset_snapshot
  payload_kind: recordset
  material_type: <market_quote | financial_statement | earnings_event>
  source_tier: tier_1
  trace_level: G1
  query_params:   {<name>: {type, required, format/allowed, default?}}
  primary_key:    [<record 内去重键>]
  time_semantics: {event_time_from, published_at_rule, provider_as_of_required}
  subject_semantics: {subject_candidates_from: [{field, subject_kind}]}
  fields:         [{name, dtype, unit, description, required, as_of_sensitive?}]
providers:
  <provider_name>:
    adapter / adapter_version
    field_map:    {<语义字段>: {column|endpoint+column, provider_unit?, transform?}}
    sql_templates: {<template_id>: {statement(绑定参数), allowed_tables, allowed_columns,
                    required_predicates, max_rows, timeout_seconds, params}}   # SQL 型
    endpoints:    {...}                                                        # API 型
    safety:       {readonly_required, single_statement_only, deny_tokens,
                   forbid_select_star, require_bound_params}
validation:  {row_checks: [...], duplicate_policy: {keys, action}}
dedup:       {semantic_key_fields: [...], content_hash: {sort_by, include_fields}}
```

维护纪律:每新增一个语义字段必须同时给 unit + 各 active provider 的 mapping + fixture;
provider 原始多余字段不进 semantic fields。物理表/列名在写实际条目时以
`aliyun-wind-rds/references/` 速查与 tushare 官方文档核实,不凭记忆填。

## 5. 首发三条数据集契约要点

### 5.1 cn_equity.eod_quote

- params:`security`、`start_date`、`end_date`、`price_adjustment=raw_plus_factor`(唯一合法值)。
- fields:security、trade_date、open/high/low/close/pre_close、volume(股)、amount(元)、
  adj_factor(as_of_sensitive)、row_published_at(=trade_date 收盘后规则)。
- 单位换算在 field_map 声明为确定性 transform(如 tushare vol 手→股 ×100、amount 千元→元 ×1000)。
- 只支持 security × date_range;长历史按年分块(payload 体积控制)。

### 5.2 cn_equity.fin_statement

- params:`security`、`report_period`、`statement=income|balance|cashflow|indicator`。
- 首发字段=预测主链最小集(收入/营业利润/净利/归母净利/EPS;总资产/负债/归母权益/应收/存货/
  合同负债;经营现金流/资本开支代理/折旧摊销;毛利率/净利率/ROE/周转类),写条目时逐个核实
  provider 字段名。
- 口径规则:合并报表口径为默认 scope(Wind STATEMENT_TYPE=408001000 或 tushare report_type 对应),
  其他口径显式 scope,不混入;同 (security, report_period) 多行取 latest
  (按 ann_date/opdate 降序),重述 → 新 content_hash → materialized + supersede。
- 与 disclosure_anchor 的对账锚点:(security, report_period);对账本身在 L2。

### 5.3 cn_equity.earnings_event

- params:`security`、`report_period?`、`event_type=forecast|express`、`ann_date range`。
- fields:event_type、report_period、published_at(=公告日)、forecast/express 的
  值或区间(net_profit_low/high 或点值)、类型、原文描述。
- 预告被修正/正式财报出来 → 各自新资产;跨 dataset 的"预告 vs 正式"验证关系是 L2 的事。

## 6. 幂等、去重、取代与 as-of

```text
semantic_key = dataset_key + 规范化 query scope(registry dedup.semantic_key_fields 声明)
               → 落 data_asset.semantic_key 列(P2 已有)
content_hash = 规范化 payload(dedup.content_hash 声明的字段与排序)的 sha256
dedup_key    = sha256(semantic_key || content_hash) → (provider, dedup_key) 唯一(P2 已有)

同 provider + 同 semantic_key + 同 content_hash → outbox observed,不建新资产
同 provider + 同 semantic_key + 新 content_hash → 新资产 + 旧 active 标
    is_active=false / superseded_by=新 asset_id + outbox materialized
不同 provider → 永远独立入账(F5)
```

- content_hash 只看 canonical 内容,不含查询时间——重复巡检零膨胀。
- registry/dataset_contract_version **不进 dedup_key**:内容变化 hash 自然变;版本变而内容不变
  不应伪造 materialized(下游失效只由 materialized 触发,§2.8)。版本归 provenance/contract 字段。
- as-of:snapshot 级 published_at = records 内 row_published_at 最大值;L2 用 row 级时间。
  正式快照 pin provider_as_of + dataset_contract_version(§16.38)。
- 需要新索引 (provider, semantic_key) WHERE is_active(supersede 查找用),随 P4 migration 加。

## 7. 安全与增量

- SQL 型:F7 全套;只读账号(SQL Server 侧限到目标库表);大表查询必须带索引前缀谓词
  (如 S_INFO_WINDCODE + 日期范围);locator = `template_id@version + params_hash`,不含 host/凭证。
- API 型:限流退避与错误映射参考同事 MCP skill 的设计,但**不引入其 SQLite cache/MCP 工具形态**
  ——账本已经是 source_access + data_asset,不许出现第二套来源账本。
- 错误三态严格分开:权限/限流/连接失败 = `error`(不是查空);真空结果 = `empty`(§3.9,
  只记 source_access);两者都不建 data_asset。
- 增量:行情按 security × trade_date 窗口(交易日历/订阅池驱动);财务/事件按公告日
  (ann_date/f_ann_date)增量 + lookback 窗口兜重述与迟到更正,**不能只按 max(report_period)**。
  checkpoint 表(intake_core.source_checkpoint)进 roadmap,M-C 用显式窗口参数。

## 8. 执行计划修订(P3 起,替代原 P3–P6)

```text
P3a 端口冻结 + registry:port.py、registry schema(pydantic)+ loader/validator、
    三个 dataset YAML(骨架级,字段以速查核实为限)、单元测试
P3b registrar:register_dataset_snapshot 全链(run/source_access/幂等三态/supersede/outbox 行)
    + FakeProvider 测试矩阵(ok/empty/error/observed/materialized/supersede/单位换算/校验拒绝)
P4  intake_public 视图(data_assets_v1、source_accesses_v1、change_events_v1)+
    (provider, semantic_key) active 索引 migration + 权限收口测试
P5  错误模型(§3.11 四码)+ 契约导出(视图列 + registry schema + payload 契约)+ 契约测试
P6  tool_result 路径(与通道决策无关,按原计划)
P-通道(等用户确认 SQL vs API 后的独立小包):真实 provider adapter 二选一 + opt-in smoke;
    之后补另一通道 mapping 验证同 key 双通道切换。
```

验收底线:同一 DatasetRequest 在 FakeProvider 下产出合法 dataset_snapshot 信封;三态正确;
同内容重跑只 observed;内容变化 materialized + supersede;L2 侧只依赖 dataset_key + 语义字段。
**P3a–P6 全部不依赖通道决策,不依赖真实凭证。**

## 9. 与 GPT-Pro 评审的取舍记录

采纳(与独立底稿一致或优于底稿):语义 key 与三条数据集拆分(earnings_event 命名取评审版)、
raw+factor 复权口径与 basis_date≤as_of 公式、registry YAML 槽位设计(§4 大体取其骨架)、
SQL 白名单细则(allowed columns/required predicates/deny tokens)、财务增量 lookback 窗口、
row_published_at、DatasetResult 加 warnings/stats、text-to-DatasetRequest 定位、
"业务两类 registry 三条"的组织、payload 分块。

驳回/修正(独立判断,理由如下):

- **R1 `requested_fields` 不进登记 DatasetRequest**(评审 §5 示例含它):字段投影改变 payload →
  content_hash 碎片化,同一数据源产生互不相认的资产,幂等与 supersede 被打穿。登记按完整契约
  字段集;投影是读侧(L2 查询/planner)概念。
- **R2 dedup_key 不含 registry_version**(评审修正 1 建议含):内容变化已由 content_hash 表达;
  版本变内容不变时强制 materialized 会无谓触发下游失效(违 §2.8 语义)。版本走 provenance。
- **R3 registry 目录放服务内**(评审建议根级 registries/):协议 §3.7 定位是 L1 adapter 配置
  清单,归属 asset_intake;根 docs/reference 只放引擎级文档。
- **R4 评审 YAML 中的具体 Wind 列名/表名仅作参考**,落条目时逐个对 `aliyun-wind-rds/references/`
  速查核实——评审引用与真实 schema 可能有出入,字典是契约,错一个字段名污染全链。
- **R5 `statement=indicator` 保留但标注**:fina_indicator 类是 provider 预计算的派生指标,
  仍按 tier_1/G1 数据服务登记,与"系统自算派生进 L3"不冲突;字典 description 必须注明
  provider-derived,防止 L2/L3 把它当原始报表事实对账。

## 10. 待决与 roadmap

- 通道确认(用户,数周内):SQL(阿里云 Wind RDS)vs API(tushare 官方)→ 决定 P-通道先做哪个 adapter。
- roadmap:source_checkpoint 表与调度驱动(理想:财务增量由 disclosure_anchor change feed 触发,
  写进 M-D 规划)、fetch_iter 分页、双 provider 并存验证、text-to-DatasetRequest planner、
  L2 numeric_observation mapper(属 L2 里程碑)。
