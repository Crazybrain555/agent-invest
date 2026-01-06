import pandas as pd
import numpy as np
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union, Any, Literal
import logging
from datetime import datetime, timedelta
from tqdm import tqdm

from src.data_service.preprocessing.methods.correlation_utils import (
    find_correlated_neighbors,
    calculate_rolling_correlation,
    CorrelationCalculator
)
from src.data_service.preprocessing.methods.future_returns_utils import (
    calculate_future_returns, get_period_returns
)
from src.data_service.preprocessing.methods.dataframe_utils import label_to_long

logger = logging.getLogger(__name__)

class BaseLabelAdjuster(ABC):
    """Base class for label adjustment strategies."""
    
    @abstractmethod
    def adjust(
        self,
        label_raw_df: pd.DataFrame,
        **kwargs
    ) -> pd.DataFrame:
        """
        Adjust raw labels according to the specific strategy.
        
        Args:
            label_raw_df: DataFrame with raw labels (index=date, columns=stock_code)
            **kwargs: Additional parameters specific to the adjustment strategy
            
        Returns:
            DataFrame with adjusted labels (index=date, columns=stock_code)
        """
        pass

class LabelAdjuster:
    """Base class for label adjusters."""
    
    def adjust(self, data: pd.Series, **kwargs) -> float:
        """
        Adjust the label based on the input data.
        
        Args:
            data: Input data for label adjustment
            **kwargs: Additional parameters for adjustment
            
        Returns:
            Adjusted label value
        """
        raise NotImplementedError("Subclasses must implement adjust method")

