# 统一索引系统使用指南

> 本文档说明如何使用新的统一索引系统（替代 ok_keys）进行训练和推理。

---

## 📋 概述

**统一索引系统**将特征完整性筛选和标签可用性筛选合并为一个索引层，产出：

1. **全量索引**：`meta/indices/index_lag{lag}.parquet`
   - 列：`trade_date, stock_code, year, month, day, split, ok_factors (uint8), has_label (uint8)`

2. **训练索引**：`meta/indices/{split}_index_lag{lag}_train.parquet`
   - 筛选条件：`ok_factors=1 AND has_label=1`
   - 列：`trade_date, stock_code, year, month, day, split, index_id`

3. **推理索引**：`meta/indices/{split}_index_lag{lag}_infer.parquet`
   - 筛选条件：`ok_factors=1`
   - 列：`trade_date, stock_code, year, month, day, split, index_id`

---

## 🚀 快速开始

### 1. 生成索引

```bash
# 使用 initiate_pip_pv_dataset.py（已集成）
python initiate_pip_pv_dataset.py

# 或使用独立 CLI 工具
python tools/generate_indices.py \
    --dataset-root data/Dataset/pv_v6 \
    --lags 30,300,500 \
    --factors auto \
    --threads 4 \
    --memory-limit 16GB
```

### 2. 查看生成的索引文件

```python
import pandas as pd
from pathlib import Path

root = Path("data/Dataset/pv_v6")

# 查看训练索引（lag=300）
train_idx = pd.read_parquet(root / "meta" / "indices" / "train_index_lag300_train.parquet")
print(f"训练样本数: {len(train_idx)}")
print(train_idx.head())

# 查看推理索引
infer_idx = pd.read_parquet(root / "meta" / "indices" / "test_index_lag300_infer.parquet")
print(f"推理样本数: {len(infer_idx)}")
```

---

## 💡 DataLoader 使用示例

### 方式 1：基于索引的 PyTorch Dataset

```python
# index_dataset.py
from pathlib import Path
import duckdb
import pandas as pd
import numpy as np
from torch.utils.data import Dataset

class IndexDataset(Dataset):
    """基于索引文件的数据集"""
    
    def __init__(self, dataset_root: str | Path, index_file: str | Path):
        self.root = Path(dataset_root)
        self.idx = pd.read_parquet(index_file)
        # columns: trade_date, stock_code, year, month, day, split, index_id
        
    def __len__(self):
        return len(self.idx)
    
    def __getitem__(self, i):
        # 仅返回索引信息，实际数据由 collate_fn 批量拉取
        row = self.idx.iloc[i]
        return {
            "trade_date": row["trade_date"],
            "stock_code": row["stock_code"],
            "year": str(row["year"]),
            "month": str(row["month"]),
            "day": str(row["day"]),
        }


def collate_fetch_wide(batch, dataset_root, feature_cols, label_col=None):
    """
    批量拉取数据的 collate 函数
    
    优化点：
    - 按 (year, month, day) 分区分组，减少小文件读取
    - 使用 DuckDB JOIN 高效过滤
    """
    from collections import defaultdict
    root = Path(dataset_root)
    
    # 按分区分组
    groups = defaultdict(list)
    for item in batch:
        key = (item["year"], item["month"], item["day"])
        groups[key].append((item["trade_date"], item["stock_code"]))
    
    # 批量读取
    feats_list, labels_list = [], []
    con = duckdb.connect()
    con.execute("SET enable_object_cache=true")
    
    for (y, m, d), keys in groups.items():
        # 构建分区路径
        glob = (root / "shards" / "wide_daily" / f"year={y}" / f"month={m}" / f"day={d}" / "*.parquet").as_posix()
        
        # 注册键表
        key_df = pd.DataFrame(keys, columns=["trade_date", "stock_code"])
        con.register("k", key_df)
        
        # 批量查询
        select_cols = ", ".join([f'"{c}"' for c in feature_cols])
        label_sql = f', "{label_col}" AS __lbl__' if label_col else ""
        sql = f"""
        SELECT w.trade_date, w.stock_code, {select_cols}{label_sql}
        FROM read_parquet('{glob}', hive_partitioning=1, union_by_name=1) AS w
        JOIN k USING (trade_date, stock_code)
        ORDER BY w.trade_date, w.stock_code
        """
        part = con.execute(sql).fetch_df()
        
        feats_list.append(part[feature_cols].to_numpy(dtype="float32"))
        if label_col:
            labels_list.append(part["__lbl__"].to_numpy(dtype="float32"))
        
        con.unregister("k")
    
    X = np.vstack(feats_list) if feats_list else np.zeros((0, len(feature_cols)), dtype="float32")
    y = np.concatenate(labels_list) if (label_col and labels_list) else None
    return X, y


# 使用示例
from torch.utils.data import DataLoader, RandomSampler
import json

root = "data/Dataset/pv_v6"
schema = json.loads((Path(root) / "meta" / "schema.json").read_text())
feature_cols = schema["expanded_factor_names"]
label_col = schema["label_col"]

# 训练
train_idx_path = Path(root) / "meta" / "indices" / "train_index_lag300_train.parquet"
train_ds = IndexDataset(root, train_idx_path)
train_loader = DataLoader(
    train_ds,
    batch_size=4096,
    sampler=RandomSampler(train_ds),
    collate_fn=lambda batch: collate_fetch_wide(batch, root, feature_cols, label_col),
    num_workers=0,  # 多 worker 时需处理 duckdb 连接
)

for X, y in train_loader:
    # X: (B, F) float32, y: (B,) float32
    print(f"Batch shape: {X.shape}, labels: {y.shape}")
    break

# 推理
test_idx_path = Path(root) / "meta" / "indices" / "test_index_lag300_infer.parquet"
test_ds = IndexDataset(root, test_idx_path)
test_loader = DataLoader(
    test_ds,
    batch_size=4096,
    collate_fn=lambda batch: collate_fetch_wide(batch, root, feature_cols, label_col=None),
    num_workers=0,
)
```

