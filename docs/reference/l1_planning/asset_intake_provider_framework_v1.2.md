# asset_intake provider 框架定稿 v1.2

状态:定稿(v1.0 2026-07-06 → v1.1 按 Wind 官方字典修订 → **v1.2 按两通道真实凭证实测 +
GPT-Pro 二轮评审独立取舍**)。形成过程与取舍全记录见 §9;实测事实见 §11(本版最高权威:
活 RDS / 活 API 的探测结果)。P3 起的实现以本文为准;修改本文需说明与协议 §2.1 扩展纪律、
§16 硬边界的一致性。

设计总纲(用户核心要求):**标准化数据接入(SQL / API / 未来 web search / MCP)一切皆配置**
——语义契约、物理映射、provider 优先级、表候选、安全规则全部是 Git 管理的数据(YAML),
代码只是解释器;换通道、换表、换 provider 改配置不改代码、不改契约、不改下游。

## 1. 定案清单

- **F1 dataset_key 语义命名,provider 无关;首发四条**:
  `cn_equity.eod_quote` / `cn_equity.fin_statement`(`statement=income|balance|cashflow`,
  **不含 indicator**)/ `cn_equity.fin_metric`(`metric_set=financial_indicator|...`)/
  `cn_equity.earnings_event`(`event_type=forecast|express`)。provider/通道只进 provenance。
- **F2 earnings_event 独立**(披露事件 ≠ 期间报表事实);**fin_metric 独立**(provider 指标
  ≠ 三大表原始事实,每条 record 带 `metric_origin=provider_derived|announced_by_company|
  wind_adjusted|ttm_mrq`,防 L2/L3 误当披露原始值对账)。
- **F3 复权口径**:L1 canonical 只存不复权 OHLCV + adj_factor(行级 row_published_at);
  qfq/hfq 为派生视图,`qfq(t;basis_date)=raw(t)×factor(t)/factor(basis_date)` 且
  `basis_date ≤ as_of`。字典级标注 `adjustment_factor_asof_grade: latest_recomputed`
  ——当前因子是"最新重算"性质,**禁止当作正式回测级 as-of-safe 结果**;将来接权益 PIT 数据后
  才允许升 `pit_safe`。实测硬事实:**adj_factor 是 provider 相对值**(同日 000001.SZ:
  Wind 85.33 vs tushare 139.008),跨 provider 绝对值不可比,只有比值有意义。
- **F4 两层配置**:
  `registry/datasets/<dataset_key>.yaml` = 语义契约(字段/单位/口径/时间语义/dedup 规则,
  小而稳定);
  `registry/providers/<provider>.catalog.yaml` = 物理世界(连接 profile 名、safety 默认、
  索引纪律、**table_alias:候选表 + activation 规则 + freshness 探测**,宽而 provider 专属)。
  dataset YAML 经 table_alias 引用 catalog,物理表改名/换代不动语义层。两者都是服务内
  Git 配置(协议 §3.7),pydantic 模型 + contract test 守护;只登记当前消费的字段。
- **F5 L1 不跨 provider 合并、不裁决**:同 dataset_key 多 provider 各自入账
  (`(provider, dedup_key)` 唯一,零改表);对账/采信在 L2。
- **F6 幂等三态**(§6):同 provider 同 semantic_key 同 content_hash → observed;
  content_hash 变 → materialized + supersede;不同 provider 永远独立。
- **F7 SQL 只走白名单模板**:绑定参数、allowed tables/columns、required predicates、
  max_rows、timeout、deny tokens、禁 SELECT *、只读账号;大表必须带证券代码前缀谓词;
  **日期列是 varchar(8),SQL 内禁止 CONVERT 破坏索引**,日期转换在 adapter 出口做;
  跨票篮子查询 `IN (:list)` 限 ≤200。
- **F8 LLM 不进登记层**:未来自由问数 = text-to-DatasetRequest(候选经 registry validator
  确定性裁决),不是 text-to-SQL;M-C 不实现。
- **F9 adapter 不碰 DB**:只返回 DatasetResult;registrar 是唯一写 DB 的地方。
- **F10 登记路径无字段投影**:按 registry 完整语义字段集落 payload;投影是读侧概念。
- **F11 provider_priority 是服务级配置,不进 dataset_key**:默认 A 股四条数据集均
  `[wind_rds, tushare_api]`(Wind SQL 主通道、tushare 备用/交叉校验;两通道均已实测可用,
  见 §11);用户改优先级 = 改一行配置。

