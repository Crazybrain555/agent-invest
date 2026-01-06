# Corr（因子相关性）工具 PRD（Phase-0）

> 目标：提供一个**可配置、可复现、跨表**的“因子相关性”工具，面向：
> - 新因子验重（1-to-many）
> - 训练前低相关筛选（many-to-many）
> - 低频临时排查（1-to-1 或 1-to-many / 指定列表）
>
> Phase-0 范围明确：**不处理 window / `z_windows` 复杂度**，默认使用**固定年份采样（fixed_years）**的速度模式，并把跨表范围/输出规范/采样策略等全部放进 `configs/tools/corr/` 配置体系。

---

## 1. 修订目标（Phase-0 收口）

### 1.1 范围收口：不含 window 表

Phase-0 明确不处理 window 复杂度：默认不纳入 `ai_is.inter_train_factors_mkt_processed_v1`、`ai_is.inter_train_factors_mkt_processed_v3` 两张表（它们在表配置里均有 `extra_fields: ['z_windows']`，见 `configs/db/local_db_configs.yaml`）。

Phase-0 的候选跨表范围**先聚焦在 `quantitative_*` 族表**（在 `configs/db/local_db_configs.yaml` 中普遍 `extra_fields: []`，天然没有 window 维度，适合快速批量相关性分析）。

### 1.2 速度模式默认：固定年份采样（fixed_years）

Phase-0 默认采样策略使用固定年份：

- `years = [2012, 2015, 2018, 2020, 2022, 2024]`

默认选日为 `random_k_per_year`（每年抽样 `random_days_per_year` 个交易日，且固定 `random_seed`），必要时再叠加 `random_stocks_per_date` 做股票抽样。

---

## 2. 数据与表结构（以 YAML 为准）

### 2.1 表结构来源（权威）

- 表结构/字段映射配置：`configs/db/local_db_configs.yaml`
  - 因子表普遍为 `table_type: long`
  - 由 `LocalTestDBDataProvider.fetch_data()` 输出并统一为标准列：`trade_date`, `stock_code`, `field_name`, `value`（以及配置声明的 `extra_fields`）

### 2.2 因子分类与候选清单来源

- 二级分类映射：`configs/field_mappings/factor_mapping.yaml`
  - 结构：`level1 -> level2 -> [factor_names]`
  - 表名规则（以 mapping 文件注释为准）：`ai_is.quantitative_{level1}_{level2}_signals`
    - 例：`growth.profitability -> ai_is.quantitative_growth_profitability_signals`

> 说明：该推导规则只用于生成“候选表名”；最终仍以 `include_tables` 白名单过滤为准（防止误扩表）。

---

## 3. Phase-0：数据源范围（默认 include/exclude tables）

### 3.1 默认 include_tables（跨表候选来源）

默认跨表候选来源表（带 schema）：

- `ai_is.quantitative_alternative_high_frequency_signals`
- `ai_is.quantitative_alternative_institutional_patent_signals`
- `ai_is.quantitative_analyst_coverage_rating_signals`
- `ai_is.quantitative_analyst_earnings_revision_signals`
- `ai_is.quantitative_growth_forecast_trend_signals`
- `ai_is.quantitative_growth_profitability_signals`
- `ai_is.quantitative_growth_revenue_asset_signals`
- `ai_is.quantitative_other_signals`
- `ai_is.quantitative_quality_cashflow_safety_signals`
- `ai_is.quantitative_quality_operating_efficiency_signals`
- `ai_is.quantitative_quality_profit_quality_signals`
- `ai_is.quantitative_sentiment_liquidity_signals`
- `ai_is.quantitative_sentiment_momentum_signals`
- `ai_is.quantitative_sentiment_price_return_signals`
- `ai_is.quantitative_sentiment_value_reversal_signals`
- `ai_is.quantitative_sentiment_volatility_signals`
- `ai_is.quantitative_value_valuation_signals`

