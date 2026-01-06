import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Union, Any, Literal, Callable
import logging
from datetime import datetime, timedelta

from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.preprocessing.methods.correlation_utils import (
    calculate_rolling_correlation,
    CorrelationCalculator
)
from src.data_service.preprocessing.methods.future_returns_utils import (
    calculate_future_returns,
    get_period_returns
)
from src.data_service.preprocessing.methods.dataframe_utils import label_to_long
from src.data_service.data_engineering.label_adjusters import TopCorAdjLabelAdjuster, RankLabelAdjuster

logger = logging.getLogger(__name__)

class LabelGenerator:
    """
    Factory class for generating various types of labels for supervised learning.
    
    This class provides a unified interface for different label generation strategies:
    1. Correlation-based label adjustment (TopCorAdjLabelAdjuster)
    2. Rank + z-score label adjustment (RankLabelAdjuster)
    3. [Future] Other label generation strategies can be added
    
    The factory pattern allows for extensibility while maintaining a consistent interface.
    """
    
    def __init__(
        self,
        market_data_provider: MarketDataProvider,
    ):
        """
        Initialize the label generator factory.
        
        Args:
            market_data_provider: Provider for market data
        """
        self.market_data_provider = market_data_provider
        
        # 🎯 策略注册表 - 可插拔的策略架构
        self._strategy_registry: Dict[str, Callable] = {
            "top_correlation": self._generate_top_correlation_labels,
            "rank": self._generate_rank_labels,
            "raw": self._generate_raw_labels
        }
    
    def generate_top_correlation_labels(
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
            use_rank: If True, use ranking method (like original code) 就是包不包含自己，写false就是不包含自己，数值会大一点
            correlation_type: Type of correlation to use ("pearson" or "spearman")
            adjuster_params: Additional parameters for the label adjuster
            
        Returns:
            Long format DataFrame with columns: stock_code, trade_date, field_name, value, label_shift
        """
        return self._generate_top_correlation_labels(
            start_date=start_date,
            end_date=end_date,
            label_shift=label_shift,
            corr_window=corr_window,
            corr_rank_num=corr_rank_num,
            min_rank_num=min_rank_num,
            use_db_pct_change=use_db_pct_change,
            use_rank=use_rank,
            correlation_type=correlation_type,
            adjuster_params=adjuster_params
        )
    
    def generate_rank_labels(
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
        return self._generate_rank_labels(
            start_date=start_date,
            end_date=end_date,
            label_shift=label_shift,
            ascending=ascending,
            adjuster_params=adjuster_params
        )
    
    def generate_labels(
        self,
        strategy: Union[str, List[str]],
        start_date: str,
        end_date: str,
        **kwargs
    ) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
        """
        General method to generate labels using any available strategy or multiple strategies.
        
        Args:
            strategy: Name of the strategy to use (e.g., 'top_correlation') or list of strategies
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            **kwargs: Additional parameters specific to the chosen strategy
            
        Returns:
            DataFrame with generated labels if single strategy, 
            or Dict[strategy_name, DataFrame] if multiple strategies
        """
        # 🎯 统一成列表处理
        strategies = [strategy] if isinstance(strategy, str) else strategy
        
        logger.info(f"Generating labels using strategies: {strategies}")
        
        results = {}
        
        for strat in strategies:
            if strat not in self._strategy_registry:
                available_strategies = list(self._strategy_registry.keys())
                raise ValueError(f"Unknown strategy: {strat}. Available strategies: {available_strategies}")
            
            logger.info(f"Executing strategy: {strat}")
            
            # 调用对应的策略生成函数
            strategy_func = self._strategy_registry[strat]
            result_df = strategy_func(
                start_date=start_date,
                end_date=end_date,
                **kwargs
            )
            
            results[strat] = result_df
            logger.info(f"Strategy '{strat}' completed, generated {len(result_df)} records")
        
        # 如果只有一个策略，直接返回DataFrame；否则返回字典
        if len(strategies) == 1:
            return results[strategies[0]]
        else:
            return results
    
    def register_strategy(self, name: str, strategy_func: Callable):
        """
        Register a new label generation strategy.
        
        Args:
            name: Name of the strategy
            strategy_func: Function that implements the strategy
        """
        self._strategy_registry[name] = strategy_func
        logger.info(f"Registered new label generation strategy: {name}")
    
    def get_available_strategies(self) -> List[str]:
        """
        Get list of available strategies.
        
        Returns:
            List of strategy names
        """
        return list(self._strategy_registry.keys())
    
    # 🎯 私有方法：具体的策略实现
    def _generate_top_correlation_labels(
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
        adjuster_params: Optional[Dict[str, Any]] = None,
        **kwargs  # 接受额外参数但不使用
    ) -> pd.DataFrame:
        """Private method to generate top correlation labels."""
        # Create the label adjuster
        adjuster = TopCorAdjLabelAdjuster(
            correlation_matrices={},  # Will be populated in generate_labels
            rank_num=corr_rank_num,
            min_rank_num=min_rank_num,
            use_rank=use_rank,
            correlation_type=correlation_type,
            market_data_provider=self.market_data_provider
        )
        
        # Generate labels
        return adjuster.generate_labels(
            start_date=start_date,
            end_date=end_date,
            label_shift=label_shift,
            corr_window=corr_window,
            corr_rank_num=corr_rank_num,
            min_rank_num=min_rank_num,
            use_db_pct_change=use_db_pct_change,
            use_rank=use_rank,
            correlation_type=correlation_type,
            adjuster_params=adjuster_params
        )
    
    def _generate_rank_labels(
        self,
        start_date: str,
        end_date: str,
        label_shift: int,
        ascending: bool = True,
        adjuster_params: Optional[Dict[str, Any]] = None,
        **kwargs  # 接受额外参数但不使用
    ) -> pd.DataFrame:
        """Private method to generate rank + z-score labels."""
        # Create the rank label adjuster
        adjuster = RankLabelAdjuster(
            market_data_provider=self.market_data_provider
        )
        
        # Generate labels
        return adjuster.generate_labels(
            start_date=start_date,
            end_date=end_date,
            label_shift=label_shift,
            ascending=ascending,
            adjuster_params=adjuster_params
        )
    
    def _generate_raw_labels(
        self,
        start_date: str,
        end_date: str,
        label_shift: int,
        **kwargs  # 接受额外参数但不使用
    ) -> pd.DataFrame:
        """Private method to generate raw labels (future returns only)."""
        logger.info(f"Generating raw labels from {start_date} to {end_date} with shift={label_shift}")
        
        try:
            # Calculate raw future returns
            label_raw_df = calculate_future_returns(
                price_df=None,
                shift=label_shift,
                method="vwap",
                market_data_provider=self.market_data_provider,
                start_date=start_date,
                end_date=end_date
            )
            
            logger.info(f"Calculated raw labels with shape {label_raw_df.shape}")
            
            # Create combined DataFrame with only raw labels
            combined_df = pd.DataFrame({
                f'label_raw': label_raw_df.stack()
            })
            
            # Reset index to get stock_code and trade_date as columns
            combined_df = combined_df.reset_index()
            combined_df.columns = ['trade_date', 'stock_code', 'label_raw']
            
            # Convert to long format
            df_long = label_to_long(combined_df, label_shift=label_shift)
            logger.info(f"Converted to long format with shape {df_long.shape}")
            
            return df_long
            
        except Exception as e:
            logger.error(f"Error generating raw labels: {str(e)}")
            raise
