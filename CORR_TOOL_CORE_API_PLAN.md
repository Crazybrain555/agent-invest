# CORR Tool Core API Refactor Plan（Implementation-ready Draft）

目标：在不破坏现有 CLI 的前提下，把 corr tool 升级为“可 import 的核心库（Core API）+ CLI 只是 wrapper”。
核心诉求是：**直接传 DataFrame**（无需落库/注册表）和 **直接传 factors/tables 列表**，支持 one-to-one / one-to-many / many-to-many，并对 one-to-many 做 O(N) 计算优化。

> 本文是方案稿（计划），按你的要求：**先写方案，不改代码**。后续你确认后再实现。

---

## 0. 已确认决策（必须遵守，避免返工）

- **Core vs CLI**：Core API 只负责“解析/取数编排/计算/返回 CorrResult”，不写盘、不写 cache；CLI/runner 作为 wrapper 才做 IO（parquet/excel/recommendation/cache）。
- **strict_name 默认 True**：Core API 默认 `strict_name=True`；`field`（不带表名）解析必须走 `configs/field_mappings/factor_mapping.yaml`（复用 `src/tools/corr/sources.py` 的 index + resolver），不做 DB 扫表/猜测。
- **user DataFrame 强制 long**：只接受 long schema（`trade_date/stock_code/value`，可选 `factor_id`）；格式不对直接 `ValueError` 并给出可复制示例。
- **stock_code 必须一致**：user df 必须自动 remove suffix（如 `.SZ/.SH`），保证与 DB 输出格式一致（复用 `output_format.remove_all_suffix` 规则）。
- **candidates 必须显式提供**：one-to-many/many-to-many 至少提供 `tables/groups/factors` 之一；否则 `ValueError`（Core API 默认不允许“空输入=全量”）。
- **forbid_pool + stock sampling 一致性**：在 `target_df` 场景必须保证 user 与 DB 在同一股票样本空间上计算；建议把 forbid/sampling 外提到 Core Engine 统一处理，并在 one-to-many 下以 target 有值股票为 sampling base。
- **time_series 统计语义**：统一输出 `n_groups + group_unit`（cross_sectional -> trade_date；time_series -> stock_code），避免把 `n_stocks` 误命名为 `n_dates`。
- **cache 安全**：当 `target_df`/user factors 参与时默认禁用 cache；若要缓存必须引入 `user_data_signature` 并加入签名 payload（在 runner/app 层实现）。

## 1. 现状盘点（基于当前代码）

当前 `src/tools/corr/` 已经做了文件拆分（`runner.py/loading.py/recommend.py/...`），但依然是“CLI 应用优先”的结构：

- `src/tools/corr/cli.py`：argparse 入口
- `src/tools/corr/runner.py`：主编排（强依赖 DB provider、写 output/caches）
- `src/tools/corr/loading.py`：仅支持 DB 拉取（`LocalTestDBDataProvider.fetch_data`）
- `src/tools/corr/compute.py`：`compute_corr_stats` 适配 many-to-many（全量两两）

缺口（对应你的需求）：

1) **没有稳定的可 import Core API**（你希望在 backtest_model.py/Notebook 里直接调用，不走 CLI、不落盘）。
2) **不支持 target_df 直接参与 one-to-many**（现在必须入库/注册表，和你的“工程化 + 可复用”诉求冲突）。
3) **factors/tables 列表的解析逻辑是 CLI 语境**（`--tables` 目前更多是 include 过滤；你要的是 API 里显式传 `tables=[...]` 并全量展开 + 去重/告警策略）。
4) **one-to-many 仍可能走 O(N²)**（当候选集合大时，`compute_corr_stats` 会算候选之间的相关性，非必要）。

---

## 2. 设计原则（你提的点 + 我补充的约束）

### 2.1 两层结构（Core vs App）

- **Core API 层**：纯计算/纯返回结果，不写盘，不写 cache，不依赖 argparse；可直接接 DataFrame。
- **App 层（runner/CLI）**：把 args/config -> request，调用 Core API，然后按需写 parquet/excel/recommendation/cache。

### 2.2 输入统一：FactorFrame（long schema）

Core API 的数据接口统一到 long schema（最稳）：

