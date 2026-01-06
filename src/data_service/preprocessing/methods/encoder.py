"""
Data encoding utilities.
"""
import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder, OneHotEncoder
from typing import Union, List, Optional, Dict, Any

class DataEncoder:
    """Utilities for encoding categorical variables."""
    
    @staticmethod
    def label_encode(
        data: Union[pd.DataFrame, pd.Series],
        columns: Optional[List[str]] = None
    ) -> Union[pd.DataFrame, pd.Series]:
        """
        Encode categorical variables using LabelEncoder.
        
        Args:
            data: Input data (DataFrame or Series)
            columns: List of columns to encode (if data is DataFrame)
            
        Returns:
            Encoded data
        """
        encoder = LabelEncoder()
        
        if isinstance(data, pd.DataFrame):
            if columns is None:
                columns = data.select_dtypes(include=['object']).columns
            for col in columns:
                data[col] = encoder.fit_transform(data[col])
            return data
        else:
            return pd.Series(encoder.fit_transform(data), index=data.index)
    
    @staticmethod
    def one_hot_encode(
        data: pd.DataFrame,
        columns: Optional[List[str]] = None,
        prefix: Optional[Dict[str, str]] = None,
        sparse: bool = False
    ) -> pd.DataFrame:
        """
        Encode categorical variables using OneHotEncoder.
        
        Args:
            data: Input DataFrame
            columns: List of columns to encode
            prefix: Dictionary mapping column names to prefix strings
            sparse: Whether to return sparse matrix
            
        Returns:
            DataFrame with one-hot encoded columns
        """
        if columns is None:
            columns = data.select_dtypes(include=['object']).columns
            
        encoder = OneHotEncoder(sparse=sparse)
        
        # Store original data
        result = data.copy()
        
        # Drop original columns
        result = result.drop(columns=columns)
        
        # Encode each column
        for col in columns:
            encoded = encoder.fit_transform(data[[col]])
            if sparse:
                encoded = encoded.toarray()
                
            # Create column names
            if prefix and col in prefix:
                col_prefix = prefix[col]
            else:
                col_prefix = col
                
            encoded_cols = [f"{col_prefix}_{i}" for i in range(encoded.shape[1])]
            
            # Add encoded columns to result
            encoded_df = pd.DataFrame(encoded, columns=encoded_cols, index=data.index)
            result = pd.concat([result, encoded_df], axis=1)
            
        return result
    
    @staticmethod
    def ordinal_encode(
        data: Union[pd.DataFrame, pd.Series],
        mapping: Optional[Dict[str, Dict[str, int]]] = None,
        columns: Optional[List[str]] = None
    ) -> Union[pd.DataFrame, pd.Series]:
        """
        Encode categorical variables using custom ordinal mapping.
        
        Args:
            data: Input data (DataFrame or Series)
            mapping: Dictionary mapping column names to value mappings
            columns: List of columns to encode (if data is DataFrame)
            
        Returns:
            Encoded data
        """
        if isinstance(data, pd.DataFrame):
            if columns is None:
                columns = data.select_dtypes(include=['object']).columns
            result = data.copy()
            
            for col in columns:
                if mapping and col in mapping:
                    result[col] = result[col].map(mapping[col])
                else:
                    # Create mapping from unique values
                    unique_values = sorted(result[col].unique())
                    value_map = {val: idx for idx, val in enumerate(unique_values)}
                    result[col] = result[col].map(value_map)
            
            return result
        else:
            if mapping:
                return data.map(mapping)
            else:
                unique_values = sorted(data.unique())
                value_map = {val: idx for idx, val in enumerate(unique_values)}
                return data.map(value_map) 