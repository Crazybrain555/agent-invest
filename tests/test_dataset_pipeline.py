# tests/test_parquet_pv_dataset.py
import pytest
from pathlib import Path
import json

import pandas as pd
import numpy as np
import pyarrow as pa, pyarrow.parquet as pq
import torch

from src.data_service.pipelines.build_pv_dataset import _apply_splits
from src.dataset.parquet_pv_dataset import ParquetPVDataset


# ───────────────────────── fixture ──────────────────────────
@pytest.fixture
def tiny_pv_dataset(tmp_path: Path) -> Path:
    root = tmp_path

    # 1) meta/schema.json ---------------------------------------------------
    schema = {
        "feature_cols": [f"f{i}_lag_{j}" for j in range(2) for i in range(3)],
        "label_col": "lbl",
        "index_cols": ["trade_date", "stock_code"],
        "feature_lag": 2,
        "n_base_features": 3,
        "n_total_features": 6,
        "clip_std": True,
        "build_start_date": "20230101",
        "build_end_date":   "20230102",
    }
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "schema.json").write_text(json.dumps(schema, indent=2))

    # 2) shards/2023/01/data.parquet ---------------------------------------
    shard_dir = root / "shards" / "2023" / "01"
    shard_dir.mkdir(parents=True)
    raw = pd.DataFrame({
        "trade_date": ["20230101", "20230101", "20230102", "20230102"],
        "stock_code": ["000001",  "000002",  "000001",  "000002"],
        **{f"f{i}_lag_{j}": np.arange(4) + 10*j + i
           for j in range(2) for i in range(3)},
        "lbl":  [0.1, 0.2, 0.3, 0.4],
        "year": ["2023"]*4,
        "month":["01"]*4,
    })
    pq.write_table(pa.Table.from_pandas(raw), shard_dir / "data.parquet")

    # 3) meta/splits.parquet -----------------------------------------------
    splits = pd.DataFrame({
        "trade_date": ["20230101", "20230101", "20230102", "20230102"],
        "stock_code": ["000001",   "000002",   "000001",   "000002"],
        "split":      ["train",    "train",    "valid",    "valid"],
    })
    pq.write_table(pa.Table.from_pandas(splits), root / "meta" / "splits.parquet")
    return root


# ──────────────────── Ⅰ. _apply_splits 单元测试 ────────────────────
def test_apply_splits_basic():
    df = pd.DataFrame({"trade_date": ["20010101", "20050101"],
                       "stock_code": ["000001",  "000001"]})
    rules = [("train", "20000101", "20031231"),
             ("test",  "20040101", "20051231")]
    out = _apply_splits(df, rules)
    assert out["split"].tolist() == ["train", "test"]


def test_apply_splits_none():
    df = pd.DataFrame({"trade_date": ["20010101"],
                       "stock_code": ["000001"]})
    assert _apply_splits(df, None) is None


# ─────────────── Ⅱ. ParquetPVDataset 迭代与张量形状 ───────────────
@pytest.mark.parametrize("split, expected", [("train", 2), ("valid", 2), (None, 4)])
def test_dataset_iteration(tiny_pv_dataset, split, expected):
    ds = ParquetPVDataset(root=tiny_pv_dataset, split=split,
                          shuffle=False, seed=0)
    rows = list(ds)
    assert len(rows) == expected
    x, y, d, c = rows[0]
    assert isinstance(x, torch.Tensor) and x.shape == (3, 2) # Shape should be F, L (3 base features, 2 lag)
    assert isinstance(y, torch.Tensor)
    assert isinstance(d, str) and isinstance(c, str)


# ────────────────── Ⅲ. shuffle reproducibility ──────────────────
def test_shuffle_determinism(tiny_pv_dataset):
    ds1 = ParquetPVDataset(root=tiny_pv_dataset, split="train",
                           shuffle=True, seed=7)
    ds2 = ParquetPVDataset(root=tiny_pv_dataset, split="train",
                           shuffle=True, seed=7)
    ds3 = ParquetPVDataset(root=tiny_pv_dataset, split="train",
                           shuffle=True, seed=8) # Using seed 8 as per user's latest code

    to_order = lambda ds: [f"{d}-{c}" for *_, d, c in ds]
    order1, order2, order3 = map(to_order, (ds1, ds2, ds3))

    # same seed → same order
    assert order1 == order2

    # 若样本不足 3 条，shuffle 可能产生相同顺序，直接跳过
    if len(order1) < 3:
        pytest.skip("train split 只有 ≤2 条记录，无法稳妥验证不同 seed 的随机性")

    # diff seed → usually different
    assert order1 != order3


# ─────────────────── Ⅳ. __len__ & 多 worker 支持 ───────────────────
@pytest.mark.parametrize("num_workers", [1, 2, 4])
def test_len_with_workers(monkeypatch, tiny_pv_dataset, num_workers):
    import duckdb # Moved import here to be within test scope as it's patched
    real_conn = duckdb.connect(":memory:")

    # monkey‑patch duckdb.connect，只返回同一个内存连接即可
    monkeypatch.setattr("duckdb.connect", lambda *a, **k: real_conn)

    ds = ParquetPVDataset(root=tiny_pv_dataset, split=None,
                          shuffle=False, seed=0,
                          num_workers=num_workers)

    tot = ds.total_count
    assert len(ds) == tot                # 无 worker_info 时

    # 模拟 DataLoader 内部的 worker_info
    class _WI: pass
    wi = _WI()
    wi.num_workers = num_workers
    wi.id = 0
    monkeypatch.setattr("torch.utils.data.get_worker_info", lambda: wi)

    import math
    expect = math.ceil(tot / num_workers)
    assert len(ds) == expect