- `trade_date`（datetime64[ns]）
- `stock_code`（str）
- `factor_id`（str，DB 因子用 `ai_is.xxx::field`；用户因子用 `user::xxx`）
- `value`（float32/float64）

约定（已确认）：
- **user DataFrame 必须是 long**（最稳、最可控、也最容易给出明确报错）。
- 如果格式不满足要求：直接 `ValueError`，并在错误信息里明确提示“需要哪些列/示例长表长什么样”。
- **user DataFrame 的重复键策略（必补工程细节）**：
  - 重复键定义：`(trade_date, stock_code)`（单因子 target_df 场景）/ 或 `(trade_date, stock_code, factor_id)`（多因子 long 场景）
  - Core API 默认 `dup_policy="error"`：发现重复键直接报错（避免 pivot `aggfunc="first"` 静默吞数据）
  - 可选：`dup_policy="last" | "mean"` 由调用方显式开启
  - 备注：如果你确信上游数据质量有保证（不会出现重复键），保持 `error` 反而更安全（尽早暴露问题）
- **stock_code 标准化必须与 DB 输出一致（已确认）**：
  - Core API 默认执行 “remove suffix”（例如 `.SZ/.SH` 等）
  - 规则来源：复用 `configs/db/table_config.yaml` 的 `output_format.remove_all_suffix`，确保与 `LocalTestDBDataProvider` 一致
  - 允许注入自定义 normalizer（高级用法），但默认必须一致

### 2.3 Factor selection 的优先级/去重/告警（按你的偏好）

当同时传了 `tables=[...]` 和 `factors=[...]`：

- `tables`：normalize -> `ai_is.xxx`，并 expand 出该表所有 factors（来自 mapping）
- `factors`：支持 `table::field` 或 `field`
  - 如果同时传了 `tables`，则 **field 的解析只在这些 tables 内发生**（“以表里面的为准”）
  - `field` 的解析来源固定为 `configs/field_mappings/factor_mapping.yaml`（复用现有 `src/tools/corr/sources.py` 的
    `build_factor_index` + `resolve_factor_specs`），不做 DB 扫表/猜测
  - 如果解析出来的 `table::field` 已被 tables expand 覆盖：记录 warning 并去重（保留一份即可）
- strict_name：
  - **Core API 默认 `strict_name=True`**（更安全，避免 silent 多表扩展）
  - `strict_name=True`（默认）：`field` 在 scope 内命中多表 -> 抛错；建议配合 `tables=[...]` 限定 scope
  - `strict_name=False`：允许 `field` 命中多表；需要在 `warnings` 里提示实际展开到哪些表，并去重

---

## 3. 目标 API（你能在任意 Python 文件里直接用）

建议新增一个稳定入口：`src/tools/corr/api.py`（或 `src/tools/corr/core/` 包），对外只暴露 1 个类 + 1 个结果对象：

### 3.1 CorrResult（返回 DataFrame，不落盘）

```python
@dataclass
class CorrResult:
    mode: str
    summary: dict
    warnings: list[str]
    missing: dict                 # 缺失项（例如 missing_tables/missing_factors/missing_specs），便于调用方决定是否 raise

    corr_table: pd.DataFrame        # pair/one_to_many 主输出
    corr_matrix: pd.DataFrame       # many_to_many（可选）
    high_corr_pairs: pd.DataFrame
    recommendation: dict            # many_to_many 才有意义（当前是 cluster 代表选择）
```

说明：
- `corr_table/corr_matrix/high_corr_pairs/recommendation` 都是 **Core API 的输出（返回值）**；是否写 parquet/excel 由 runner/app 层决定。
- `summary/warnings/missing` 用于让调用方（Notebook/backtest/CLI）可控地处理“缺失/歧义/降级”等情况。
- `missing` 建议结构：
  - `{"missing_tables": [...], "missing_factors": [...], "missing_specs": [...]}`（调用方可决定是 raise 还是 log warning 继续跑）

### 3.2 CorrEngine（核心）