---

## 📊 索引统计查询

```python
import duckdb
from pathlib import Path

root = Path("data/Dataset/pv_v6")
con = duckdb.connect()

# 查看各 split 的样本分布
con.execute(f"""
SELECT split,
       COUNT(*) AS total_samples,
       SUM(ok_factors) AS ready_samples,
       SUM(has_label) AS labeled_samples,
       SUM(CASE WHEN ok_factors=1 AND has_label=1 THEN 1 ELSE 0 END) AS train_ready
FROM read_parquet('{(root / "meta" / "indices" / "index_lag300.parquet").as_posix()}')
GROUP BY split
ORDER BY split
""").fetch_df()
```

---

## ⚙️ 配置选项

### `generate_indices` 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `lags` | 滞后天数列表 | `(30, 300, 500)` |
| `factors` | 因子列表（auto/list/file/csv） | `"auto"` |
| `min_non_null` | 窗口内最少非空天数阈值 | `max(10, lag//10)` |
| `require_label_for_train` | 训练索引是否要求 has_label=1 | `True` |
| `with_splits` | 是否生成分 split 的索引 | `True` |
| `force` | 强制重新生成 | `False` |

---

## 🔍 与旧 ok_keys 的对比

| 特性 | ok_keys | 统一索引 |
|------|---------|----------|
| **特征完整性筛选** | ✅ | ✅ |
| **标签可用性标记** | ❌ | ✅ |
| **训练/推理分离** | ❌ | ✅ |
| **index_id 字段** | ❌ | ✅ |
| **year/month/day 字段** | ❌ | ✅（便于分区读取） |
| **DataLoader 直接使用** | 需额外 join | ✅ 直接驱动 |

---

## 🎯 最佳实践

1. **训练时使用 `_train.parquet` 索引**：自动过滤无标签样本
2. **推理时使用 `_infer.parquet` 索引**：保留所有特征完整的样本
3. **按日期/分区 batch**：利用 year/month/day 字段优化 I/O
4. **多 worker 时独立 DuckDB 连接**：避免跨进程共享
5. **selected_factors 筛选**：仅读取必要列，降低内存/I/O

---

## 🐛 故障排查

### 问题：训练索引为空

```python
# 检查全量索引
df = pd.read_parquet("meta/indices/index_lag300.parquet")
print(df[["ok_factors", "has_label"]].value_counts())

# 可能原因：
# 1. label 表缺失或全为空
# 2. min_non_null 阈值过高
# 3. splits.parquet 与实际数据不匹配
```

### 问题：推理索引包含无效样本

```python
# 检查 ok_factors=0 的样本
df = pd.read_parquet("meta/indices/index_lag300.parquet")
invalid = df[df["ok_factors"] == 0]
print(f"无效样本数: {len(invalid)}")

# 解决：重新生成索引，检查 factors 和 min_non_null 配置
```

---

## 📚 相关文件

- `src/data_service/pipelines/Dataset_builder/indices.py` — 核心逻辑
- `tools/generate_indices.py` — CLI 工具
- `initiate_pip_pv_dataset.py` — 集成脚本
- `schema.json` — 索引元信息（`indices` 节点）


