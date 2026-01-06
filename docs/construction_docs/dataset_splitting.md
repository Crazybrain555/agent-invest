# Flexible Dataset Splitting

This document explains how to use the flexible dataset splitting functionality in the Price-Volume dataset builder.

## Overview

The dataset builder supports a flexible splitting mechanism that allows you to:

1. Define any number of custom splits (not limited to just train/valid/test)
2. Set custom date ranges for each split
3. Skip splitting entirely when not needed

The splitting mechanism is controlled via CLI arguments or function parameters when calling the dataset builder.

## Usage

### Via Command Line

To build a dataset with custom splits using the CLI:

```bash
python src/data_service/pipelines/build_pv_dataset.py \
  --start 20030101 --end 20231231 \
  --splits \
  train:20030101:20161231 \
  valid:20170101:20191231 \
  test:20200101:20231231
```

To build a dataset without any splits:

```bash
python src/data_service/pipelines/build_pv_dataset.py \
  --start 20030101 --end 20231231
```

### Via Function Call

To build a dataset with custom splits programmatically:

```python
from src.data_service.pipelines.build_pv_dataset import build_pv_dataset

# Define custom splits
split_rules = [
    ("train", "20030101", "20161231"),
    ("valid", "20170101", "20191231"),
    ("test", "20200101", "20231231")
]

# Build dataset with splits
build_pv_dataset(
    output_dir="data/Dataset/pv_v1",
    start_date="20030101",
    end_date="20231231",
    split_rules=split_rules
)

# Build dataset without splits
build_pv_dataset(
    output_dir="data/Dataset/pv_v2",
    start_date="20030101",
    end_date="20231231",
    split_rules=None  # No splits will be created
)
```

## Loading Data with ParquetPVDataset

The `ParquetPVDataset` class in `src/dataset/parquet_pv_dataset.py` is designed to work with these flexible splits:

```python
from torch.utils.data import DataLoader
from src.dataset.parquet_pv_dataset import ParquetPVDataset

# Load a specific split
train_dataset = ParquetPVDataset(root="data/Dataset/pv_v1", split="train")
valid_dataset = ParquetPVDataset(root="data/Dataset/pv_v1", split="valid")
test_dataset = ParquetPVDataset(root="data/Dataset/pv_v1", split="test")

# Load all data regardless of splits
full_dataset = ParquetPVDataset(root="data/Dataset/pv_v1", split=None)

# Create DataLoader
train_loader = DataLoader(
    train_dataset,
    batch_size=32,
    num_workers=4,
    shuffle=True
)
```

If the dataset was built without splits (no `splits.parquet` file), `ParquetPVDataset` will automatically load all available data.

### Reproducible Shuffling

The `ParquetPVDataset` class supports reproducible shuffling using the `seed` parameter:

```python
# Load training data with reproducible shuffling (fixed seed)
train_dataset = ParquetPVDataset(
    root="data/Dataset/pv_v1",
    split="train",
    shuffle=True,
    seed=42  # Set a fixed seed for reproducible shuffling
)

# The same seed guarantees identical sample ordering across runs
# This helps with debugging and reproducing experimental results
loader = DataLoader(
    train_dataset,
    batch_size=512,
    num_workers=8,
    persistent_workers=True  # Recommended for large datasets
)

# To run with a different shuffle pattern, just change the seed
different_shuffle_dataset = ParquetPVDataset(
    root="data/Dataset/pv_v1",
    split="train",
    shuffle=True,
    seed=101  # Different seed produces different sample ordering
)
```

Setting a fixed seed is especially important when:
- Debugging model training issues
- Reproducing experimental results 
- Running ablation studies where you want to isolate the effect of model changes

## Implementation Details

The splitting mechanism works in the following way:

1. The `_apply_splits` function takes a DataFrame and a list of split rules, each rule being a tuple of `(split_name, start_date, end_date)`.
2. For each rule, it creates a mask for dates within the specified range and assigns the split name to matching rows.
3. If `split_rules` is `None` or empty, no splits are created and no `splits.parquet` file is written.
4. The `ParquetPVDataset` class looks for a `splits.parquet` file in the dataset's `meta` directory. If found and a split name is provided, it filters the data accordingly.

### Shuffling Implementation

The shuffling mechanism is implemented in a way that:

1. Uses numpy's random number generator for efficiency and reproducibility
2. Applies shuffling at the index level (not the raw data) for memory efficiency 
3. Ensures proper data distribution across workers in multi-worker scenarios
4. Avoids expensive database-level shuffling operations like `ORDER BY RANDOM()`

## Benefits

- **Flexibility**: Define any number of splits with custom names and date ranges
- **Consistency**: All data loaders use the same `splits.parquet` file, ensuring consistent splits across experiments
- **Optional**: Skip splitting entirely when not needed
- **Adjustable**: Easily change split dates by rebuilding only the splits.parquet file
- **Reproducible**: Control shuffling patterns with seed parameter for reproducible experiments

## Best Practices

1. Use meaningful split names that reflect their purpose (e.g., "train", "valid", "test", "backtest", "oos")
2. Ensure splits don't overlap in time unless intentional
3. Document your split definitions for reproducibility
4. Use fixed seeds for reproducible experiments
5. When modifying models or hyperparameters, keep the same seed to isolate the effects of your changes 