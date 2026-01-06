#!/usr/bin/env python3
"""
回测框架主运行文件
提供简单易用的接口来运行量化策略回测
"""
import pandas as pd

from backtest.backtester.backtest_runner import BacktestRunner
from backtest.configs.backtest_config import BacktestConfig
from backtest.expression.expression import AlphaExpression


def alpha_backtest(config:BacktestConfig,ind_df:pd.DataFrame,expressions:[str],plot=False):
    # ind_df的stock_code字段必须是带有.SZ .SH .BJ后缀的，否则没法获取行情数据
    if "trade_date" not in ind_df.columns:
        raise RuntimeError("ind_df中缺失trade_date字段")
    if "stock_code" not in ind_df.columns:
        raise RuntimeError("ind_df中缺失stock_code字段")
    if "name" not in ind_df.columns:
        raise RuntimeError("name中缺失stock_code字段")
    if "value" not in ind_df.columns:
        raise RuntimeError("value中缺失value字段")


    if config is None:
        config = BacktestConfig(
            start_date="20240101",
            end_date="20241231",
            rebalance_frequency="5D",  # 每20个交易日调仓
            initial_capital=1000000.0,
            max_position_size=0.03,  # 单只股票最大权重3%
            trade_cost_rate=5 / 10000,  # 交易费率0.05%
            max_stocks=50,  # 最多持有50只股票
            factor_return_period=20,  # 因子收益率的未来收益计算周期
            factor_return_calculation_frequency=20  # 因子收益率的截面回归计算频率
        )
    runner = BacktestRunner(config)
    strategies = []
    count = 0
    for expression in expressions:
        count = count + 1
        expression = expression.strip()
        custom_alpha = AlphaExpression(
            name=f"expression_{count}",
            expression=expression
        )
        strategies.append(custom_alpha)

    performance_dict = {}
    for strategy in strategies:
        results = runner.run_single_backtest(
            strategy,
            data_source="backtest/sample_data.csv",
            plot_results=plot,
            enable_detailed_log=False,  # 启用详细交易记录
            detailed_log_path=f"logs/{strategy.name}_详细交易记录.csv"  # 自定义日志文件路径
        )
        perf = results['performance']
        performance_dict[results['alpha'].name] = {
            "总收益率":f"{perf.total_return:.2%}",
            "夏普比率":f"{perf.sharpe_ratio:.3f}",
            "最大回撤":f"{perf.max_drawdown:.2%}",
            "Calmar比率":f"{perf.calmar_ratio:.3f}",
            "胜率":f"{perf.hit_rate:.2%}",
            "盈亏比":f"{perf.profit_loss_ratio:.3f}",
            "VaR(95%)":f"{perf.var_95:.2%}",
            "CVaR(95%)":f"{perf.cvar_95:.2%}",
            "IC均值":f"{perf.mean_ic:.4f}",
            "IC标准差":f"{perf.ic_std:.4f}",
            "IC胜率":f"{perf.ic_hit_rate:.2%}",
            "因子收益率总和":f"{perf.factor_return_total:.6f}",
            "因子收益率均值":f"{perf.factor_return_mean:.6f}",
            "因子收益率T值":f"{perf.factor_return_t_stat:.4f}"
        }
    return performance_dict

