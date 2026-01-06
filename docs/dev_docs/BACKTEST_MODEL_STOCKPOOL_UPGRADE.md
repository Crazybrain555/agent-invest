# Backtest StockPool 改造方案（Phase 1 之后）

> 目标：在现有 `backtest_model.py`（Pipeline 版本）基础上，新增“按股票池选股”的回测能力：  
> - 在保留“全市场选股回测”的基础上，新增 **沪深300 / 中证500 / 中证1000** 三个指数池内选股回测（可扩展更多 pool）  
> - 全市场与股票池内的选股数量分别可配置（默认：池内 50；全市场保持原逻辑可单独配置）  
> - 产出表格/NAV/图表均新增“股票池”维度（最后能同时看到 4 种情况：全市场 + 3 个池）  
> - 规范超额曲线口径：保留 `excess_nav=1+diff` 兼容旧产物，同时新增 `excess_nav_diff=diff` 并让图表优先绘制 `excess_nav_diff`

---

## 0. 现状梳理（当前代码结构）

当前回测入口为 `backtest_model.py`，其职责仅为：
- CLI → `ModelBacktestConfig`
- 调用 `BacktestResultPipeline(cfg).run()`

Pipeline 的核心 steps：
1. `step_factor`: 生成 `df_factor`（长表：`trade_date/stock_code/name/value`）
2. `step_signal`: 生成 `AlphaExpression` 列表（如 `expression_1`）
3. `step_backtest`: `BacktestRunner` 执行回测（输出 `StrategyBacktestResult`）
4. `step_benchmark`: 基准对齐与 NAV 计算（输出 `BenchmarkNavResult`）
5. `step_aggregate`: 汇总总体/年度表格（`AggregatedTables`）
6. `step_export`: 导出 nav csv/png、excel、manifest 等

关键现状限制：
- 当前只支持 **单一 benchmark**（`cfg.benchmark_code`）。
- 回测选股 universe 默认等同于“因子数据覆盖的全市场股票集合”，没有“按股票池约束 universe”的能力。

---

## 1. 需求拆解（你提出的改造点）

### 1.1 新增“股票池内选股”

新增参数：
- `pool_codes`：股票池列表（默认：`000300.SH / 000905.SH / 000852.SH`）
- `top_n`：每次调仓选股数量（默认 50，作为 alias 保留，指代池内选股数量）
- `pool_top_n`：股票池内每次调仓选股数量（默认 50）
- `market_top_n`：全市场每次调仓选股数量（保持现有语义，默认值沿用你当前全市场回测的设置）

行为：
- 最终同时跑 4 个 universe：
  - `ALL`：全市场（保持现有回测逻辑）
  - `000300.SH`：沪深300池内
  - `000905.SH`：中证500池内
  - `000852.SH`：中证1000池内
- 对每个 `pool_code`，在该 pool 的股票集合内做因子排序选股，回测流程与当前一致。
- 产出需包含“股票池”维度，并可以后续扩展更多 pool。

### 1.2 增加 3 个指数基准（与股票池对应）

默认口径（建议）：
- 全市场：使用现有 `cfg.benchmark_code`（你现在已有的基准测试逻辑）
- 股票池：对每个 pool 的回测结果，使用 **同名指数**作为 benchmark（即 `benchmark_code = pool_code`）

### 1.3 报表结构变更：增加“股票池”列

你希望最前面新增一列 `股票池`，例如：
- 核心指标汇总：`股票池 | 时期 | 策略 | ...`
- 总体表现 / 年度表现：同样在最前面新增 `股票池`

### 1.4 修正 `excess_nav` 定义

你希望“超额曲线/超额净值”用差值口径表达（首日=0），并且图表可直接画差值序列：
- `excess_nav_diff = strategy_nav - benchmark_nav`（首日=0，允许负值）
- 同时保留 `excess_nav` 的“净值形态”输出用于兼容（当前代码实际是 `excess_nav = 1 + diff`，首日=1）

---

## 2. 三个必须补齐/修正点（不处理会踩坑）

> 这 3 点必须先补齐，否则会出现：**pool 过滤全空 / 产物互相覆盖 / excess 指标口径与图表对不上**。

### 2.1 不保证：`stock_code` 口径不一致（不处理=pool 过滤全空），需要考虑这个情况

现状（两边口径不同）：
- 因子回测侧：`df_factor.stock_code` 有可能是带交易所后缀的代码，例如 `000001.SZ/600000.SH`（来自因子/信号侧的格式化逻辑）。
- 股票池表：`ai_is.stk_pool_of_index.stock_code` 是写入时 **去后缀并 `zfill(6)`** 的 6 位纯数字，例如 `000001/600000`（见 `src/tasks/index_stk_pool_task.py` 的写入逻辑）。

