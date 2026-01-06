import numpy as np
import pandas as pd


class FactorReturnCalculator:
    """
    计算因子收益率
    - 因子收益率是指，在控制了其他风险因子后，单位因子暴露所能带来的股票收益率
    - 通过回归法计算，回归方程：R_i = a + b * F_i + e_i
    - R_i: 股票i的收益率
    - F_i: 股票i的因子值
    - b: 因子收益率
    """

    def __init__(self, period: int = 1, calculation_frequency: int = 1):
        """
        Args:
            period: 计算未来收益率的周期（天数）
            calculation_frequency: 计算因子收益率的频率（天数），例如，每N天计算一次
        """
        if period < 1:
            raise ValueError("period必须大于等于1")
        if calculation_frequency < 1:
            raise ValueError("calculation_frequency必须大于等于1")

        self.period = period
        self.calculation_frequency = calculation_frequency

    def calculate_forward_returns(self, prices: pd.DataFrame) -> pd.DataFrame:
        """计算未来n天的收益率"""
        return prices.pct_change(self.period,fill_method=None).shift(-self.period)

    def calculate_factor_returns(self,
                                 factor_data: pd.DataFrame,
                                 price_data: pd.DataFrame,
                                 ) -> dict:
        """
        计算每个时间点的因子收益率，并返回统计指标

        Args:
            factor_data: 因子暴露 (日期 x 股票)
            price_data: 价格数据 (日期 x 股票)

        Returns:
            包含因子收益率均值、标准差、T值等统计指标的字典
        """

        # 1. 计算未来收益率
        forward_returns = self.calculate_forward_returns(price_data)

        # 2. 对齐数据
        aligned_factor, aligned_returns = factor_data.align(forward_returns, join='inner', axis=0)

        if aligned_factor.empty or aligned_returns.empty:
            return {
                'factor_return_series': pd.Series(dtype=float),
                'factor_return_total': 0,
                'factor_return_mean': 0,
                'factor_return_std': 0,
                'factor_return_t_stat': 0
            }

        # 3. 根据频率选择计算日期
        if self.calculation_frequency > 1:
            dates_to_calculate = aligned_factor.index[::self.calculation_frequency]
            calc_factor_data = aligned_factor.loc[dates_to_calculate]
        else:
            calc_factor_data = aligned_factor

        if calc_factor_data.empty:
            return {
                'factor_return_series': pd.Series(dtype=float),
                'factor_return_total': 0,
                'factor_return_mean': 0,
                'factor_return_std': 0,
                'factor_return_t_stat': 0
            }

        # 4. 在选定日期计算截面回归
        factor_returns = calc_factor_data.apply(
            lambda x: self._cross_sectional_regression(x, aligned_returns.loc[x.name]),
            axis=1
        )

        # 5. 计算统计指标
        total_return = factor_returns.sum()
        mean_return = factor_returns.mean()
        std_return = factor_returns.std()

        # 检查factor_returns的长度，避免除以零
        n_obs = len(factor_returns)
        if n_obs > 0 and std_return > 0:
            t_stat = mean_return / (std_return / np.sqrt(n_obs))
        else:
            t_stat = 0

        return {
            'factor_return_series': factor_returns,
            'factor_return_total': total_return,
            'factor_return_mean': mean_return,
            'factor_return_std': std_return,
            'factor_return_t_stat': t_stat
        }

    def _cross_sectional_regression(self, factor_exposure: pd.Series, returns: pd.Series) -> float:
        """
        执行截面回归

        Args:
            factor_exposure: 单个时间点的因子暴露
            returns: 单个时间点的未来收益率

        Returns:
            因子收益率 (回归系数)
        """
        df = pd.DataFrame({'factor': factor_exposure, 'return': returns}).dropna()

        if len(df) < 2:
            return 0.0

        # OLS回归： return = a + b * factor
        # 我们只需要b
        factor = df['factor'].values
        ret = df['return'].values

        # 使用更稳健的回归方法
        X = np.vstack([np.ones(len(factor)), factor]).T

        try:
            # 使用最小二乘法求解
            result = np.linalg.lstsq(X, ret, rcond=None)[0]
            slope = result[1]  # 斜率即为因子收益率
        except (np.linalg.LinAlgError, IndexError):
            slope = 0.0

        return slope