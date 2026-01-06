import pandas as pd
import numpy as np
from typing import Dict, List, Literal, Optional, Tuple, Union, Any
import logging
from datetime import datetime, timedelta
from tqdm import tqdm
import numba
from numba import njit, prange
import multiprocessing
import torch

logger = logging.getLogger(__name__)

# 设置 Numba 线程数
numba.set_num_threads(multiprocessing.cpu_count())

class CorrelationCalculator:
    """
    相关性计算器类，提供静态和滚动相关性计算功能。
    
    支持Pearson和Spearman相关性计算，可以使用CPU或GPU加速。
    """
    
    def __init__(
        self,
        data_df: pd.DataFrame,
        correlation_type: Literal["pearson", "spearman"] = "pearson",
        use_torch: bool = False,
        device: str = 'cuda',
        batch_size: int = 32
    ):
        """
        初始化相关性计算器。
        
        Args:
            data_df: 输入数据DataFrame (index=date, columns=stock_code)
            correlation_type: 相关性类型 ("pearson" 或 "spearman")
            use_torch: 是否使用GPU加速
            device: 计算设备 ('cuda' 或 'cpu')
            batch_size: GPU批量处理大小
        """
        self.data_df = data_df
        self.correlation_type = correlation_type
        self.use_torch = use_torch
        self.device = device
        self.batch_size = batch_size
        
        # 检查GPU可用性
        if use_torch and device == 'cuda' and not torch.cuda.is_available():
            logger.warning("CUDA not available, falling back to CPU")
            self.device = 'cpu'
    
    def calculate_for_date(
        self,
        date: pd.Timestamp,
        window: int,
        min_periods: int = 5
    ) -> Optional[pd.DataFrame]:
        """
        计算指定日期的相关性矩阵。
        
        Args:
            date: 目标日期
            window: 窗口大小
            min_periods: 最小有效观测数
            
        Returns:
            相关性矩阵DataFrame，如果计算失败则返回None
        """
        # 使用target_dates参数调用rolling函数
        result = calculate_rolling_correlation(
            self.data_df,
            window=window,
            min_periods=min_periods,
            correlation_type=self.correlation_type,
            use_torch=self.use_torch,
            batch_size=self.batch_size,
            device=self.device,
            target_dates=[date]
        )
        
        # 返回指定日期的相关性矩阵
        return result.get(date)
    
    def calculate_rolling(
        self,
        window: int,
        min_periods: int = 5,
        target_dates: Optional[List[pd.Timestamp]] = None
    ) -> Dict[pd.Timestamp, pd.DataFrame]:
        """
        计算滚动相关性矩阵。
        
        Args:
            window: 窗口大小
            min_periods: 最小有效观测数
            target_dates: 目标日期列表，如果为None则计算所有可能的日期
            
        Returns:
            日期到相关性矩阵的映射字典
        """
        return calculate_rolling_correlation(
            self.data_df,
            window=window,
            min_periods=min_periods,
            correlation_type=self.correlation_type,
            use_torch=self.use_torch,
            batch_size=self.batch_size,
            device=self.device,
            target_dates=target_dates
        )

@njit(parallel=True)
def _spearman_correlation_matrix(data, min_periods=5):
    """
    Numba-accelerated Spearman correlation matrix calculation.
    
    Args:
        data: 2D numpy array of shape (n_periods, n_stocks)
        min_periods: Minimum number of observations required
        
    Returns:
        2D numpy array of shape (n_stocks, n_stocks) containing correlation matrix
    """
    n_periods, n_stocks = data.shape
    result = np.full((n_stocks, n_stocks), np.nan)
    
    # For each pair of stocks
    for i in prange(n_stocks):
        for j in range(i, n_stocks):
            # Get data for both stocks
            x = data[:, i]
            y = data[:, j]
            
            # Check for sufficient non-NaN values
            valid_mask = ~(np.isnan(x) | np.isnan(y))
            valid_count = np.sum(valid_mask)
            
            if valid_count >= min_periods:
                # Get valid data
                x_valid = x[valid_mask]
                y_valid = y[valid_mask]
                
                # Calculate ranks
                x_ranks = np.argsort(np.argsort(x_valid))
                y_ranks = np.argsort(np.argsort(y_valid))
                
                # Calculate correlation
                x_mean = np.mean(x_ranks)
                y_mean = np.mean(y_ranks)
                
                numerator = np.sum((x_ranks - x_mean) * (y_ranks - y_mean))
                x_std = np.sqrt(np.sum((x_ranks - x_mean) ** 2))
                y_std = np.sqrt(np.sum((y_ranks - y_mean) ** 2))
                
                if x_std > 0 and y_std > 0:
                    corr = numerator / (x_std * y_std)
                    result[i, j] = corr
                    result[j, i] = corr  # Symmetric
                else:
                    result[i, j] = np.nan
                    result[j, i] = np.nan
            else:
                result[i, j] = np.nan
                result[j, i] = np.nan
    
    return result

