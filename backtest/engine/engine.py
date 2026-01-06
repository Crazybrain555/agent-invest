import logging
from typing import List, Union
import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm
pd.set_option('future.no_silent_downcasting', True)

class VectorizedEngine:
    """高性能向量化计算引擎 - 支持WorldQuant风格因子函数"""

    @staticmethod
    def rank(data: pd.DataFrame, pct: bool = True) -> pd.DataFrame:
        """横截面排名 - 类似WorldQuant的rank函数"""
        if pct:
            return data.rank(axis=1, pct=True)
        return data.rank(axis=1)

    @staticmethod
    def delay(data: pd.DataFrame, periods: int) -> pd.DataFrame:
        """时间序列延迟 - 类似WorldQuant的delay函数"""
        return data.shift(periods)

    @staticmethod
    def delta(data: pd.DataFrame, periods: int) -> pd.DataFrame:
        """时间序列差分 - 类似WorldQuant的delta函数"""
        return data.diff(periods)

    @staticmethod
    def ts_delta(data: pd.DataFrame, periods: int) -> pd.DataFrame:
        """时间序列差分 - delta的别名"""
        return VectorizedEngine.delta(data, periods)

    @staticmethod
    def ts_rank(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动排名"""
        return data.rolling(window=window).rank(pct=True)

    @staticmethod
    def ts_mean(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列移动平均"""
        return data.rolling(window=window).mean()

    @staticmethod
    def ts_std(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列移动标准差"""
        return data.rolling(window=window).std()

    @staticmethod
    def ts_sum(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动求和"""
        return data.rolling(window=window).sum()

    @staticmethod
    def ts_prod(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动乘积"""
        return data.rolling(window=window).apply(lambda x: np.prod(x), raw=True)

    @staticmethod
    def ts_min(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动最小值"""
        return data.rolling(window=window).min()

    @staticmethod
    def ts_max(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动最大值"""
        return data.rolling(window=window).max()

    @staticmethod
    def ts_argmin(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动最小值位置"""
        return data.rolling(window=window).apply(lambda x: np.argmin(x) + 1, raw=True)

    @staticmethod
    def ts_argmax(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动最大值位置"""
        return data.rolling(window=window).apply(lambda x: np.argmax(x) + 1, raw=True)

    @staticmethod
    def ts_zscore(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动标准分数（Z-score）"""
        rolling_mean = data.rolling(window=window).mean()
        rolling_std = data.rolling(window=window).std()
        return (data - rolling_mean) / rolling_std

    @staticmethod
    def ts_skew(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动偏度"""
        return data.rolling(window=window).skew()

    @staticmethod
    def ts_kurt(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动峰度"""
        return data.rolling(window=window).kurt()

    @staticmethod
    def ts_returns(data: pd.DataFrame, periods: int) -> pd.DataFrame:
        """时间序列收益率计算"""
        return data.pct_change(periods)

    @staticmethod
    def decay_linear(data: pd.DataFrame, window: int) -> pd.DataFrame:
        """线性衰减加权移动平均"""

        def linear_decay_weights(n):
            weights = np.arange(1, n + 1)
            return weights / weights.sum()

        weights = linear_decay_weights(window)
        result = data.rolling(window=window).apply(
            lambda x: np.dot(x, weights), raw=True
        )
        return result

    @staticmethod
    def decay_exp(data: pd.DataFrame, alpha: float) -> pd.DataFrame:
        """指数衰减加权移动平均"""
        return data.ewm(alpha=alpha).mean()

    @staticmethod
    def correlation(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
        """滚动相关性计算"""
        result = pd.DataFrame(index=x.index, columns=x.columns)
        for col in x.columns:
            if col in y.columns:
                result[col] = x[col].rolling(window).corr(y[col])
        return result

    @staticmethod
    def ts_corr(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动相关性 - correlation的别名"""
        return VectorizedEngine.correlation(x, y, window)

    @staticmethod
    def covariance(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
        """滚动协方差计算"""
        result = pd.DataFrame(index=x.index, columns=x.columns)
        for col in x.columns:
            if col in y.columns:
                result[col] = x[col].rolling(window).cov(y[col])
        return result

    @staticmethod
    def ts_cov(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列滚动协方差 - covariance的别名"""
        return VectorizedEngine.covariance(x, y, window)

    @staticmethod
    def ts_co_kurtosis(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列协峰度计算

        协峰度衡量两个变量同时经历极端值的趋势
        """

        def co_kurtosis(x_vals, y_vals):
            if len(x_vals) < 4 or len(y_vals) < 4:
                return np.nan

            # 标准化数据
            x_std = (x_vals - np.mean(x_vals)) / np.std(x_vals)
            y_std = (y_vals - np.mean(y_vals)) / np.std(y_vals)

            # 计算协峰度 E[(X-μx)(Y-μy)]^2 / (σx*σy)^2
            co_kurt = np.mean((x_std * y_std) ** 2)
            return co_kurt

        result = pd.DataFrame(index=x.index, columns=x.columns)
        for col in x.columns:
            if col in y.columns:
                result[col] = x[col].rolling(window).apply(
                    lambda vals: co_kurtosis(vals.values,
                                             y[col].rolling(window).apply(lambda v: v.values)[-len(vals):]),
                    raw=False
                )
        return result

    @staticmethod
    def ts_co_skewness(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
        """时间序列协偏度计算

        协偏度衡量两个变量偏离正态分布的程度
        """

        def co_skewness(x_vals, y_vals):
            if len(x_vals) < 3 or len(y_vals) < 3:
                return np.nan

            try:
                # 标准化数据
                x_std = (x_vals - np.mean(x_vals)) / (np.std(x_vals) + 1e-8)
                y_std = (y_vals - np.mean(y_vals)) / (np.std(y_vals) + 1e-8)

                # 计算协偏度 E[(X-μx)(Y-μy)^2] / (σx*σy^2)
                co_skew = np.mean(x_std * (y_std ** 2))
                return co_skew
            except:
                return np.nan

        result = pd.DataFrame(index=x.index, columns=x.columns)
        for col in x.columns:
            if col in y.columns:
                for i in range(window - 1, len(x)):
                    x_window = x[col].iloc[i - window + 1:i + 1]
                    y_window = y[col].iloc[i - window + 1:i + 1]
                    result.iloc[i, result.columns.get_loc(col)] = co_skewness(x_window.values, y_window.values)
        return result

    @staticmethod
    def abs(data: pd.DataFrame) -> pd.DataFrame:
        """绝对值"""
        return data.abs()

    @staticmethod
    def sign(data: pd.DataFrame) -> pd.DataFrame:
        """符号函数"""
        return np.sign(data)

    @staticmethod
    def log(data: pd.DataFrame) -> pd.DataFrame:
        """自然对数"""
        return np.log(data.replace(0, np.nan))

    @staticmethod
    def sqrt(data: pd.DataFrame) -> pd.DataFrame:
        """平方根"""
        return np.sqrt(data.abs())

    @staticmethod
    def power(data: pd.DataFrame, exp: float) -> pd.DataFrame:
        """幂函数"""
        return np.power(data, exp)

    @staticmethod
    def scale(data: pd.DataFrame, method: str = 'sum') -> pd.DataFrame:
        """横截面标准化

        Args:
            method: 'sum' - 使和为1, 'std' - 标准化到均值0方差1
        """
        if method == 'sum':
            return data.div(data.abs().sum(axis=1), axis=0)
        elif method == 'std':
            return data.sub(data.mean(axis=1), axis=0).div(data.std(axis=1), axis=0)
        else:
            return data

    @staticmethod
    def condition(cond: pd.DataFrame, value_if_true: Union[pd.DataFrame, float],
                  value_if_false: Union[pd.DataFrame, float]) -> pd.DataFrame:
        """条件函数 - 实现三元运算符 condition ? value1 : value2"""
        return pd.DataFrame(
            np.where(cond, value_if_true, value_if_false),
            index=cond.index,
            columns=cond.columns
        )

    @staticmethod
    def greater_than(x: pd.DataFrame, y: Union[pd.DataFrame, float]) -> pd.DataFrame:
        """大于比较"""
        return x > y

    @staticmethod
    def less_than(x: pd.DataFrame, y: Union[pd.DataFrame, float]) -> pd.DataFrame:
        """小于比较"""
        return x < y

    @staticmethod
    def greater_equal(x: pd.DataFrame, y: Union[pd.DataFrame, float]) -> pd.DataFrame:
        """大于等于比较"""
        return x >= y

    @staticmethod
    def less_equal(x: pd.DataFrame, y: Union[pd.DataFrame, float]) -> pd.DataFrame:
        """小于等于比较"""
        return x <= y


# 为了方便使用，创建引擎实例
engine = VectorizedEngine()