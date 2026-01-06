# 数据处理重构蓝图

## 1. 流程总览

```
┌─── 数据输入 ────────────────────────────────────────────────────┐
│ 输入表结构: trade_date | stock_code | field_name | value        │
│ 数据来源: MarketDataProvider.fetch_data()                      │
└────────────────────────────────────────────────────────────────┘
                                ↓
┌─── 字段分类与配置解析 ──────────────────────────────────────────┐
│ • 读取 Z_WINDOW_MAP_FACTOR_ENGINEERING 配置                   │
│ • 根据 FIELD_CATEGORY_MAP_FACTOR_ENGINEERING 进行字段分类      │
│ • 验证配置合理性（状态字段只能window=0等）                      │
└────────────────────────────────────────────────────────────────┘
                                ↓
┌─── 按批次处理 ──────────────────────────────────────────────────┐
│ for each date_batch in chunked_dates:                         │
│   ├── 提取该批次的原始数据                                       │
│   ├── 按股票代码分组，确保时序操作正确性                          │
│   ├── 根据字段类别调用相应的处理函数                              │
│   ├── 合并所有处理结果                                          │
│   └── 写入目标表                                               │
└────────────────────────────────────────────────────────────────┘
                                ↓
┌─── 输出结果 ────────────────────────────────────────────────────┐
│ 输出表结构: trade_date | stock_code | factor_name |            │
│            factor_value | z_windows                           │
│ 表名: inter_factors_processed (覆盖原表)                       │
└────────────────────────────────────────────────────────────────┘
```

## 2. 核心算法伪代码

### 2.1 主处理流程

```python
def process_factors_engineering(df_raw, field_window_config):
    """
    主处理函数 - 因子工程模式
    Args:
        df_raw: 原始数据 [trade_date, stock_code, field_name, value]
        field_window_config: 字段窗口配置映射
    Returns:
        processed_df: [trade_date, stock_code, factor_name, factor_value, z_windows]
    """
    results = []
    
    # 按字段分组处理
    for field_name in df_raw['field_name'].unique():
        field_data = df_raw[df_raw['field_name'] == field_name].copy()
        category = get_field_category_factor_engineering(field_name)
        windows = field_window_config.get(field_name, [0])
        
        for window in windows:
            if category == 'price':
                result = process_price_field(field_data, field_name, window)
            elif category in ['volume', 'value', 'forecast']:
                result = process_volume_like_field(field_data, field_name, window)
            elif category == 'ratio':
                result = process_ratio_field(field_data, field_name, window)
            elif category == 'status':
                result = process_status_field(field_data, field_name, window)
            elif category == 'technical':
                result = process_technical_field(field_data, field_name, window)
            else:
                logger.warning(f"Unknown category for field {field_name}")
                continue
                
            results.append(result)
    
    return pd.concat(results, ignore_index=True)
```

### 2.2 价格字段处理（修正版）

```python
def process_price_field(df, field_name, window):
    """
    价格字段处理逻辑
    特点：分母统一使用 adj_close，结果都要log1p
    """
    df = df.sort_values(['stock_code', 'trade_date'])
    results = []
    
    if window == 0:
        # window=0: log1p(price)
        df_out = df.copy()
        df_out['factor_value'] = np.log1p(df_out['value'])
        df_out['factor_name'] = field_name
        df_out['z_windows'] = 0
        results.append(df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
    
    else:
        # 需要获取adj_close数据作为分母
        adj_close_data = get_adj_close_for_batch(df)  # 辅助函数获取adj_close
        merged = df.merge(adj_close_data, on=['trade_date', 'stock_code'], how='left')
        
        # 计算移动平均分母 (shift 1避免前视偏差)
        merged['adj_close_ma'] = (merged.groupby('stock_code')['adj_close']
                                 .transform(lambda x: x.shift(1).rolling(window, min_periods=window).mean()))
        
        # 计算ROC分母
        merged['adj_close_roc'] = merged.groupby('stock_code')['adj_close'].shift(window)
        
        # 生成MAR因子: logp(adj_xxxx / adj_close_ma)
        df_mar = merged.copy()
        df_mar['factor_value'] = np.log(merged['value'] / merged['adj_close_ma'])
        df_mar['factor_name'] = f"{field_name}_mar"
        df_mar['z_windows'] = window
        results.append(df_mar[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        # 生成ROC因子: logp(adj_xxxx / adj_close_roc)
        df_roc = merged.copy()
        df_roc['factor_value'] = np.log(merged['value'] / merged['adj_close_roc'])
        df_roc['factor_name'] = f"{field_name}_roc" 
        df_roc['z_windows'] = window
        results.append(df_roc[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
    
    return pd.concat(results, ignore_index=True)
```