@njit(parallel=True)
def _pearson_correlation_matrix(data, min_periods=5):
    """
    Numba-accelerated Pearson correlation matrix calculation.
    
    Args:
        data: 2D numpy array of shape (n_periods, n_stocks)
        min_periods: Minimum number of observations required
        
    Returns:
        2D numpy array of shape (n_stocks, n_stocks) containing correlation matrix
    """
    n_periods, n_stocks = data.shape
    result = np.full((n_stocks, n_stocks), np.nan)
    
    # For each pair of stocks
    for i in prange(n_stocks):
        for j in range(i, n_stocks):
            # Get data for both stocks
            x = data[:, i]
            y = data[:, j]
            
            # Check for sufficient non-NaN values
            valid_mask = ~(np.isnan(x) | np.isnan(y))
            valid_count = np.sum(valid_mask)
            
            if valid_count >= min_periods:
                # Get valid data
                x_valid = x[valid_mask]
                y_valid = y[valid_mask]
                
                # Calculate means
                x_mean = np.mean(x_valid)
                y_mean = np.mean(y_valid)
                
                # Calculate correlation
                numerator = np.sum((x_valid - x_mean) * (y_valid - y_mean))
                x_std = np.sqrt(np.sum((x_valid - x_mean) ** 2))
                y_std = np.sqrt(np.sum((y_valid - y_mean) ** 2))
                
                if x_std > 0 and y_std > 0:
                    corr = numerator / (x_std * y_std)
                    result[i, j] = corr
                    result[j, i] = corr  # Symmetric
                else:
                    result[i, j] = np.nan
                    result[j, i] = np.nan
            else:
                result[i, j] = np.nan
                result[j, i] = np.nan
    
    return result

def torch_nanstd(tensor, dim=None, keepdim=False, unbiased=True):
    """
    计算忽略NaN的标准差，兼容PyTorch张量。
    
    Args:
        tensor: 输入张量
        dim: 计算维度
        keepdim: 是否保持维度
        unbiased: 是否使用无偏估计 (n-1)，与Pandas保持一致
    """
    mask = ~torch.isnan(tensor)
    count = mask.sum(dim=dim, keepdim=True)
    
    # 使用无偏估计 (n-1)
    if unbiased:
        count = count - 1
        count = count.clamp_min(1)  # 避免除以0
    
    mean = torch.nanmean(tensor, dim=dim, keepdim=True)
    squared_diff = (tensor - mean) ** 2
    squared_diff[~mask] = 0
    var = squared_diff.sum(dim=dim, keepdim=keepdim) / count
    std = torch.sqrt(var)
    
    if not keepdim and dim is not None:
        std = std.squeeze(dim)
    return std

