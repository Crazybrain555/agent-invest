# 🔧 索引系统重构总结

> 将 `ok_keys` 升级为统一索引系统（indices），集成特征完整性筛选和标签可用性标记。

---

## ✅ 核心改动

### 1. **新增文件**

| 文件 | 说明 |
|------|------|
| `src/data_service/pipelines/Dataset_builder/indices.py` | 统一索引生成模块（替代 ok_keys.py） |
| `tools/generate_indices.py` | CLI 工具（替代 generate_ok_keys.py） |
| `docs/indices_usage_guide.md` | 使用指南 |
| `docs/indices_refactor_summary.md` | 本文档 |

### 2. **修改文件**

| 文件 | 改动 |
|------|------|
| `initiate_pip_pv_dataset.py` | 调用 `generate_indices` 替代 `generate_ok_keys` |

### 3. **删除文件**

| 文件 | 原因 |
|------|------|
| `tools/generate_ok_keys.py` | 被 `generate_indices.py` 替代 |

### 4. **可选删除**（如不再需要）

| 文件 | 说明 |
|------|------|
| `src/data_service/pipelines/Dataset_builder/ok_keys.py` | 旧实现，已被 indices.py 替代 |

---

## 🎯 核心改进

### 功能对比

| 特性 | ok_keys（旧） | indices（新） |
|------|--------------|--------------|
| **特征完整性筛选** | ✅ 滑窗非空计数 | ✅ 滑窗非空计数（逻辑一致） |
| **标签可用性标记** | ❌ | ✅ `has_label` 字段 |
| **训练/推理分离** | ❌ | ✅ 独立的 `_train.parquet` 和 `_infer.parquet` |
| **index_id 字段** | ❌ | ✅ 连续索引，方便采样 |
| **year/month/day 字段** | ❌ | ✅ 便于按分区批量读取 |
| **DataLoader 直接驱动** | ⚠️ 需额外 join | ✅ 索引即数据集 |
| **训练时自动过滤无标签** | ❌ 需手动实现 | ✅ `_train` 索引自动筛选 |

### 产物对比

#### 旧 ok_keys 产物

```
meta/
  ok_keys/
    ok_keys_lag30.parquet           # 仅 (trade_date, stock_code)
    ok_keys_lag300.parquet
  train_indices_lag30_ok.parquet    # 与 splits 关联，带 index_id
  valid_indices_lag30_ok.parquet
  test_indices_lag30_ok.parquet
```

#### 新 indices 产物

```
meta/
  indices/
    ready_pairs/                    # 内部中间产物
      ready_lag30.parquet
      ready_lag300.parquet
    index_lag30.parquet             # 全量索引（带 ok_factors + has_label）
    index_lag300.parquet
    train_index_lag30_train.parquet # 训练索引（ok_factors=1 & has_label=1）
    train_index_lag30_infer.parquet # 推理索引（ok_factors=1）
    valid_index_lag30_train.parquet
    valid_index_lag30_infer.parquet
    test_index_lag30_train.parquet
    test_index_lag30_infer.parquet
```

---

## 📦 产物详解

### 1. 全量索引：`index_lag{lag}.parquet`

| 列名 | 类型 | 说明 |
|------|------|------|
| `trade_date` | VARCHAR | 交易日期（YYYYMMDD） |
| `stock_code` | VARCHAR | 股票代码 |
| `year` | INTEGER | 年份（便于分区读取） |
| `month` | VARCHAR | 月份（2位） |
| `day` | VARCHAR | 日期（2位） |
| `split` | VARCHAR | 数据集分割（train/valid/test） |
| `ok_factors` | UINT8 | 特征完整性标记（1=通过，0=不通过） |
| `has_label` | UINT8 | 标签可用性标记（1=有标签，0=无标签） |

**用途**：诊断、统计分析

### 2. 训练索引：`{split}_index_lag{lag}_train.parquet`

筛选条件：`ok_factors=1 AND has_label=1`

| 列名 | 类型 | 说明 |
|------|------|------|
| `trade_date` | VARCHAR | 交易日期 |
| `stock_code` | VARCHAR | 股票代码 |
| `year` | INTEGER | 年份 |
| `month` | VARCHAR | 月份 |
| `day` | VARCHAR | 日期 |
| `split` | VARCHAR | 数据集分割 |
| `index_id` | INTEGER | 连续索引（从0开始） |

**用途**：训练 DataLoader 的数据源

### 3. 推理索引：`{split}_index_lag{lag}_infer.parquet`

筛选条件：`ok_factors=1`

结构同训练索引。

**用途**：推理/回测 DataLoader 的数据源

---

## 🚀 使用方式

### 1. 生成索引（自动执行）

```bash
python initiate_pip_pv_dataset.py
```

或手动执行：

```bash
python tools/generate_indices.py \
    --dataset-root data/Dataset/pv_v6 \
    --lags 30,300,500 \
    --factors auto \
    --threads 4 \
    --memory-limit 16GB
```

### 2. 在训练脚本中使用

```python
from pathlib import Path
import pandas as pd
from torch.utils.data import DataLoader

# 读取训练索引
root = Path("data/Dataset/pv_v6")
train_idx = pd.read_parquet(root / "meta" / "indices" / "train_index_lag300_train.parquet")

# 创建 Dataset（使用索引驱动）
class IndexDataset:
    def __init__(self, index_df):
        self.idx = index_df
    
    def __len__(self):
        return len(self.idx)
    
    def __getitem__(self, i):
        row = self.idx.iloc[i]
        return {
            "trade_date": row["trade_date"],
            "stock_code": row["stock_code"],
            "year": str(row["year"]),
            "month": str(row["month"]),
            "day": str(row["day"]),
        }

ds = IndexDataset(train_idx)
loader = DataLoader(ds, batch_size=4096, shuffle=True)

print(f"训练样本数: {len(ds)}")  # 直接从索引获取长度
```

