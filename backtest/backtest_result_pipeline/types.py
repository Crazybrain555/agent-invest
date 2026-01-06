"""
Pipeline 数据合约（dataclass）

Steps 之间只传递 dataclass，禁止散 dict 串来串去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pandas as pd

if TYPE_CHECKING:
    # 仅用于类型提示；避免在 import pipeline/types 时触发 expression/engine 等重型依赖
    # （例如 statsmodels、数据库连接等），让报表/导出工具可在轻量环境运行。
    from backtest.configs.performance_metrics import PerformanceMetrics
    from backtest.expression.expression import AlphaExpression


@dataclass(frozen=True)
class RunContext:
    """运行上下文：目录布局"""
    run_id: str
    run_dir: Path
    config_dir: Path
    data_dir: Path
    factors_dir: Path  # data/factors
    nav_dir: Path      # data/nav
    signals_dir: Path  # data/signals
    tables_dir: Path
    plots_dir: Path
    logs_dir: Path


@dataclass
class StrategyBacktestResult:
    """单个策略的回测结果"""
    pool_code: str
    alpha: "AlphaExpression"
    performance: "PerformanceMetrics"
    portfolio_history: pd.Series          # DatetimeIndex, 组合市值
    trade_log: pd.DataFrame
    risk_log: pd.DataFrame
    ic_analysis: Optional[Dict[str, Any]] = None   # {'ic_series', 'ic_metrics', 'monthly_ic', 'yearly_ic', ...}
    factor_return_analysis: Optional[Dict[str, Any]] = None  # {'factor_return_series', ...}


@dataclass
class BenchmarkNavResult:
    """基准 + NAV 对齐结果"""
    pool_code: str
    benchmark_code: str
    strategy_name: str
    nav_df: pd.DataFrame  # 含 strategy_nav/benchmark_nav/excess_nav/excess_nav_diff/strategy_ret/benchmark_ret/active_ret
    strategy_total_return: float
    benchmark_total_return: float
    excess_total_return: float  # = strategy_total_return - benchmark_total_return


@dataclass
class YearlyMetrics:
    """年度指标（单策略单年）"""
    year: int
    strategy_name: str
    pool_code: str
    # portfolio 类指标
    total_return: float = 0.0
    annual_return: float = 0.0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    hit_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    # 基准相关
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    # IC 相关
    mean_ic: float = 0.0
    ic_std: float = 0.0
    ic_hit_rate: float = 0.0
    # 因子收益相关
    factor_return_total: float = 0.0
    factor_return_mean: float = 0.0
    factor_return_t_stat: float = 0.0
    # 换手相关
    turnover_mean: float = 0.0
    turnover_total: float = 0.0


@dataclass
class OverallMetrics:
    """总体指标（单策略）"""
    strategy_name: str
    pool_code: str
    # portfolio 类指标
    total_return: float = 0.0
    annual_return: float = 0.0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    hit_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    # 基准相关
    benchmark_code: str = ""
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    # IC 相关
    mean_ic: float = 0.0
    ic_std: float = 0.0
    ic_hit_rate: float = 0.0
    # 因子收益相关
    factor_return_total: float = 0.0
    factor_return_mean: float = 0.0
    factor_return_t_stat: float = 0.0
    # 换手相关
    turnover_mean: float = 0.0
    turnover_total: float = 0.0


@dataclass
class AggregatedTables:
    """聚合后的表格数据"""
    summary: pd.DataFrame   # 核心指标汇总（总体+年度）
    overall: pd.DataFrame   # 总体表现（每策略一行）
    yearly: pd.DataFrame    # 年度表现（每年*每策略）


@dataclass
class StrategyNameMapping:
    """策略名称映射"""
    original_name: str
    safe_name: str


@dataclass(frozen=True)
class PipelineResult:
    """Pipeline 运行结果（最终产物路径）"""
    run_dir: Path
    tables_excel_path: Path
    manifest_path: Path
    nav_csv_paths: Dict[str, Dict[str, Path]]       # pool_code -> strategy_name -> csv path
    nav_png_paths: Dict[str, Dict[str, Path]]       # pool_code -> strategy_name -> png path
    signals_csv_paths: Dict[str, Dict[str, Path]]   # pool_code -> strategy_name -> csv path (enable_detailed_log=True 时)
    factor_paths: List[Path] = field(default_factory=list)  # 因子文件路径
    strategy_name_mapping: Dict[str, str] = field(default_factory=dict)  # original -> safe


@dataclass
class PipelineConfig:
    """Pipeline 配置（从 ModelBacktestConfig 提取关键字段）"""
    # 路径相关
    model_path: str
    dataset_path: Optional[str]
    backtest_result_path: Optional[str]
    
    # 日期相关
    start_date: str
    end_date: str
    
    # 基准
    benchmark_code: str
    
    # 交易配置
    initial_capital: float
    max_position_size: float
    max_stocks: int
    rebalance_frequency: str
    trade_cost_rate: float
    slippage_ratio: float
    
    # 因子相关
    factor_shift: int
    signal_negative: bool
    
    # IC/因子收益配置
    ic_calculation_period: int
    ic_method: str
    factor_return_period: int
    factor_return_calculation_frequency: int
    
    # 输出配置
    enable_detailed_log: bool
    detailed_log_path: str
    
    # 其他
    weight_method: str = "equal"
    neutralize_method: List[str] = field(default_factory=lambda: ["industry", "market_cap"])
    neutralize_industry_name: str = "CSI"
    neutralize_algo: str = "ols"
    min_market_cap: float = 1e4  # 万元