备注：
- 这套清单与项目里“多表合并/标准化参数生成”维护源表 list 的方式一致（便于版本化与回溯）。
- Phase-0 不引入 `z_windows` 维度；对没有 `z_windows` 字段的表（如 `ai_is.quantitative_other_signals`），直接按普通因子处理即可。

### 3.2 默认 exclude_tables（Phase-0 显式排除）

Phase-0 显式排除：

- `ai_is.inter_train_factors_mkt_processed_v1`
- `ai_is.inter_train_factors_mkt_processed_v3`

注意：
- `ai_is.inter_train_factors_mkt_norm_academic_dcount1` 为历史训练因子表（计划删除/不再使用）；Phase-0 不纳入。

---

## 4. 配置目录：`configs/tools/corr/`（Phase-0 必需）

### 4.1 文件布局（建议）

建议新增配置目录 `configs/tools/corr/`，用于承载默认参数、默认数据源表清单、输出目录规范、采样策略等；让“跨表范围”可通过 config 或 CLI 参数轻量维护。

推荐文件布局：
- `configs/tools/corr/default.yaml`：默认配置（绝大多数场景使用）
- `configs/tools/corr/tables.yaml`：默认跨表清单（Phase-0 的 `include_tables`）
- `configs/tools/corr/sampling.yaml`：采样策略模板（fixed_years / recent_n_years / date_range + random_k_per_year）
- （可选）`configs/tools/corr/presets/*.yaml`：场景化预设（`train-precheck` / `check-new-factor` / `adhoc`）

> 备注：项目已有 `ConfigLoader`（并带缓存）。若配置修改未生效，通常需要重启进程或清 cache（以实际实现为准）。

### 4.2 `default.yaml`（示例结构，供实现与对齐）

```yaml
corr_tool:
  version: 0.1

  output:
    root: "data/factor_correlation"
    run_dir_naming: "{mode}__{method}__{corr_type}__{sample_tag}__{ts}"

  compute:
    method: "cross_sectional"     # cross_sectional | time_series
    corr_type: "spearman"         # pearson | spearman
    min_periods: 30
    high_corr_threshold_abs: 0.7
    use_gpu: false                # 可选：是否启用 GPU（默认 false，优先靠采样/分块提速）

  sampling:
    mode: "fixed_years"           # fixed_years | recent_n_years | date_range
    years: [2012, 2015, 2018, 2020, 2022, 2024]
    day_picker: "random_k_per_year"  # random_k_per_year
    random_seed: 42
    random_days_per_year: 60      # 仅对 random_k_per_year 生效：每年抽样交易日数量（默认 60）
    random_stocks_per_date: 2000  # 可选：每个日期抽样股票数量（默认 2000；设 null 表示不抽样）

  universe:
    exclude_forbid_pool: true
    forbid_pool_table: "ai_is.forbid_pool_comprehensive"

  sources:
    include_tables_ref: "configs/tools/corr/tables.yaml"
    exclude_tables:
      - "ai_is.inter_train_factors_mkt_processed_v1"
      - "ai_is.inter_train_factors_mkt_processed_v3"


  naming:
    strict_name: false
    factor_key:
      include_source_table: true


  cache:
    enable: true
    registry_path: "data/factor_correlation/cache/factor_registry.parquet"
    edges_cache_path: "data/factor_correlation/cache/corr_edges.parquet"
```

### 4.3 CLI 覆盖原则（建议）

- 默认以 `default.yaml` 为准；CLI 仅覆盖少量参数（例如：`--mode/--group/--table/--years/--out-root`）。
- sampling 覆盖建议：`--random-days-per-year/--random-stocks-per-date/--random-seed`（仅覆盖必要参数，避免 config 失控）。
- 每次运行将“最终生效配置快照”写入 `summary.json`（保证可复现、缓存可命中、便于审计）。

### 4.4 新代码放哪里（推荐落地位置）