因此如果直接做：`df_factor.merge(pool_members, on=["trade_date","stock_code"])` 有可能会全空。 需要先检查 df_factor.stock_code是否和`ai_is.stk_pool_of_index.stock_code`一样 **去后缀并 `zfill(6)`**的6位数据的格式，就是哪怕不一致，要处理成一直，做兜底。


df_factor.stock_code 可能来自不同来源，不保证是否带后缀。由于 ai_is.stk_pool_of_index.stock_code 已统一为“去后缀+6位”，回测侧必须统一生成 stock_code_key/trade_date_key 做 join（幂等兜底，避免 merge 结果静默为空）。

也可以加一句 1 行自检（写在实现清单里）
df_factor["stock_code"].astype(str).str.contains(r"\.").any()


true 就说明带后缀，false 基本就是 6 位。这样你心里更踏实。


### 2.2 产物覆盖的坑：只用 `strategy_name` 当 key（不处理=互相覆盖）

你当前 Pipeline 合约里大量结构是：
- `backtest_results: Dict[strategy_name, StrategyBacktestResult]`
- `benchmark_results: Dict[strategy_name, BenchmarkNavResult]`
- `manifest.nav_csv_files[strategy_name] = path`（以及 plots/signals 也类似）

引入 4 个 universe 后，`expression_1` 会出现 4 份：
- 如果 key 不升级成带 pool 维度（例如嵌套 dict 或复合 key），**后写的一份会覆盖前面的**（CSV/PNG/manifest/结果表均存在同样风险）。

必须改造为“pool 维度优先”的结构（推荐嵌套 dict，改动更系统也更直观）：
- `Dict[pool_code, Dict[strategy_name, ...]]`
- 导出目录也按 pool 分子目录，避免文件名撞车
- manifest 同样按 pool 维度登记，避免覆盖

### 2.3 `excess_nav` 口径的坑：当前已是 diff，但 baseline=1（要与图表/验收对齐）

当前代码（`backtest/backtest_result_pipeline/benchmark/aligner.py`）口径是：
- `excess_nav = 1.0 + (strategy_nav - benchmark_nav)`（差值口径，但整体上移 1，首日=1）

而图表侧（`plot_report.py`）也有基于 “baseline=1” 的兼容逻辑。

你现在更想要的是“差值本身（首日=0）”用于绘图，所以推荐落地方案（最少破坏兼容）：
- 保留 `excess_nav = 1 + diff`（兼容已有产物/旧逻辑，也可作为“净值形态”使用）
- 新增 `excess_nav_diff = diff = strategy_nav - benchmark_nav`（首日=0）
- 图表侧优先绘制 `excess_nav_diff`，baseline 固定 0；若旧产物没有该列，则回退使用 `excess_nav` 的旧逻辑

验收一眼对账（典型首日）：
- `strategy_nav == 1`
- `benchmark_nav == 1`
- `excess_nav == 1`
- `excess_nav_diff == 0`

---

## 3. 数据来源与落地依赖（StockPool 表）

股票池数据源来自第一阶段已完成的表：
- `ai_is.stk_pool_of_index`（字段：`trade_date, pool_code, stock_code, signal, insert_time`）

回测侧使用方式（建议）：
- 在回测执行前，按 `pool_code + [start_date, end_date]` 拉取该 pool 的 `(trade_date, stock_code)` 集合；
- 将 `df_factor` 过滤到该集合后再进入回测引擎。

这样可确保：
- DataManager 获取行情时只拉该 pool 相关股票（性能更好）
- PortfolioConstructor 只在 pool 内排序选股（满足需求）

---

## 4. 改造方案总览（推荐实现路线）

核心设计：在 **同一次 Pipeline run** 中，按“universe（全市场/股票池）”批量执行 `step_backtest/step_benchmark/step_aggregate/step_export`，并合并输出。

### 4.1 配置层新增字段（建议）

在 `configs/backtest/model_backtest_config.py:ModelBacktestConfig` 增加：
- `pool_codes: list[str] = ["000300.SH","000905.SH","000852.SH"]`
- `pool_table: str = "ai_is.stk_pool_of_index"`
- `pool_signal_value: int = 1`（可选，若未来支持 signal!=1）
- `pool_top_n: int = 50`（池内选股数量）
- `market_top_n: int = <沿用你现有全市场设置>`（全市场选股数量；与池内参数不冲突）

CLI 参数（`backtest_model.py`）新增：
- `--pool-codes 000300.SH 000905.SH 000852.SH`（nargs="+", 默认同上）
- `--top-n 50`（alias，等同 `--pool-top-n`）
- `--pool-top-n 50`
- `--market-top-n <N>`（可选；若不传则沿用你现有全市场设置）

