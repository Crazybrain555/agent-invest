from pathlib import Path
import logging
import torch
from src.dataset.DFZQ_GRU_PV_dataset.parquet_pv_dataset import ParquetPVDataset
from src.dataloader.DataLoader import get_dataloader

dataset_path = Path("data/Dataset/pv_v1")

if __name__ == "__main__":
    print("exists:", dataset_path.exists())

    # 2. 直接迭代 Dataset（逐样本）
    ds = ParquetPVDataset(root=dataset_path, split="train", shuffle=True, seed=42, chunk_size=64)
    it = iter(ds)
    x, y, date, code = next(it)
    print("sample:", x.shape, y, date, code)

    # 3. DataLoader 批量模式测试：不带 meta
    loader = get_dataloader("train", {
        "dataset_path": str(dataset_path),
        "batch_size": 16,
        "num_workers": 0,
        "shuffle": True,
        "seed": 42,
    }, keep_meta=False)

    feats, labels = next(iter(loader))
    print("batch without meta:", feats.shape, labels.shape)

    # # 4. DataLoader 批量模式测试：带 meta
    # loader_meta = get_dataloader("train", {
    #     "dataset_path": str(dataset_path),
    #     "batch_size": 8,
    #     "num_workers": 0,
    #     "shuffle": True,
    #     "seed": 42,
    # }, keep_meta=True)

    # feats, labels, dates, codes = next(iter(loader_meta))
    # print("batch with meta:", feats.shape, labels.shape, len(dates), len(codes))


    # # 5. 使用 DataLoader 批量模式测试：不带 meta
    # loader_no_meta = get_dataloader("train", {
    #     "dataset_path": str(dataset_path),
    #     "batch_size": 8,
    #     "num_workers": 0,
    #     "shuffle": True,
    #     "seed": 63,
    # }, keep_meta=False)

    # feats, labels = next(iter(loader_no_meta))
    # print("batch without meta:", feats.shape, labels.shape)

    # 6. 使用 DataLoader 批量模式测试：带 meta
    loader_meta = get_dataloader("train", {
        "dataset_path": str(dataset_path),
        "batch_size": 8,
        "num_workers": 2,
        "shuffle": True,
        "seed": 52,
    }, keep_meta=True)

    feats, labels, dates, codes = next(iter(loader_meta))
    print("batch with meta:", feats.shape, labels.shape, len(dates), len(codes))


    # 7. 使用 DataLoader 批量模式测试：不带 meta
    loader_no_meta = get_dataloader("train", {
        "dataset_path": str(dataset_path),
        "batch_size": 8,
        "num_workers": 4,
        "shuffle": True,
        "seed": 73,
    }, keep_meta=False)

    feats, labels = next(iter(loader_no_meta))
    print("batch without meta:", feats.shape, labels.shape)

