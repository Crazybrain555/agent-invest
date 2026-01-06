import pandas as pd
import numpy as np
from typing import List, Optional, Union, Dict, Any
import logging

logger = logging.getLogger(__name__)

def wide_to_long(
    df: pd.DataFrame,
    id_vars: List[str],
    value_vars: List[str],
    var_name: str = "field_name",
    value_name: str = "value",
    additional_cols: Optional[Dict[str, Any]] = None
) -> pd.DataFrame:
    """
    Convert a wide DataFrame to long format using pd.melt.
    
    Args:
        df: Wide DataFrame to convert
        id_vars: Column names to use as identifier variables
        value_vars: Column names to unpivot
        var_name: Name to use for the 'variable' column
        value_name: Name to use for the 'value' column
        additional_cols: Dictionary of additional columns to add with constant values
                        {column_name: value}
    
    Returns:
        Long format DataFrame
    """
    # Create a copy to avoid modifying the original
    df_copy = df.copy()
    
    # Reset index if it's a MultiIndex
    if isinstance(df_copy.index, pd.MultiIndex):
        df_copy = df_copy.reset_index()
    
    # Melt the DataFrame
    df_long = pd.melt(
        df_copy,
        id_vars=id_vars,
        value_vars=value_vars,
        var_name=var_name,
        value_name=value_name
    )
    
    # Add additional columns if provided
    if additional_cols:
        for col_name, value in additional_cols.items():
            df_long[col_name] = value
    
    return df_long

def long_to_wide(
    df_long: pd.DataFrame,
    id_vars: List[str] = ['stock_code', 'trade_date'],
    value_var: str = 'value',
    name_var: str = 'field_name',
    chunk_size: int = 100000
) -> pd.DataFrame:
    """
    Convert long-format DataFrame to wide format.
    
    Optimized for large datasets by using chunking to reduce memory usage.
    
    Args:
        df_long: Long format DataFrame
        id_vars: Columns to use as identifiers
        value_var: Column name containing the values
        name_var: Column name containing the field names
        chunk_size: Size of chunks to process at once (to reduce memory usage)
        
    Returns:
        Wide format DataFrame
    """
    # Check if df_long has the required columns
    required_columns = id_vars + [value_var, name_var]
    missing_columns = [col for col in required_columns if col not in df_long.columns]
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")
    
    logger.info(f"Converting long format with {len(df_long)} rows to wide format")
    
    # Get unique field names for pivot
    unique_fields = df_long[name_var].unique()
    
    # Process in chunks based on the unique identifiers
    unique_ids = df_long[id_vars].drop_duplicates()
    chunks = []
    
    for i in range(0, len(unique_ids), chunk_size):
        # Get the chunk of unique IDs
        id_chunk = unique_ids.iloc[i:i+chunk_size]
        
        # Filter the long DataFrame for this chunk
        filter_conditions = None
        for id_var in id_vars:
            if filter_conditions is None:
                filter_conditions = df_long[id_var].isin(id_chunk[id_var])
            else:
                filter_conditions = filter_conditions & df_long[id_var].isin(id_chunk[id_var])
        
        chunk_data = df_long[filter_conditions].copy()
        
        # Pivot the chunk
        chunk_wide = chunk_data.pivot(
            index=id_vars,
            columns=name_var,
            values=value_var
        ).reset_index()
        
        chunks.append(chunk_wide)
    
    # Concatenate all chunks
    df_wide = pd.concat(chunks, ignore_index=True)
    
    logger.info(f"Conversion complete, resulting in {len(df_wide)} rows and {len(df_wide.columns)} columns")
    
    return df_wide

def label_to_long(
    df_label_wide: pd.DataFrame, 
    label_shift: int,
    value_columns: Optional[List[str]] = None,
    chunk_size: int = 100000
) -> pd.DataFrame:
    """
    Convert wide-format label DataFrame to long format.
    
    Optimized for large datasets by using chunking to reduce memory usage.
    
    Args:
        df_label_wide: Wide format DataFrame with columns 'stock_code', 'trade_date', and label columns
        label_shift: Number of days shift for the label
        value_columns: List of columns to melt (if None, will use all columns except 'stock_code' and 'trade_date')
        chunk_size: Size of chunks to process at once (to reduce memory usage)
        
    Returns:
        Long format DataFrame with columns 'stock_code', 'trade_date', 'field_name', 'value', 'label_shift'
    """
    # Make a copy of the DataFrame to avoid modifying the original
    df = df_label_wide.copy()
    
    # Identify ID columns and value columns
    id_vars = ['stock_code', 'trade_date']
    
    # If value_columns is not provided, use all columns except id_vars
    if value_columns is None:
        value_vars = [col for col in df.columns if col not in id_vars]
    else:
        value_vars = value_columns
        
    logger.info(f"Converting wide format with {len(df)} rows and {len(value_vars)} value columns to long format")
    
    # Process in chunks to reduce memory usage
    chunks = []
    for i in range(0, len(df), chunk_size):
        chunk = df.iloc[i:i+chunk_size].reset_index(drop=True)
        
        # Melt the chunk
        chunk_long = chunk.melt(
            id_vars=id_vars,
            value_vars=value_vars,
            var_name='field_name',
            value_name='value'
        )
        
        # Add label_shift column
        chunk_long['label_shift'] = label_shift
        
        chunks.append(chunk_long)
    
    # Concatenate all chunks
    df_long = pd.concat(chunks, ignore_index=True)
    
    # Ensure the output columns are in the right order
    df_long = df_long[['stock_code', 'trade_date', 'field_name', 'value', 'label_shift']]
    
    logger.info(f"Conversion complete, resulting in {len(df_long)} rows")
    
    return df_long 