## 2. 分层与写权边界

```text
调用方(人 / agent / 未来 planner / 未来 L6)
  │ DatasetRequest(dataset_key + 语义 query_params)
  ▼
registry/datasets(语义契约)── validator:key/params/字段合法
  │        └── 经 table_alias 引用 ──► registry/providers(物理 catalog:候选表、activation、safety)
  ▼
provider adapter(DatasetProvider 端口;wind_rds=SQL 型 / tushare_api=API 型 / 未来其他)
  │ 参数规整 → 白名单模板渲染或 endpoint 调用 → 字段映射 + 确定性单位换算 → DatasetResult
  ▼
registrar(register_dataset_snapshot,确定性,唯一写 DB)
  │ processing_run → source_access(ok/empty/error)→ 幂等判定(§6)
  │ → data_asset(envelope_kernel 校验)→ outbox(observed/materialized)
  ▼
intake_public.*_v1 视图 → L2
```

模块边界(P3 实现):

```text
src/asset_intake/providers/port.py         DatasetRequest / DatasetResult / DatasetProvider / ProviderError
src/asset_intake/providers/registry.py     dataset 条目与 provider catalog 的 load / validate / resolve
src/asset_intake/providers/sql_template.py 模板校验、参数绑定、安全规则(F7)
src/asset_intake/providers/wind_rds.py     SQL 型 adapter(P-通道)
src/asset_intake/providers/tushare_api.py  API 型 adapter(P-通道,备用)
src/asset_intake/application/register_dataset.py  registrar
registry/datasets/*.yaml + registry/providers/*.catalog.yaml
```

## 3. 端口定稿签名(同 v1.1,冻结)

```python
@dataclass(frozen=True)
class DatasetRequest:
    dataset_key: str
    query_params: dict[str, Any]

@dataclass(frozen=True)
class ScopeHints:
    subject_candidates: list[str] | None = None
    report_period: str | None = None
    event_time: datetime | None = None
    published_at: datetime | None = None
    title: str | None = None
    semantic_key: str | None = None

@dataclass(frozen=True)
class DatasetResult:
    records: list[dict[str, Any]]
    returned_fields: list[str]
    provider_as_of: str | None
    locator: str | None      # 含 table_alias 与实际 active 物理表;严禁 host/凭证
    raw_asset_ref: str | None = None
    scope: ScopeHints = ScopeHints()
    warnings: tuple[str, ...] = ()
    stats: dict[str, int] | None = None

class ProviderError(Exception): ...

class DatasetProvider(Protocol):
    provider_name: str
    adapter_name: str
    adapter_version: str
    source_tier: SourceTier   # 首发均 tier_1
    trace_level: TraceLevel   # G1
    def fetch(self, request: DatasetRequest) -> DatasetResult: ...
```

locator 形如 `wind_rds://alias/earnings_forecast?active_table=AShareProfitNoticeNew&template=...@v1&params_hash=...`。

## 4. 两层配置的 schema 槽位

### 4.1 registry/datasets/<dataset_key>.yaml(语义层,同 v1.1 §4 结构)

semantic_contract(asset_kind/payload_kind/material_type/tier/trace、query_params、primary_key、
time_semantics、subject_semantics、fields 含 unit/description/required)+ providers(每 provider:
table_alias 或 endpoints 引用、field_map、transform)+ validation + dedup。

### 4.2 registry/providers/<provider>.catalog.yaml(物理层,v1.2 新增)

```yaml
schema_version: provider_catalog.v1
provider: wind_rds
provider_kind: sql_server_rds        # 未来可扩 http_api / mcp / web_search
connection_profile: WIND_RDS         # 只是环境变量组名,凭证不进任何 YAML
default_safety: {readonly_required, single_statement_only, deny_tokens, forbid_select_star,
                 require_bound_params, default_timeout_seconds, default_max_rows}
index_discipline:
  required_prefix_predicates: [S_INFO_WINDCODE]
  date_columns_are_varchar8: true    # 列侧禁 CONVERT
table_aliases:
  <alias>:
    semantic_datasets: [<dataset_key>...]
    candidates: [{table, role, key_columns, date_column(s), required_columns}]
    active_table: <实测选定;可为 null 待 smoke test>
    activation_rule: [table_exists, required_columns_present, max_date_freshness, sample_rows_pass]
    freshness_check: {sql 或 endpoint 探针}
```