```python
class CorrEngine:
    def __init__(..., mapping: dict, provider: Optional[LocalTestDBDataProvider] = None, ...):
        ...

    def pair(
        self,
        *,
        factor_a: str,  # "ai_is.xxx::field"
        factor_b: str,  # "ai_is.yyy::field"
        sampling_cfg: Optional[dict] = None,
        compute_cfg: Optional[dict] = None,
        universe_cfg: Optional[dict] = None,
    ) -> CorrResult

    def one_to_many(
        *,
        target: Optional[str] = None,         # DB factor key: "ai_is.xxx::field"
        target_df: Optional[pd.DataFrame] = None,
        target_key: str = "user::target",
        factors: Optional[list[str]] = None,  # field 或 table::field
        tables: Optional[list[str]] = None,   # 表名或 ai_is.表名
        groups: Optional[list[str]] = None,   # "growth.profitability"
        allow_all_if_empty: bool = False,     # 仅用于兼容 CLI 语义；Core 默认 False（空输入直接报错）
        strict_name: bool = True,
        dup_policy: str = "error",            # "error" | "last" | "mean"
        sampling_cfg: Optional[dict] = None,
        compute_cfg: Optional[dict] = None,
        universe_cfg: Optional[dict] = None,
    ) -> CorrResult

    def many_to_many(
        self,
        *,
        factors: Optional[list[str]] = None,  # field 或 table::field
        tables: Optional[list[str]] = None,   # 表名或 ai_is.表名
        groups: Optional[list[str]] = None,   # "growth.profitability"
        allow_all_if_empty: bool = False,     # 仅用于兼容 CLI 语义；Core 默认 False（空输入直接报错）
        strict_name: bool = True,
        sampling_cfg: Optional[dict] = None,
        compute_cfg: Optional[dict] = None,
        universe_cfg: Optional[dict] = None,
    ) -> CorrResult
```

说明：
- Core API 只返回结果，不生成 run_dir、不写 parquet/excel。
- `runner.py`（App）仍可用：把 CLI args 翻译成 `CorrEngine` 调用，再写输出。

---

## 4. DataFrame 直连（你最关心的场景 1）

### 4.1 规范化函数（Core 级别必需）

新增：`normalize_user_factor_df(df, factor_key=...) -> FactorFrame(long)`

支持：
- 你的 backtest CSV：`trade_date, stock_code, model_pred`
- 你也可以自己先 rename：`model_pred -> value`

输入要求（已确认，强制 long）：
- 必需列：`trade_date`, `stock_code`, `value`
- 可选列：不关心（会被丢弃）
- 如果缺列：直接报错，并给出可复制的示例（例如你 backtest 的 `model_pred` 应先 rename 成 `value`）

关键点：
- `trade_date` 转 datetime
- `stock_code` -> str，并 **自动 remove suffix**（已确认必须做，否则和 DB 对不上）
  - 实现上建议复用现有 `output_format.remove_all_suffix` 的 suffix 规则（`configs/db/table_config.yaml`），保证和
    `LocalTestDBDataProvider` 的输出一致
- `value` -> float32
- 填 `factor_id = target_key`

### 4.2 trade_dates 的来源策略（减少 DB IO）

one-to-many 若传了 `target_df`：

1) 默认 `trade_dates = sorted(target_df.trade_date.unique())`
2) 可选：在这些日期上再应用 sampling（random k per year），但不跨出 target_df 日期范围
3) 将 `trade_dates` 传给 DB loader（复用现有 chunk + pushdown），最大化减少 DB 拉取

### 4.3 forbid_pool / stock sampling 的一致性

这是“必坑点”，需要在方案里明确落地方式：当前 `loading.py/load_long_df` 已经在 loader 内部做了 forbid_pool 与 stock sampling。
如果直接在 Core API 里 concat user df，会出现：
- DB 部分已经被采样/过滤，但 user df 没有 → 样本不一致
- forbid_pool 可能只过滤了 DB 部分 → user df 未过滤

✅ 方案：**把 forbid_pool / stock sampling 从 loader 外提到 Core Engine**

- loader 增加开关（或提供 “raw loader”）：
  - `apply_forbid_pool: bool`
  - `random_stocks_per_date: Optional[int]`
  - Core API 场景（尤其 `target_df`）调用 raw loader：`apply_forbid_pool=False`、`random_stocks_per_date=None`
- Core Engine 负责统一处理：
  1) 统一拉 forbid_df（基于最终 `trade_dates_used`）
  2) concat `db_long_df + user_long_df`
  3) 对 concat 后的 long_df 统一 forbid filter
  4) 再统一 stock sampling（见下条“采样基准口径”）

