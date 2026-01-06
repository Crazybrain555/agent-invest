"""
Outlier detection and handling utilities.
"""
import pandas as pd
import numpy as np
from typing import Union, List, Optional, Dict, Any
from scipy import stats

class OutlierHandler:
    """Utilities for detecting and handling outliers."""
    
    @staticmethod
    def detect_outliers(
        data: pd.DataFrame,
        columns: Optional[List[str]] = None,
        method: str = 'zscore',
        threshold: float = 3.0
    ) -> pd.DataFrame:
        """
        Detect outliers in the data.
        
        Args:
            data: Input DataFrame
            columns: List of columns to check
            method: Method to use ('zscore', 'iqr')
            threshold: Threshold for outlier detection
            
        Returns:
            DataFrame with boolean mask indicating outliers
        """
        if columns is None:
            columns = data.select_dtypes(include=[np.number]).columns
            
        result = pd.DataFrame(index=data.index)
        
        for col in columns:
            if method == 'zscore':
                z_scores = np.abs(stats.zscore(data[col]))
                result[f"{col}_is_outlier"] = z_scores > threshold
            elif method == 'iqr':
                Q1 = data[col].quantile(0.25)
                Q3 = data[col].quantile(0.75)
                IQR = Q3 - Q1
                lower_bound = Q1 - threshold * IQR
                upper_bound = Q3 + threshold * IQR
                result[f"{col}_is_outlier"] = (data[col] < lower_bound) | (data[col] > upper_bound)
            else:
                raise ValueError(f"Unsupported method: {method}")
                
        return result
    
    @staticmethod
    def handle_outliers(
        data: pd.DataFrame,
        columns: Optional[List[str]] = None,
        method: str = 'clip',
        threshold: float = 3.0
    ) -> pd.DataFrame:
        """
        Handle outliers in the data.
        
        Args:
            data: Input DataFrame
            columns: List of columns to process
            method: Method to use ('clip', 'remove', 'mean', 'median')
            threshold: Threshold for outlier detection
            
        Returns:
            DataFrame with handled outliers
        """
        if columns is None:
            columns = data.select_dtypes(include=[np.number]).columns
            
        result = data.copy()
        
        for col in columns:
            if method == 'clip':
                # Clip values to threshold
                z_scores = stats.zscore(result[col])
                result[col] = result[col].clip(
                    lower=result[col].mean() - threshold * result[col].std(),
                    upper=result[col].mean() + threshold * result[col].std()
                )
            elif method == 'remove':
                # Remove rows with outliers
                z_scores = np.abs(stats.zscore(result[col]))
                result = result[z_scores <= threshold]
            elif method in ['mean', 'median']:
                # Replace outliers with mean/median
                z_scores = np.abs(stats.zscore(result[col]))
                mask = z_scores > threshold
                if method == 'mean':
                    result.loc[mask, col] = result[col].mean()
                else:
                    result.loc[mask, col] = result[col].median()
            else:
                raise ValueError(f"Unsupported method: {method}")
                
        return result
    
    @staticmethod
    def winsorize(
        data: pd.DataFrame,
        columns: Optional[List[str]] = None,
        limits: tuple = (0.05, 0.05)
    ) -> pd.DataFrame:
        """
        Winsorize data by limiting extreme values.
        
        Args:
            data: Input DataFrame
            columns: List of columns to process
            limits: Tuple of (lower limit, upper limit) as percentiles
            
        Returns:
            Winsorized DataFrame
        """
        if columns is None:
            columns = data.select_dtypes(include=[np.number]).columns
            
        result = data.copy()
        
        for col in columns:
            lower = result[col].quantile(limits[0])
            upper = result[col].quantile(1 - limits[1])
            result[col] = result[col].clip(lower=lower, upper=upper)
            
        return result 