class TopCorAdjLabelAdjuster(LabelAdjuster):
    """Label adjuster for top correlation strategy."""
    
    def __init__(
        self,
        correlation_matrices: Dict[pd.Timestamp, pd.DataFrame] = None,
        rank_num: int = 10,
        min_rank_num: int = 5,
        use_rank: bool = True,
        correlation_type: Literal["pearson", "spearman"] = "pearson",
        market_data_provider = None
    ):
        """
        Initialize the top correlation label adjuster.
        
        Args:
            correlation_matrices: Dictionary mapping dates to correlation matrices
            rank_num: Number of top correlated neighbors to use
            min_rank_num: Minimum number of neighbors required
            use_rank: If True, use ranking method  相关性排序时，通常自身相关性为1，排名第一，包含自身。
            correlation_type: Type of correlation to use ("pearson" or "spearman")
            market_data_provider: Provider for market data
        """
        self.correlation_matrices = correlation_matrices or {}
        self.rank_num = rank_num
        self.min_rank_num = min_rank_num
        self.use_rank = use_rank
        self.correlation_type = correlation_type
        self.market_data_provider = market_data_provider
        
        # Statistics for monitoring
        self.stats = {
            'insufficient_neighbors': 0,
            'zero_std': 0,
            'total_processed': 0
        }
    
    def adjust(
        self,
        label_raw_df: pd.DataFrame,
        **kwargs
    ) -> pd.DataFrame:
        """
        Adjust raw labels using top correlated neighbors.
        
        Args:
            label_raw_df: DataFrame with raw labels (index=date, columns=stock_code)
            **kwargs: Additional parameters (not used in this implementation)
            
        Returns:
            DataFrame with adjusted labels (index=date, columns=stock_code)
        """
        # Create a copy to avoid modifying the original
        df = label_raw_df.copy()
        
        # Initialize the result DataFrame with the same structure as label_raw_df
        label_adj_df = pd.DataFrame(index=df.index, columns=df.columns)
        
        # Process each date with progress bar
        for date in tqdm(df.index, desc="Adjusting labels"):
            # Skip if no correlation matrix for this date
            if date not in self.correlation_matrices:
                logger.warning(f"No correlation matrix for date {date}, skipping")
                continue
                
            # Get the correlation matrix for this date
            corr_matrix = self.correlation_matrices[date]
            
            # Process each stock
            for stock in df.columns:
                self.stats['total_processed'] += 1
                
                # Skip if no raw label for this stock on this date
                if pd.isna(df.loc[date, stock]):
                    continue
                    
                try:
                    # Find top correlated neighbors
                    neighbors = find_correlated_neighbors(
                        correlation_matrix=corr_matrix,
                        target_stock=stock,
                        rank_num=self.rank_num,
                        use_rank=self.use_rank
                    )
                    #通过neighbors是否在df.columns中，来进行筛选，不在的就去掉
                    neighbors = [n for n in neighbors if n in df.columns]
                    
                    # Check if we have enough neighbors
                    if len(neighbors) < self.min_rank_num:
                        self.stats['insufficient_neighbors'] += 1
                        # Use market statistics (all stocks with valid labels on this date)
                        valid_labels = df.loc[date].dropna()
                        if len(valid_labels) > 0:
                            mean = valid_labels.mean()
                            std = valid_labels.std()
                        else:
                            # If no valid labels, use 0 and 1
                            mean = 0
                            std = 1
                    else:
                        # Get neighbor labels
                        neighbor_labels = df.loc[date, neighbors].dropna()
                        
                        # Calculate statistics
                        mean = neighbor_labels.mean()
                        std = neighbor_labels.std()
                    
                    # Check for zero or very small standard deviation
                    if std < 1e-10:
                        self.stats['zero_std'] += 1
                        std = 1e-12  # Use 1e-12 to avoid division by zero
                    
                    # Calculate adjusted label
                    raw_label = df.loc[date, stock]
                    label_adj_df.loc[date, stock] = (raw_label - mean) / (std + 1e-12)  # Add 1e-12 to denominator like original code
                    
                except Exception as e:
                    logger.error(f"Error processing stock {stock} on date {date}: {str(e)}")
                    # Set to NaN if there's an error
                    label_adj_df.loc[date, stock] = np.nan
        
        # Log statistics
        logger.info(f"Label adjustment statistics: {self.stats}")
        
        return label_adj_df
    
    def generate_labels(
        self,
        start_date: str,
        end_date: str,
        label_shift: int,
        corr_window: int,
        corr_rank_num: int,
        min_rank_num: int,
        use_db_pct_change: bool = False,
        use_rank: bool = True,
        correlation_type: Literal["pearson", "spearman"] = "pearson",
        adjuster_params: Optional[Dict[str, Any]] = None
    ) -> pd.DataFrame:
        """
        Generate correlation-based labels for the specified date range.
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            label_shift: Number of days to look ahead for future returns
            corr_window: Window size for calculating rolling correlations
            corr_rank_num: Number of top correlated neighbors to use
            min_rank_num: Minimum number of neighbors required
            use_db_pct_change: Whether to use pre-calculated pct_change from database
            use_rank: If True, use ranking method (like original code)
            correlation_type: Type of correlation to use ("pearson" or "spearman")
            adjuster_params: Additional parameters for the label adjuster
            
        Returns:
            Long format DataFrame with columns: stock_code, trade_date, field_name, value, label_shift
        """
        logger.info(f"Generating correlation-based labels from {start_date} to {end_date} with shift={label_shift}")
        
        # 1. Determine the date range for calculations
        # Convert to datetime for calculations
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        # Calculate the lookback date (approximately corr_window trading days before start_date)
        lookback_days = corr_window*1.5 + 20  # Add buffer days
        lookback_dt = start_dt - pd.Timedelta(days=lookback_days)
        
        # Format dates for the calculations
        lookback_date = lookback_dt.strftime('%Y-%m-%d')
        
        # Choose method based on setting
        method = "pct_change_db" if use_db_pct_change else "adj_close"
        
        # 1. Calculate daily returns directly using utility function
        try:
            daily_returns = get_period_returns(
                price_df=None,
                period=1,
                method=method,
                market_data_provider=self.market_data_provider,
                start_date=lookback_date,
                end_date=end_date
            )
            logger.info(f"Calculated daily returns with shape {daily_returns.shape}")
        except Exception as e:
            logger.error(f"Error calculating daily returns: {str(e)}")
            raise
        
        # 2. Calculate rolling correlation matrices - OPTIMIZED to only calculate for required date range
        try:
            # Filter daily_returns to only include dates needed for correlation calculation
            # We need data from (start_dt - corr_window) to end_dt
            corr_start_dt = start_dt - pd.Timedelta(days=(corr_window*1.5+ 20))  # Add buffer
            filtered_returns = daily_returns.loc[corr_start_dt:end_dt]

            # Only calculate correlation matrices for the target date range else calculate range(window, len(dates))
            target_dates = [d for d in filtered_returns.index if start_dt <= d <= end_dt]
            
            # 使用CorrelationCalculator类计算相关性
            correlation_calculator = CorrelationCalculator(
                data_df=filtered_returns,
                correlation_type=correlation_type,
                use_torch=True
            )
            
            correlation_matrices = correlation_calculator.calculate_rolling(
                window=corr_window,
                min_periods=int(corr_window*0.8),
                target_dates=target_dates
            )
            
            logger.info(f"Calculated {len(correlation_matrices)} correlation matrices for date range {start_dt} to {end_dt}")
        except Exception as e:
            logger.error(f"Error calculating correlation matrices: {str(e)}")
            raise
        
        # 3. Calculate label_raw (future returns) directly using utility function
        try:
            label_raw_df = calculate_future_returns(
                price_df=None,
                shift=label_shift,
                method="vwap",
                market_data_provider=self.market_data_provider,
                start_date=start_date,
                end_date=end_date
            )
            
            logger.info(f"Calculated label_raw with shape {label_raw_df.shape}")
        except Exception as e:
            logger.error(f"Error calculating label_raw: {str(e)}")
            raise
        
        # 4. Calculate label_adj
        try:
            # Determine the use_rank value, prioritizing adjuster_params
            adj_params = adjuster_params or {}
            # Default to True if not provided in adjuster_params or generate_labels args
            actual_use_rank = adj_params.get('use_rank', use_rank) 

            # Create the adjuster using the determined use_rank
            adjuster = TopCorAdjLabelAdjuster(
                correlation_matrices=correlation_matrices,
                rank_num=corr_rank_num,
                min_rank_num=min_rank_num,
                use_rank=actual_use_rank, # Use the value from adjuster_params if available
                correlation_type=correlation_type,
                market_data_provider=self.market_data_provider
            )
            
            # Adjust the labels (passing adjuster_params might still be needed if other params are used)
            label_adj_df = adjuster.adjust(
                label_raw_df,
                **adj_params
            )
            
            logger.info(f"Calculated label_adj with shape {label_adj_df.shape}")
        except Exception as e:
            logger.error(f"Error calculating label_adj: {str(e)}")
            raise
        
        # 5. Combine label_raw and label_adj into a single wide DataFrame
        try:
            # Create a multi-index DataFrame with both labels
            combined_df = pd.DataFrame({
                f'label_raw': label_raw_df.stack(),
                f'tc_t{label_shift}_n{corr_rank_num}_adj': label_adj_df.stack()
            })
            
            # Reset index to get stock_code and trade_date as columns
            combined_df = combined_df.reset_index()
            
            # Rename columns
            combined_df.columns = ['trade_date', 'stock_code', 
                                 f'label_raw',
                                 f'tc_t{label_shift}_n{corr_rank_num}_adj']
            
            logger.info(f"Combined labels with shape {combined_df.shape}")
        except Exception as e:
            logger.error(f"Error combining labels: {str(e)}")
            raise
        
        # 6. Convert to long format
        try:
            df_long = label_to_long(combined_df, label_shift=label_shift)
            logger.info(f"Converted to long format with shape {df_long.shape}")
        except Exception as e:
            logger.error(f"Error converting to long format: {str(e)}")
            raise
        
        return df_long

