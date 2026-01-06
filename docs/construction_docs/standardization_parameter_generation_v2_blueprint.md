# 标准化参数生成 V2 蓝图

## 概述
将标准化参数生成从宽表（`intermediate_training_factors_market_normalize_lag30_countday1`）迁移到长表（`inter_train_factors_mkt_processed_v1`）处理模式。

## 数据结构变化

### 原数据结构（宽表）
```
trade_date | stock_code | adj_close_lag_0 | adj_close_lag_1 | volume_lag_0 | ...
2005-01-04 | 000001     | -0.0106790847   | -0.0092447242   | 1000000      | ...
```

### 新数据结构（长表）
```
trade_date | stock_code | factor_name     | factor_value    | z_windows
2005-01-04 | 000001     | adj_close_mar   | -0.0106790847   | 1
2005-01-05 | 000001     | adj_close_mar   | -0.0092447242   | 1
```

## 输出格式变化

### 原输出格式
```
feature_name | upper | lower | mean | std
adj_close_lag_0 | 1.5 | -1.5 | 0.1 | 0.8
```

### 新输出格式
```
feature_name | window | upper | lower | mean | std
adj_close_mar | 1 | 1.5 | -1.5 | 0.1 | 0.8
```

## 核心逻辑变化

### 1. 数据获取方式
- **原方式**: 从宽表按列名获取特征数据
- **新方式**: 从长表按 `(factor_name, z_windows)` 组合获取数据

### 2. 特征识别方式
- **原方式**: 通过表结构的列名识别特征
- **新方式**: 通过 `DISTINCT factor_name, z_windows` 查询识别特征组合

### 3. 数据过滤条件
- **原方式**: 基于时间范围过滤行
- **新方式**: 基于时间范围 + 特征组合过滤

## 伪代码逻辑

```python
def extract_standard_params_from_long_table(
    start_date: str,
    end_date: str,
    table_name: str = "inter_train_factors_mkt_processed_v1",
    mad_multiplier: float = 7.0,
    min_samples: int = 1000
) -> pd.DataFrame:
    """
    从长表提取标准化参数
    """
    
    # 1. 获取所有特征组合
    feature_combinations = get_feature_combinations(table_name, start_date, end_date)
    # SQL: SELECT DISTINCT factor_name, z_windows FROM table 
    #      WHERE trade_date BETWEEN start_date AND end_date
    
    # 2. 初始化结果DataFrame
    result = pd.DataFrame(columns=['feature_name', 'window', 'upper', 'lower', 'mean', 'std'])
    
    # 3. 对每个特征组合计算统计参数
    for factor_name, z_window in feature_combinations:
        
        # 3.1 获取该组合的所有数据
        feature_data = get_feature_data(
            table_name, start_date, end_date, factor_name, z_window
        )
        # SQL: SELECT factor_value FROM table 
        #      WHERE trade_date BETWEEN start_date AND end_date
        #      AND factor_name = ? AND z_windows = ?
        
        # 3.2 检查数据量是否足够
        if len(feature_data) < min_samples:
            logger.warning(f"组合 ({factor_name}, {z_window}) 数据量不足: {len(feature_data)} < {min_samples}")
            result.loc[len(result)] = {
                'feature_name': factor_name,
                'window': z_window,
                'upper': np.nan,
                'lower': np.nan,
                'mean': np.nan,
                'std': np.nan
            }
            continue
        
        # 3.3 计算统计参数
        stats = calculate_mad_statistics(feature_data, mad_multiplier)
        # - 计算中位数和MAD
        # - 计算上下界 (median ± mad_multiplier * MAD)
        # - 基于裁剪后的数据计算均值和标准差
        
        # 3.4 存储结果
        result.loc[len(result)] = {
            'feature_name': factor_name,
            'window': z_window,
            'upper': stats['upper'],
            'lower': stats['lower'],
            'mean': stats['mean'],
            'std': stats['std']
        }
    
    return result

def calculate_mad_statistics(data: pd.Series, mad_multiplier: float) -> dict:
    """计算基于MAD的统计参数"""
    # 1. 计算中位数和MAD
    median_val = data.median()
    mad_val = (data - median_val).abs().median()
    
    # 2. 计算上下界
    upper_bound = median_val + mad_multiplier * mad_val
    lower_bound = median_val - mad_multiplier * mad_val
    
    # 3. 裁剪异常值
    clipped_data = data.clip(lower=lower_bound, upper=upper_bound)
    
    # 4. 计算均值和标准差
    mean_val = clipped_data.mean()
    std_val = clipped_data.std()
    
    return {
        'upper': upper_bound,
        'lower': lower_bound,
        'mean': mean_val,
        'std': std_val
    }
```