activation 纪律:**文档与 dataset YAML 永不硬编码物理表名**;active_table 由 adapter 初始化/
smoke test 按 activation_rule 选定并写进 locator 与 source_access。依据(实测,§11):同名语义
在不同 Wind 产品/同步包里物理表不同——本 RDS 中 `AShareProfitNoticeNew` 是活表(数据到当天),
官方字典展示的 `AShareProfitNotice` 在本 RDS 停在 20240905;两者都存在,靠新鲜度裁决。

## 5. 首发四条数据集契约要点(以 §11 实测列为准)

### 5.1 cn_equity.eod_quote

- params:security、start_date、end_date、price_adjustment(枚举仅 raw_plus_factor)。
- fields:security、trade_date、open/high/low/close/pre_close、volume(股)、amount(元)、
  adj_factor(as_of_sensitive;字典级 asof_grade=latest_recomputed)、row_published_at。
- 单位实测勾稽:Wind S_DQ_VOLUME 与 tushare vol 同值(单位=手,×100→股);
  S_DQ_AMOUNT 与 tushare amount 同值(单位=千元,×1000→元)。
- wind_rds:alias `eod_prices` → AShareEODPrices(实测活,至上一交易日);
  tushare_api:daily + adj_factor 两 endpoint inner join(ts_code, trade_date)。
- 只支持 security × date_range,长历史按年分块;增量可用 AShareCalendar
  (实测存在,交易日到 20271231)驱动。

### 5.2 cn_equity.fin_statement(仅三大表)

- params:security、report_period(或 start/end_period)、statement=income|balance|cashflow、
  report_scope(默认 consolidated 合并口径,显式枚举,不混入)。
- wind_rds alias(**本 RDS 实测真实表名**):income → `AShareIncome`(99 列,含 ANN_DT/
  ACTUAL_ANN_DT/STATEMENT_TYPE/OPDATE)、balance → `AShareBalancesheet`(小写 s——与官方
  字典拼写不同,以 RDS 为准)、cashflow → `AShareCashflow`(同理)。
- 同 (security, report_period, statement, scope) 多行取 latest:ANN_DT DESC → OPDATE DESC;
  无 OPDATE 的表在 catalog 里声明 fallback 序,不写死代码。
- tushare_api:income/balancesheet/cashflow(实测可用,含 ann_date/f_ann_date/report_type/
  comp_type/update_flag;金额单位=元,无需换算)。
- 首发字段=预测主链最小集(v1.1 清单不变),两 provider 字段名逐个对 §11 实测列核实。

### 5.3 cn_equity.fin_metric(v1.2 新增)

- params:security、report_period(或区间)、metric_set(M-C 仅 `financial_indicator` active;
  ann_financial_indicator / financial_derivative / reportperiod_adjusted / ttm_mrq 为 roadmap
  ——**且本 RDS 实测不存在这些表**,启用需 tushare 或未来同步包)。
- 每条 record 必带 metric_origin(F2)。
- wind_rds:`AShareFinancialIndicator`(实测 169 列,**无 ANN_DT**,有 OPDATE)——
  published_at 规则:join AShareIncome.ANN_DT on (windcode, report_period),缺失退 OPDATE
  (在 dataset YAML 的 time_semantics 声明,不写死代码);
  tushare_api:fina_indicator(实测**自带 ann_date**,百分比字段单位=percent)。
- 首发字段:ROE/毛利率/净利率/扣非净利/周转类的最小集,逐列核实两侧名称与单位。

### 5.4 cn_equity.earnings_event

- params:security 或 ann_date range、report_period?、event_type=forecast|express。
- **forecast**:wind_rds alias `earnings_forecast`,candidates=[AShareProfitNoticeNew(实测
  active:36.2 万行、数据到当天)、AShareProfitNotice(实测停 20240905,保留候选)];
  实测 NoticeNew 只有 **24 列**——官方字典 V3 的营收预告/权益预告/扣除后营收等低覆盖列
  **本 RDS 不存在**,首发语义字段以 24 列 ∩ tushare forecast 为核:event_type、report_period、
  published_at(最新公告日)、first_ann_date、style(**归一枚举**:Wind 数字代码 454010000 系
  与 tushare 中文标签"预增/预减…"各配 code_map,原值保留 provider_raw)、sign_change、
  净利区间(万元→元)、同比幅度区间(%)、扣非净利区间与上年扣非、EPS 区间与上年 EPS、
  上年归母净利、公布次数、abstract、reason(3000 字文本,原样入 payload)。