### 3. 批量读取数据（collate_fn）

详见 `docs/indices_usage_guide.md`。

---

## 🔄 迁移指南

### 从旧 ok_keys 迁移

#### 步骤 1：停用旧代码（已完成）

- ✅ `initiate_pip_pv_dataset.py` 已改用 `generate_indices`
- ✅ 旧 CLI 工具 `generate_ok_keys.py` 已删除

#### 步骤 2：重新生成数据集

```bash
# 方式 1：完全重建
rm -rf data/Dataset/pv_v6
python initiate_pip_pv_dataset.py

# 方式 2：仅重新生成索引
rm -rf data/Dataset/pv_v6/meta/indices
python tools/generate_indices.py --dataset-root data/Dataset/pv_v6 --lags 30,300,500
```

#### 步骤 3：更新训练脚本

##### 旧代码（使用 ok_keys）

```python
# 需要手动 join ok_keys、splits、labels
ok_keys = pd.read_parquet("meta/ok_keys/ok_keys_lag300.parquet")
splits = pd.read_parquet("meta/splits.parquet")
labels = pd.read_parquet("shards/labels/**/*.parquet")

train_keys = (
    splits[splits["split"] == "train"]
    .merge(ok_keys, on=["trade_date", "stock_code"])
    .merge(labels, on=["trade_date", "stock_code"], how="inner")  # 手动过滤无标签
)
```

##### 新代码（使用 indices）

```python
# 直接读取训练索引（已筛选好）
train_idx = pd.read_parquet("meta/indices/train_index_lag300_train.parquet")
# 包含: trade_date, stock_code, year, month, day, split, index_id
```

#### 步骤 4：更新 DataLoader（推荐）

使用 `docs/indices_usage_guide.md` 中的 `IndexDataset` + `collate_fetch_wide` 模式。

---

## 📊 性能优化建议

### 1. 按分区批量读取

利用索引的 `year/month/day` 字段，让 DuckDB 只扫描必要的分区：

```python
# 坏习惯：扫描全部数据
glob = "shards/wide_daily/**/*.parquet"

# 好习惯：指定分区
glob = f"shards/wide_daily/year={year}/month={month}/day={day}/*.parquet"
```

### 2. 多 worker 时独立连接

```python
def worker_init_fn(worker_id):
    import duckdb
    # 每个 worker 创建独立的 DuckDB 连接
    worker_con = duckdb.connect()
    worker_con.execute("SET enable_object_cache=true")
    # 存储到全局变量或线程本地存储
```

### 3. selected_factors 筛选

只读取必要的列：

```python
# 假设模型只用 20 个因子
selected_factors = ["vwap_mar_w30", "amount_mar_w30", ...]  # 20个

# 在 SQL 中只 SELECT 这些列
select_cols = ", ".join([f'"{c}"' for c in selected_factors])
sql = f"SELECT w.trade_date, w.stock_code, {select_cols} FROM ..."
```

---

## 🐛 常见问题

### Q1：训练索引为空怎么办？

**原因**：label 表缺失或全为空

**排查**：

```python
import pandas as pd

# 检查全量索引
df = pd.read_parquet("meta/indices/index_lag300.parquet")
print(df[["ok_factors", "has_label"]].value_counts())

# 输出示例：
# ok_factors  has_label
# 1           1            500000  ← 正常
# 1           0             50000  ← 这些是推理可用但无标签的
# 0           0              5000  ← 这些是特征不完整的
```

**解决**：检查 label 表路径、列名、数据是否正确。

### Q2：推理索引包含训练样本吗？

**是的**。推理索引 = 所有 `ok_factors=1` 的样本（包括训练集）。

如需严格区分，可在 DataLoader 中额外过滤：

```python
infer_idx = pd.read_parquet("test_index_lag300_infer.parquet")
# 进一步过滤：排除训练时间段
infer_idx = infer_idx[infer_idx["trade_date"] >= "20220101"]
```

### Q3：如何调整 min_non_null 阈值？

```bash
python tools/generate_indices.py \
    --dataset-root data/Dataset/pv_v6 \
    --lags 300 \
    --min-non-null 50  # 要求至少 50 天有数据（默认 30）
    --force
```

### Q4：能否兼容旧的 ok_keys？

短期可以，但不推荐。如需兼容，在 `indices.py` 末尾添加：

```python
def generate_ok_keys(dataset_root, **kwargs):
    """兼容旧接口（仅产出 ok_factors=1 的键）"""
    return generate_indices(dataset_root, **kwargs)
```

---

## 📚 相关文档

- [索引使用指南](./indices_usage_guide.md)
- [数据集构建文档](./dataset_builder.md)（如有）
- [DataLoader 优化指南](./dataloader_optimization.md)（如有）

---

## ✨ 下一步

1. **运行测试**：`python initiate_pip_pv_dataset.py` 确认索引生成成功
2. **更新训练脚本**：参考 `docs/indices_usage_guide.md`
3. **性能对比**：对比新旧 DataLoader 的吞吐量/内存占用
4. **删除旧代码**：确认无问题后可删除 `ok_keys.py`

---

**🎉 重构完成！现在你有了一个统一、高效、易用的索引系统。**