### 2.3 体量/资金流字段处理（修正版）

```python
def process_volume_like_field(df, field_name, window):
    """
    体量/资金流字段处理逻辑
    特点：分母使用自身的历史值，结果都要log1p或者log window=0 是用log1p
    """
    df = df.sort_values(['stock_code', 'trade_date'])
    results = []
    
    if window == 0:
        # window=0: log1p(volume)
        df_out = df.copy()
        df_out['factor_value'] = np.log1p(df_out['value'])
        df_out['factor_name'] = field_name
        df_out['z_windows'] = 0
        results.append(df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
    
    else:
        # 计算移动平均分母 (shift 1避免前视偏差)
        df['value_ma'] = (df.groupby('stock_code')['value']
                         .transform(lambda x: x.shift(1).rolling(window, min_periods=window).mean()))
        
        # 计算ROC分母
        df['value_roc'] = df.groupby('stock_code')['value'].shift(window)
        
        # 生成MAR因子: logp(value / value_ma)
        df_mar = df.copy()
        df_mar['factor_value'] = np.log(df_mar['value'] / df_mar['value_ma'])
        df_mar['factor_name'] = f"{field_name}_mar"
        df_mar['z_windows'] = window
        results.append(df_mar[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        # 生成ROC因子: logp(value / value_roc)
        df_roc = df.copy()
        df_roc['factor_value'] = np.log(df_roc['value'] / df_roc['value_roc'])
        df_roc['factor_name'] = f"{field_name}_roc"
        df_roc['z_windows'] = window
        results.append(df_roc[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
    
    return pd.concat(results, ignore_index=True)
```

### 2.4 比例字段处理（修正版）

```python
def process_ratio_field(df, field_name, window):
    """
    比例字段处理逻辑
    特点：window=0时保留原值，其他时候和体量字段一样处理
    """
    df = df.sort_values(['stock_code', 'trade_date'])
    results = []
    
    if window == 0:
        # window=0: 保留原值（不做log1p）
        df_out = df.copy()
        df_out['factor_value'] = df_out['value']  # 保留原值
        df_out['factor_name'] = field_name
        df_out['z_windows'] = 0
        results.append(df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
    
    else:
        # window>=1: 和体量字段一样的处理逻辑（包含logp）
        return process_volume_like_field(df, field_name, window)
    
    return pd.concat(results, ignore_index=True)
```

### 2.5 状态字段处理

```python
def process_status_field(df, field_name, window):
    """
    状态字段处理逻辑
    特点：只允许window=0，保留原值
    """
    if window != 0:
        raise ValueError(f"状态字段 {field_name} 只能设置 z_windows=0，当前值: {window}")
    
    df_out = df.copy()
    df_out['factor_value'] = df_out['value']  # 保留原值
    df_out['factor_name'] = field_name
    df_out['z_windows'] = 0
    
    return df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']]
```

## 3. 项目文件改动清单

### 3.1 `src/data_service/preprocessing/methods/norm_config.py` - 新增因子工程配置

