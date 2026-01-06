import pandas as pd
import numpy as np
from typing import Dict, List, Literal, Optional, Tuple, Union
import logging
from datetime import datetime, timedelta
from tqdm import tqdm

logger = logging.getLogger(__name__)

def get_period_returns(
    price_df: Optional[pd.DataFrame] = None,
    period: int = 1,
    method: Literal["adj_close", "pct_change_db"] = "adj_close",
    market_data_provider = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stock_codes: Optional[List[str]] = None
) -> pd.DataFrame:
    """
    Calculate period returns for stocks. This function can either use existing price data
    or fetch data directly from the database.
    
    Args:
        price_df: Optional DataFrame with price data (index=date, columns=stock_code)
                 If None, data will be fetched using market_data_provider
        period: Number of days for return calculation (must be >= 1)
        method: Method to calculate returns:
            - "adj_close": Calculate from adjusted close prices
            - "pct_change_db": Use pre-calculated percentage change from database
        market_data_provider: Provider for market data (required if price_df is None)
        start_date: Start date for fetching data (required if price_df is None)
        end_date: End date for fetching data (required if price_df is None)
        stock_codes: List of stock codes to fetch (optional, all available if None)
        
    Returns:
        DataFrame with period returns (index=date, columns=stock_code)
    """
    if period < 1:
        raise ValueError("Period must be >= 1")
    
    # If no price_df provided, fetch data using market_data_provider
    if price_df is None:
        if market_data_provider is None:
            raise ValueError("market_data_provider is required when price_df is None")
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date are required when price_df is None")
        
        # Convert dates to datetime for calculations
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        # Add buffer days to ensure we have enough data for calculations
        buffer_days = period * 1.5 + 10  # Add extra days to account for weekends/holidays
        lookback_dt = start_dt - pd.Timedelta(days=buffer_days)
        
        # Format dates for the data provider
        lookback_date = lookback_dt.strftime('%Y%m%d')
        fetch_end_date = end_dt.strftime('%Y%m%d')
        
        logger.info(f"Fetching data from {lookback_date} to {fetch_end_date} for period={period} returns")
        
        if method == "pct_change_db":
            # Fetch pre-calculated percentage change from database
            field = 'pct_change'
            logger.info(f"Using pre-calculated {field} from database")
        else:
            # Default to fetching adj_close
            field = 'adj_close'
            logger.info(f"Using {field} to calculate period returns")
        
        # Fetch data
        try:
            data_df = market_data_provider.fetch_data(
                fields=[field],
                start_date=lookback_date,
                end_date=fetch_end_date,
                stock_codes=stock_codes,
                format='wide'
            )
            
            # Ensure the DataFrame is in the correct format
            if field in data_df.columns:
                # If it's a long format DataFrame, pivot it
                data_df = data_df.pivot(
                    index='trade_date',
                    columns='stock_code',
                    values=field
                )
            
            # Convert index to datetime if it's not already
            if not isinstance(data_df.index, pd.DatetimeIndex):
                data_df.index = pd.to_datetime(data_df.index)
            
            logger.info(f"Fetched {field} data with shape {data_df.shape}")
            
        except Exception as e:
            logger.error(f"Error fetching {field} data: {str(e)}")
            raise
    else:
        # Use provided price_df
        data_df = price_df.copy()
        
        # Convert index to datetime if it's not already
        if not isinstance(data_df.index, pd.DatetimeIndex):
            data_df.index = pd.to_datetime(data_df.index)
    
    # Calculate returns based on method
    if method == "pct_change_db":
        # Data is already percentage change, convert to decimal
        returns_df = data_df / 100
        
        if period > 1:
            # For multi-day periods, calculate cumulative returns
            # (1+r1) * (1+r2) * ... * (1+rn) - 1
            returns_df = returns_df.rolling(window=period).apply(
                lambda x: (1 + x).prod() - 1, 
                raw=True
            )
    else:
        # Calculate returns from price data
        returns_df = data_df.pct_change(period)
    
    # Filter to requested date range if start_date and end_date were provided
    if start_date is not None and end_date is not None:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        returns_df = returns_df.loc[start_dt:end_dt]
    
    logger.info(f"Calculated period={period} returns with shape {returns_df.shape}")
    return returns_df

