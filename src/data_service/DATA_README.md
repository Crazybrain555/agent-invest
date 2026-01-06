# 数据模块总览（data_service）

## 🧠 核心目标

- 提取原始行情 / 财务 / 宏观等多源数据，支持字段级调用
- 构造 lag 特征，并进行归一化处理（lag_n / lag_0）
- 生成标准化参数（按日期，来自 2002-2012 的归一化数据）
- 应用标准化参数，得到最终训练数据
- 所有数据使用 **长表结构** 存储，便于维护与扩展

---

## 🔁 数据处理流程图（数据库流版本）

- （已完成）    Step 1️⃣ 原始行情数据 → 构造 lag → 归一化 → 存测试数据库：normalized_market_data                       
- （已完成）   Step 2️⃣ 拉取归一化数据（2002-2012） → 生成标准化参数 → 存测试数据库：standard_params 
- （暂时不做）   Step 3️⃣ 拉归一化数据（任意时间）+ 拉标准化参数 → 应用标准化 → 存测试数据库：train_ready_data (选做，暂时不做，因为计算量不大)
-   （已完成）  Step 4️⃣ 拉原始收盘价 → 构造未来收益率 → 使用历史收益计算相关性矩阵 → 提取Top相关股票收益均值 → 标准化为 label_adj → 存测试数据库：training_label

## 第四步思路和核心伪代码：
📌 Label 构造逻辑说明（长表结构）
本模块用于构造用于回归/排序监督学习的 label_adj，以股票未来收益为基础，结合历史收益相关性进行平滑标准化处理，最终输出为标准 长表结构（Tidy Format）。

💾 数据格式（long 表）
字段	类型	含义
stock_code	string	股票代码（6位）
trade_date	int	当前样本的交易日（如 20230101）
field_name	string	标签字段名称（如 label_raw、label_adj）
value	float	标签数值
label_shift	int	标签预测天数跨度（如10）
示例数据：


stock_code  trade_date   field_name   value     label_shift
000001      2023-01-03   label_raw    0.032     10
000001      2023-01-03   label_adj    0.67      10
000002      2023-01-03   label_raw   -0.014     10
000002      2023-01-03   label_adj   -0.28      10


⚙️ label_adj  标签生成流程
每条label_adj  标签数据的构建过程如下：

注意：我建议基础工具可以拆分到src\data_service\preprocessing\methods下面，写一到打两个工具类的py文件，类似src\data_service\preprocessing\methods
src\data_service\preprocessing\methods\__pycache__
src\data_service\preprocessing\methods\encoder.py
src\data_service\preprocessing\methods\missing_value.py
src\data_service\preprocessing\methods\normalizer.py
src\data_service\preprocessing\methods\outlier.py
src\data_service\preprocessing\methods\standardizer.py
src\data_service\DATA_README.md

，然后基础工具的类和方法可以整合到 src\data_service\data_engineering\labels_engineering.py里面做最终的处理

写完后 参考src\tasks\market_price_norm_data_initialization.py
src\tasks\standardization_parameter_generation.py  
写一个任务 然后 最后再测试  测试成功就组装到src\scheduler\Dfzq_gru_scheduler.py 里面


整体思路规程
1️⃣ 计算未来收益率 label_raw
使用 T+1 到 T+label_shift+1 的收盘价计算累计收益：

python
label_raw = adjclose[t+label_shift+1] / adjclose[t+1] - 1
2️⃣ 构造 Spearman 历史相关性矩阵
对每个交易日，向前回看 corr_window 天的日收益（ret）矩阵，计算股票间 Spearman 相关性。

3️⃣ 聚合相关邻居收益均值与标准差
获取相关性前 corr_rank_num 的股票

若有效邻居不足 min_rank_num，回退用全市场均值/标准差

python
label_adj = (label_raw - mean(neighbors)) / std(neighbors)
4️⃣ 存储为长表结构
使用 pd.melt 转换为长表，输出字段为 stock_code、trade_date、field_name、value、label_shift

🧪 示例调用伪代码
python

df_label = generate_label(
    date_from="2020-01-01",
    date_to="2023-12-31",
    label_shift=10,
    corr_window=240,
    corr_rank_num=30
)