def calculate_rolling_correlation_torch(
    returns_df: pd.DataFrame,
    window: int,
    min_periods: int = 5,
    correlation_type: Literal["pearson", "spearman"] = "pearson",
    batch_size: int = 32,
    device: str = 'cuda',
    target_dates: Optional[List[pd.Timestamp]] = None
) -> Dict[pd.Timestamp, pd.DataFrame]:
    """
    GPU加速的rolling相关性计算，支持Pearson和Spearman相关性。
    
    Args:
        returns_df: DataFrame with daily returns (index=date, columns=stock_code)
        window: Rolling window size in days
        min_periods: Minimum number of observations required for correlation calculation
        correlation_type: Type of correlation to calculate ("pearson" or "spearman")
        batch_size: GPU批量处理窗口数
        device: 'cuda' or 'cpu'
        target_dates: 只计算这些目标日期的rolling correlation（每个target_date都需要前window天数据）
        
    Returns:
        Dictionary mapping dates to correlation matrices
    """
    # 检查是否有GPU可用
    if device == 'cuda' and not torch.cuda.is_available():
        logger.warning("CUDA not available, falling back to CPU")
        device = 'cpu'
    
    # 转换为torch tensor，使用float64以匹配Pandas精度
    arr = torch.tensor(returns_df.values, dtype=torch.float64, device=device)
    n_dates, n_stocks = arr.shape
    dates = returns_df.index
    stock_codes = returns_df.columns.tolist()
    correlation_matrices = {}
    
    # 确定要计算的日期索引
    if target_dates is not None:
        # 确保 target_dates 都在 returns_df.index 里，且每个日期前有足够的窗口数据
        target_indices = [dates.get_loc(date) for date in target_dates 
                         if date in dates and dates.get_loc(date) >= window]
    else:
        target_indices = list(range(window, n_dates))
    
    # 如果没有有效的目标日期，直接返回空字典
    if not target_indices:
        return {}
    
    # 批量处理
    for batch_start in tqdm(range(0, len(target_indices), batch_size), 
                           desc=f"GPU Rolling {correlation_type.capitalize()}"):
        batch_indices = target_indices[batch_start:batch_start+batch_size]
        
        # 提取这批次所有日期的窗口数据
        batch_windows = []
        for idx in batch_indices:
            batch_windows.append(arr[idx-window:idx])
        
        # 对每个日期计算相关性矩阵
        for b, idx in enumerate(batch_indices):
            # 获取当前窗口数据
            window_data = batch_windows[b]  # shape: (window, n_stocks)
            
            # 计算每只股票的有效观测数 (非NaN值的数量)
            valid_counts = (~torch.isnan(window_data)).sum(dim=0)  # shape: (n_stocks,)
            
            # 找出有效观测数足够的股票
            valid_stocks = valid_counts >= min_periods
            
            # 如果没有足够的有效股票，跳过这个日期
            if valid_stocks.sum() < min_periods:
                continue
            
            # 提取有效股票的数据
            valid_indices = torch.where(valid_stocks)[0]
            valid_data = window_data[:, valid_stocks]
            
            # 计算相关性矩阵
            if valid_stocks.sum() == 1:
                # 只有一只股票时，直接设置为1
                valid_corr = torch.ones(1, 1, device=device)
            else:
                if correlation_type == "pearson":
                    # 标准化数据 (z-score)，使用无偏估计
                    valid_mean = torch.nanmean(valid_data, dim=0, keepdim=True)
                    valid_std = torch_nanstd(valid_data, dim=0, keepdim=True, unbiased=True)
                    valid_std[valid_std == 0] = 1.0  # 避免除零错误
                    
                    # 使用广播标准化数据
                    normalized = (valid_data - valid_mean) / valid_std
                    normalized[torch.isnan(normalized)] = 0.0  # 将NaN替换为0
                    
                    # 计算相关性矩阵 - 使用无偏估计 (n-1)
                    n_valid = (~torch.isnan(valid_data)).float().sum(dim=0)
                    scaling = n_valid - 1  # 使用n-1作为分母
                    scaling[scaling < 1] = 1  # 避免除零
                    
                    # 矩阵乘法计算相关性
                    valid_corr = torch.mm(normalized.t(), normalized) / scaling
                
                else:  # spearman
                    # 初始化排名矩阵
                    ranks = torch.zeros_like(valid_data)
                    
                    # 为每列分别计算排名
                    for i in range(valid_data.shape[1]):
                        col = valid_data[:, i]
                        mask = ~torch.isnan(col)
                        if mask.sum() > 0:
                            # 获取非NaN值
                            col_valid = col[mask]
                            # 计算排名 (排序后再排序得到排名)
                            rank_values = col_valid.argsort().argsort().float()
                            # 将排名放回原位置
                            ranks[mask, i] = rank_values
                    
                    # 使用Pearson相关性计算排名之间的相关性
                    rank_mean = torch.nanmean(ranks, dim=0, keepdim=True)
                    rank_std = torch.nanstd(ranks, dim=0, keepdim=True, unbiased=True)
                    rank_std[rank_std == 0] = 1.0
                    
                    normalized_ranks = (ranks - rank_mean) / rank_std
                    normalized_ranks[torch.isnan(normalized_ranks)] = 0.0
                    
                    n_valid = (~torch.isnan(ranks)).float().sum(dim=0)
                    scaling = n_valid - 1  # 使用n-1作为分母
                    scaling[scaling < 1] = 1
                    
                    valid_corr = torch.mm(normalized_ranks.t(), normalized_ranks) / scaling
            
            # 创建结果DataFrame
            # 先创建全NaN矩阵
            corr_matrix = np.full((n_stocks, n_stocks), np.nan)
            
            # 只填充有效值 - 使用numpy高效操作而不是循环
            valid_idx = valid_indices.cpu().numpy()
            valid_corr_np = valid_corr.cpu().numpy()
            
            # 使用numpy的索引操作填充相关性矩阵
            for i, ii in enumerate(valid_idx):
                corr_matrix[ii, valid_idx] = valid_corr_np[i, :]
            
            # 转换为DataFrame
            corr_df = pd.DataFrame(
                corr_matrix,
                index=stock_codes,
                columns=stock_codes
            )
            
            # 添加到结果字典
            correlation_matrices[dates[idx]] = corr_df
    
    return correlation_matrices