兼容性处理（避免歧义）：
- 保留历史的 `--max_stocks`（全市场口径），在帮助文本中明确它等同 `--market-top-n`。
- `--pool-top-n` 仅用于股票池内选股；与 `--market-top-n/--max_stocks` 不冲突。

### 4.2 Pipeline 数据合约扩展（dataclass）

建议新增一个维度字段 `pool_code`（其中全市场使用 `ALL`）：
- `StrategyBacktestResult`：新增 `pool_code: str`
- `BenchmarkNavResult`：新增 `pool_code: str`
- `OverallMetrics/YearlyMetrics`：新增 `pool_code: str`（用于落表）

字典结构建议：
- 当前：`Dict[strategy_name, StrategyBacktestResult]`
- 改为：`Dict[pool_code, Dict[strategy_name, StrategyBacktestResult]]`

### 4.3 Step 级改造点

#### Step Backtest（核心改造）

位置：`backtest/backtest_result_pipeline/steps/step_backtest.py`

改造：
- 外层循环：先跑全市场（`pool_code="ALL"`），再跑 `cfg.pool_codes`
  - 全市场：直接使用原始 `df_factor`，选股数量使用 `market_top_n`
  - 股票池：先按 pool 过滤后的 `df_factor_pool`，选股数量使用 `pool_top_n`
- 针对单个 pool：
  1) 拉取 pool 成份（`trade_date, stock_code`）
  2) 生成 join key（见 2.1）：`stock_code_key + trade_date_key`
  3) `df_factor_pool = df_factor.merge(pool_members_keyed, on=["trade_date_key","stock_code_key"], how="inner")`
  4) 保留 `df_factor.stock_code` 原字段（带后缀）继续喂回测引擎
  5) 使用同一个 `BacktestRunner`（或每 pool 一个 runner）跑所有策略

输出：
- `results[pool_code][strategy_name] = StrategyBacktestResult(pool_code=pool_code, ...)`

性能建议：
- pool 成份可做缓存（按 pool_code + date range），避免重复 SQL。
- 对 `df_factor` 提前确保 `trade_date` 与 pool 表的日期口径一致（Datetime/日期字段统一）。

#### Step Benchmark（基准对齐）

位置：`backtest/backtest_result_pipeline/steps/step_benchmark.py`

改造：
- 由单 benchmark → “按 pool 选择 benchmark”
- 默认：
  - `pool_code="ALL"`：使用 `cfg.benchmark_code`
  - 其他：`benchmark_code = pool_code`

产物：
- `BenchmarkNavResult(pool_code=pool_code, benchmark_code=<resolved>, ...)`

#### Step Aggregate（表格汇总）

位置：`backtest/backtest_result_pipeline/steps/step_aggregate.py`

改造：
- 遍历所有 pool + strategy 的结果，生成 overall/yearly/summary
- `summary_rows`/`overall_list`/`yearly_list` 均新增 `pool_code`
- `summary_df` 的列顺序调整为：`股票池, 时期, 策略, ...`
- 同步 `backtest/backtest_result_pipeline/report/excel_report.py`：列定义与格式化新增“股票池”列

#### Step Export（按 pool 输出 NAV/图表）

位置：`backtest/backtest_result_pipeline/steps/step_export.py`

改造：
- 输出目录按 pool 分子目录（`ALL` + 每个 pool），例如：
- 输出目录按 pool 分子目录（`ALL` + 每个 pool），例如：
  - `data/nav/<pool_code>/nav_with_benchmark_<benchmark>_<strategy>.csv`
  - `plots/<pool_code>/nav_with_benchmark_<benchmark>_<strategy>.png`
- Excel 输出到 `tables/`，但内容包含所有 pool
- manifest 的登记也必须包含 pool 维度（否则覆盖），推荐：`nav_csv_files[pool_code][strategy_name] = path`

---

## 5. `excess_nav` 修正方案（保留 1+diff + 新增 diff + 图表优先 diff）

位置：`backtest/backtest_result_pipeline/benchmark/aligner.py:calculate_nav_and_returns`

建议改造：
- 新增 `excess_nav_diff`（差值本身，首日=0）：  
  - `excess_nav_diff = strategy_nav - benchmark_nav`
- 保留 `excess_nav` 为“净值形态”（首日=1），用于兼容旧产物：  
  - `excess_nav = 1.0 + excess_nav_diff`

