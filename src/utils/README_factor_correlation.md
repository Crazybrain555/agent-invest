# 因子相关性分析工具使用说明

## 概述

`FactorCorrelationAnalyzer` 是一个专门用于分析因子库中各个因子之间相关性的工具。它可以从数据库中读取长表格式的因子数据，转换为宽表格式，并计算因子间的相关性矩阵。

## 功能特性

- **多种相关性类型**: 支持 Pearson 和 Spearman 相关性计算
- **多计算方法**: 支持截面相关性和时间序列相关性
- **滚动相关性**: 支持计算滚动窗口相关性分析
- **高相关性分析**: 自动识别高相关性因子对
- **可视化**: 提供相关性热力图和分布图
- **结果导出**: 支持结果导出为CSV和图表文件
- **GPU加速**: 可选择使用GPU加速大规模计算

## 快速开始

### 1. 基本使用

```python
from src.utils.factor_correlation_analyzer import FactorCorrelationAnalyzer

# 创建分析器
analyzer = FactorCorrelationAnalyzer(
    table_name="ai_is.inter_train_factors_mkt_norm_academic_dcount1",
    use_gpu=False  # 设置为True以启用GPU加速
)

# 加载数据
raw_data = analyzer.load_factor_data(
    start_date="20231201",
    end_date="20231231",
    lag=0
)

# 准备因子矩阵
factor_matrix = analyzer.prepare_factor_matrix()

# 计算相关性
corr_matrix = analyzer.calculate_factor_correlation(
    correlation_type="pearson",
    method="cross_sectional"
)

# 分析高相关性因子对
high_corr_pairs = analyzer.analyze_high_correlations(
    corr_matrix, 
    threshold=0.7
)

# 导出结果
analyzer.export_results()
```

### 2. 运行测试脚本

```bash
# 完整测试
python test_factor_correlation.py

# 快速测试
python test_factor_correlation.py --quick
```

## 详细API说明

### 初始化参数

```python
FactorCorrelationAnalyzer(
    table_name: str = "ai_is.inter_train_factors_mkt_norm_academic_dcount1",
    use_gpu: bool = False,
    device: str = 'cuda'
)
```

- `table_name`: 因子数据表名
- `use_gpu`: 是否使用GPU加速（需要安装PyTorch）
- `device`: 计算设备，'cuda' 或 'cpu'

### 主要方法

#### 1. 加载因子数据

```python
load_factor_data(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stock_codes: Optional[List[str]] = None,
    factor_names: Optional[List[str]] = None,
    lag: int = 0
) -> pd.DataFrame
```

#### 2. 准备因子矩阵

```python
prepare_factor_matrix() -> pd.DataFrame
```

将长表格式转换为宽表格式，便于相关性计算。

#### 3. 计算因子相关性

```python
calculate_factor_correlation(
    correlation_type: str = "pearson",
    min_periods: int = 30,
    method: str = "cross_sectional"
) -> pd.DataFrame
```

- `correlation_type`: "pearson" 或 "spearman"
- `method`: "cross_sectional" (截面相关性) 或 "time_series" (时间序列相关性)
- `min_periods`: 最小有效观测数

#### 4. 计算滚动相关性

```python
calculate_rolling_correlation(
    window: int = 60,
    correlation_type: str = "pearson",
    min_periods: int = 30,
    target_dates: Optional[List[str]] = None
) -> Dict[pd.Timestamp, pd.DataFrame]
```

#### 5. 分析高相关性

```python
analyze_high_correlations(
    correlation_matrix: pd.DataFrame,
    threshold: float = 0.7,
    exclude_self: bool = True
) -> pd.DataFrame
```

#### 6. 可视化

```python
# 相关性热力图
plot_correlation_heatmap(
    correlation_matrix: pd.DataFrame,
    title: str = "Factor Correlation Matrix",
    figsize: Tuple[int, int] = (12, 10),
    save_path: Optional[str] = None
)

# 相关性分布直方图
plot_correlation_distribution(
    correlation_matrix: pd.DataFrame,
    title: str = "Factor Correlation Distribution",
    figsize: Tuple[int, int] = (10, 6),
    save_path: Optional[str] = None
)
```

## 数据表结构要求

工具支持以下格式的长表数据：

```
trade_date | stock_code | factor_name | factor_value | lag | z_windows
2021-03-12 | 300116     | dividend_yield_12m | -0.971510 | 0 | 1
2021-03-12 | 300117     | dividend_yield_12m | -0.091143 | 0 | 1
...
```

必需字段：
- `trade_date`: 交易日期
- `stock_code`: 股票代码
- `factor_name`: 因子名称
- `factor_value`: 因子值

可选字段：
- `lag`: 滞后期
- `z_windows`: 标准化窗口

## 相关性计算方法

### 1. 截面相关性 (Cross-sectional)
在每个时间点，计算不同因子在股票截面上的相关性，然后对时间维度取平均。

**适用场景**: 分析因子在同一时间点上的共同表现

### 2. 时间序列相关性 (Time-series)
对每只股票，计算不同因子的时间序列相关性，然后对股票维度取平均。

**适用场景**: 分析因子的时间序列行为相似性

## 输出文件说明

运行分析后，会在 `analysis_output/` 目录下生成以下文件：

- `correlation_matrix_*.csv`: 相关性矩阵
- `data_summary_*.txt`: 数据概要统计
- `factor_correlation_heatmap.png`: 相关性热力图
- `correlation_distribution.png`: 相关性分布直方图

## 性能优化建议

1. **数据量控制**: 对于大规模数据，建议分批处理或使用日期/股票过滤
2. **GPU加速**: 对于大规模计算，启用GPU可显著提升性能
3. **内存管理**: 滚动相关性计算可能消耗大量内存，注意监控内存使用

## 常见问题

### Q: 如何处理缺失值？
A: 工具会自动处理缺失值，通过 `min_periods` 参数控制计算相关性所需的最小有效观测数。

### Q: 如何选择相关性计算方法？
A: 
- 截面相关性：关注因子在同一时间点的共同表现
- 时间序列相关性：关注因子的时间趋势相似性
- 通常推荐使用截面相关性分析因子库

### Q: 热力图显示不完整怎么办？
A: 如果因子数量过多，工具会自动只显示前10个因子的热力图。可以通过筛选因子来控制显示数量。

### Q: 如何加速大规模计算？
A: 
1. 启用GPU加速 (`use_gpu=True`)
2. 减少时间窗口或股票数量
3. 使用较大的 `min_periods` 值
4. 考虑使用滚动相关性的 `target_dates` 参数只计算关键日期

## 依赖库

- pandas
- numpy
- matplotlib
- seaborn
- torch (可选，用于GPU加速)
- tqdm (进度条显示)

## 注意事项

1. 确保数据库连接正常
2. 表配置信息需要在 `configs/db/local_db_configs.yaml` 中正确配置
3. 大规模数据计算时注意内存使用情况
4. GPU加速需要安装PyTorch和CUDA环境 