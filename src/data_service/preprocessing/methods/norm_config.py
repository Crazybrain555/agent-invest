"""
数据归一化配置模块 v2024 - 因子工程模式

此模块定义了因子工程的字段分类和处理策略。
基于窗口的因子工程方法，实现系统性的因子构建：

价格类：分母统一用adj_close，结果log
体量/资金流类：分母用自身，结果log
比例类：window=0保留原值，其他和体量一样处理
状态类：只允许window=0

处理流程：经济学归一化 → 窗口因子工程 → 生成不同窗口的因子
"""

from enum import Enum
from typing import Dict, Set, List

# ========= 1. 因子工程字段分类枚举 ===========
class FieldCategoryFactorEng(Enum):
    PRICE = "price"           # 价格类：分母用adj_close，结果log
    VOLUME = "volume"         # 体量/资金流类：分母用自身，结果log
    RATIO = "ratio"           # 比例类：window=0保留原值，其他log
    VALUE = "value"           # 市值类：同体量处理
    FORECAST = "forecast"     # 预测类：同体量处理  
    STATUS = "status"         # 状态类：只允许window=0
    TECHNICAL = "technical"   # 技术指标类：优先window=0

# ========= 2. 因子工程字段分类 ===========
# ---- 价格类：分母统一用adj_close，结果都要log ----
PRICE_FIELDS_FACTOR_ENG = {
    'adj_close', 'adj_open', 'adj_high', 'adj_low', 'adj_preclose',
    'close', 'open', 'high', 'low', 'vwap', 'limit_up', 'limit_down'
}

# ---- 体量/资金流类：分母用自身，结果都要log ----
VOLUME_FIELDS_FACTOR_ENG = {
    'volume', 'amount', 'avg_volume_3m', 'TRADES_COUNT',
    'initiative_buy_money', 'initiative_sell_money',
    'large_buy_money', 'large_sell_money',
    'inst_buy_value', 'inst_sell_value',
    'large_buy_value', 'large_sell_value',
    'med_buy_value', 'med_sell_value',
    'small_buy_value', 'small_sell_value',
    'net_inflow', 'open_net_inflow', 'close_net_inflow',
    'large_net_inflow', 'large_open_net_inflow', 'large_close_net_inflow',
    'inst_buy_value_act', 'inst_sell_value_act',
    'large_buy_value_act', 'large_sell_value_act',
    'med_buy_value_act', 'med_buy_value_act',
    'small_buy_value_act'
}

# ---- 比例类：window=0保留原值，其他和体量一样处理 ----
RATIO_FIELDS_FACTOR_ENG = {
    'turnover_rate', 'free_turnover_rate', 'swing',
    'dividend_yield_12m', 'price_dividend_ratio',
    'initiative_buy_rate', 'initiative_sell_rate',
    'large_buy_rate', 'large_sell_rate',
    'entrust_rate', 'pct_change',
    'net_inflow_rate', 'open_net_inflow_rate', 'close_net_inflow_rate',
    'moneyflow_pct', 'open_moneyflow_pct', 'close_moneyflow_pct',
    'large_open_moneyflow_pct', 'large_close_moneyflow_pct'
}

# ---- 市值/估值类：同体量处理逻辑 ----
VALUE_FIELDS_FACTOR_ENG = {
    'market_cap', 'float_market_cap',
    'pe_ratio', 'pe_ttm', 'pe_deducted_ttm',
    'pb_ratio', 'pb_mrq',
    'ps_ttm', 'ps_lyr',
    'pcf_ocf_ttm', 'pcf_ocf_lyr',
    'pcf_ncf_ttm', 'pcf_ncf_lyr'
}

# ---- 预测类：同体量处理逻辑 ----
FORECAST_FIELDS_FACTOR_ENG = {
    'total_shares', 'float_shares', 'free_shares',
    'operating_cash_flow_ttm', 'net_cash_increase_ttm',
    'net_profit_ttm', 'operating_revenue_ttm',
    'consensus_np', 'consensus_np_growth_2y', 'consensus_forecast',
    'change'
}

# ---- 状态类：只允许window=0 ----
STATUS_FIELDS_FACTOR_ENG = {
    'trade_status', 'up_down_limit_status', 'lowest_highest_status',
    'adj_factor'
}

# ---- 技术指标类：优先window=0 ----
TECHNICAL_FIELDS_FACTOR_ENG = {
    'rc_50d'
}

# ========= 3. 因子工程分类映射 =========
FIELD_CATEGORIES_FACTOR_ENG = {
    'price': PRICE_FIELDS_FACTOR_ENG,
    'volume': VOLUME_FIELDS_FACTOR_ENG,
    'ratio': RATIO_FIELDS_FACTOR_ENG,
    'value': VALUE_FIELDS_FACTOR_ENG,
    'forecast': FORECAST_FIELDS_FACTOR_ENG,
    'status': STATUS_FIELDS_FACTOR_ENG,
    'technical': TECHNICAL_FIELDS_FACTOR_ENG
}

