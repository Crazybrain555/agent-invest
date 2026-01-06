from dataclasses import dataclass


@dataclass
class PerformanceMetrics:
    """标准化性能指标"""
    total_return: float = 0.0
    annual_return: float = 0.0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    information_ratio: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    tracking_error: float = 0.0
    hit_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0

    # IC相关指标
    mean_ic: float = 0.0  # IC均值
    ic_std: float = 0.0  # IC标准差
    ic_ir: float = 0.0  # IC信息比率 (IC均值/IC标准差)
    ic_hit_rate: float = 0.0  # IC胜率 (正IC的比例)

    # 因子收益率
    factor_return_total: float = 0.0  # 新增：因子收益总和
    factor_return_mean: float = 0.0
    factor_return_std: float = 0.0
    factor_return_t_stat: float = 0.0

    # 换手率相关指标（用于记录交易活跃度）
    turnover_mean: float = 0.0  # 平均换手率（每次调仓的换手比例的均值）
    turnover_std: float = 0.0   # 换手率标准差
    turnover_total: float = 0.0 # 期间换手率总和（各次调仓换手率之和）