print(df_label.head())
🧱 推荐输出格式转换代码（宽转长） 伪代码
python
def label_to_long(df_label_wide: pd.DataFrame, label_shift: int) -> pd.DataFrame:
    df_long = df_label_wide.reset_index().melt(
        id_vars=["id", "tdate"],
        value_vars=["label_raw", "label_adj"],
        var_name="field_name",
        value_name="value"
    )
    df_long.rename(columns={"id": "stock_code", "tdate": "trade_date"}, inplace=True)
    df_long["label_shift"] = label_shift
    return df_long[["stock_code", "trade_date", "field_name", "value", "label_shift"]]



### step4 开发蓝本

1. New Utility Module for Correlation Calculations
🆕 （完整替换原段落）

File: src/data_service/preprocessing/methods/correlation_utils.py
Purpose: 统一放置所有与收益率计算、相关系数与邻居筛选相关的可复用函数，保持主流程简洁。

Function	Signature & 说明
calculate_period_returns	python<br>def calculate_period_returns(price_df: pd.DataFrame,<br> period: int = 1,<br> method: Literal["adj_close", "pct_change_db"] = "adj_close") -> pd.DataFrame
• period>0 表示向前 period 日收益率，内部 price_df.pct_change(period)；
• 当 period==1 且 method=="pct_change_db" 时，优先尝试直接从数据库字段（如 S_DQ_PCTCHANGE）提取，减少计算量。
calculate_rolling_spearman_correlation	与上一版相同（滚动窗口 Spearman）。
find_correlated_neighbors	与上一版相同。

File: src/data_service/preprocessing/methods/future_returns_utils.py
Purpose: 提供未来收益率计算相关的功能。

Function	Signature & 说明
calculate_future_returns	python<br>def calculate_future_returns(price_df: pd.DataFrame,<br> shift: int) -> pd.DataFrame
计算公式：price[t+shift+1] / price[t+1] - 1；shift ≥ 1。

2. New Core Label Engineering Module
🆕 （部分修改，保留不变处）

File: src/data_service/data_engineering/labels_engineering.py
Purpose: Contains the main class responsible for orchestrating the label generation process, using the utility functions and data loading components.

Key Class: LabelGenerator

text
复制
编辑
__init__(self,
         market_data_provider: MarketDataProvider,
         adjuster: "BaseLabelAdjuster"):
    • market_data_provider 用于取原始行情
    • adjuster 实现不同的 label 调整策略（策略模式）
内部方法

_wide_to_long 已移至 dataframe_utils.py（接口保持不变，内部直接调用）。

不再包含 _calculate_future_returns；改为直接调用 future_returns_utils.calculate_future_returns。

_calculate_top_cor_adj_label 实现在 label_adjusters.py（见下）。

generate_labels(...) 更新要点

新增参数 adjuster_params: Dict[str, Any]，用于把窗口大小等超参数传给具体 adjuster。

其余流程（取数、计算 returns、rolling corr、future_returns、调整、wide→long）逻辑与上一版一致，仅调用名与文件位置改变。

3. label_adjusters.py 🆕（新增文件）
text
复制
编辑
class BaseLabelAdjuster(ABC):
    @abstractmethod
    def adjust(self,
               label_raw_df: pd.DataFrame,
               **kwargs) -> pd.DataFrame: ...
text
复制
编辑
class TopCorAdjLabelAdjuster(BaseLabelAdjuster):
    def __init__(self,
                 correlation_matrices: Dict[pd.Timestamp, pd.DataFrame],
                 rank_num: int,
                 min_rank_num: int):
        ...
    def adjust(...):
        # 原 _calculate_adjusted_label 逻辑，重命名后实现
4. New Task Script for Execution and Saving
（以下段落完全沿用原文）