结论（推荐）：
- **工具核心代码**放在 `src/tools/corr/`（独立于 `src/utils`，避免与“通用工具杂项”混在一起；且你已明确 `src/utils/factor_correlation_analyzer*.py` / `src/utils/README_factor_correlation.md` 未来会删除，新的实现不应继续堆在同一位置）。
- **CLI 入口脚本**放在仓库根目录 `tools/` 下（与现有脚本风格一致，例如 `tools/generate_indices.py`），例如：
  - `tools/corr_tool.py`（或 `tools/corr/run_corr.py`）

建议目录结构（示例）：
- `src/tools/corr/cli.py`：argparse + modes（check-new-factor / train-precheck / adhoc）
- `src/tools/corr/config.py`：读取 `configs/tools/corr/default.yaml` + presets + CLI 覆盖，输出最终 config 快照
- `src/tools/corr/sources.py`：解析 `factor_mapping.yaml`，按 `include/exclude tables` 过滤并生成取数计划
- `src/tools/corr/sampling.py`：fixed_years + day_picker（Phase-0 仅 `random_k_per_year`，保证可复现）
- `src/tools/corr/compute.py`：相关性计算内核（支持分块/阈值边表输出；GPU 可选）
- `src/tools/corr/cache.py`：factor registry + corr edges 增量缓存
- `src/tools/corr/report.py`：统一输出 `summary.json`/parquet/plots/recommendation

> 备选方案：如果你更希望“工具都放 utils”，也可以用 `src/utils/corr_tool/`；但不建议继续在 `src/utils/` 根目录堆脚本文件，容易与 legacy 文件混淆、后续清理困难。

约束：
- `src/utils/factor_correlation_analyzer*.py`/`src/utils/factor_correlation_analyzer2*.py` / `src/utils/README_factor_correlation.md` 仅可作为历史参考；新工具实现不要 import/依赖它们（你已明确后续会删除）。

---

## 5. Phase-0：FactorKey（因子唯一标识）与命名策略

### 5.1 FactorKey（写死，但先不引入 z_windows）

既然 Phase-0 排除了 window 表，PRD 里因子唯一标识先写成：

- `FactorKey := (source_table, field_name)`

> Phase-1 若纳入 window 表，再升级为 `(source_table, field_name, z_windows)` 并引入 window 维度的命名/冲突处理。

### 5.2 展示名（display_name）

默认展示名带来源前缀，避免跨表同名冲突：

- `display_name := "{source_table}::{field_name}"`

### 5.3 `--strict-name`（严格模式）

- 默认 `strict_name=false`：允许存在“同名但不同来源表”的因子（适合训练前批量筛选）
- `strict_name=true` / `--strict-name`：当用户仅提供 `field_name` 且跨表同名存在歧义时，直接报错并列出候选（适合低频排查）

---

## 6. 计算口径（默认）与采样策略

### 6.1 相关性定义

默认相关性：**截面相关（cross_sectional）**

对每个交易日 `t`：
- 在当日股票截面上，对 `factor_A` 与 `factor_B` 的 `value` 做相关性（Pearson/Spearman）
- 得到日度相关 `corr_t(A,B)`

全区间相关性矩阵：
- 默认输出 `mean_t corr_t(A,B)`，并可扩展输出 `median/std/分位数`

可选相关性：**时间序列相关（time_series）**

对每只股票 `s`：
- 在时间序列上对两个因子做相关性，得到 `corr_s(A,B)`
- 全样本对 `corr_s` 做平均，得到矩阵

### 6.2 Phase-0 默认配置

- `method = cross_sectional`
- `corr_type = spearman`（更稳健；可配成 pearson）
- `min_periods = 30`

### 6.3 Phase-0 默认速度模式：fixed_years

采样模式：
- `mode = fixed_years`
- `years = [2012, 2015, 2018, 2020, 2022, 2024]`

选日规则（day_picker，Phase-0）：
- `random_k_per_year`（每年随机抽样 `random_days_per_year` 个交易日；必须固定 `random_seed` 以保证可复现）