def calculate_rolling_correlation(
    returns_df: pd.DataFrame,
    window: int,
    min_periods: int = 5,
    correlation_type: Literal["pearson", "spearman"] = "pearson",
    use_torch: bool = False,
    batch_size: int = 32,
    device: str = 'cuda',
    target_dates: Optional[List[pd.Timestamp]] = None
) -> Dict[pd.Timestamp, pd.DataFrame]:
    """
    Calculate rolling correlation matrices for each date or for specified target_dates.
    Args:
        returns_df: DataFrame with daily returns (index=date, columns=stock_code)
        window: Rolling window size in days
        min_periods: Minimum number of observations required for correlation calculation
        correlation_type: Type of correlation to calculate ("pearson" or "spearman")
        use_torch: 是否用GPU加速
        batch_size: GPU批量处理窗口数
        device: 'cuda' or 'cpu'
        target_dates: 只计算这些目标日期的rolling correlation（每个target_date都需要前window天数据）
    Returns:
        Dictionary mapping dates to correlation matrices
    """
    if use_torch:
        return calculate_rolling_correlation_torch(
            returns_df, window, min_periods, correlation_type, batch_size, device, target_dates
        )
    # --- 原有 Numba CPU 版本 ---
    if window <= 0:
        raise ValueError("Window must be positive")
    correlation_matrices = {}
    dates = returns_df.index
    returns_array = returns_df.values
    stock_codes = returns_df.columns.tolist()

    # 只计算 target_dates
    if target_dates is not None:
        # 确保 target_dates 都在 returns_df.index 里
        target_indices = [dates.get_loc(date) for date in target_dates if date in dates and dates.get_loc(date) >= window]
    else:
        target_indices = range(window, len(dates))

    for i in tqdm(target_indices, desc=f"Calculating {correlation_type} correlations"):
        window_returns = returns_array[i-window:i]
        if correlation_type == "pearson":
            corr_matrix_array = _pearson_correlation_matrix(window_returns, min_periods)
        else:  # spearman
            corr_matrix_array = _spearman_correlation_matrix(window_returns, min_periods)
        corr_matrix = pd.DataFrame(
            corr_matrix_array, 
            index=stock_codes, 
            columns=stock_codes
        )
        correlation_matrices[dates[i]] = corr_matrix
    return correlation_matrices

# 为了向后兼容，保留原来的函数名
def calculate_rolling_spearman_correlation(
    returns_df: pd.DataFrame,
    window: int,
    min_periods: int = 5,
    use_torch: bool = False,
    batch_size: int = 32,
    device: str = 'cuda'
) -> Dict[pd.Timestamp, pd.DataFrame]:
    """
    Calculate rolling Spearman correlation matrices for each date.
    This is a wrapper for calculate_rolling_correlation with correlation_type="spearman".
    """
    return calculate_rolling_correlation(
        returns_df, window, min_periods, "spearman", use_torch, batch_size, device
    )

def find_correlated_neighbors(
    correlation_matrix: pd.DataFrame,
    target_stock: str,
    rank_num: int,
    use_rank: bool = True
) -> List[str]:
    """
    Find the top correlated neighbors for a target stock.
    
    Args:
        correlation_matrix: Correlation matrix (stocks x stocks)
        target_stock: Target stock code
        rank_num: Number of top neighbors to return
        use_rank: If True, use ranking method (like original code)
        
    Returns:
        List of stock codes for the top correlated neighbors
    """
    if target_stock not in correlation_matrix.index:
        raise ValueError(f"Target stock {target_stock} not found in correlation matrix")
    
    # Get correlations for the target stock
    stock_correlations = correlation_matrix.loc[target_stock].copy()
    
    
    if use_rank:
        # 使用排序方法（原代码的方式）
        # 对相关性进行排序（降序）
        # 使用 fillna(1e8) 而不是 dropna()
        ranks = stock_correlations.rank( axis=0,ascending=False, method='dense').fillna(1e8)
        # 选择排序值小于等于rank_num的股票
        top_neighbors = ranks[ranks <= rank_num].index.tolist()
    else:
        # Remove the target stock itself
        stock_correlations = stock_correlations.drop(target_stock)
        # 直接选择相关性最高的N只股票
        top_neighbors = stock_correlations.nlargest(rank_num).index.tolist()
    
    return top_neighbors 