File: src/tasks/label_generation_task.py
Purpose: A runnable script, similar to StandardParamsGenerator, that orchestrates the entire label generation and saving process.
Key Class: LabelGenerationTask
init(self, start_date: str, end_date: str, label_shift: int, corr_window: int, corr_rank_num: int, min_rank_num: int, save_format: str = 'database', table_name_prefix: str = 'training_label', skip_if_exists: bool = False): Store parameters. Define the target table name dynamically based on parameters (e.g., training_label_ls10_cw240_cr30).
execute(self):
Initialize MarketDataProvider, TestDBManager, and LabelGenerator.
Construct the target table name.
Check if save_format is 'database' and skip_if_exists is True. If so, check if the table exists using db_manager.check_table_exists() and return if it does.
Log the start of the process with parameters.
Call label_generator.generate_labels(...) with the provided parameters.
If a valid DataFrame is returned:
Define the database table schema (columns: stock_code, trade_date, field_name, value, label_shift, potentially index/primary key).
Use db_manager.save_dataframe() to save the long DataFrame. Set mode='replace' to overwrite if the table exists (or handle based on skip_if_exists logic).
Log success and the number of rows saved.
If label generation fails or returns an empty DataFrame, log an error.
Return True on success, False on failure.

5. Modifications to Existing Files
🆕 （在原基础上补充）

src/data_service/data_loading/market_data.py — 原要求保持。额外：暴露 fetch_data(..., pivot=True) 参数，直接返回 wide 格式。

src/data_service/preprocessing/methods/dataframe_utils.py — 新增，提供 wide_to_long 与可选 long_to_wide。

configs/field_mapping.yaml — 若未来要支持数据库涨跌幅字段，需要新增 pct_change → S_DQ_PCTCHANGE 的映射。

其余文件同上一版描述。

6. Future Integration
（以下段落完全沿用原文）

src/scheduler/Dfzq_gru_scheduler.py: Once the task is implemented and tested, add LabelGenerationTask to the appropriate place in the scheduler pipeline.
Flow Summary:
Scheduler (or manual run) triggers LabelGenerationTask.execute().
LabelGenerationTask initializes LabelGenerator and TestDBManager.
LabelGenerationTask calls label_generator.generate_labels().
LabelGenerator calls market_data_provider.fetch_data() to get wide-format adj_close.
LabelGenerator calls correlation_utils and future_returns_utils functions to calculate returns, rolling correlations, and future returns.
LabelGenerator calls internal methods (_calculate_future_returns, _calculate_adjusted_label) using the fetched data and correlation results.
LabelGenerator calls _wide_to_long to format the output.
LabelGenerator returns the long DataFrame to LabelGenerationTask.
LabelGenerationTask calls db_manager.save_dataframe() to store the result in the database.
This structure separates concerns (data fetching, core logic, utilities, task execution) and follows the pattern established in your existing code.

7. Additional Optimizations 🆕
内存与速度

rolling corr 可用 numba 并行或 pandas.DataFrame.rolling(...).corr(pairwise=True) 后再选行列，避免字典存储爆内存。

中间结果（returns、corr）可按月写 Feather/Parquet 做缓存，增量更新时先查缓存再补缺口。

增量调度

在 LabelGenerationTask 加 --mode append：仅对最近 N 个交易日重新计算，旧日期跳过，显著缩短每日调度耗时。

日志与监控

关键步骤（取数、计算、写库）写 timedelta 到日志，方便后期瓶颈排查。

异常行数（如邻居不足、std=0）计数并落表，便于质量监控。

数据库 Schema 预留

若未来引入多种 adj_method，表中加字段 adj_method varchar(32)，并与 (trade_date, stock_code, field_name, label_shift) 共同做复合主键。

src/
├── data_service/
│   ├── preprocessing/
│   │   └── methods/
│   │       ├── correlation_utils.py      # ① 计算收益、相关系数
│   │       ├── future_returns_utils.py   # ② 计算未来收益
│   │       └── dataframe_utils.py        # ③ wide↔long 公共函数
│   │
│   └── data_engineering/
│       ├── label_adjusters.py            # ④ 各种调整策略类
│       └── labels_engineering.py         # ⑤ LabelGenerator 主流程
├── tasks/
│   └── label_generation_task.py          # ⑥ 调度脚本
└── configs/
    └── field_mapping.yaml

## 参考的代码库（以前的代码，都很乱，你取其精华）