对应改动：
- `types.py` 注释更新（说明 `excess_nav` 的含义）
- `plot_report.py`：超额面板**优先绘制** `excess_nav_diff`（baseline 固定 0，允许负值）；若旧产物没有该列，则回退使用 `excess_nav` 的旧逻辑
- （如图表有面板标题/label）建议将超额面板的 label 明确为 `Excess NAV (Diff)`

验收标准：
- `nav_with_benchmark_*.csv` 中新增列 `excess_nav_diff`
- `excess_nav_diff == strategy_nav - benchmark_nav`（允许浮点误差）
- `excess_nav == 1 + excess_nav_diff`

---

## 6. 输出结果（你关心的三类表）如何加“股票池”

### 6.1 核心指标汇总（summary）

目标列：`股票池 | 时期 | 策略 | 总收益率 | 基准收益率 | 超额收益率 | 夏普比率 | 最大回撤 | Calmar比率 | 胜率 | IC均值 | IC胜率`

### 6.2 总体表现（overall）

目标列：`股票池 | 策略名称 | ...`

### 6.3 年度表现（yearly）

目标列：`股票池 | 年份 | 策略名称 | ...`

---

## 7. 最小验证清单（建议你本地验收）

1) 参数生效：
- `--pool-codes` 不同取值会生成对应 pool 的 nav/图表子目录
- `--pool-top-n` 变化会影响池内持仓数量；`--market-top-n` 影响全市场持仓数量（trade_log 里可观测）

2) 对账回归（功能正确性）：
- 选择一个策略、一个 pool、一个短区间（最近 30 交易日），手动检查：
  - 当日入选股票均属于该 pool（随机抽样 5-10 只核验）

3) NAV 字段口径：
- 抽样验证：`excess_nav_diff == strategy_nav - benchmark_nav`（允许浮点误差）
- 抽样验证：`excess_nav == 1 + excess_nav_diff`

---

## 8. 运行结束后的产物结构（你希望明确看到的目录/文件）

> 下述为 **一次 `backtest_model.py` 运行**后，建议的最终产物布局（同时包含：全市场 + 3 个股票池）。  
> 目录根为：`<model_path>/bt_results/<run_id>/`（由 `create_run_context()` 创建）。

### 8.1 run_dir 目录树（建议）

```
<model_path>/bt_results/<run_id>/
  config/
    run_config.json
  data/
    factors/
      model_facor_2021.csv
      ...
    nav/
      ALL/
        nav_with_benchmark_<BENCH_ALL>_expression_1.csv
        ...
      000300.SH/
        nav_with_benchmark_000300SH_expression_1.csv
        ...
      000905.SH/
        nav_with_benchmark_000905SH_expression_1.csv
        ...
      000852.SH/
        nav_with_benchmark_000852SH_expression_1.csv
        ...
    signals/                 # enable_detailed_log=True 时
      ALL/
        signals_expression_1.csv
        ...
      000300.SH/
        signals_expression_1.csv
        ...
      ...
  tables/
    模型回测结果_<run_id>.xlsx   # 含“股票池”列（summary/overall/yearly）
  plots/
    ALL/
      nav_with_benchmark_<BENCH_ALL>_expression_1.png
      ...
    000300.SH/
      nav_with_benchmark_000300SH_expression_1.png
      ...
    ...
  logs/
    detailed_trading_log.csv
  manifest.json
```

说明：
- `<BENCH_ALL>` 为全市场回测的基准代码（来自 `cfg.benchmark_code`，例如 `000852SH`）。

### 8.2 关键 CSV 字段（nav_with_benchmark）

建议 nav CSV 至少包含：
- `trade_date`
- `strategy_nav`
- `benchmark_nav`
- `excess_nav`（净值形态：`1 + (strategy_nav - benchmark_nav)`）
- `excess_nav_diff`（差值：`strategy_nav - benchmark_nav`）
- `strategy_ret`
- `benchmark_ret`
- （可选）`active_ret`（差值：`strategy_ret - benchmark_ret`，可由前两列计算）

---

## 9. 实施顺序（建议拆 2 个小 PR）

PR1（数据与口径）：
- pool 过滤 join key 统一（去后缀 + zfill(6)，trade_date 统一到 datetime）
- 结果/manifest key 升级为 pool 维度（防覆盖）
- `excess_nav_diff` 落盘 + 图表优先绘制 diff（兼容旧结果）
- 多 pool 聚合输出（summary/overall/yearly 增加“股票池”列）
- nav/plots 分 pool 输出
- run_id / naming 规范化

PR2（增强项，择期）：
- pool 成份 SQL 缓存/性能优化（必要时按日期分批）
- benchmark 映射表/自定义 pool 支持（如未来扩展到自定义股票池）
- 更细粒度的详细交易日志（按 pool 分目录，或日志里加 `pool_code` 列）
