# 本地测试数据库 (TDSQL - ai_is Schema) 数据字典

## 1. 概述

本文档描述了本地测试数据库 `ai_is` Schema 中用于支持 AI 模型训练和回测的关键数据表的结构和含义。这些表是根据项目数据处理流程（详见 `src/data_service/DATA_README.md`）生成，并遵循 `docs/database_specification.md` 中定义的基础规范。

---

## 2. 数据表详情

### 2.1 标准化参数表 (`inter_train_factors_std_l30_d1_2002_2012`)

**用途**: 存储基于 2002-01-01 至 2012-12-31 期间的归一化市场数据 (`intermediate_training_factors_market_normalize_lag30_countday1`) 计算得出的特征标准化参数。这些参数包括每个特征在该时间段内的均值（mean）、标准差（std）以及基于中位数绝对偏差（MAD）计算的上下限（lower, upper）。此表用于后续对新的归一化数据进行标准化（Standardization）处理，是数据处理流程 **Step 2** 的产物。

**表结构**:

| 字段名         | 数据类型      | 中文含义             | 说明                                                                 |
|----------------|---------------|----------------------|----------------------------------------------------------------------|
| `feature_name` | VARCHAR(100)  | 特征名称             | 参与计算的原始归一化特征字段名 (例如: `adj_close_lag_5`, `volume_lag_10`) |
| `mean`         | DECIMAL(20, 8)| 均值                 | 该特征在 2002-2012 样本期间计算得到的均值                            |
| `std`          | DECIMAL(20, 8)| 标准差               | 该特征在 2002-2012 样本期间计算得到的标准差                          |
| `lower`        | DECIMAL(20, 8)| 标准化下限 (MAD)     | 基于 MAD 计算的标准化稳健下边界 (通常为 median - n * MAD)            |
| `upper`        | DECIMAL(20, 8)| 标准化上限 (MAD)     | 基于 MAD 计算的标准化稳健上边界 (通常为 median + n * MAD)            |

**索引**:
- 建议在 `feature_name` 上创建唯一索引。

**说明**:
- 表名解析:
    - `inter_train_factors_std`: 表示用于训练的中间因子标准化参数。
    - `l30`: 基于滞后 30 期 (`lag=30`) 的特征计算。
    - `d1`: 基于日频 (`days_count=1`) 数据计算。
    - `2002_2012`: 参数是使用 2002 年至 2012 年的数据计算得出的。
- 此表存储的是每个特征在该历史时间段计算出的 *单一* 组统计参数，用于后续时间段数据的标准化。

### 2.2 市场数据归一化表 (`intermediate_training_factors_market_normalize_lag30_countday1`)

**用途**: 存储经过滞后处理和归一化（通常是 lag_n / lag_0）的 A 股市场日行情数据。此表是模型训练特征工程的基础，提供了包含价格、成交量、成交额、换手率等指标及其历史滞后值的宽表数据。这是数据处理流程 **Step 1** 的产物。

**表结构**:

| 字段名         | 数据类型      | 中文含义             | 说明                                                                                                   |
|----------------|---------------|----------------------|--------------------------------------------------------------------------------------------------------|
| `trade_date`   | TIMESTAMP     | 交易日期             | 数据对应的交易日期                                                                                       |
| `stock_code`   | VARCHAR(20)   | 股票代码             | 标准 6 位 A 股代码                                                                                       |
| `adj_open_lag_N`| DECIMAL(20, 8)| 滞后N日归一化开盘价  | N 范围从 0 到 29。`adj_open_lag_0` 代表当日归一化开盘价。                                                   |
| `adj_high_lag_N`| DECIMAL(20, 8)| 滞后N日归一化最高价  | N 范围从 0 到 29。                                                                                       |
| `adj_low_lag_N` | DECIMAL(20, 8)| 滞后N日归一化最低价  | N 范围从 0 到 29。                                                                                       |
| `adj_close_lag_N`| DECIMAL(20, 8)| 滞后N日归一化收盘价  | N 范围从 0 到 29。这是常用的归一化基准。                                                                 |
| `vwap_lag_N`    | DECIMAL(20, 8)| 滞后N日归一化VWAP    | N 范围从 0 到 29, 成交量加权平均价。                                                                     |
| `volume_lag_N`  | DECIMAL(20, 8)| 滞后N日归一化成交量  | N 范围从 0 到 29。                                                                                       |
| `amount_lag_N`  | DECIMAL(20, 8)| 滞后N日归一化成交额  | N 范围从 0 到 29。                                                                                       |
| `turnover_rate_lag_N` | DECIMAL(20, 8) | 滞后N日归一化换手率 | N 范围从 0 到 29。                                                                                       |
| `model_version`| VARCHAR(50)   | 模型/流程版本        | 产生此数据的归一化处理流程的版本标识，便于追溯。                                                              |
| `insert_time`  | TIMESTAMP     | 数据入库时间         | 记录插入或更新到数据库的时间戳。                                                                           |
| `is_temporary` | BOOLEAN       | 是否临时数据标识     | 指示该数据是否为中间步骤产生的临时数据 (通常为 False，表示为持久化结果)。                                      |