参考others/original_programes/code_pvnet_20241212ms/prepare_data.py项目的这个数据处理的代码，进行数据处理，你要基本复刻里面的思路，方法可以优化。
你先

读取wind和聚源数据 你参考others\original_programes\get_data_and_save的相关代码，你要注意我当时都是把数据存在本地，这个现在没必要，我这个项目尽量取数据库的数据，除非我有要求要新建一个数据库，存在测试数据库，你不要存数据在本地。
我给你介绍一下这些问间的文件目的：
Debts_related_signals_generator.py                 #取wind数据，都是和负债相关的一些数据
factor_creator.py                                  #这是我主要的取数代码，涉及到了主要存放的量价和基本面数据
FAMA_Factors_indicators.py                         # fama三因子数据，这个可以不看，以前的旧项目
                                                
get_basic_data.py                                  #启动 类似pipline
get_data_from_shared_disk.py                       #取量化投资部手写的600多个因子和获取量化的禁投池，从一个网盘里面取出来，然后存放在本地的
Get_data_from_winddatabase.py                      # 主要是wind数据的中信一级到三级的数据
get_fund_data.py                                   #基金数据

get_signals_from_disk.py                             #启动 类似pipline   

signal_creator.py                                 #启动 类似pipline   
Specific_signal_generator.py                      #我自己的自己的一些因子，你可以先不看
tools.py                                          # 我以前尝试写的tool后来放弃了 


---

## 你有可能用到的wind数据库
中国A股日行情 - AShareEODPrices
Create  unique  index Business_Primary_Key_Index ON AShareEODPrices (S_INFO_WINDCODE,TRADE_DT)


1	Wind代码	S_INFO_WINDCODE	VARCHAR2(40)
2	交易日期	TRADE_DT	VARCHAR2(8)
3	货币代码	CRNCY_CODE	VARCHAR2(10)
4	昨收盘价(元)	S_DQ_PRECLOSE	NUMBER(20,4)
5	开盘价(元)	S_DQ_OPEN	NUMBER(20,4)
6	最高价(元)	S_DQ_HIGH	NUMBER(20,4)
7	最低价(元)	S_DQ_LOW	NUMBER(20,4)
8	收盘价(元)	S_DQ_CLOSE	NUMBER(20,4)
9	涨跌(元)	S_DQ_CHANGE	NUMBER(20,4)
10	涨跌幅(%)	S_DQ_PCTCHANGE	NUMBER(20,4)
11	成交量(手)	S_DQ_VOLUME	NUMBER(20,4)
12	成交金额(千元)	S_DQ_AMOUNT	NUMBER(20,4)
13	复权昨收盘价(0元)	S_DQ_ADJPRECLOSE	NUMBER(20,4)
14	复权开盘价(0元)	S_DQ_ADJOPEN	NUMBER(20,4)
15	复权最高价(0元)	S_DQ_ADJHIGH	NUMBER(20,4)
16	复权最低价(0元)	S_DQ_ADJLOW	NUMBER(20,4)
17	复权收盘价(0元)	S_DQ_ADJCLOSE	NUMBER(20,4)
18	复权因子(0)	S_DQ_ADJFACTOR	NUMBER(20,6)
19	均价(VWAP)	S_DQ_AVGPRICE	NUMBER(20,4)
20	交易状态	S_DQ_TRADESTATUS	VARCHAR2(10)
21	交易状态代码	S_DQ_TRADESTATUSCODE	NUMBER(5,0)
22	涨停价(元)	S_DQ_LIMIT	NUMBER(20,4)
23	跌停价(元)	S_DQ_STOPPING	NUMBER(20,4)
24	前复权收盘价(元)	S_DQ_ADJCLOSE_BACKWARD	NUMBER(20,4)
25	证券ID	SEC_ID	VARCHAR2(10)