#### 6.3.1 交易日从哪来（写死，避免扫全表）

为避免“为了找交易日而扫描所有因子表”的浪费，Phase-0 交易日统一来自 Wind 交易日历表 `wind_quant.dbo.AShareCalendar`（`S_INFO_EXCHMARKET='SSE'`）。该来源**不做成可配置参数**，避免误配导致运行时报错（如果未来确实要换交易日日历来源，应修改代码而不是改 config）。

参考实现（仓库里已有）：
- `src/data_service/data_loading/market_data.py`（`MarketDataProvider._get_trading_dates`）
- `src/tasks/index_stk_pool_task.py`（`IndexStockPoolTask._get_trading_dates`）
- `src/data_service/pipelines/Dataset_builder/calendar_utils.py`（`_get_trading_days_before`，带交易日历缓存）

- calendar 查询只取 `TRADE_DAYS`（建议 DB 端 `SELECT TRADE_DAYS ... ORDER BY TRADE_DAYS`，并过滤 `S_INFO_EXCHMARKET='SSE'`）
- `random_k_per_year`：对每个 year，从该年交易日集合中随机抽样 `random_days_per_year` 个交易日（固定 `random_seed`），并将最终抽样日期列表写入 `summary.json`

> 说明：交易日历仅用于选日；最终取数仍按 `include_tables` 的因子表范围执行。

### 6.4 可复现采样（强约束）

为了让结果稳定、缓存可命中：
- Phase-0 默认 `random_k_per_year`：必须固定 `random_seed`，并将抽样结果（最终 trade_date 列表）写入 `summary.json`。

### 6.5 性能优化设计（Phase-0 必须考虑）

#### 6.5.1 数据量控制（最重要）

- **默认采样**：Phase-0 固定年份（`fixed_years`）+ 随机抽样日（`random_k_per_year`）+（可选）随机抽样股票（`random_stocks_per_date`）是第一层降维。
- **按需取数（必须可执行）**：对 long 表取因子时必须传 `fields=[factor_names...]`（不允许空 `fields`），以触发 DB 端 `field_name IN (...)` 过滤；`LocalTestDBDataProvider._build_long_query()` 已实现该逻辑（并会自动把 `extra_fields` 一起 select 出来），见 `src/data_service/data_loading/local_testdb_data.py`。
- **可选股票降采样**（建议做成开关，且可复现）：
  - `random_stocks_per_date`：每个日期随机抽样 N 只股票（固定 `random_seed` 或用稳定 hash）
  - 适用于大规模场景（many-to-many）进一步提速
- **阈值优先输出**：当因子数很大时，默认产物以 `high_corr_pairs`（边表）为主；完整 `corr_matrix` 仅在规模可控时生成（见 6.5.3）。

#### 6.5.2 GPU 加速（可选，默认关闭）

- 默认 `use_gpu=false`（对大多数 Phase-0 规模，CPU + 采样 + 分块更稳定）。
- 当满足以下条件时才考虑启用 GPU：
  - 环境已安装并可用 GPU 版 PyTorch
  - 因子数与样本规模足够大，且 CPU 版本成为瓶颈
- GPU 设计原则：
  - 只加速“纯矩阵相关性计算”部分；组内聚合/对齐/取数仍在 CPU
  - 使用 `float32` 避免显存压力
  - 必须支持 CPU fallback（GPU 不可用时自动降级，并在 `summary.json` 记录）

#### 6.5.3 内存管理（避免大矩阵 OOM）

- **分块计算（block/chunk）**：
  - many-to-many 默认支持按 factor block 计算相关性（例如 128×128 block），避免一次性生成超大矩阵
- **矩阵 vs 边表策略**：
  - 当 `n_factors` 小于阈值（例如 `max_factors_full_matrix`）时才生成 `corr_matrix.parquet`
  - 超过阈值时默认只生成 `high_corr_pairs.parquet`（边表）+ `recommendation.*`（训练前筛选结论）
