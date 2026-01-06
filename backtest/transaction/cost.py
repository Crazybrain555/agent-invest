from typing import Dict

import pandas as pd

from backtest.configs.backtest_config import BacktestConfig


class TransactionCostModel:
    def __init__(self, config: BacktestConfig):
        self.trade_cost_rate = config.trade_cost_rate
        self.slippage_ratio = config.slippage_ratio

    def calculate_costs(self,
                        trades: pd.Series,
                        prices: pd.Series,
                        market_cap: pd.Series = None
                        ) -> Dict[str, float]:
        """计算交易费用

        Args:
            trades: 交易金额序列（正数为买入，负数为卖出）
            prices: 价格序列
            market_cap: 市值序列（可选）

        Returns:
            包含各项费用的字典
        """
        trade_value = trades.abs().sum()

        # 分别计算佣金和滑点费用
        # 假设trade_cost_rate主要是佣金费用
        commission_cost = trade_value * self.trade_cost_rate

        # 滑点费用基于滑点比率计算
        slippage_cost = trade_value * self.slippage_ratio

        # 总费用
        total_cost = commission_cost + slippage_cost

        return {
            'total_cost': total_cost,
            'commission': commission_cost,
            'slippage': slippage_cost,
            'trade_value': trade_value,  # 额外返回交易总价值，便于分析
            'commission_rate': self.trade_cost_rate,
            'slippage_rate': self.slippage_ratio
        }