def calculate_future_returns(
    price_df: Optional[pd.DataFrame] = None,
    shift: int = 10,
    method: Literal["adj_close", "pct_change_db", "vwap"] = "adj_close",
    market_data_provider = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stock_codes: Optional[List[str]] = None
) -> pd.DataFrame:
    """
    Calculate future returns based on the formula: price[t+shift+1] / price[t+1] - 1
    This function can either use existing price data or fetch data directly from the database.
    
    Args:
        price_df: Optional DataFrame with price data (index=date, columns=stock_code)
                 If None, data will be fetched using market_data_provider
        shift: Number of days to look ahead for future returns (must be >= 1)
        method: Method to calculate returns:
            - "adj_close": Calculate from adjusted close prices
            - "pct_change_db": Use pre-calculated percentage change from database
            - "vwap": Use volume-weighted average price
        market_data_provider: Provider for market data (required if price_df is None)
        start_date: Start date for fetching data (required if price_df is None)
        end_date: End date for fetching data (required if price_df is None)
        stock_codes: List of stock codes to fetch (optional, all available if None)
        
    Returns:
        DataFrame with future returns (index=date, columns=stock_code)
    """
    if shift < 1:
        raise ValueError("Shift must be >= 1")
    
    # If no price_df provided, fetch data using market_data_provider
    if price_df is None:
        if market_data_provider is None:
            raise ValueError("market_data_provider is required when price_df is None")
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date are required when price_df is None")
        
        # Convert dates to datetime for calculations
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        # We need data from start_date to end_date + shift + 1 to calculate future returns
        forward_days = (shift + 1) * 1.5 + 10  # Add extra days to account for weekends/holidays
        forward_dt = end_dt + pd.Timedelta(days=forward_days)
        
        # Format dates for the data provider
        fetch_start_date = start_dt.strftime('%Y%m%d')
        fetch_end_date = forward_dt.strftime('%Y%m%d')
        
        logger.info(f"Fetching data from {fetch_start_date} to {fetch_end_date} for future returns (shift={shift})")
        
        if method == "pct_change_db":
            # For pct_change_db, we'll fetch pct_change and calculate cumulative returns
            field = 'pct_change'
            logger.info(f"Using pre-calculated {field} from database")
        elif method == "vwap":
            # Fetch volume-weighted average price
            field = 'vwap'
            logger.info(f"Using {field} to calculate future returns")
        else:
            # Default to fetching adj_close
            field = 'vwap'
            logger.info(f"Using {field} to calculate future returns")
        
        # Fetch data
        try:
            data_df = market_data_provider.fetch_data(
                fields=[field],
                start_date=fetch_start_date,
                end_date=fetch_end_date,
                stock_codes=stock_codes,
                format='wide'
            )
            
            # Ensure the DataFrame is in the correct format
            if field in data_df.columns:
                # If it's a long format DataFrame, pivot it
                data_df = data_df.pivot(
                    index='trade_date',
                    columns='stock_code',
                    values=field
                )
            
            # Convert index to datetime if it's not already
            if not isinstance(data_df.index, pd.DatetimeIndex):
                data_df.index = pd.to_datetime(data_df.index)
            
            logger.info(f"Fetched {field} data with shape {data_df.shape}")
            
        except Exception as e:
            logger.error(f"Error fetching {field} data: {str(e)}")
            raise
    else:
        # Use provided price_df
        data_df = price_df.copy()
        
        # Convert index to datetime if it's not already
        if not isinstance(data_df.index, pd.DatetimeIndex):
            data_df.index = pd.to_datetime(data_df.index)
    
    # Calculate future returns based on method
    if method == "pct_change_db":
        # Data is already percentage change, convert to decimal
        pct_change_df = data_df / 100
        
        # Calculate future returns for each stock and date
        future_returns = pd.DataFrame(index=pct_change_df.index, columns=pct_change_df.columns)
        
        for date in tqdm(pct_change_df.index[:-shift-1], desc="Calculating future returns"):
            # Get the next trading day
            next_day_idx = pct_change_df.index.get_indexer([date])[0] + 1
            if next_day_idx >= len(pct_change_df.index):
                continue
            next_day = pct_change_df.index[next_day_idx]
            
            # Get the future date (shift days after next_day)
            future_idx = next_day_idx + shift
            if future_idx >= len(pct_change_df.index):
                continue
            future_day = pct_change_df.index[future_idx]
            
            # For each stock, reconstruct price series and calculate future returns
            for stock in pct_change_df.columns:
                # Get the price series from next_day to future_day
                price_series = pct_change_df.loc[next_day:future_day, stock].dropna()
                
                if len(price_series) > 0:
                    # Reconstruct price series using cumulative product
                    # Start with 1.0 as base price
                    reconstructed_prices = (1 + price_series).cumprod()
                    
                    # Calculate future return as (future_price / next_day_price) - 1
                    future_return = reconstructed_prices.iloc[-1] / reconstructed_prices.iloc[0] - 1
                    future_returns.loc[date, stock] = future_return
    else:
        # Calculate future returns from price data
        # Shift the price data forward by shift+1 days
        future_prices = data_df.shift(-(shift + 1))
        
        # Shift the price data forward by 1 day
        next_day_prices = data_df.shift(-1)
        
        # Calculate future returns
        future_returns = future_prices / next_day_prices - 1
    
    # Filter to requested date range if start_date and end_date were provided
    if start_date is not None and end_date is not None:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        future_returns = future_returns.loc[start_dt:end_dt]
    
    logger.info(f"Calculated future returns (shift={shift}) with shape {future_returns.shape}")
    return future_returns 