样例：
Wind代码S_INFO_WINDCODE	交易日期TRADE_DT	货币代码CRNCY_CODE	昨收盘价(元)S_DQ_PRECLOSE	开盘价(元)S_DQ_OPEN	最高价(元)S_DQ_HIGH	最低价(元)S_DQ_LOW	收盘价(元)S_DQ_CLOSE	涨跌(元)S_DQ_CHANGE	涨跌幅(%)S_DQ_PCTCHANGE	成交量(手)S_DQ_VOLUME	成交金额(千元)S_DQ_AMOUNT	复权昨收盘价S_DQ_ADJPRECLOSE	复权开盘价S_DQ_ADJOPEN	复权最高价S_DQ_ADJHIGH	复权最低价S_DQ_ADJLOW	复权收盘价S_DQ_ADJCLOSE	复权因子S_DQ_ADJFACTOR	均价(VWAP)S_DQ_AVGPRICE	交易状态S_DQ_TRADESTATUS	交易状态代码S_DQ_TRADESTATUSCODE	涨停价(元)S_DQ_LIMIT	跌停价(元)S_DQ_STOPPING	前复权收盘价(元)S_DQ_ADJCLOSE_BACKWARD	证券IDSEC_ID
600463.SH	20241017	CNY	11.5	11.57	12.34	11.01	12.06	0.56	4.8696	284799.86	330168.856	33.7449	33.9503	36.2098	32.3071	35.3882	2.934343	11.593	交易	-1	12.65	10.35	12.06	S10548



## 🧱 模块说明

### 1️⃣ data_loading（数据提取）
- `market_data.py`: 获取行情数据并生成 lag 字段
- `get_data.py`: **统一数据接口**
  - 示例：`get_data(fields, data_type, start_date, end_date, lag=120)`
  - 支持按字段名提取，不暴露底层表结构
  - 后续支持多数据源（如聚源、CSV、本地等）
   - 使用src/utils/db_connection.py 的连接方法
### 2️⃣ preprocessing（数据预处理）
- `methods/normalizer.py`: 归一化函数（lag_n / lag_0）
- `methods/standardizer.py`: 生成标准化参数、执行标准化
- `pipeline.py`: 主控调度器，负责拼装预处理流程
-  目前参考others/original_programes/code_pvnet_20241212ms/prepare_data.py的，因为优先按照这个项目跑通代码，构建框架

### 3️⃣ data_saving（数据存储）
- `data_to_testdb.py`: 封装数据库写入逻辑（支持 append / replace / upsert 等）
   - 尽量使用src/utils/db_connection.py 的连接方法
### 4️⃣ utils（通用工具）
- `db_connection.py`: 从测试数据库、wind、聚源（gogoal）中统一读取数据（标准化参数 / 归一化表）
   路径src/utils/db_connection.py 的连接方法
---

## 🧪 数据结构样例（所有数据为长表结构）

### `normalized_market_data`


=== 测试带滞后特征的市场数据获取 ===

宽表格式数据:
数据形状: (6, 10)

数据示例:
   stock_code trade_date  adj_close      volume  adj_close_lag_0  adj_close_lag_1  adj_close_lag_2  volume_lag_0  volume_lag_1  volume_lag_2
6      000001 2023-01-03    1568.90  2194127.94          1568.90          1499.40          1484.59    2194127.94     818035.98     666890.09
7      000001 2023-01-04    1631.57  2189682.53          1631.57          1568.90          1499.40    2189682.53    2194127.94     818035.98
8      000001 2023-01-05    1649.80  1665425.18          1649.80          1631.57          1568.90    1665425.18    2189682.53    2194127.94
15     000002 2023-01-03    3150.58   636399.63          3150.58          3145.40          3138.48     636399.63     514863.83     606868.83
16     000002 2023-01-04    3295.75  1087146.91          3295.75          3150.58          3145.40    1087146.91     636399.63     514863.83

滞后特征列表:
['adj_close_lag_0', 'adj_close_lag_1', 'adj_close_lag_2', 'volume_lag_0', 'volume_lag_1', 'volume_lag_2']
2025-04-01 08:43:51,862 - INFO - Standardizing stock codes using output_format.remove_all_suffix

长表格式数据:
数据形状: (144, 5)

数据示例:
    stock_code trade_date field_name   value  lag
48      000001 2023-01-03  adj_close  1568.9    0
49      000001 2023-01-03  adj_close  1568.9    0
50      000001 2023-01-03  adj_close  1568.9    0
51      000001 2023-01-03  adj_close  1568.9    0
120     000001 2023-01-03  adj_close  1499.4    1



