# tests/DFZQ_TRAIN_TEST_dataset.py
from pathlib import Path
import pytest
import pandas as pd
import pyarrow.parquet as pq
import json

# Assuming the builder script is importable
from src.data_service.pipelines.build_pv_dataset import build_pv_dataset

# Mock LocalTestDBDataProvider to avoid actual DB calls during testing
# This is crucial for isolated unit tests
@pytest.fixture
def mock_db_provider(mocker): # mocker is a pytest-mock fixture
    mock = mocker.MagicMock()

    # --- Mock fetch_data for X ---
    # Create dummy X data (wide format)
    dates = pd.to_datetime(["20030101", "20030102", "20030103"])
    codes = ["000001.SZ", "600000.SH"]
    index = pd.MultiIndex.from_product([dates, codes], names=['trade_date', 'stock_code'])
    lag = 30
    base_cols = ["adj_open", "adj_high", "adj_low", "adj_close", "vwap", "amount", "turnover_rate"]
    x_fields = [f"{c}_lag_{i}" for c in base_cols for i in range(lag)]
    x_data = {
        col: range(len(index)) # Simple dummy data
        for col in x_fields
    }
    # Add date/code columns after reset_index
    dummy_x_df = pd.DataFrame(x_data)
    dummy_x_df['trade_date'] = index.get_level_values('trade_date').strftime('%Y%m%d')
    dummy_x_df['stock_code'] = index.get_level_values('stock_code')
    # Reorder columns to match expected structure after reset_index
    dummy_x_df = dummy_x_df[['trade_date', 'stock_code'] + x_fields]

    # --- Mock fetch_data for y ---
    # Create dummy y data (long format initially)
    dummy_y_long_df = pd.DataFrame({
        'trade_date': dummy_x_df['trade_date'].tolist() * 2, # Example with two field names
        'stock_code': dummy_x_df['stock_code'].tolist() * 2,
        'field_name': ['tc_t10_n30_adj'] * len(dummy_x_df) + ['other_label'] * len(dummy_x_df),
        'value': [i * 0.1 for i in range(len(dummy_x_df) * 2)] # Simple dummy values
    })

    # --- Mock fetch_data for stats ---
    dummy_stats_df = pd.DataFrame({
        'feature_name': x_fields,
        'mean': [0.5] * len(x_fields),
        'std': [1.0] * len(x_fields),
        'lower': [-2.0] * len(x_fields),
        'upper': [2.0] * len(x_fields),
    })

    # --- Mock fetch_data for restricted pool ---
    dummy_restricted_df = pd.DataFrame({
        'trade_date': ["20030102"],
        'stock_code': ["000001.SZ"],
        'signal': [1]
    })

    def mock_fetch_data(*args, **kwargs):
        table = kwargs.get('table') or args[0]
        if "intermediate_training_factors" in table:
            return dummy_x_df.copy()
        elif "training_label" in table:
            return dummy_y_long_df.copy()
        elif "inter_train_factors_std" in table:
            return dummy_stats_df.copy()
        elif "restricted_stock_pool" in table:
            return dummy_restricted_df.copy()
        else:
            raise ValueError(f"Unexpected table requested in mock: {table}")

    mock.fetch_data = mocker.MagicMock(side_effect=mock_fetch_data)
    return mock

# Test function using pytest features (tmp_path, mocker)
def test_build_small(tmp_path: Path, mocker, mock_db_provider):
    """Tests the build_pv_dataset function with mocked data for a small date range."""
    # Patch the LocalTestDBDataProvider instantiation within the builder module
    mocker.patch('src.data_service.pipelines.build_pv_dataset.LocalTestDBDataProvider', return_value=mock_db_provider)

    out = tmp_path / "pv_test"
    start_date = "20030101"
    end_date = "20030103"
    lag = 30
    label = "tc_t10_n30_adj"

    # Run the builder function
    build_pv_dataset(out, start_date, end_date, lag=lag, label_name=label, clip_std=False)

    # --- Assertions ---
    # 1. Check directory structure
    assert out.is_dir()
    assert (out / "meta").is_dir()
    assert (out / "shards").is_dir()
    assert (out / "shards" / "year=2003" / "month=01").is_dir() # Check hive partitioning structure

    # 2. Check key meta files exist
    schema_path = out / "meta/schema.json"
    splits_path = out / "meta/splits.parquet"
    stats_path = out / "stats.parquet"
    assert schema_path.exists()
    assert splits_path.exists()
    assert stats_path.exists()

    # 3. Check schema content (basic checks)
    with open(schema_path, 'r') as f:
        schema_data = json.load(f)
    assert schema_data["feature_lag"] == lag
    assert schema_data["label_col"] == label
    assert schema_data["n_total_features"] == 7 * lag
    assert "tc_t10_n30_adj" in schema_data["label_col"]
    assert len(schema_data["feature_cols"]) == 7 * lag
    assert "adj_open_lag_0" in schema_data["feature_cols"]
    assert "turnover_rate_lag_29" in schema_data["feature_cols"]

    # 4. Check splits file (basic checks - structure and content)
    splits_df = pq.read_table(splits_path).to_pandas()
    assert list(splits_df.columns) == ['trade_date', 'stock_code', 'split']
    # Based on our mock data & split logic (all 2003 -> train)
    assert all(splits_df['split'] == 'train')
    # Check if the restricted stock instance was filtered out before splitting
    assert not ((splits_df['trade_date'] == '20030102') & (splits_df['stock_code'] == '000001.SZ')).any()
    assert len(splits_df) == 5 # 3 dates * 2 stocks - 1 restricted

    # 5. Check stats file
    stats_df = pq.read_table(stats_path).to_pandas()
    assert list(stats_df.columns) == ['feature_name', 'mean', 'std', 'lower', 'upper']
    assert len(stats_df) == 7 * lag

    # 6. Check shard files exist (using recursive glob for hive partitioning)
    shard_files = list((out / "shards").rglob("*.parquet"))
    assert len(shard_files) == 1 # Only one month in test data (2003-01)
    shard_file_path = shard_files[0]

    # 7. Check content of a shard file (optional - more detailed check)
    shard_df = pq.read_table(shard_file_path).to_pandas()
    # Expected columns: trade_date, stock_code, features..., label
    assert 'trade_date' in shard_df.columns
    assert 'stock_code' in shard_df.columns
    assert label in shard_df.columns
    assert "adj_open_lag_0" in shard_df.columns
    assert len(shard_df.columns) == 2 + (7 * lag) + 1 # date, code, features, label
    # Check number of rows matches splits_df for that month/year
    assert len(shard_df) == len(splits_df[splits_df['trade_date'].str.startswith('200301')])

# Add more tests: e.g., test_build_with_clipping, test_build_different_dates, test_empty_data