## 批处理优化策略

### 1. 内存优化
- 按特征组合分批处理，避免一次性加载所有数据
- 使用数据库查询直接过滤，减少内存占用

### 2. 性能优化
- 使用索引优化查询性能（trade_date, factor_name, z_windows）
- 批量处理相同window的特征，减少数据库查询次数

### 3. 并行处理
- 考虑对不同特征组合并行计算统计参数
- 使用连接池管理数据库连接

## 代码结构调整

### 1. 修改文件
- `src/tasks/standardization_parameter_generation.py` - 主要逻辑修改
- `src/data_service/preprocessing/methods/standardizer.py` - 添加长表支持
- `src/scheduler/Dfzq_gru_scheduler.py` - 更新调用参数

### 2. 新增方法
- `_get_feature_combinations()` - 获取特征组合
- `_fetch_feature_data()` - 获取特征数据
- `_extract_standard_params_long_table()` - 长表参数提取

### 3. 参数调整
- 移除 `lag` 和 `days_count` 参数
- 添加 `min_samples` 参数（默认1000）
- 更新表名为 `inter_train_factors_mkt_processed_v1`

## 错误处理和日志

### 1. 数据验证
- 检查表是否存在
- 验证时间范围内是否有数据
- 检查特征组合的数据量

### 2. 异常处理
- 数据库连接异常
- 数据格式异常
- 计算异常（如标准差为0）

### 3. 日志记录
- 记录处理的特征组合数量
- 记录数据量不足的特征组合
- 记录处理时间和性能指标

## 测试计划

### 1. 单元测试
- 测试特征组合获取
- 测试统计参数计算
- 测试边界情况（数据量不足等）

### 2. 集成测试
- 测试完整的标准化参数生成流程
- 测试数据库保存功能
- 测试性能基准

### 3. 数据验证
- 对比新旧方法的结果一致性
- 验证生成的参数表结构正确性
- 验证统计参数的合理性

## 配置更新

### 1. 配置文件调整
```python
# 新的配置参数
STANDARD_PARAMS_CONFIG = {
    "source_table": "inter_train_factors_mkt_processed_v1",  # 新的源表
    "min_samples": 1000,                                     # 最小样本数
    "start_date": "2002-01-01",                             # 开始日期
    "end_date": "2012-12-31",                               # 结束日期
    "mad_multiplier": 7.0,                                  # MAD倍数
    "batch_size": 50,                                       # 批处理大小
    "save_format": "database",                              # 保存格式
    "skip_if_exists": True                                  # 跳过已存在
}
```

### 2. 数据库表结构
```sql
-- 新的标准化参数表结构
CREATE TABLE inter_train_factors_std_v2 (
    feature_name VARCHAR(100) NOT NULL,
    window INTEGER NOT NULL,
    upper DOUBLE PRECISION,
    lower DOUBLE PRECISION,
    mean DOUBLE PRECISION,
    std DOUBLE PRECISION,
    sample_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (feature_name, window)
);
```

## 实施步骤

1. **第一阶段**: 创建新的长表处理方法
2. **第二阶段**: 修改现有代码调用新方法
3. **第三阶段**: 测试和验证
4. **第四阶段**: 部署和监控
5. **第五阶段**: 清理旧代码和配置

## 风险和注意事项

### 1. 性能风险
- 长表查询可能比宽表慢，需要优化索引
- 大量特征组合可能导致处理时间过长

### 2. 数据质量风险
- 需要确保长表数据的完整性和准确性
- 需要处理缺失值和异常值

### 3. 兼容性风险
- 需要确保下游系统能够处理新的参数表格式
- 需要提供向后兼容性或迁移方案 