### `standard_params`
| date       | field_name   | mean | std     | min  | max  |
|------------|--------------|------|---------|------|------|
| 2003-01-01 | adj_close    | 0.83 | 1.45    | 0.65 | 1.92 |

### `train_ready_data`
| code    | date       | field_name   | lag  | std_value  |
|---------|------------|--------------|------|------------|
| 000001  | 2023-01-01 | adj_close    | 1    | 0.89       |

---

## ✅ 数据读取模块设计建议
你可以在 src/data_service/data_loading/get_data.py 中，做这样一个统一入口：

```python

def get_data(fields: list, data_type: str, start_date: str, end_date: str, lag: int = None) -> pd.DataFrame:
    """
    统一数据接口：根据字段列表、数据类型、时间范围返回数据（长表结构）
    - data_type 决定用哪个具体子模块（比如 market、macro、finance）
    - fields 是你想取的数据字段名（如 adj_close、pe_ttm）
    - lag 是是否需要构造 lag 特征（如 0~120）
    """
    if data_type == "market":
        return get_market_data(fields, start_date, end_date, lag=lag,等等可能参数)
    elif data_type == "macro":
        return get_macro_data(fields, start_date, end_date,等等可能参数)
    # 可以加更多 data_type
    else:
    #这个地方可以扩展，没有data_type也可以自行尝试查询数据，但是会有warning警告，可能数据会不准
        return  get_from_all_datasource(fields, start_date, end_date,等等可能参数)       


```



## 🧩 字段映射与配置

字段建议维护在 `configs/field_mapping.yaml`，用于统一各数据源字段名：

```yaml
adj_close:
  wind_code: S_DQ_ADJPRICE
  jydb_code: LCPriceAdj
  description: 后复权收盘价
pe_ttm:
  wind_code: S_VAL_PE_TTM
  jydb_code: LCPETTM
  description: 市盈率TTM
```

## 🔧 调用示例伪代码

 
```python

# Step 1: 构造归一化数据
raw_df = get_data(fields=["adj_close", "pe_ttm"], data_type="market", start_date="2002-01-01", end_date="2024-12-31", lag=120)
normalized = normalize_lagged_data(raw_df)
save_data_to_testdb(normalized, table="normalized_market_data")

# Step 2: 生成标准化参数
norm_df = get_data_from_testdb(table="normalized_market_data", start_date="2002-01-01", end_date="2012-12-31")
params = generate_standardization_params(norm_df)
save_data_to_testdb(params, table="standard_params")

# Step 3: 生成训练数据
norm_df = get_data_from_testdb(table="normalized_market_data", start_date="2013-01-01", end_date="2024-12-31")
params = get_data_from_testdb(table="standard_params")
std_df = apply_standardization(norm_df, params)
save_data_to_testdb(std_df, table="train_ready_data")
```


📎 TODO & 扩展计划
 接入宏观数据模块

 接入异常值处理模块（如 winsor、z-score 剔除）

 将标准化参数按行业、因子类别做版本化

 支持本地 parquet 保存（与数据库互补）

 加入长表转宽表模块（必要时）

📌 建议文档存放位置
建议保存为：

📁 src/data_service/DATA_README.md ✅
📁 或 src/docs/data_module.md（如果你有统一文档目录）

主目录 README.md 建议添加链接指向：

数据模块说明详见：src/data_service/DATA_README.md

## 🔄 量化投资部数据获取流程

### 📝 数据规范 (Data Contract - Forbid Pool)

**目标**: 定义从 NAS 获取的量化投资部禁投池数据的规范，确保后续处理和入库的一致性。

**数据来源**: NAS 共享目录 (`\\space\forbid`)

**文件命名约定**:
-   格式: `forbid.YYYYMMDD.csv`
-   示例: `forbid.20250415.csv`
-   `YYYYMMDD` 部分将作为数据的 `trade_date`。