```python
# ========= 因子工程字段分类系统 =========

# 字段类别枚举
class FieldCategoryFactorEng(Enum):
    PRICE = "price"           # 价格类：分母用adj_close，结果log
    VOLUME = "volume"         # 体量/资金流类：分母用自身，结果log
    RATIO = "ratio"           # 比例类：window=0保留原值，其他log
    VALUE = "value"           # 市值类：同体量处理
    FORECAST = "forecast"     # 预测类：同体量处理  
    STATUS = "status"         # 状态类：只允许window=0
    TECHNICAL = "technical"   # 技术指标类：优先window=0

# ========= 因子工程字段分类 =========
# ---- 价格类：分母统一用adj_close，结果都要log ----
PRICE_FIELDS_FACTOR_ENG = {
    'adj_close', 'adj_open', 'adj_high', 'adj_low', 'vwap'
}

# ---- 体量/资金流类：分母用自身，结果都要log ----
VOLUME_FIELDS_FACTOR_ENG = {
    'volume', 'amount', 'TRADES_COUNT',
    'large_net_inflow', 'inst_buy_value', 'inst_sell_value',
    'large_buy_value', 'large_sell_value', 'med_buy_value', 'med_sell_value',
    'small_buy_value', 'small_sell_value'
}

# ---- 比例类：window=0保留原值，其他和体量一样处理 ----
RATIO_FIELDS_FACTOR_ENG = {
    'turnover_rate', 'swing', 'pct_change',
    'initiative_buy_rate', 'initiative_sell_rate',
    'large_buy_rate', 'large_sell_rate',
    'net_inflow_rate', 'open_net_inflow_rate', 'close_net_inflow_rate',
    'moneyflow_pct'
}

# ---- 市值/估值类：同体量处理逻辑 ----
VALUE_FIELDS_FACTOR_ENG = {
    'float_market_cap', 'market_cap',
    'pe_ttm', 'pb_ratio', 'dividend_yield_12m'
}

# ---- 预测类：同体量处理逻辑 ----
FORECAST_FIELDS_FACTOR_ENG = {
    'consensus_np', 'consensus_np_growth_2y', 'consensus_forecast'
}

# ---- 状态类：只允许window=0 ----
STATUS_FIELDS_FACTOR_ENG = {
    'trade_status', 'up_down_limit_status'
}

# ---- 技术指标类：优先window=0 ----
TECHNICAL_FIELDS_FACTOR_ENG = {
    'rc_50d'
}

# ========= 因子工程分类映射 =========
FIELD_CATEGORIES_FACTOR_ENG = {
    'price': PRICE_FIELDS_FACTOR_ENG,
    'volume': VOLUME_FIELDS_FACTOR_ENG,
    'ratio': RATIO_FIELDS_FACTOR_ENG,
    'value': VALUE_FIELDS_FACTOR_ENG,
    'forecast': FORECAST_FIELDS_FACTOR_ENG,
    'status': STATUS_FIELDS_FACTOR_ENG,
    'technical': TECHNICAL_FIELDS_FACTOR_ENG
}

# ========= 自动生成字段分类映射 =========
FIELD_CATEGORY_MAP_FACTOR_ENGINEERING = {}

# 自动映射：遍历所有分类，为每个字段分配类别
for category, fields in FIELD_CATEGORIES_FACTOR_ENG.items():
    for field in fields:
        FIELD_CATEGORY_MAP_FACTOR_ENGINEERING[field] = FieldCategoryFactorEng(category)

# 因子工程窗口配置（直接使用现有的Z_WINDOW_MAP_DEFAULT）
Z_WINDOW_MAP_FACTOR_ENGINEERING = Z_WINDOW_MAP_DEFAULT.copy()

def get_field_category_factor_engineering(field_name: str) -> FieldCategoryFactorEng:
    """获取字段的因子工程分类"""
    return FIELD_CATEGORY_MAP_FACTOR_ENGINEERING.get(field_name, FieldCategoryFactorEng.TECHNICAL)

def validate_field_window_config_factor_engineering(field_name: str, windows: List[int]) -> bool:
    """验证字段窗口配置的合理性"""
    category = get_field_category_factor_engineering(field_name)
    
    if category == FieldCategoryFactorEng.STATUS:
        invalid_windows = [w for w in windows if w != 0]
        if invalid_windows:
            raise ValueError(f"状态字段 {field_name} 只能设置 z_windows=0，发现无效配置: {invalid_windows}")
    
    return True

# ========= 扩展新字段的便捷方法 =========
def add_field_to_category(field_name: str, category: FieldCategoryFactorEng):
    """动态添加字段到指定分类"""
    if category.value not in FIELD_CATEGORIES_FACTOR_ENG:
        raise ValueError(f"未知的分类: {category.value}")
    
    FIELD_CATEGORIES_FACTOR_ENG[category.value].add(field_name)
    FIELD_CATEGORY_MAP_FACTOR_ENGINEERING[field_name] = category

def add_fields_to_category(field_names: List[str], category: FieldCategoryFactorEng):
    """批量添加字段到指定分类"""
    for field_name in field_names:
        add_field_to_category(field_name, category)
```

