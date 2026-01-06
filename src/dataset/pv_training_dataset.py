import torch
from torch.utils.data import Dataset
import pandas as pd
import numpy as np
import logging

# Adjust the import path based on your project structure
# Assuming LocalTestDBDataProvider is in src.data_service.data_loading.local_testdb_data

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider


logger = logging.getLogger(__name__)

class PVTrainingDataset(Dataset):
    """
    PyTorch Dataset for loading and preprocessing Price-Volume training data.

    Args:
        start_date (str): Start date for data loading (YYYYMMDD).
        end_date (str): End date for data loading (YYYYMMDD).
        feature_lag (int): Number of lagged days for features (L dimension).
        label_name (str): The specific label column name to fetch from the label table.
        provider (object, optional): Data provider instance. Defaults to LocalTestDBDataProvider().
        standardize (bool): Whether to apply z-score standardization. Defaults to True.
        clip_std (bool): Whether to clip standardized values based on bounds from stats table. Defaults to False.
    """
    def __init__(self,
                 start_date: str = "20030101",
                 end_date:   str = "20131231",
                 feature_lag: int = 30,
                 label_name: str = "tc_t10_n30_adj", # Make label name explicit
                 provider=None,
                 standardize: bool = True,
                 clip_std: bool = False):

        logger.info(f"Initializing PVTrainingDataset for {start_date} to {end_date}...")
        self.start_date = start_date
        self.end_date = end_date
        self.lag = feature_lag
        self.label_name = label_name
        self.standardize = standardize
        self.clip_std = clip_std

        try:
            self.prov = provider or LocalTestDBDataProvider()
        except NameError:
            logger.error("LocalTestDBDataProvider is not defined. Cannot fetch data.")
            raise RuntimeError("Failed to initialize data provider.")


        # Define base feature columns
        self.base_feature_cols = [
            "adj_open", "adj_high", "adj_low",
            "adj_close", "vwap", "amount", "turnover_rate"
        ]
        self.num_base_features = len(self.base_feature_cols)

        # 1. Read market features (wide format)
        logger.info("Loading raw features (X_raw)...")
        self.x_raw = self._load_x(start_date, end_date)

        # Store original features before potential standardization
        self.x = self.x_raw.copy()

        # 2. Apply optional second-stage standardization
        if self.standardize:
            logger.info("Applying standardization...")
            self.stats_map = self._load_std_stats() # Load stats only once
            self.x = self._apply_std(self.x, self.stats_map, clip=self.clip_std)
        else:
            logger.info("Skipping standardization.")
            self.stats_map = None # No stats loaded if not standardizing

        # 3. Read label table (long format) -> pivot to wide
        logger.info("Loading labels (y)...")
        self.y = self._load_y(start_date, end_date)

        # 4. Join features and labels, filter restricted pool, and reshape features
        logger.info("Aligning data and applying masks...")
        self.samples = self._align_and_mask()

        logger.info(f"Dataset initialized. Number of samples: {len(self.samples)}")

    def __len__(self):
        """Returns the total number of samples in the dataset."""
        return len(self.samples)

    def __getitem__(self, idx: int):
        """
        Retrieves a single sample from the dataset.

        Args:
            idx (int): Index of the sample to retrieve.

        Returns:
            tuple: Contains:
                - x (torch.Tensor): Feature tensor (F x L).
                - y (torch.Tensor): Label tensor (1).
                - trade_date (str): Trade date of the sample.
                - stock_code (str): Stock code of the sample.
        """
        if idx >= len(self.samples):
            raise IndexError("Index out of range")

        row = self.samples.iloc[idx]
        # x is already a precomputed numpy array in the 'samples' DataFrame
        x_np = row["x"]
        y_val = row["y"]

        # Ensure x is F x L. Should be (num_base_features, lag)
        if x_np.shape != (self.num_base_features, self.lag):
             logger.warning(f"Unexpected shape for x at index {idx}: {x_np.shape}. Expected: {(self.num_base_features, self.lag)}")
             # Attempt to reshape or handle error appropriately
             # For now, raise error if shape is wrong after align_and_mask
             raise ValueError(f"Incorrect feature shape at index {idx}")

        x = torch.tensor(x_np, dtype=torch.float32)
        y = torch.tensor([y_val], dtype=torch.float32) # Ensure y is [1]

        return x, y, row["trade_date"], row["stock_code"]

    # ---------- Helper Methods ----------
    def _standardize_date_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Standardizes trade_date column to YYYYMMDD string format."""
        if 'trade_date' in df.columns:
            df = df.copy()
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y%m%d')
        return df

    def _load_x(self, s_date: str, e_date: str) -> pd.DataFrame:
        """Loads wide-format market features from the provider."""
        fields = [
            f"{col}_lag_{i}"
            for col in self.base_feature_cols
            for i in range(self.lag)
        ]
        logger.debug(f"Fetching features: {fields} for table ai_is.intermediate_training_factors_market_normalize_lag30_countday1")
        try:
            df = self.prov.fetch_data(
                table="ai_is.intermediate_training_factors_market_normalize_lag30_countday1",
                start_date=s_date,
                end_date=e_date,
                fields=fields,
                format="wide" # Ensures trade_date and stock_code are index or columns
            )
            # Ensure essential columns exist
            if 'trade_date' not in df.columns or 'stock_code' not in df.columns:
                 # If format='wide' puts them in index, reset index
                 if isinstance(df.index, pd.MultiIndex) and df.index.names == ['trade_date', 'stock_code']:
                     df = df.reset_index()
                 else:
                     raise ValueError("fetch_data did not return 'trade_date' and 'stock_code' columns.")
            
            # Standardize date format
            df = self._standardize_date_format(df)
            return df
        except Exception as e:
            logger.error(f"Failed to load features (X): {e}", exc_info=True)
            raise

    def _load_std_stats(self) -> pd.DataFrame:
        """Loads standardization statistics."""
        logger.debug("Fetching standardization stats from ai_is.inter_train_factors_std_l30_d1_2002_2012")
        try:
            stats = self.prov.fetch_data(
                table="ai_is.inter_train_factors_std_l30_d1_2002_2012",
                # Fetch all stats, no date range needed for the stats table itself
            )
            # Expecting columns: feature_name, mean, std, lower, upper
            required_cols = ["feature_name", "mean", "std", "lower", "upper"]
            if not all(col in stats.columns for col in required_cols):
                raise ValueError(f"Standardization stats table missing required columns. Found: {stats.columns}")
            # Set index for quick lookup
            stats = stats.set_index("feature_name")
            return stats
        except Exception as e:
            logger.error(f"Failed to load standardization stats: {e}", exc_info=True)
            raise


    def _apply_std(self, df: pd.DataFrame, stats_map: pd.DataFrame, clip: bool = False) -> pd.DataFrame:
        """Applies Z-score standardization (and optional clipping) to feature columns."""
        df_std = df.copy()
        feature_cols = [col for col in df.columns if "_lag_" in col]

        if stats_map is None:
            logger.warning("Standardization skipped: stats_map is None.")
            return df_std

        missing_stats = []
        for col in feature_cols:
            base_feature_name = col.split('_lag_')[0] # Assuming format 'feature_lag_N'

             # Use the stats for the specific lag feature 'col' directly if available
            if col in stats_map.index:
                 stats = stats_map.loc[col]
                 mu, sigma = stats['mean'], stats['std']
                 lower, upper = stats['lower'], stats['upper']

                 # Apply standardization
                 z = (df_std[col] - mu) / (sigma + 1e-12) # Add epsilon for numerical stability

                 # Apply optional clipping
                 if clip:
                     # Transform clipping bounds to standardized scale
                     lower_bound_z = (lower - mu) / (sigma + 1e-12)
                     upper_bound_z = (upper - mu) / (sigma + 1e-12)
                     z = z.clip(lower=lower_bound_z, upper=upper_bound_z)

                 df_std[col] = z

            else:
                missing_stats.append(col)
                # Option: fill with 0 or neutral value if stat is missing? Or raise error?
                df_std[col] = 0 # Fill with 0 for now if stat is missing
                # logger.warning(f"No standardization stats found for feature: {col}. Setting to 0.")


        if missing_stats:
             logger.warning(f"Standardization stats missing for {len(missing_stats)} features (filled with 0): {missing_stats[:10]}...") # Log first 10

        return df_std


    def _load_y(self, s_date: str, e_date: str) -> pd.DataFrame:
        """Loads label data (long format) and pivots it to wide format."""
        logger.debug(f"Fetching label '{self.label_name}' from ai_is.training_label_ls10_adj_topcor_cr30_cw240")
        try:
            df_long = self.prov.fetch_data(
                table="ai_is.training_label_ls10_adj_topcor_cr30_cw240",
                start_date=s_date,
                end_date=e_date,
                fields=[self.label_name], # Fetch only the specific label needed
                format="long" # Expects trade_date, stock_code, field_name, value
            )

            # Filter for the specific label name just in case fetch_data returns others
            df_long = df_long[df_long['field_name'] == self.label_name]

            if df_long.empty:
                 logger.warning(f"No label data found for '{self.label_name}' in the specified date range.")
                 # Return empty dataframe with expected columns after pivot
                 return pd.DataFrame(columns=['trade_date', 'stock_code', self.label_name])

            # Standardize date format before pivoting
            df_long = self._standardize_date_format(df_long)

            # Pivot to wide format: index=[date, stock], columns=[field_name], values=[value]
            df_wide = df_long.pivot_table(index=["trade_date", "stock_code"],
                                          columns="field_name",
                                          values="value")

            # Reset index to make trade_date and stock_code columns
            df_wide = df_wide.reset_index()

            # Ensure the specific label column exists after pivoting
            if self.label_name not in df_wide.columns:
                 raise ValueError(f"Label '{self.label_name}' not found after pivoting.")

            return df_wide[['trade_date', 'stock_code', self.label_name]] # Keep only necessary columns

        except Exception as e:
            logger.error(f"Failed to load labels (y): {e}", exc_info=True)
            raise


    def _align_and_mask(self) -> pd.DataFrame:
        """
        Merges features (X) and labels (y), filters by restricted stock pool,
        handles NaNs, and reshapes features into numpy arrays (F x L).
        """
        # 1. Merge features and labels
        # Use self.x which is either raw or standardized features
        logger.debug(f"Merging features ({self.x.shape}) and labels ({self.y.shape})")
        if 'trade_date' not in self.x.columns or 'stock_code' not in self.x.columns:
            raise ValueError("Feature DataFrame 'self.x' must contain 'trade_date' and 'stock_code' columns.")
        if 'trade_date' not in self.y.columns or 'stock_code' not in self.y.columns:
             raise ValueError("Label DataFrame 'self.y' must contain 'trade_date' and 'stock_code' columns.")

        # Select only necessary columns before merge for efficiency
        feature_id_cols = ['trade_date', 'stock_code']
        feature_data_cols = [col for col in self.x.columns if '_lag_' in col]
        label_id_cols = ['trade_date', 'stock_code']
        label_data_col = self.label_name

        df = pd.merge(self.x[feature_id_cols + feature_data_cols],
                      self.y[label_id_cols + [label_data_col]],
                      on=["trade_date", "stock_code"],
                      how="inner") # Inner join ensures only samples with both features and labels remain

        logger.info(f"Shape after merging X and y: {df.shape}")
        if df.empty:
            logger.warning("DataFrame is empty after merging features and labels.")
            # Return empty dataframe with expected final columns
            return pd.DataFrame(columns=['trade_date', 'stock_code', 'x', 'y'])


        # 2. Remove stocks in the restricted pool for the corresponding trade date
        logger.debug("Fetching restricted stock pool data...")
        min_date, max_date = df['trade_date'].min(), df['trade_date'].max()
        try:
            rest = self.prov.fetch_data(
                table="ai_is.forbid_pool_comprehensive",
                start_date=min_date,
                end_date=max_date,
                fields=['trade_date', 'stock_code', 'signal'] # Ensure 'signal' is fetched
            )
             # Ensure columns exist
            if not all(col in rest.columns for col in ['trade_date', 'stock_code', 'signal']):
                 raise ValueError("Restricted stock pool data missing required columns.")

            # Filter for signals indicating restriction (signal == 1)
            rest = rest.loc[rest['signal'] == 1, ["trade_date", "stock_code"]]
            logger.debug(f"Found {len(rest)} restricted stock entries in the date range.")

            if not rest.empty:
                # Use left merge with indicator to identify restricted rows
                df = pd.merge(df, rest, on=["trade_date", "stock_code"],
                              how="left", indicator=True)

                # Keep only rows that were not in the restricted pool
                initial_count = len(df)
                df = df[df['_merge'] == 'left_only'].drop(columns=['_merge'])
                removed_count = initial_count - len(df)
                logger.info(f"Removed {removed_count} samples due to restricted stock pool. Shape after filtering: {df.shape}")
            else:
                logger.info("No restricted stocks found in the date range. Skipping filtering.")

        except Exception as e:
            logger.error(f"Failed to apply restricted stock pool filter: {e}", exc_info=True)
            # Decide whether to proceed without filtering or raise error
            # raise # Re-raise if filtering is critical


        # 3. Handle potential NaNs introduced during merging or processing
        # Check for NaNs in feature columns and the label column
        nan_feature_rows = df[feature_data_cols].isnull().any(axis=1)
        nan_label_rows = df[label_data_col].isnull()
        rows_with_nan = nan_feature_rows | nan_label_rows

        if rows_with_nan.any():
            initial_count = len(df)
            df = df.dropna(subset=feature_data_cols + [label_data_col])
            removed_count = initial_count - len(df)
            logger.warning(f"Removed {removed_count} samples containing NaN values in features or labels. Shape after NaN drop: {df.shape}")

        if df.empty:
            logger.error("DataFrame is empty after filtering and NaN handling. Cannot proceed.")
            return pd.DataFrame(columns=['trade_date', 'stock_code', 'x', 'y'])


        # 4. Reshape wide feature columns (F*L) into a numpy array (F x L) for each row
        # Ensure the order matches the desired F dimension
        # Order generated by _load_x is [f1_l0...f1_l(L-1), f2_l0...f2_l(L-1), ...]
        feature_matrix = df[feature_data_cols].to_numpy() # Shape (N, F*L)
        logger.debug(f"Feature matrix shape before reshape: {feature_matrix.shape}")

        try:
             # Reshape to (N, F, L) where F=num_base_features, L=lag
             reshaped_features = feature_matrix.reshape(-1, self.num_base_features, self.lag)
             logger.debug(f"Feature matrix shape after reshape: {reshaped_features.shape}")
        except ValueError as e:
             logger.error(f"Failed to reshape features. Expected shape (-1, {self.num_base_features}, {self.lag}). Error: {e}", exc_info=True)
             raise ValueError(f"Cannot reshape features. Check feature count ({len(feature_data_cols)}) vs expected ({self.num_base_features * self.lag}).")


        # Store the reshaped numpy array in the 'x' column
        # Convert array of arrays into a list of arrays for storing in DataFrame column
        df['x'] = list(reshaped_features)

        # Rename the label column to 'y' for consistency
        df = df.rename(columns={self.label_name: 'y'})

        # Select and return final columns
        final_cols = ["trade_date", "stock_code", "x", "y"]
        logger.info(f"Final sample count: {len(df)}")
        return df[final_cols]