- **express**:wind_rds `AShareProfitExpress`(实测活,全局 max ANN_DT=20260507,34 列:
  营收/营业利润/利润总额/归母净利/EPS/ROE/总资产/净资产 + BRIEF_PERFORMANCE 文本);
  tushare express 实测可用,字段对应。
- 修正/多次预告 = 新行(公布次数 + 首次公告日),与 §6 supersede 语义对应。

## 6. 幂等、去重、取代与 as-of(同 v1.1,冻结)

semantic_key = dataset_key + 规范化 query scope → `semantic_key` 列;
dedup_key = sha256(semantic_key || content_hash) → `(provider, dedup_key)` 唯一;
observed / materialized + supersede 三态;content_hash 不含查询时间;
registry 版本不进 dedup_key(版本走 provenance);snapshot 级 published_at = 行级最大值;
(provider, semantic_key) active 部分索引随 P4 migration 加。

## 7. 安全与增量(v1.1 基础上强化)

- SQL:F7 全套 + varchar8 日期列禁 CONVERT + 篮子 ≤200 + 大表禁无证券码谓词的全市场扫
  (全市场增量需求走"按交易日 + 分块"模板,逐票或小篮子)。
- API:限流退避与错误映射;权限/频控 = `error`,真空 = `empty`,两者都不建 data_asset;
  **不引入 skill 的 SQLite cache/MCP 工具形态**(账本唯一)。
- 增量:行情按交易日历(AShareCalendar)驱动 security × trade_date 窗口;财务/事件按
  公告日(ANN_DT / ann_date)增量 + lookback 兜重述,不按 max(report_period);
  checkpoint 表进 roadmap,M-C 显式窗口。

## 8. 执行计划(P3 起)

```text
P3a 端口冻结 + registry 双层 schema(dataset + provider catalog 的 pydantic 模型/loader/
    validator)+ 4 个 dataset YAML + 2 个 provider catalog YAML(active_table 填实测值,
    候选与 activation_rule 齐备)+ 单元测试(含:alias 引用必须存在、active mapping 覆盖
    required fields、transform 有 fixture、未 active 的 alias 不可被执行器调用)
P3b registrar 全链 + FakeProvider 测试矩阵(ok/empty/error/observed/materialized/supersede/
    单位换算/校验拒绝;fixture 含 NoticeNew 形态与 tushare forecast 形态各一)
P4  intake_public 三视图 + (provider, semantic_key) active 索引 migration + 权限收口
P5  错误模型(§3.11 四码)+ 契约导出(视图列 + 两层 registry schema + payload 契约)+ 契约测试
P6  tool_result 路径(不受通道影响)
P-通道 首选 WindRdsProvider(table alias resolver + 模板执行器 + 映射/换算 + freshness smoke),
    次做 TushareApiProvider(join/限流);两通道实测均已打通(§11),真实 smoke 均为显式 opt-in
```

验收底线不变(v1.0 §8);P3a–P6 仍不依赖真实凭证。

## 9. 取舍记录(累计)

对 GPT-Pro 二轮(Wind 现实修订版)的取舍:

- **采纳 R6:拆出 fin_metric + metric_origin**——实测支持:Wind indicator 表 169 列且无
  ANN_DT,与三大表(99 列、有 ANN_DT)物理形态迥异;tushare 侧也是独立 endpoint。
- **采纳 R7:预告表名不硬编码,candidates + activation**——实测支持且**反转了 v1.1 的判断**:
  本 RDS 活表恰是 NoticeNew(v1.1 曾依官方字典断言"评审的 NoticeNew 有误",错;官方字典
  描述 Wind 产品全集,同步包各异,唯一裁决者是活库探测)。权威链据此强化为:
  **活库/活 API 探测 > RDS 文档/官方字典 > 任何评审或记忆**。
- **采纳 R8:wind_rds 默认主通道、tushare 备用/交叉校验**(F11)——实测两通道全通,
  Wind SQL 批量与表宽占优;priority 是一行配置,用户随时可改。
- **采纳**:provider catalog 两层配置、asof_grade 标注、SQL hardening(CONVERT 禁令/篮子上限)、
  express 独立 alias。
- **部分驳回:earnings_event 全字段扩容**——评审按官方字典 35 列全登;实测 NoticeNew 仅 24 列,
  官方字典的营收/权益预告列不存在。按 F4"只登记当前消费字段"+ 实测存在性登记,不登幽灵列。