### 3.2 `src/data_service/preprocessing/methods/normalizer.py` - 新增因子工程方法

```python
class DataNormalizer:
    # ... 现有代码保持不变 ...
    
    def normalize_data_factor_engineering(
        self,
        df: pd.DataFrame,
        field_window_config: Dict[str, List[int]],
        enable_validation: bool = True,
        **kwargs
    ) -> pd.DataFrame:
        """
        因子工程数据处理方法 - 基于窗口的因子工程
        
        Args:
            df: 输入数据 [trade_date, stock_code, field_name, value]
            field_window_config: 字段窗口配置 {field_name: [window1, window2, ...]}
            enable_validation: 是否启用配置验证
            
        Returns:
            处理后的数据 [trade_date, stock_code, factor_name, factor_value, z_windows]
        """
        # 验证输入
        required_cols = {'trade_date', 'stock_code', 'field_name', 'value'}
        if not required_cols.issubset(df.columns):
            raise ValueError(f"输入数据必须包含列: {required_cols}")
        
        # 验证配置
        if enable_validation:
            for field_name, windows in field_window_config.items():
                validate_field_window_config_factor_engineering(field_name, windows)
        
        # 主处理逻辑
        return self._process_factors_engineering(df, field_window_config)
    
    def _process_factors_engineering(self, df: pd.DataFrame, field_window_config: Dict[str, List[int]]) -> pd.DataFrame:
        """因子工程主处理逻辑实现"""
        # 实现前面伪代码中的主处理流程
        pass
    
    def _process_price_field_engineering(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """因子工程价格字段处理实现"""
        # 实现前面伪代码中的价格字段处理逻辑
        pass
    
    # ... 其他字段处理方法的具体实现 ...
```

### 3.3 `src/tasks/market_price_norm_data_initialization.py` - 适配因子工程

```python
class MarketPriceNormDataTask:
    """市场数据处理任务 - 支持因子工程模式"""
    
    def __init__(self, 
                 processing_mode: str = "academic",  # "academic" 或 "factor_engineering"
                 field_window_config: Optional[Dict[str, List[int]]] = None,
                 table_name: str = "inter_factors_processed",
                 **kwargs):
        """
        Args:
            processing_mode: 处理模式，"academic"使用原有逻辑，"factor_engineering"使用新的因子工程逻辑
            field_window_config: 字段窗口配置，默认使用相应模式的配置
            table_name: 输出表名
        """
        self.processing_mode = processing_mode
        
        if processing_mode == "factor_engineering":
            # 使用因子工程配置
            if field_window_config is None:
                from src.data_service.preprocessing.methods.norm_config import Z_WINDOW_MAP_FACTOR_ENGINEERING
                self.field_window_config = Z_WINDOW_MAP_FACTOR_ENGINEERING.copy()
            else:
                self.field_window_config = field_window_config
        else:
            # 使用原有配置
            if field_window_config is None:
                from src.data_service.preprocessing.methods.norm_config import Z_WINDOW_MAP_DEFAULT
                self.field_window_config = Z_WINDOW_MAP_DEFAULT.copy()
            else:
                self.field_window_config = field_window_config
            
        self.table_name = table_name
        # ... 其他初始化 ...
    
    def _execute_processing_factor_engineering(self) -> bool:
        """因子工程数据处理执行逻辑"""
        # 1. 获取所有需要的字段
        all_fields = list(self.field_window_config.keys())
        
        # 2. 按批次处理数据
        for batch_data in self._get_data_batches(all_fields):
            # 3. 调用因子工程处理方法
            processed_data = self.normalizer.normalize_data_factor_engineering(
                batch_data, 
                self.field_window_config
            )
            
            # 4. 保存到数据库
            success = self._save_to_database_factor_engineering(processed_data)
            if not success:
                return False
        
        return True
    
    def execute(self, **kwargs) -> bool:
        """执行数据处理任务"""
        if self.processing_mode == "factor_engineering":
            return self._execute_processing_factor_engineering()
        else:
            # 使用原有的academic处理逻辑
            return self._execute_initialization()
```

### 3.4 数据库表结构更新