备选方案（侵入更小，但实现更绕，不建议优先）：
- 保留 loader 内 forbid/sampling，但 loader 必须额外返回 `sampled_stock_map`（每个 trade_date 抽到的股票集合）与 forbid 过滤口径；
  Core Engine 再用相同集合去过滤 user df，才能保证一致性。

采样“从哪里抽股票”的口径（必补，避免有效样本被稀释）：
- **one-to-many + target_df**：按 target_df 每日“有值的股票集合”作为 sampling base，再用这批股票同时过滤候选因子
- **one-to-many + target 是 DB factor**：按 target factor 的可用股票集合为 sampling base（需要先取 target 的值或在 wide 后基于 non-null 选择）
- **many-to-many**：可按 union（DB long_df 中有值的股票集合）或按 universe_cfg 约束后的股票集合（如果未来引入统一 universe）

---

## 5. factors/tables 列表输入（你最关心的场景 2）

### 5.1 新增 selection 解析函数（Core 层）

建议新增 `resolve_selection(...) -> (table_factors, target_key, candidate_keys, warnings, ...)`：

输入：
- `tables: List[str]`：expand 全表 factors（来自 mapping）
- `groups: List[str]`：expand（复用 `build_table_factor_map`）
- `factors: List[str]`：支持 `table::field` 或 `field`

输出：
- `table_factors: Dict[table, List[field]]`（DB 取数用）
- `factor_keys: List[str]`（最终参与 corr 的 `table::field` 列表）
- `warnings: List[str]`（重复、缺失、歧义提示）

### 5.2 去重/告警规则（落到可实现的细节）

1) 先 expand `tables/groups` 得到 `expanded_set`（table::field）
2) 再解析 `factors`：
   - `table::field`：直接 normalize
   - `field`：按 scope（若 tables 非空则仅在 tables 内 resolve，否则用 factor_index）
3) 对每个解析出的 key：
   - 如果已在 `expanded_set`：warning + skip
   - 否则加入集合

补充（已确认）：
- Core API 的候选集合 **必须显式提供**：至少给 `tables/groups/factors` 之一。
  - 如果都为空：直接报错（`ValueError`），并提示应该如何填写（例如 `tables=[...]` 或 `groups=[...]` 或 `factors=[...]`）。
- （可选兼容）如果未来需要保留 CLI 的“空输入 -> 全量展开”行为：在 runner/app 层显式传 `allow_all_if_empty=True`；
  Core API 默认仍保持 `False`（安全默认，避免误跑全量）。

---

## 6. 计算层优化：pair / one-to-many 不走全量两两

### 6.1 新增 focused compute

在 `src/tools/corr/compute.py`（或新文件 `compute_targeted.py`）新增：

- `compute_corr_one_to_many(df_wide, target_col, candidate_cols, method, corr_type, min_periods)`
  - 输出 corr_table（target vs each candidate 的 mean/median/std/count）
  - 覆盖 `cross_sectional`（按 trade_date 分组）与 `time_series`（按 stock_code 分组）
  - 实现建议：优先使用向量化（`corrwith` 或 numpy 批量计算），避免 Python for-loop 逐列 corr（性能差）

实现参考（伪代码，cross_sectional 用 corrwith 避免 O(N²)）：

```python
def compute_corr_one_to_many_cs(df_wide, target_col, candidate_cols, corr_type, min_periods):
    per_date = []
    for date, g in df_wide.groupby("trade_date", sort=True):
        # 这里的 min_periods 需要按“pairwise 有效样本”口径处理（可先用粗过滤，最终再按 count 严格过滤）
        if len(g) < min_periods:
            continue
        target = g[target_col]
        candidates = g[candidate_cols]
        corrs = candidates.corrwith(target, method=corr_type)
        corrs.name = date
        per_date.append(corrs)

    if not per_date:
        return pd.DataFrame()

    stacked = pd.concat(per_date, axis=1).T  # index=trade_date, columns=candidates
    stats = stacked.agg(["mean", "median", "std", "count"]).T
    stats = stats.rename(columns={"count": "n_groups"}).assign(group_unit="trade_date")
    # min_periods：最终建议按 stacked.count() 再严格过滤（n_groups 或 pairwise valid count）
    return stats
```

pair：
- 直接复用 one_to_many（candidates=1）