- **滚动相关性**：
  - 滚动相关性是典型高内存场景，Phase-0 默认不做（或必须显式开启并限制因子数/日期数）
- **I/O 格式**：
  - 默认 Parquet（可压缩），避免巨大 CSV；必要时提供小规模 `summary.csv`

---

## 7. 场景化需求（工具必须覆盖的常用模式）

### 7.1 场景 1：2-to-2（两个因子快速看截面相关性）

输入：
- `factor_A`：DB 因子或 DataFrame 因子
- `factor_B`：DB 因子或 DataFrame 因子
- 可选：采样策略、`corr_type/method`、universe（排除禁投等）

输出（建议最小集）：
- `corr_mean`, `corr_median`, `corr_std`, `n_dates`, `avg_n_stocks`
- 极端日期 Top-k（便于排查数据/异常）

### 7.2 场景 2：1-to-many（一个因子对一组候选因子）

输入：
- `target_factor`（DB 或 DataFrame）
- `candidate_factors`（列表；可来自 YAML group、手工列表、或代表性因子集合）

输出：
- 相关性排序表（含来源表、覆盖度、统计口径）
- 高相关警报：`abs(corr_mean) >= threshold` 的候选

### 7.3 场景 3：many-to-many（训练前筛选：查候选互相关）

输入：
- 待分析因子集合（可跨表；来自 YAML 分类 + 手工补充）
- 采样策略、相关性类型、阈值

输出（建议）：
- `corr_matrix.parquet`（必要时也可输出 csv，但大矩阵不建议）
- `high_corr_pairs.parquet`（长表：包含 abs corr、来源表、因子键）
- `recommendation.yaml|txt`（可选但建议默认生成）：给出“推荐低相关子集”与阈值说明（可复现）

### 7.4 场景 4：低频排查（指定列表互相关）

输入：
- 用户指定的因子列表（可跨表、可混合 DB/DataFrame）

输出：
- 小规模相关矩阵 + 高相关 pairs + 可选图

---

## 8. 跨表因子自动解析（factor_mapping + include/exclude）

### 8.1 从 `factor_mapping.yaml` 推导“组 → 表 → 因子列表”

工具应支持：
- `--group growth.profitability` 这类选择器
- 展开为 `(table, factor_name_list)` 的取数计划（按表分组拉取）

补充说明：
- “组 → 表”推导规则以 `factor_mapping.yaml` 文件注释为准（见其命名规则与示例）。
- 推导规则只用于生成候选表名；最终仍以 `include_tables` 白名单过滤为准。

### 8.2 sources 范围控制（白名单/黑名单）

为了让“跨表范围”可轻量维护、且避免误扩表：
- `configs/tools/corr/tables.yaml`（或 `default.yaml` 中直接写 `include_tables`）作为**白名单**：只允许从指定表集合拉取因子
- `exclude_tables` 作为显式黑名单：Phase-0 用于排除 window 表；也可用于临时下线某个表

当 `factor_mapping.yaml` 推导出的表不在白名单中：
- 默认跳过，并在 `summary.json` 里记录（包含被跳过表名与原因）

---

## 9. 输出规范（机器可读 + 人可读）

### 9.1 输出根目录

默认输出根目录：
- `data/factor_correlation/`

允许通过 config 或 CLI 覆盖输出位置（同一套规范）。

### 9.2 run 目录命名（建议）

建议命名模板（可配置）：
- `{mode}__{method}__{corr_type}__{sample_tag}__{ts}`

### 9.3 每次运行固定产物（Phase-0 强约束）

每次运行固定生成：
- `summary.json`：参数（含 config 快照）、采样信息、表清单、因子清单 hash、最终 trade_date 列表（若采样）、覆盖统计、耗时（建议作为 run 目录里第一文件）
- `corr_table.parquet`（1-to-many）或 `corr_matrix.parquet`（many-to-many）
- `high_corr_pairs.parquet`（过滤后的长表：`abs(corr) >= threshold`）
- `plots/`（可选）：`heatmap.png`, `distribution.png`
- `recommendation.yaml|txt`（训练前筛选场景）：推荐低相关子集与阈值说明