**文件内容格式**:
-   CSV 文件，无表头。
-   编码: 假设为 `utf-8` (可在 `nas_config.yaml` 中配置)。
-   列定义:
    1.  `stock_code` (string): 股票代码 (原始格式，加载后处理为6位数字符串)。
    2.  `signal` (integer): 禁投标志，通常为 1。

**数据库目标表**: `restricted_stock_pool`

**数据库字段定义**:
-   `trade_date` (Date, PK): 交易日期，来自文件名。
-   `stock_code` (String(15), PK): 股票代码，处理为6位数字符串。
-   `signal` (Boolean/SmallInt): 禁投标志 (例如，True 或 1 表示禁投)。
-   `insert_time` (DateTime): 数据插入或更新的时间戳。

**增量/更新策略**:
-   任务运行时，根据 `overlap_days` 配置，加载指定日期范围内的所有文件。
-   使用数据库的 `UPSERT` (INSERT ON CONFLICT UPDATE) 逻辑，基于 (`trade_date`, `stock_code`) 主键，实现数据的插入或更新。

### 实现计划

1. 配置文件 (`configs/nas_disk/nas_config.yaml`)
   - [x] 创建 NAS 连接配置（主机、用户名、密码、路径等）
   - [x] 配置数据源路径（禁投池、因子库等）
   - [x] 配置数据处理参数（overlap_days、batch_size 等）
   - [x] 配置数据库表名和字段映射

2. NAS 连接工具类 (`src/utils/nas_connection.py`)
   - [x] 实现 NAS 连接管理 (通过配置加载和 OS 访问)
   - [x] 实现文件列表获取 (`list_files`)
   - [x] 实现文件读取功能 (`read_file_to_buffer`)
   - [x] 添加错误处理和重试机制 (使用 `tenacity`)

3. 数据加载模块 (`src/data_service/data_loading/forbid_data.py`)
   - [x] 实现禁投池数据加载 (`ForbidDataLoader` 类)
   - [x] 实现数据格式转换 (文件名解析日期, stock_code 格式化, signal 类型转换)
   - [x] 实现数据验证和清洗 (基础的列数检查和类型转换)
   - [x] 添加日志记录

4. 数据库表模型 (`src/utils/table_schema.py`)
   - [x] 定义 RestrictedStockPool 表结构 (`create_forbid_table_schema`)
   - [x] 添加字段类型和约束 (Date, String(15), SmallInteger, DateTime)
   - [x] 实现表创建和更新逻辑 (将由 TestDBManager 使用此 schema)

5. 任务定义 (`src/tasks/nas_forbid_data_task.py`)
   - [x] 创建 NASForbidDataTask 类 (继承自 `BaseTask`)
   - [x] 实现数据获取和处理逻辑 (调用 Loader, 确定日期范围)
   - [x] 实现数据保存到数据库 (调用 `TestDBManager.save_dataframe` 实现 Upsert)
   - [x] 添加任务执行状态监控 (通过日志记录)

6. 调度器 (`src/scheduler/nas_get_data_Scheduler.py`)
   - [x] 创建 NASDataScheduler 类
   - [x] 实现定时任务调度 (使用 `schedule` 库)
   - [x] 添加任务执行日志
   - [x] 实现错误处理和通知 (基础日志记录)

7. 更新任务定义 (`src/scheduler/job_definitions.py`)
   - [N/A] 添加 NAS 数据获取任务配置 (使用独立调度器，无需修改)
   - [N/A] 设置执行时间和频率
   - [N/A] 配置任务参数



### 扩展计划
- [ ] 支持因子库数据获取
- [ ] 优化数据获取性能
- [ ] 添加数据质量监控
- [ ] 实现增量更新机制

📎 TODO & 扩展计划
 接入宏观数据模块

 接入异常值处理模块（如 winsor、z-score 剔除）

 将标准化参数按行业、因子类别做版本化

 支持本地 parquet 保存（与数据库互补）

 加入长表转宽表模块（必要时）

📌 建议文档存放位置
建议保存为：

📁 src/data_service/DATA_README.md ✅
📁 或 src/docs/data_module.md（如果你有统一文档目录）

主目录 README.md 建议添加链接指向：

数据模块说明详见：src/data_service/DATA_README.md