### 6.2 many-to-many 仍用现有 compute_corr_stats

many-to-many 保持现状（全量两两），但 Core API 要允许用户显式选择：
- `many_to_many_full=True/False`
- 或当因子数很大时给 warning（目前 `max_factors_full_matrix` 只影响输出不影响计算，这是已知风险点）

time_series 语义坑（必补说明，避免把历史坑带到新 API）：
- 当前实现里 `method="time_series"` 时是按 `stock_code` 分组算 corr，count 的语义是 “n_stocks”，不是 “n_dates”
- Core API 建议统一输出为：
  - `n_groups`：分组数（cross_sectional -> n_trade_dates；time_series -> n_stocks）
  - `group_unit`: `"trade_date" | "stock_code"`
- CLI 如需兼容旧字段名，可在输出层 alias（例如继续输出 `n_dates` 但同时写入 `group_unit`）

---

## 7. App 层（runner/CLI）如何复用 Core（不推倒重写）

目标：CLI 行为不变，但内部不再“自己实现一遍流程”。

### 7.1 runner 只做 3 件事

1) args/config 合并（保留现有 `_apply_*`）
2) `engine = CorrEngine.from_config(cfg, mapping, provider=...)`
3) 调 `engine.pair/one_to_many/many_to_many` 得到 `CorrResult`，再：
   - 写 parquet/excel（复用 report/formats）
   - 写 recommendation.yaml（many-to-many）
   - 写 cache（可开关）

Core API 不应依赖 `Path/run_dir` 等 IO 概念。

Cache 策略（必补，避免 user 因子污染 cache）：
- `target_df` / user factors 参与时：runner/app 层默认关闭 cache（否则同名 `user::target` 可能在不同实验间互相污染）
- 如果强行要 cache：必须由调用方提供 `user_data_signature`（例如模型 run_id / 文件 hash），并加入 signature payload
  - 否则同名 `user::target` 会在不同实验之间互相污染
- runner/app 层建议加硬校验（伪代码）：

```python
if enable_cache and target_df is not None and not user_data_signature:
    raise ValueError("user_data_signature required when caching user DataFrame results")
```

---

## 8. Implementation Notes（落地注意事项）

- **API 形态**：避免“碎类/碎入口”，保持 `CorrEngine + CorrResult` 为唯一稳定入口，其他尽量是纯函数（selection/normalize/compute）。
- **性能实现**：one-to-many/pair 优先向量化（`corrwith` / numpy 批量公式），避免 Python for-loop 逐列 `Series.corr`。
- **一致性优先**：`target_df` 场景 forbid/sampling 必须在 concat 后统一做；one-to-many sampling base 以 target 有值股票为准。
- **语义修正**：time_series 下 count 是 `n_stocks`，建议 Core 输出 `n_groups + group_unit`，CLI 再决定是否 alias 成旧字段名。
- **安全默认**：`strict_name=True`、`dup_policy="error"`、空 candidates 直接报错；兼容模式（如 `allow_all_if_empty=True`）只能由 wrapper 显式开启。

---

## 9. 实施步骤（最小可用 → 工程化复用）

### Step 0（只加 API，不动 CLI 行为）

- 新增 `src/tools/corr/api.py`（CorrEngine/CorrResult）
- 新增 `normalize_user_factor_df`
- 新增 `resolve_selection`（支持 tables/groups/factors + warnings）
- one-to-many 先复用现有 wide+pivot 路径（保证正确性）

### Step 1（one-to-many focused compute）

- 新增 `compute_corr_one_to_many`
- runner/engine 在 one-to-many 模式下改用 focused compute

### Step 2（runner 复用 Core）

- runner 改为调用 CorrEngine，输出写盘逻辑保留
- CLI 参数保持不变

### Step 3（验证）

- 用现有 CLI 用例回归：
  - `./.venv_wsl/bin/python tools/corr_tool.py --mode many_to_many --group growth.profitability`
  - `./.venv_wsl/bin/python tools/corr_tool.py --mode one_to_many --target ai_is.quantitative_growth_profitability_signals::nde2p --group growth.profitability`
- 新增一个 Python 直调示例（不落盘）：
  - 读取 `model_factor_2020.csv...2025.csv` 合并 -> `engine.one_to_many(target_df=..., tables=[...])`