```sql
-- 覆盖原有的处理结果表
DROP TABLE IF EXISTS inter_factors_processed;

CREATE TABLE inter_factors_processed (
    trade_date DATE NOT NULL,
    stock_code VARCHAR(10) NOT NULL,
    factor_name VARCHAR(50) NOT NULL,
    factor_value NUMERIC(20,8),
    z_windows INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    PRIMARY KEY (trade_date, stock_code, factor_name, z_windows)
);

-- 索引优化
CREATE INDEX idx_factors_processed_date ON inter_factors_processed (trade_date);
CREATE INDEX idx_factors_processed_stock ON inter_factors_processed (stock_code);
CREATE INDEX idx_factors_processed_factor ON inter_factors_processed (factor_name);
```

### 3.5 `run_daily_data_pipeline.py` - 支持因子工程模式

```python
# 在配置部分新增因子工程选项
PROCESSING_MODE = "factor_engineering"  # "academic" 或 "factor_engineering"

DATA_NORM_CONFIG_FACTOR_ENGINEERING = {
    "processing_mode": "factor_engineering",
    "field_window_config": "Z_WINDOW_MAP_FACTOR_ENGINEERING",  # 使用因子工程配置
    "table_name": "inter_train_factors_mkt_norm_academic_v3",  # 覆盖原表
    "enable_validation": True,
    "start_date": "2005-01-01",
    "overlap_days": 60,
    "force_update": False,
    "field_batch_size": 8,
}

def main():
    # 根据模式选择不同的处理逻辑
    if PROCESSING_MODE == "factor_engineering":
        # 因子工程处理逻辑
        # 解析配置
        map_name = DATA_NORM_CONFIG_FACTOR_ENGINEERING.get("field_window_config", None)
        if isinstance(map_name, str):
            z_map = getattr(nc, map_name, None)
            if z_map is None:
                logger.warning(f"未找到配置 '{map_name}'，使用默认 Z_WINDOW_MAP_DEFAULT")
                z_map = nc.Z_WINDOW_MAP_DEFAULT
        else:
            z_map = nc.Z_WINDOW_MAP_DEFAULT
        
        task = MarketPriceNormDataTask(
            processing_mode="factor_engineering",
            field_window_config=z_map,
            **{k: v for k, v in DATA_NORM_CONFIG_FACTOR_ENGINEERING.items() 
               if k not in ['processing_mode', 'field_window_config']}
        )
        success = task.execute()
    else:
        # 原有的academic处理逻辑
        # ... 现有代码 ...
```

## 4. 测试数据

股票000002 
	开盘价(元)	收盘价(元)			