- **维持驳回**(v1.0 轮):R1 requested_fields 不进登记路径;R2 dedup_key 不含 registry 版本;
  R3 registry 放服务内。
- 评审 catalog 示例中的 optional_later 表(ANN indicator / derivative / TTM / 审计 / 披露日历)
  本 RDS 均不存在:不进 catalog 正文,进 §10 roadmap 备注,避免配置里堆幽灵条目。

## 10. 待决与 roadmap

- **默认 provider_priority = wind_rds 优先**已按实测写入;用户若对最终通道另有安排,
  改 F11 配置一行即可(P-通道实现顺序随之调整)。
- roadmap:AShareValuationIndicator / AShareEODDerivativeIndicator(估值日频,M-D 议)、
  AShareForcast(**实测为一致预期表**——协议 §15.6 baseline claims 暂不启用,仅备注)、
  AShareDescription / AShareST / 行业分类表(Subject Registry 相关,L4/L6 议)、
  权益 PIT 接入后 asof_grade 升 pit_safe、source_checkpoint 表、text-to-DatasetRequest
  planner、L2 numeric_observation mapper、web search / MCP 轻载源纳入同一"catalog 化"
  配置思想(tool_result 路径的 source catalog,M-D 议)。

## 11. 实测事实附录(2026-07-06,真实凭证只读探测;凭证不落任何文件)

**Wind RDS(阿里云 SQL Server,pymssql 直连)**:

- 库内 **50 张基表 + 1 视图**,全部 dbo schema。A 股核心:AShareEODPrices、AShareIncome、
  AShareBalancesheet(小写 s)、AShareCashflow(小写 f)、AShareFinancialIndicator、
  AShareProfitNotice、AShareProfitNoticeNew、AShareProfitExpress、AShareCalendar、
  AShareDescription、AShareST、AShareValuationIndicator、AShareEODDerivativeIndicator(视图)、
  AShareForcast(一致预期)、行业分类表、指数表、基金表、HK 表若干。
- **官方字典有而本 RDS 没有**:AShareANNFinancialIndicator、AShareFinancialderivative、
  AShareReportperiodindex、AShareTTMAndMRQ、AShareIssuingDatePredict、审计表。
- 预告双表裁决:NoticeNew 362,681 行、max 公告日 **20260706(当天)**;Notice 121,776 行、
  max **20240905(停更)**。NoticeNew 实际 **24 列**(官方字典 V3 的 35 列是产品全集,
  营收/权益预告等低覆盖列本 RDS 缺)。
- 新鲜度:EODPrices 至 20260703(上一交易日);Income ANN_DT 至 20260425;ProfitExpress
  全局 ANN_DT 至 20260507(个股会停发,如 000001.SZ 停在 2023,不能按个股判死);
  Calendar 至 20271231。
- 结构要点:Income 99 列(ANN_DT / ACTUAL_ANN_DT / STATEMENT_TYPE / OPDATE);
  FinancialIndicator 169 列(**无 ANN_DT**,有 OPDATE);ProfitExpress 34 列;
  日期列均 varchar(8) YYYYMMDD。
- 单位勾稽(000001.SZ 20260703):S_DQ_VOLUME=863326.64(手)、S_DQ_AMOUNT=888789.393(千元)、
  close=10.29 → 86.3M 股 × 10.29 ≈ 8.89 亿元 ✓。S_DQ_ADJFACTOR=85.329579。

**tushare(HTTP api.tushare.pro,同事共享 token,用户将换密钥)**:

- 实测**全部可用**:daily、adj_factor、income、balancesheet、cashflow、fina_indicator、
  forecast(可按 ann_date 全市场查)、express。
- daily 的 vol/amount 与 Wind 同值同单位(手/千元);三大表金额单位=**元**;
  forecast 金额单位=**万元**——同一 provider 内不同 endpoint 单位也不同,单位换算必须
  per-dataset 在 field_map 声明。
- forecast 字段:type 为**中文标签**("预增"等)、p_change_min/max、net_profit_min/max、
  last_parent_net、first_ann_date、summary、change_reason、update_flag。
- **同日 adj_factor=139.008 vs Wind 85.329579**:因子基准不同,provider 相对值,
  跨 provider 绝对值不可比(F3/F5 实证)。
- fina_indicator 自带 ann_date(Wind indicator 表没有)——fin_metric 的 published_at 规则
  因 provider 而异,在 dataset YAML time_semantics 按 provider 声明。