### 9.4 输出文件 schema 约定（写死）

为便于自动化与后续增量缓存，以下 schema 建议在 Phase-0 就写死：

`high_corr_pairs.parquet`（输出边表）至少包含：
- `factor_a`, `factor_b`
- `corr_mean`, `abs_corr`
- `source_table_a`, `source_table_b`

---

## 10. 缓存与增量更新（Phase-0 先把接口/格式写死）

### 10.1 缓存目标

训练前筛选通常不需要每天全量重算；希望维护一张“大相关性边表”，新增因子时只补新增边。

### 10.2 缓存 key（签名）必须包含

为了避免错误复用，签名至少包含：
- `tool_version`
- `include_tables_hash`（防“换了表清单却误命中缓存”）
- `sampling_hash`
- `universe_hash`
- 采样策略（fixed_years/recent_n_years/date_range + years/day_picker/random_seed）
- universe 约束（是否排除禁投、禁投表名等）
- 相关性口径（method/corr_type/min_periods）
- 因子唯一标识（Phase-0：`FactorKey=(source_table, field_name)`）与其它会影响取数/对齐的 filters

> 建议：signature 由以上信息规范化（排序/序列化）后做 hash；并写入 `summary.json` + `corr_edges.parquet`，保证缓存可复现、可审计。

### 10.3 缓存文件建议

建议路径（可配置）：
- `data/factor_correlation/cache/factor_registry.parquet`
- `data/factor_correlation/cache/corr_edges.parquet`

schema 约定（建议写死）：

`corr_edges.parquet`（缓存边表）列固定：
- `factor_a`, `factor_b`
- `corr_mean`, `corr_median`
- `n_dates`, `avg_n_stocks`
- `signature`, `created_at`

---

## 11. 代表性因子集合（P1：先写格式，避免返工）

Phase-0 可先只定义产物格式与优先级，自动挑选与聚类算法放到 P1。

固定产物格式建议：
- `data/factor_correlation/representatives.yaml`：允许人工 override（必须；且优先级最高）
- `data/factor_correlation/clusters.parquet`：簇成员与边信息（用于溯源/审计）

自动挑选规则（P1 预期写清楚）：
- 缺失率/覆盖度优先
- 或图中心性/代表性指标
- 人工 override 永远优先于自动结果

---

## 12. Phase-1（后续扩展，不在 Phase-0 范围）

当需要纳入训练因子表或 window 维度时（例如：
`ai_is.inter_train_factors_mkt_processed_v1/v3` 等）：
- 需要引入 `z_windows`，并升级 `FactorKey`：
  - Phase-1：`FactorKey := (source_table, field_name, z_windows)`
- 需要明确 window 的对齐策略（固定/忽略/并行）
- 缓存签名必须纳入 `z_windows`（避免错复用）

---

## 13. 验收标准（DoD）

### P0（Phase-0 最小可用）

- 默认不纳入 window 表（`ai_is.inter_train_factors_mkt_processed_v1/v3` 等），避免 `z_windows` 复杂度
- 默认速度模式：`fixed_years`（2012/2015/2018/2020/2022/2024），且采样可复现（确定性选日或固定 random_seed）
- 支持 2-to-2、1-to-many、many-to-many 三类核心场景（跨表）
- 能通过 `factor_mapping.yaml` 自动展开候选因子集合，并受 `include/exclude tables` 配置控制
- 输出落在 `data/factor_correlation/`，产物固定生成：`summary.json` / `corr_*` / `high_corr_pairs` / `recommendation.*`

### P1（增强）

- 代表性因子集合维护：`representatives.yaml` + `clusters.parquet`，并能用于新因子验重加速
- 缓存增量更新：新增因子只补算新增边，且签名机制能正确命中/失效
