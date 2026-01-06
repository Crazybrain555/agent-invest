from typing import Dict, Any

import numpy as np
import pandas as pd

from backtest.configs.backtest_config import BacktestConfig


class RiskManager:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def check_portfolio_risk(self,
                             weights:pd.Series,
                             returns:pd.DataFrame,
                             market_cap:pd.Series,
                             industry:pd.Series=None)->Dict[str, Any]:
        risk_metrics = {}

        # 集中度风险
        risk_metrics['concentration'] = {
            'max_weight': weights.max(),
            'top5_weight': weights.nlargest(5).sum(),
            'herfindahl_index': (weights ** 2).sum()
        }

        # 行业集中度
        if industry is not None:
            industry_weights = weights.groupby(industry.loc[weights.index]).sum()
            risk_metrics['industry_concentration'] = {
                'max_industry_weight': industry_weights.max(),
                'industry_herfindahl': (industry_weights ** 2).sum()
            }

        # 市值分布
        if market_cap is not None:
            mcap_weights = weights * market_cap.loc[weights.index]
            total_mcap = mcap_weights.sum()

            # 按市值分位数分组
            mcap_percentiles = market_cap.loc[weights.index].rank(pct=True)
            large_cap_weight = weights[mcap_percentiles > 0.8].sum()
            mid_cap_weight = weights[(mcap_percentiles > 0.2) & (mcap_percentiles <= 0.8)].sum()
            small_cap_weight = weights[mcap_percentiles <= 0.2].sum()

            risk_metrics['market_cap_distribution'] = {
                'large_cap': large_cap_weight,
                'mid_cap': mid_cap_weight,
                'small_cap': small_cap_weight
            }

        # 历史波动率风险
        if len(returns) > 20:
            portfolio_returns = (returns.loc[:, weights.index] * weights).sum(axis=1)
            risk_metrics['volatility'] = {
                'daily_vol': portfolio_returns.std(),
                'annual_vol': portfolio_returns.std() * np.sqrt(252),
                'var_95': portfolio_returns.quantile(0.05),
                'cvar_95': portfolio_returns[portfolio_returns <= portfolio_returns.quantile(0.05)].mean()
            }

        return risk_metrics