**索引**:
- 主键/唯一索引: (`trade_date`, `stock_code`)
- 可选索引: `stock_code`, `trade_date`

**说明**:
- 表名解析:
    - `intermediate_training_factors`: 表示用于训练的中间因子/特征。
    - `market_normalize`: 表示市场数据经过了归一化处理。
    - `lag30`: 包含了最多 30 期的滞后特征 (`lag=30`)。
    - `countday1`: 数据是日频的 (`days_count=1`)。
- "归一化" 通常指将滞后 N 期的值除以滞后 0 期（当日）的值 (即 `feature_lag_N / feature_lag_0`)，以消除股价绝对水平的影响。
- 此表采用宽表结构，字段数量较多。

### 2.3 禁投池表 (`restricted_stock_pool`)

**用途**: 存储每日更新的股票禁投池信息。模型在生成交易信号、进行回测或构建投资组合时，应查询此表，排除被标记为禁投的股票。数据通常来源于外部系统或文件（例如，NAS 共享目录）。

**表结构**:

| 字段名         | 数据类型          | 中文含义       | 说明                                                                       |
|----------------|-------------------|----------------|----------------------------------------------------------------------------|
| `trade_date`   | TIMESTAMP / DATE  | 交易日期       | 该禁投状态生效的具体日期。                                                   |
| `stock_code`   | VARCHAR(20)       | 股票代码       | 被限制投资的 A 股代码。                                                      |
| `signal`       | BOOLEAN / SMALLINT| 禁投标志       | 通常为 1 或 TRUE 表示该股票在该 `trade_date` 被禁止投资。若无记录或为 0/FALSE，则表示允许。 |
| `insert_time`  | TIMESTAMP         | 数据入库时间   | 记录该条禁投信息插入或更新到数据库的时间戳。                                   |

**索引**:
- 主键/唯一索引: (`trade_date`, `stock_code`)

**说明**:
- 此表的数据通常通过每日定时任务从外部数据源同步（例如，读取 `forbid.YYYYMMDD.csv` 文件）。
- 数据更新策略通常采用 `UPSERT` (INSERT ON CONFLICT UPDATE)，确保每个交易日每只股票只有一条最新的禁投状态记录。

### 2.4 训练标签表 (`training_label_ls10_adj_topcor_cr30_cw240`)

**用途**: 存储用于监督学习模型训练的目标变量（Label）。此表包含了根据特定算法（Top-Correlation 调整）计算和处理后的未来收益率标签。这是数据处理流程 **Step 4** 的产物。

**表结构**:

| 字段名         | 数据类型      | 中文含义       | 说明                                                                                                     |
|----------------|---------------|----------------|----------------------------------------------------------------------------------------------------------|
| `trade_date`   | TIMESTAMP     | 交易日期       | 标签所对应的 T 日期（即基于 T 日信息预测未来收益）。                                                        |
| `stock_code`   | VARCHAR(20)   | 股票代码       | 标准 6 位 A 股代码。                                                                                       |
| `field_name`   | VARCHAR(50)   | 标签字段名称   | 用于区分不同类型或处理阶段的标签。常见值包括 `label_raw` (原始未来 N 日收益率) 和 `tc_t10_n30_adj` (经过特定方法调整后的标签)。 |
| `value`        | DECIMAL(20, 8)| 标签数值       | 实际计算得到的标签值。                                                                                     |
| `label_shift`  | INTEGER       | 标签预测期     | 表示该标签是预测未来多少个交易日的收益。根据表名，此表固定为 10。                                             |
| `model_version`| VARCHAR(50)   | 模型/流程版本  | 生成此标签数据的算法或流程的版本标识。                                                                       |
| `insert_time`  | TIMESTAMP     | 数据入库时间   | 记录插入或更新到数据库的时间戳。                                                                           |
| `is_temporary` | BOOLEAN       | 是否临时数据标识 | 指示该数据是否为中间步骤产生的临时数据 (通常为 False)。                                                       |

**索引**:
- 主键/唯一索引: (`trade_date`, `stock_code`, `field_name`, `label_shift`)
- 可选索引: (`field_name`, `trade_date`), (`stock_code`, `trade_date`)

**说明**:
- 表名详细编码了标签的生成参数:
    - `training_label`: 表明是训练用标签。
    - `ls10`: `label_shift` = 10 天，即预测未来 10 日的收益。
    - `adj_topcor`: `adjustment_method` = Top Correlation，使用相关性最高的邻居进行调整。
    - `cr30`: `corr_rank_num` = 30，选取相关性排名前 30 的股票作为邻居。
    - `cw240`: `corr_window` = 240，计算历史相关性时回看 240 个交易日。
- 采用长表（Tidy Format）结构存储，便于存储不同类型（`field_name`）的标签。
- `label_raw` 通常指 `future_return = close[T+1+shift] / close[T+1] - 1`。
- `tc_t10_n30_adj` 通常指 `(label_raw - mean(neighbor_raw_labels)) / std(neighbor_raw_labels)`。

---
