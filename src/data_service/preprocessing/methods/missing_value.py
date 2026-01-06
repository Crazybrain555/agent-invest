"""
Missing value handling utilities.
"""
import pandas as pd
import numpy as np
from typing import Union, List, Optional, Dict, Any
from sklearn.impute import SimpleImputer, KNNImputer

class MissingValueHandler:
    """Utilities for handling missing values in data."""
    
    @staticmethod
    def fill_missing(
        data: pd.DataFrame,
        method: str = 'ffill',
        columns: Optional[List[str]] = None,
        value: Any = None
    ) -> pd.DataFrame:
        """
        Fill missing values using various methods.
        
        Args:
            data: Input DataFrame
            method: Method to use ('ffill', 'bfill', 'mean', 'median', 'mode', 'value')
            columns: List of columns to process
            value: Value to use if method is 'value'
            
        Returns:
            DataFrame with filled missing values
        """
        if columns is None:
            columns = data.columns
            
        if method == 'value' and value is None:
            raise ValueError("Value must be specified when using 'value' method")
            
        if method in ['ffill', 'bfill']:
            data[columns] = data[columns].fillna(method=method)
        elif method == 'value':
            data[columns] = data[columns].fillna(value)
        else:
            for col in columns:
                if method == 'mean':
                    data[col] = data[col].fillna(data[col].mean())
                elif method == 'median':
                    data[col] = data[col].fillna(data[col].median())
                elif method == 'mode':
                    data[col] = data[col].fillna(data[col].mode()[0])
                else:
                    raise ValueError(f"Unsupported method: {method}")
        
        return data
    
    @staticmethod
    def impute_missing(
        data: pd.DataFrame,
        strategy: str = 'mean',
        columns: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Impute missing values using scikit-learn's SimpleImputer.
        
        Args:
            data: Input DataFrame
            strategy: Imputation strategy ('mean', 'median', 'most_frequent', 'constant')
            columns: List of columns to process
            
        Returns:
            DataFrame with imputed missing values
        """
        if columns is None:
            columns = data.columns
            
        imputer = SimpleImputer(strategy=strategy)
        data[columns] = imputer.fit_transform(data[columns])
        return data
    
    @staticmethod
    def knn_impute(
        data: pd.DataFrame,
        n_neighbors: int = 5,
        columns: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Impute missing values using KNN.
        
        Args:
            data: Input DataFrame
            n_neighbors: Number of neighbors to use
            columns: List of columns to process
            
        Returns:
            DataFrame with imputed missing values
        """
        if columns is None:
            columns = data.columns
            
        imputer = KNNImputer(n_neighbors=n_neighbors)
        data[columns] = imputer.fit_transform(data[columns])
        return data 