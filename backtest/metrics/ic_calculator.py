"""
IC (Information Coefficient) 计算模块
用于计算因子与未来收益率之间的相关性
"""

import warnings
from typing import Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd
from scipy import stats


class ICCalculator:
    """信息系数(IC)计算器 - 性能优化版本"""

    def __init__(self, ic_period: int = 20, ic_method: str = "spearman", min_stocks: int = 10):
        """
        初始化IC计算器

        Args:
            ic_period: IC计算周期，默认20天
            ic_method: IC计算方法，支持'spearman'和'pearson'
            min_stocks: 计算IC所需的最少股票数量
        """
        self.ic_period = ic_period
        self.ic_method = ic_method.lower()
        self.min_stocks = min_stocks

        if self.ic_method not in ['spearman', 'pearson']:
            raise ValueError("ic_method 必须是 'spearman' 或 'pearson'")

        # 性能优化：缓存计算结果
        self._cached_forward_returns = None
        self._cached_price_data_hash = None

    def calculate_forward_returns(self, price_data: pd.DataFrame) -> pd.DataFrame:
        """
        计算未来收益率 - 带缓存优化

        Args:
            price_data: 价格数据，index为日期，columns为股票代码

        Returns:
            未来收益率数据
        """
        # 性能优化：使用简单的哈希检查是否需要重新计算
        current_hash = hash(str(price_data.shape) + str(price_data.index[0]) + str(price_data.index[-1]))

        if (self._cached_forward_returns is not None and
            self._cached_price_data_hash == current_hash):
            return self._cached_forward_returns

        # 计算未来n天收益率
        forward_returns = price_data.pct_change(periods=self.ic_period, fill_method=None).shift(-self.ic_period)

        # 缓存结果
        self._cached_forward_returns = forward_returns
        self._cached_price_data_hash = current_hash

        return forward_returns

    def _calculate_correlation_vectorized(self, factor_values: np.ndarray, return_values: np.ndarray) -> float:
        """
        向量化计算相关系数

        Args:
            factor_values: 因子值数组
            return_values: 收益率数组

        Returns:
            相关系数
        """
        # 移除NaN值
        valid_mask = ~(np.isnan(factor_values) | np.isnan(return_values))
        if valid_mask.sum() < self.min_stocks:
            return np.nan

        factor_clean = factor_values[valid_mask]
        return_clean = return_values[valid_mask]

        # 检查标准差
        if np.std(factor_clean) < 1e-8 or np.std(return_clean) < 1e-8:
            return np.nan

        # 根据方法计算相关系数
        if self.ic_method == 'pearson':
            correlation = np.corrcoef(factor_clean, return_clean)[0, 1]
        else:  # spearman
            # 使用更快的ranking方法
            factor_rank = stats.rankdata(factor_clean)
            return_rank = stats.rankdata(return_clean)
            correlation = np.corrcoef(factor_rank, return_rank)[0, 1]

        return correlation if not np.isnan(correlation) else 0.0

    def calculate_ic_series(self, factor_data: pd.DataFrame, price_data: pd.DataFrame) -> pd.Series:
        """
        计算IC时间序列 (高度优化的向量化版本)

        Args:
            factor_data: 因子数据，index为日期，columns为股票代码
            price_data: 价格数据，index为日期，columns为股票代码

        Returns:
            IC时间序列
        """
        # 计算未来收益率
        forward_returns = self.calculate_forward_returns(price_data)

        # 确保因子数据和收益数据的index和columns对齐
        common_dates = factor_data.index.intersection(forward_returns.index)
        common_stocks = factor_data.columns.intersection(forward_returns.columns)

        if len(common_dates) == 0 or len(common_stocks) == 0:
            return pd.Series(dtype=float, name="ic")

        # 性能优化：直接使用numpy数组操作
        factor_aligned = factor_data.loc[common_dates, common_stocks].values
        returns_aligned = forward_returns.loc[common_dates, common_stocks].values

        # 向量化计算IC
        ic_values = []
        valid_dates = []

        for i, date in enumerate(common_dates):
            factor_row = factor_aligned[i]
            return_row = returns_aligned[i]

            # 快速检查有效数据点数量
            valid_mask = ~(np.isnan(factor_row) | np.isnan(return_row))
            if valid_mask.sum() < self.min_stocks:
                continue

            # 快速检查方差
            factor_valid = factor_row[valid_mask]
            return_valid = return_row[valid_mask]

            if np.std(factor_valid) < 1e-8 or np.std(return_valid) < 1e-8:
                continue

            # 计算相关系数
            if self.ic_method == 'pearson':
                correlation = np.corrcoef(factor_valid, return_valid)[0, 1]
            else:  # spearman
                factor_rank = stats.rankdata(factor_valid)
                return_rank = stats.rankdata(return_valid)
                correlation = np.corrcoef(factor_rank, return_rank)[0, 1]

            if not np.isnan(correlation):
                ic_values.append(correlation)
                valid_dates.append(date)

        # 创建结果Series
        if len(ic_values) == 0:
            return pd.Series(dtype=float, name="ic")

        ic_series = pd.Series(ic_values, index=valid_dates, name='ic')
        return ic_series

    def calculate_ic_metrics(self, ic_series: pd.Series) -> Dict[str, float]:
        """
        计算IC相关统计指标 - 优化版本

        Args:
            ic_series: IC时间序列

        Returns:
            IC统计指标字典
        """
        if ic_series.empty or len(ic_series) == 0:
            return {
                'mean_ic': 0.0,
                'ic_std': 0.0,
                'ic_ir': 0.0,
                'ic_hit_rate': 0.0,
                'ic_skewness': 0.0,
                'ic_kurtosis': 0.0,
                'ic_t_stat': 0.0,
                'ic_p_value': 1.0
            }

        # 性能优化：使用numpy直接计算，避免pandas开销
        ic_values = ic_series.values

        # 基础统计量
        mean_ic = np.mean(ic_values)
        ic_std = np.std(ic_values, ddof=1) if len(ic_values) > 1 else 0.0
        ic_ir = mean_ic / ic_std if ic_std > 0 else 0.0
        ic_hit_rate = np.mean(ic_values > 0)

        # 高阶统计量
        if len(ic_values) > 2:
            ic_skewness = stats.skew(ic_values)
            ic_kurtosis = stats.kurtosis(ic_values)
        else:
            ic_skewness = 0.0
            ic_kurtosis = 0.0

        # t检验
        if len(ic_values) > 1:
            t_stat, p_value = stats.ttest_1samp(ic_values, 0)
        else:
            t_stat, p_value = 0.0, 1.0

        return {
            'mean_ic': float(mean_ic),
            'ic_std': float(ic_std),
            'ic_ir': float(ic_ir),
            'ic_hit_rate': float(ic_hit_rate),
            'ic_skewness': float(ic_skewness),
            'ic_kurtosis': float(ic_kurtosis),
            'ic_t_stat': float(t_stat),
            'ic_p_value': float(p_value)
        }

    def calculate_ic_decay(self, factor_data: pd.DataFrame, price_data: pd.DataFrame,
                          max_period: int = 60) -> pd.DataFrame:
        """
        计算IC衰减曲线 - 优化版本

        Args:
            factor_data: 因子数据
            price_data: 价格数据
            max_period: 最大计算周期

        Returns:
            IC衰减曲线，index为周期，columns为IC统计量
        """
        periods = range(1, min(max_period + 1, 61))  # 最多计算60期
        decay_results = []

        # 保存原始设置
        original_period = self.ic_period

        # 性能优化：预先对齐数据，避免重复操作
        common_dates = factor_data.index.intersection(price_data.index)
        common_stocks = factor_data.columns.intersection(price_data.columns)

        if len(common_dates) == 0 or len(common_stocks) == 0:
            self.ic_period = original_period
            return pd.DataFrame()

        factor_aligned = factor_data.loc[common_dates, common_stocks]
        price_aligned = price_data.loc[common_dates, common_stocks]

        # 性能优化：批量计算多个周期的forward returns
        forward_returns_cache = {}
        for period in periods:
            try:
                forward_returns = price_aligned.pct_change(periods=period, fill_method=None).shift(-period)
                forward_returns_cache[period] = forward_returns
            except Exception:
                continue

        # 批量计算IC
        for period in periods:
            if period not in forward_returns_cache:
                continue

            try:
                forward_returns = forward_returns_cache[period]

                # 使用优化的向量化计算
                factor_values = factor_aligned.values
                return_values = forward_returns.values

                ic_values = []
                valid_dates = []

                for i, date in enumerate(common_dates):
                    factor_row = factor_values[i]
                    return_row = return_values[i]

                    # 快速过滤
                    valid_mask = ~(np.isnan(factor_row) | np.isnan(return_row))
                    if valid_mask.sum() < self.min_stocks:
                        continue

                    factor_valid = factor_row[valid_mask]
                    return_valid = return_row[valid_mask]

                    if np.std(factor_valid) < 1e-8 or np.std(return_valid) < 1e-8:
                        continue

                    # 计算相关系数
                    if self.ic_method == 'pearson':
                        correlation = np.corrcoef(factor_valid, return_valid)[0, 1]
                    else:
                        factor_rank = stats.rankdata(factor_valid)
                        return_rank = stats.rankdata(return_valid)
                        correlation = np.corrcoef(factor_rank, return_rank)[0, 1]

                    if not np.isnan(correlation):
                        ic_values.append(correlation)
                        valid_dates.append(date)

                if len(ic_values) > 0:
                    ic_series = pd.Series(ic_values, index=valid_dates)
                    ic_metrics = self.calculate_ic_metrics(ic_series)

                    result = {
                        'period': period,
                        'mean_ic': ic_metrics['mean_ic'],
                        'ic_std': ic_metrics['ic_std'],
                        'ic_ir': ic_metrics['ic_ir'],
                        'ic_hit_rate': ic_metrics['ic_hit_rate'],
                        'sample_size': len(ic_series)
                    }
                    decay_results.append(result)

            except Exception as e:
                warnings.warn(f"计算周期{period}的IC时出错: {str(e)}")
                continue

        # 恢复原始周期设置
        self.ic_period = original_period

        if not decay_results:
            return pd.DataFrame()

        return pd.DataFrame(decay_results).set_index('period')

    def analyze_ic_performance(self, factor_data: pd.DataFrame, price_data: pd.DataFrame) -> Dict[str, Any]:
        """
        全面分析IC表现 - 优化版本

        Args:
            factor_data: 因子数据
            price_data: 价格数据

        Returns:
            完整的IC分析结果
        """
        # 计算主要IC序列
        ic_series = self.calculate_ic_series(factor_data, price_data)
        ic_metrics = self.calculate_ic_metrics(ic_series)

        # 计算IC衰减
        ic_decay = self.calculate_ic_decay(factor_data, price_data)

        # 性能优化：使用numpy操作进行分组统计
        if not ic_series.empty:
            # 分月度统计
            monthly_periods = ic_series.index.to_period('M')
            monthly_ic = ic_series.groupby(monthly_periods).mean()

            # 年度统计
            yearly_periods = ic_series.index.year
            yearly_ic = ic_series.groupby(yearly_periods).mean()
        else:
            monthly_ic = pd.Series(dtype=float)
            yearly_ic = pd.Series(dtype=float)
        
        return {
            'ic_series': ic_series,
            'ic_metrics': ic_metrics,
            'ic_decay': ic_decay,
            'monthly_ic': monthly_ic,
            'yearly_ic': yearly_ic,
            'config': {
                'ic_period': self.ic_period,
                'ic_method': self.ic_method,
                'min_stocks': self.min_stocks
            }
        } 