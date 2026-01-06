# 多策略标签生成系统

## 概述

新的多策略标签生成系统支持同时使用多种标签生成策略，提供了更灵活和可扩展的标签生成能力。系统支持以下策略：

1. **`top_correlation`** - 基于相关性的标签调整策略
2. **`rank`** - 基于排序+Z-score标准化的策略  
3. **`raw`** - 原始未来收益率策略

## 主要特性

### 1. 支持单策略和多策略执行
- **单策略**: `strategy="top_correlation"`
- **多策略**: `strategy=["top_correlation", "rank"]`

### 2. 可插拔的策略架构
- 通过策略注册表实现可扩展性
- 新策略可以轻松添加到系统中
- 每个策略独立执行，互不干扰

### 3. 统一的数据格式
- 所有策略生成的标签都使用相同的长格式DataFrame
- 包含字段：`trade_date`, `stock_code`, `field_name`, `value`, `label_shift`
- 多策略结果会自动合并到同一张表

## 使用方法

### 1. 基本配置

```python
from src.tasks.label_generation_task import LabelGenerationConfig, LabelGenerationTask
from src.data_service.data_loading.market_data import MarketDataProvider

# 单策略配置
config_single = LabelGenerationConfig(
    strategy="rank",  # 使用rank策略
    label_shift=10,
    save_intermediate=True
)

# 多策略配置  
config_multi = LabelGenerationConfig(
    strategy=["top_correlation", "rank"],  # 同时使用两种策略
    label_shift=10,
    corr_window=240,
    corr_rank_num=30,
    save_intermediate=True
)
```

### 2. 执行标签生成

```python
# 初始化
market_data_provider = MarketDataProvider()
task = LabelGenerationTask(market_data_provider, config=config_multi)

# 执行
result_df = task.execute(
    start_date="2023-01-01",
    end_date="2023-01-31"
)
```

### 3. 在数据管道中使用

```python
# 在 run_daily_data_pipeline.py 中配置
LABEL_GENERATION_CONFIG = {
    "strategies": ["top_correlation", "rank"],  # 多策略
    "label_shift": 20,
    "corr_window": 240,
    # ... 其他参数
}

# 调用
success = scheduler.data_manager.run_label_generation(
    strategies=LABEL_GENERATION_CONFIG["strategies"],
    **other_params
)
```

## 策略详解

### 1. Top Correlation 策略 (`top_correlation`)

基于股票间相关性的标签调整策略：

- **输入**: 原始未来收益率
- **处理**: 找到相关性最高的邻居股票，用邻居标签的统计量调整目标股票标签
- **输出**: `label_raw` + `tc_t{shift}_n{rank_num}_adj`
- **参数**: `corr_window`, `corr_rank_num`, `min_rank_num`, `correlation_type`

### 2. Rank 策略 (`rank`)

基于排序和Z-score标准化的策略：

- **输入**: 原始未来收益率  
- **处理**: 
  1. 当日横截面排序 → 百分位数
  2. 对百分位数做Z-score标准化
- **输出**: `label_raw` + `rank_zscore_d1`
- **参数**: `ascending` (是否升序排列)

### 3. Raw 策略 (`raw`)

原始未来收益率策略：

- **输入**: 价格数据
- **处理**: 计算未来N日收益率
- **输出**: `label_raw`
- **参数**: `label_shift`

## 表命名规则

系统会根据策略自动生成表名：

- **单策略**: `training_label_ls{shift}_adj_{strategy_suffix}`
  - top_correlation: `training_label_ls10_adj_topcor_cr30_cw240`
  - rank: `training_label_ls10_adj_rank_zscore_d1`
  - raw: `training_label_ls10_adj_raw`

- **多策略**: `training_label_ls{shift}_adj_multi_{sorted_strategies}`
  - 例如: `training_label_ls10_adj_multi_rk_tc30`

## 扩展新策略

### 1. 创建策略调整器

```python
class MyCustomLabelAdjuster(LabelAdjuster):
    def adjust(self, label_raw_df: pd.DataFrame, **kwargs) -> pd.DataFrame:
        # 实现自定义调整逻辑
        pass
    
    def generate_labels(self, start_date: str, end_date: str, **kwargs) -> pd.DataFrame:
        # 实现完整的标签生成流程
        pass
```

### 2. 注册策略

```python
# 在 LabelGenerator 中添加私有方法
def _generate_my_custom_labels(self, **kwargs) -> pd.DataFrame:
    adjuster = MyCustomLabelAdjuster(market_data_provider=self.market_data_provider)
    return adjuster.generate_labels(**kwargs)

# 在 __init__ 中注册
self._strategy_registry["my_custom"] = self._generate_my_custom_labels
```

### 3. 更新配置验证

```python
# 在 LabelGenerationConfig.__post_init__ 中添加
supported_strategies = {"top_correlation", "raw", "rank", "my_custom"}
```

## 测试

使用提供的测试脚本验证功能：

```bash
# 测试单策略
python test_multi_strategy_labels.py --strategy single --test-strategy rank

# 测试多策略
python test_multi_strategy_labels.py --strategy multi --test-strategies top_correlation rank

# 自定义日期范围
python test_multi_strategy_labels.py --strategy multi --start-date 2023-01-01 --end-date 2023-01-10
```

## 注意事项

1. **数据一致性**: 多策略执行时，所有策略使用相同的日期范围和基础数据
2. **性能考虑**: 多策略会增加计算时间，建议根据实际需求选择策略组合
3. **存储空间**: 多策略会生成更多字段，注意数据库存储空间
4. **参数传递**: 不同策略可能需要不同参数，通过`adjuster_params`传递策略特定参数

## 配置示例

```python
# 完整配置示例
config = LabelGenerationConfig(
    strategy=["top_correlation", "rank"],
    label_shift=20,
    
    # Top correlation 策略参数
    corr_window=240,
    corr_rank_num=30,
    min_rank_num=20,
    correlation_type="pearson",
    
    # 通用参数
    use_db_pct_change=False,
    overlap_days=20,
    
    # Rank 策略特定参数
    adjuster_params={
        "ascending": False,  # rank策略使用
        "correlation_type": "pearson"  # 会自动传递给所有策略
    },
    
    # 存储参数
    save_intermediate=True,
    batch_size=10000
)
``` 