# ========= 4. 自动生成字段分类映射 =========
FIELD_CATEGORY_MAP_FACTOR_ENGINEERING = {}

# 自动映射：遍历所有分类，为每个字段分配类别
for category, fields in FIELD_CATEGORIES_FACTOR_ENG.items():
    for field in fields:
        FIELD_CATEGORY_MAP_FACTOR_ENGINEERING[field] = FieldCategoryFactorEng(category)

# ========= 5. 因子工程窗口配置 =========
# 基于原有配置，但调整为因子工程需求
Z_WINDOW_MAP_FACTOR_ENGINEERING: Dict[str, List[int]] = {
# 价格 ================================
    "adj_close":  [ 1,30,60,90],
    "adj_open":   [ 1,30,60,90],
    "adj_high":   [ 1,30,60,90],
    "adj_low":    [ 1,30,60,90],
    "vwap":       [ 1,30,60,90],
    
    # 体量 / 资金流 ========================
    "TRADES_COUNT":      [1,30,60,90],
    "amount":            [1,30,60,90],
    "volume":            [1,30,60,90],
    "large_net_inflow":  [1,30,60,90],
    "inst_buy_value":     [1,30,60,90],
    "inst_sell_value":    [1,30,60,90],
    "large_buy_value":    [1,30,60,90],
    "large_sell_value":   [1,30,60,90],
    "med_buy_value":      [1,30,60,90],
    "med_sell_value":     [1,30,60,90],
    "small_buy_value":    [1,30,60,90],
    "small_sell_value":   [1,30,60,90],
    # 比率 / 情绪 ==========================
    "turnover_rate":          [0,1],
    "swing":                  [0],
    "pct_change":             [0],
    "initiative_buy_rate":    [0,30],
    "initiative_sell_rate":   [0,30], 
    "large_buy_rate":         [0,30],
    "large_sell_rate":        [0,30],
    "net_inflow_rate":        [0,30],
    "open_net_inflow_rate":   [0,30],
    "close_net_inflow_rate":  [0,30],
    "moneyflow_pct":          [0,30],
    
    # 估值 / 市值 / 分红 ===================
    "pe_ttm":             [0],
    "pb_ratio":           [0],
    "dividend_yield_12m": [0],
    "float_market_cap":   [0],
    
    # 技术指标类 - 主要用原值
    "rc_50d":               [0],
    
    # 状态类 - 只允许window=0
    "up_down_limit_status": [0],
    "trade_status":         [0],
    # "adj_factor":           [0],
}

# ========= 6. 辅助函数 =========
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

def get_fields_by_category_factor_engineering(category: str) -> Set[str]:
    """获取指定分类的所有字段集合"""
    return FIELD_CATEGORIES_FACTOR_ENG.get(category, set())

# ========= 7. 扩展新字段的便捷方法 =========
def add_field_to_category_factor_engineering(field_name: str, category: FieldCategoryFactorEng):
    """动态添加字段到指定分类"""
    if category.value not in FIELD_CATEGORIES_FACTOR_ENG:
        raise ValueError(f"未知的分类: {category.value}")
    
    FIELD_CATEGORIES_FACTOR_ENG[category.value].add(field_name)
    FIELD_CATEGORY_MAP_FACTOR_ENGINEERING[field_name] = category

def add_fields_to_category_factor_engineering(field_names: List[str], category: FieldCategoryFactorEng):
    """批量添加字段到指定分类"""
    for field_name in field_names:
        add_field_to_category_factor_engineering(field_name, category)






# ========= 8. 当前默认使用的配置 =========
# 主要使用因子工程配置
Z_WINDOW_MAP_DEFAULT = Z_WINDOW_MAP_FACTOR_ENGINEERING
Z_WINDOW_MAP_V1 = Z_WINDOW_MAP_FACTOR_ENGINEERING

# 默认窗口（当字段不在映射中时使用）
DEFAULT_Z_WINDOWS = [0, 20]

# ========= 9. 向后兼容性支持 =========
# 保留一些原有的定义，用于向后兼容
PRICE_FIELDS = PRICE_FIELDS_FACTOR_ENG
VOLUME_FIELDS = VOLUME_FIELDS_FACTOR_ENG
RATIO_FIELDS = RATIO_FIELDS_FACTOR_ENG
VALUE_FIELDS = VALUE_FIELDS_FACTOR_ENG
STATUS_FIELDS = STATUS_FIELDS_FACTOR_ENG
FACTOR_FIELDS = STATUS_FIELDS_FACTOR_ENG  # 原来的FACTOR_FIELDS合并到STATUS中

# 保留原有的字段集合定义，用于向后兼容
FIELD_CATEGORIES = {
    'price': PRICE_FIELDS,
    'volume': VOLUME_FIELDS,
    'ratio': RATIO_FIELDS,
    'value': VALUE_FIELDS,
    'status': STATUS_FIELDS,
}

def get_field_category(field_name: str) -> str:
    """获取字段所属的分类 - 向后兼容方法"""
    category = get_field_category_factor_engineering(field_name)
    return category.value 