from dataclasses import dataclass
from typing import List


@dataclass
class BacktestConfig:
    """回测配置参数"""

    # 基础配置
    start_date: str = "20200101"  # 回测开始日期
    end_date: str = "20231231"  # 回测结束日期

    # 权重分配配置
    weight_method: str = "equal"  # 权重分配方法: equal(等权重), factor_score(因子得分加权),

    # 中性化配置
    neutralize_method: List[str] = None  # 空列表代表无中性化，industry行业中性化，market_cap市值中性化
    neutralize_industry_name: str = "CSI"  # 行业分类标准
    neutralize_algo: str = "ols"  # ols 普通最小二乘回归 wls 加权最小二乘法回归（使用市值开方作为权重）

    # 涨跌停和极端情况处理
    # limit_up_handling: str = "defer"  # 涨停处理: defer(递延), skip(跳过选下一个), execute(执行)
    # limit_up_handling_defer_days: int = 5  # 涨停处理最大递延天数
    # limit_down_handling: str = "defer"  # 跌停处理: defer(递延), skip(跳过不卖), execute(执行)
    # limit_down_handling_defer_days: int = 5  # 跌停处理最大递延天数
    # buy_suspended_handling: str = "skip"  # 买入时停牌处理方式:defer, skip, execute
    # sell_suspended_handling: str = "defer"  # 卖出时停牌处理方式:defer, skip, execute

    # 调仓与交易配置
    rebalance_frequency: str = "5D"  # 调仓频率: 1D, 5D, 10D, 1M, 1Q等
    trade_at: str = "vwap"  # 交易价格: close, vwap

    # 交易成本配置
    trade_cost_rate: float = 0.0003  # 交易成本费率（双边）
    slippage_ratio: float = 0.0001  # 滑点比例

    # IC计算配置
    ic_calculation_period: int = 20
    ic_method: str = 'spearman'  # 'pearson' or 'spearman'

    # 因子收益率
    factor_return_period: int = 1
    factor_return_calculation_frequency: int = 1  # 因子收益率计算频率，1表示每天计算

    # 组合配置（保留向后兼容）
    initial_capital: float = 1000000.0  # 初始资金
    max_position_size: float = 0.05  # 单只股票最大权重5%
    min_market_cap: float = 1e8 /10000  # 最小市值1亿
    max_stocks: int = 100  # 最大持股数量

    # 详细交易记录输出配置
    enable_detailed_log: bool = False  # 是否启用详细交易记录输出
    detailed_log_path: str = "logs/detailed_trading_log.csv"  # 详细交易记录文件路径
    log_holdings: bool = True  # 是否记录持仓详情
    log_trades: bool = True  # 是否记录交易详情
    log_costs: bool = True  # 是否记录费用详情

    def __post_init__(self):
        """初始化后处理"""
        if self.neutralize_method is None:
            self.neutralize_method = ["industry", "market_cap"]