日期	adj_open	adj_close	window30_price	adj_open_mar	adj_open_roc
2015-07-01	1756.9692	1756.9692			
2015-07-02	1764.3669	1752.0373			
2015-07-03	1717.5144	1689.1563			
2015-07-06	1840.8105	1834.6457			
2015-07-07	1787.7932	1792.7250			
2015-07-08	1647.2357	1634.9061			
2015-07-09	1634.9061	1793.9580			
2015-07-10	1750.8044	1845.7424			
2015-07-13	1800.1228	1861.7709			
2015-07-14	1809.9865	1793.9580			
2015-07-15	1780.3954	1813.6854			
2015-07-16	1813.6854	1812.4524			
2015-07-17	1821.0831	1858.0720			
2015-07-20	1845.7424	1892.5949			
2015-07-21	1882.3991	1849.2627			
2015-07-22	1849.2627	1873.4778			
2015-07-23	1853.0862	1932.1036			
2015-07-24	1933.3781	1963.9655			
2015-07-27	1963.9655	1809.7540			
2015-07-28	1771.5198	1863.2820			
2015-07-29	1860.7330	1860.7330			
2015-07-30	1858.1841	1794.4603			
2015-07-31	1777.8922	1807.2051			
2015-08-03	1797.0093	1862.0075			
2015-08-04	1862.0075	1884.9480			
2015-08-05	1877.3012	1841.6159			
2015-08-06	1817.4009	1827.5967			
2015-08-07	1844.1648	1860.7330			
2015-08-10	1872.2033	1920.6333			
2015-08-11	1905.3396	1984.3571	1835.62697		
2015-08-12	1937.2015	1902.7907	1840.487687	0.05385831	0.097654127
2015-08-13	1898.9673	1896.4183	1845.300387	0.031279628	0.08053093
2015-08-14	1893.8694	1905.3396	1852.506497	0.025979962	0.114392864
2015-08-17	1883.6736	1881.1246	1854.055793	0.016684328	0.026372529
2015-08-18	1878.5757	1812.3030	1854.708393	0.013138323	0.046777075
2015-08-19	1795.7348	1847.9883	1861.811133	-0.032313186	0.093828926
2015-08-20	1828.8712	1819.9498	1862.677527	-0.017850796	0.019274594
2015-08-21	1808.4796	1772.7943	1860.245923	-0.029528491	-0.020395089
2015-08-24	1739.6579	1681.0321	1854.221297	-0.067020211	-0.067839647
2015-08-25	1631.3276	1656.8171	1849.649933	-0.12807066	-0.09503019
2015-08-26	1695.0513	1688.6789	1845.48305	-0.08728339	-0.067647902
2015-08-27	1738.3834	1679.7576	1841.05989	-0.059785458	-0.041725244
2015-08-28	1696.3258	1730.7366	1836.815377	-0.081876815	-0.091074773
2015-08-31	1709.0705	1758.7750	1832.354713	-0.072083643	-0.101999195
2015-09-01	1746.0303	1813.5775	1831.165207	-0.048257057	-0.057442208
2015-09-02	1754.9516	1812.3030	1829.126047	-0.042511211	-0.065355212
2015-09-07	1782.9901	1733.2856	1822.49878	-0.025546496	-0.080317571
2015-09-08	1725.6387	1743.4813	1815.149307	-0.054611273	-0.129368401
2015-09-09	1747.3048	1753.6771	1813.280077	-0.038093241	-0.035116438
2015-09-10	1737.1090	1725.6387	1808.691967	-0.042915165	-0.070117212
2015-09-11	1732.0111	1716.7174	1803.891447	-0.043320695	-0.071687277
2015-09-14	1716.7174	1681.0321	1800.110507	-0.049532267	-0.044290329
2015-09-15	1681.0321	1659.3660	1795.182537	-0.068440106	-0.072373558
2015-09-16	1668.2873	1711.6195	1790.169603	-0.073309177	-0.109857675
2015-09-17	1701.4237	1669.5618	1782.990063	-0.050844995	-0.102434863
2015-09-18	1672.1108	1672.1108	1777.339893	-0.064204985	-0.096556612
2015-09-21	1664.4639	1679.7576	1772.411923	-0.065614715	-0.093498735
2015-09-22	1686.1300	1689.9534	1766.71927	-0.049905325	-0.098534534
2015-09-23	1675.9342	1675.9342	1758.562633	-0.052753566	-0.136284234
2015-09-24	1682.3066	1668.2873	1748.026973	-0.044330962	-0.165129155
2015-09-25	1659.3660	1644.0723	1739.403027	-0.052052106	-0.136885996
2015-09-28	1641.5234	1637.7000	1730.779083	-0.057917253	-0.144342289
2015-09-29	1624.9552	1605.8381	1720.7957	-0.063091398	-0.159180014
2015-09-30	1614.7594	1622.4063	1712.171757	-0.063600833	-0.152683822
2015-10-08	1688.6789	1665.7384	1707.28627	-0.013816091	-0.070651905
2015-10-09	1673.3852	1686.1300	1701.890993	-0.020056493	-0.099249001
2015-10-12	1686.1300	1717.9919	1698.492397	-0.00930402	-0.076372956
2015-10-13	1709.0705	1716.7174	1696.623167	0.006208623	-0.036607347
2015-10-14	1697.6003	1692.5024	1697.00551	0.000575763	0.009807716
2015-10-15	1695.0513	1723.0898	1699.2146	-0.001152227	0.022814654
2015-10-16	1724.3642	1739.6579	1700.9139	0.014692259	0.020911896
2015-10-19	1746.0303	1724.3642	1702.400787	0.026179116	0.038695314
2015-10-20	1711.6195	1726.9132	1702.27334	0.005400516	-0.0111071
2015-10-21	1726.9132	1686.1300	1699.85184	0.014370921	-0.018282007
2015-10-22	1686.1300	1701.4237	1696.11338	-0.008105132	-0.072865452
2015-10-23	1702.6982	1751.1282	1694.07422	0.003874783	-0.062384243
2015-10-26	1768.9708	1742.2069	1694.371597	0.0432615	0.02037911
2015-10-27	1726.9132	1744.7558	1694.41408	0.019023605	-0.009548324
2015-10-28	1743.4813	1740.9324	1693.989257	0.028546856	-0.005830922
2015-10-29	1740.9324	1735.8345	1694.329117	0.027334578	0.008823589
2015-10-30	1746.0303	1742.2069	1695.178767	0.03005795	0.016930832
2015-11-02	1734.5600	1728.1877	1696.75062	0.022965577	0.031345829
2015-11-03	1728.1877	1740.9324	1699.4695	0.018358265	0.040637685
2015-11-04	1739.6579	1794.4603	1702.23086	0.023372341	0.016248486
2015-11-05	1790.6369	1799.5582	1706.564073	0.050631705	0.07001017
2015-11-06	1798.2838	1811.0285	1711.194663	0.05235073	0.072745985
2015-11-09	1811.0285	1835.2435	1716.377527	0.056703156	0.075245419
2015-11-10	1826.3222	1818.6754	1720.66826	0.062088237	0.077603263
2015-11-11	1811.0285	1805.9306	1725.001473	0.051182177	0.077524175
2015-11-12	1814.8519	1795.7348	1729.249723	0.050775962	0.084206335
2015-11-13	1784.2645	1771.5198	1733.497973	0.031318657	0.081830012
2015-11-16	1760.0495	1784.2645	1738.383457	0.015200617	0.072049115
2015-11-17	1788.0880	1784.2645	1744.331003	0.028191259	0.107501092
2015-11-18	1793.1859	1869.6543	1752.572603	0.027622767	0.100083453
2015-11-19	1860.7330	1847.9883	1758.6476	0.059885729	0.110701988
2015-11-20	1841.6159	1846.7138	1764.000393	0.046098288	0.088207431
2015-11-23	1847.9883	1831.4201	1767.781333	0.046513461	0.072941533
2015-11-24	1831.4201	1847.9883	1772.15703	0.035366401	0.064677698
2015-11-25	1837.7925	1855.6351	1777.594787	0.036367657	0.082356979
2015-11-26	1854.3606	1847.9883	1781.75807	0.04227874	0.073420872
2015-11-27	1839.0669	1817.4009	1784.349503	0.031657767	0.055569839
2015-11-30	1821.2243	1920.6333	1790.891807	0.020455042	0.054650564
2015-12-01	1910.4375	2113.0790	1803.764	0.064618562	0.100996736
2015-12-02	2125.8237	2324.6418	1825.047727	0.164283758	0.231723389
2015-12-03	2351.4058	2440.6190	1849.68757	0.253407224	0.323547991
2015-12-04	2408.7571	2418.9529	1871.948393	0.264094145	0.318850622
2015-12-07	2398.5613	2296.6034	1890.428277	0.247889289	0.319716456
2015-12-08	2281.3097	2264.7415	1907.761133	0.187946303	0.268135105
2015-12-09	2287.6820	2491.5980	1932.78332	0.181608705	0.273118246
2015-12-10	2496.6959	2490.3235	1957.932953	0.256007119	0.36347994
2015-12-11	2439.3445	2536.2046	1984.399543	0.219840055	0.336576713
2015-12-14	2497.9704	2559.1451	2012.098123	0.230162191	0.368405275
2015-12-15	2497.9704	2686.5926	2043.62013	0.216300542	0.36105773
2015-12-16	2629.2412	2573.1644	2069.576933	0.251972479	0.381990979
2015-12-17	2597.3794	2830.6082	2103.945267	0.227158807	0.366961823
2015-12-18	2854.8233	3113.5416	2147.36237	0.305195669	0.455115034
2015-12-21	3113.5416	3113.5416	2189.972307	0.371520572	0.528583686
2015-12-22	3113.5416	3113.5416	2233.134513	0.351871958	0.537652422
2015-12-23	3113.5416	3113.5416	2276.721547	0.332354647	0.54468483
2015-12-24	3113.5416	3113.5416	2320.64844	0.313024366	0.550346559
