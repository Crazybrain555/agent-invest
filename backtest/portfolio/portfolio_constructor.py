import pandas as pd

from backtest.configs.backtest_config import BacktestConfig

class PortfolioConstructor:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def _clean_data(self, alpha_scores:pd.Series,market_cap:pd.Series):
        valid_alpha = alpha_scores.dropna()
        valid_mcap = market_cap.dropna()
        # 取交集
        valid_stocks = valid_alpha.index.intersection(valid_mcap.index)
        mcap_filter = market_cap.loc[valid_stocks] >= self.config.min_market_cap
        valid_stocks = valid_stocks[mcap_filter]

        # alpha值过滤（删除极端值）
        alpha_clean = alpha_scores.loc[valid_stocks]
        if len(alpha_clean)==0:
            return pd.Series(dtype=float), pd.Index([])
        alpha_q01 = alpha_clean.quantile(0.01)
        alpha_q99 = alpha_clean.quantile(0.99)
        alpha_clean = alpha_clean.clip(lower=alpha_q01,upper=alpha_q99)
        return alpha_clean,valid_stocks

    def _calculate_weights(self, alpha_scores:pd.Series, alpha_rank: pd.Series, selected_stocks: pd.Index) -> pd.Series:
        if self.config.weight_method == "equal":
            n_stocks = len(selected_stocks)
            return pd.Series(1.0/n_stocks, index=selected_stocks)
        elif self.config.weight_method == "factor_score":
            selected_scores = alpha_scores.loc[selected_stocks]
            # 确保得分为正值
            if selected_scores.min() < 0:
                adjusted_scores = selected_scores - selected_scores.min() + 1e-6
            else:
                adjusted_scores = selected_scores.copy()
            # 按因子得分分配权重
            weights = adjusted_scores / adjusted_scores.sum()
            return weights


    def _apply_constraints(self, alpha_scores: pd.Series):
        # 应用投资约束
        alpha_rank = alpha_scores.rank(pct=True)
        long_signals = alpha_rank
        if not long_signals.any():
            return pd.Series(dtype=float)
        n_stocks = min(self.config.max_stocks, long_signals.sum())
        top_stocks = alpha_rank.nlargest(n_stocks).index
        # 根据配置选择权重分配方法
        initial_weights = self._calculate_weights(alpha_scores,alpha_rank,top_stocks)
        # 应用单股权重限制
        max_weight = self.config.max_position_size
        constrained_weights = initial_weights.clip(upper=max_weight)
        # 重新标准化
        constrained_weights = constrained_weights / constrained_weights.sum()
        return constrained_weights


    def construct_portfolio(self,
                            alpha_scores:pd.Series,
                            market_cap:pd.Series,
                            ) -> pd.Series:
        alpha_clean,valid_data = self._clean_data(alpha_scores, market_cap)
        if len(valid_data)==0:
            return pd.Series(dtype=float)
        mcap_clean = market_cap.reindex(valid_data)
        # 应用约束条件
        weights = self._apply_constraints(alpha_clean)
        return weights
