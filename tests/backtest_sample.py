#!/usr/bin/env python3
"""
回测框架主运行文件
提供简单易用的接口来运行量化策略回测
"""
import pandas as pd

from backtest import alpha_backtest
from backtest.configs.backtest_config import BacktestConfig


if __name__ == "__main__":
    config = BacktestConfig(
        start_date="20240101",
        end_date="20241231",
        rebalance_frequency="20D",  # 每20个交易日调仓
        initial_capital=1000000.0,
        max_position_size=0.1,  # 单只股票最大权重3%
        trade_cost_rate=5 / 10000,  # 交易费率0.05%
        max_stocks=50,  # 最多持有50只股票
        factor_return_period=20,  # 因子收益率的未来收益计算周期
        factor_return_calculation_frequency=20  # 因子收益率的截面回归计算频率
    )
    sample = pd.read_csv("backtest\sample_data.csv")
    p = alpha_backtest(config,sample,["gru007","ts_max(gru007,5)"],plot=False)
    print(p)