class RankLabelAdjuster(LabelAdjuster):
    """Label adjuster for rank + z-score strategy."""
    
    def __init__(self, market_data_provider=None):
        """
        Initialize the rank label adjuster.
        
        Args:
            market_data_provider: Provider for market data
        """
        self.market_data_provider = market_data_provider
        
        # Statistics for monitoring
        self.stats = {
            'zero_std_dates': 0,
            'total_dates_processed': 0,
            'total_stocks_processed': 0
        }
    
    def adjust(
        self,
        label_raw_df: pd.DataFrame,
        ascending: bool = True,
        **kwargs
    ) -> pd.DataFrame:
        """
        Adjust raw labels using rank + z-score transformation.
        
        Process:
        1. For each date, rank all stocks by their raw label values
        2. Convert ranks to percentiles (0-1 range)
        3. Apply z-score normalization to percentiles
        
        Args:
            label_raw_df: DataFrame with raw labels (index=date, columns=stock_code)
            ascending: If True, smaller values get higher ranks (default: False for returns)
            **kwargs: Additional parameters (not used in this implementation)
            
        Returns:
            DataFrame with rank-zscore adjusted labels (index=date, columns=stock_code)
        """
        logger.info(f"Starting rank + z-score adjustment for {len(label_raw_df)} dates")
        
        # Create a copy to avoid modifying the original
        df = label_raw_df.copy()
        
        # Initialize the result DataFrame with the same structure as label_raw_df
        label_adj_df = pd.DataFrame(index=df.index, columns=df.columns)
        
        # Process each date with progress bar
        for date in tqdm(df.index, desc="Applying rank + z-score adjustment"):
            self.stats['total_dates_processed'] += 1
            
            # Get valid labels for this date (non-NaN values)
            date_labels = df.loc[date].dropna()
            
            if len(date_labels) == 0:
                logger.warning(f"No valid labels for date {date}, skipping")
                continue
            
            self.stats['total_stocks_processed'] += len(date_labels)
            
            try:
                # Step 1: Rank within the date (cross-sectional ranking)
                # method='dense' ensures consecutive ranks, pct=True converts to percentiles (0-1)
                ranks_pct = date_labels.rank(method='dense', ascending=ascending, pct=True)
                
                # Step 2: Apply z-score normalization to the percentiles
                mean_rank = ranks_pct.mean()
                std_rank = ranks_pct.std()
                
                # Check for zero or very small standard deviation
                if std_rank < 1e-10:
                    self.stats['zero_std_dates'] += 1
                    logger.warning(f"Very small std ({std_rank:.2e}) for date {date}, using 1e-12")
                    std_rank = 1e-12
                
                # Calculate z-score: (x - mean) / std
                rank_zscore = (ranks_pct - mean_rank) / (std_rank + 1e-12)
                
                # Assign the results back to the result DataFrame
                label_adj_df.loc[date, rank_zscore.index] = rank_zscore
                
            except Exception as e:
                logger.error(f"Error processing date {date}: {str(e)}")
                # Set to NaN if there's an error
                label_adj_df.loc[date, date_labels.index] = np.nan
        
        # Log statistics
        logger.info(f"Rank + z-score adjustment statistics: {self.stats}")
        
        return label_adj_df
    
    def generate_labels(
        self,
        start_date: str,
        end_date: str,
        label_shift: int,
        ascending: bool = True,
        adjuster_params: Optional[Dict[str, Any]] = None
    ) -> pd.DataFrame:
        """
        Generate rank + z-score labels for the specified date range.
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            label_shift: Number of days to look ahead for future returns
            ascending: If True, smaller values get higher ranks (default: False for returns)
            adjuster_params: Additional parameters for the label adjuster
            
        Returns:
            Long format DataFrame with columns: stock_code, trade_date, field_name, value, label_shift
        """
        logger.info(f"Generating rank + z-score labels from {start_date} to {end_date} with shift={label_shift}")
        
        # 1. Calculate label_raw (future returns) directly using utility function
        try:
            label_raw_df = calculate_future_returns(
                price_df=None,
                shift=label_shift,
                method="vwap",
                market_data_provider=self.market_data_provider,
                start_date=start_date,
                end_date=end_date
            )
            
            logger.info(f"Calculated label_raw with shape {label_raw_df.shape}")
        except Exception as e:
            logger.error(f"Error calculating label_raw: {str(e)}")
            raise
        
        # 2. Calculate rank + z-score adjusted labels
        try:
            # Get adjuster parameters
            adj_params = adjuster_params or {}
            actual_ascending = adj_params.get('ascending', ascending)
            
            # Apply rank + z-score adjustment
            label_adj_df = self.adjust(
                label_raw_df,
                ascending=actual_ascending,
                **adj_params
            )
            
            logger.info(f"Calculated rank + z-score adjusted labels with shape {label_adj_df.shape}")
        except Exception as e:
            logger.error(f"Error calculating rank + z-score adjusted labels: {str(e)}")
            raise
        
        # 3. Combine label_raw and rank_zscore_d1 into a single wide DataFrame
        try:
            # Create a multi-index DataFrame with both labels
            combined_df = pd.DataFrame({
                f'label_raw': label_raw_df.stack(),
                f'rank_zscore_d1': label_adj_df.stack()
            })
            
            # Reset index to get stock_code and trade_date as columns
            combined_df = combined_df.reset_index()
            
            # Rename columns
            combined_df.columns = ['trade_date', 'stock_code', 
                                 f'label_raw',
                                 f'rank_zscore_d1']
            
            logger.info(f"Combined labels with shape {combined_df.shape}")
        except Exception as e:
            logger.error(f"Error combining labels: {str(e)}")
            raise
        
        # 4. Convert to long format
        try:
            df_long = label_to_long(combined_df, label_shift=label_shift)
            logger.info(f"Converted to long format with shape {df_long.shape}")
        except Exception as e:
            logger.error(f"Error converting to long format: {str(e)}")
            raise
        
        return df_long

