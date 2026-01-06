请根据我下面的思路，给我的代码列一个指导，我之前写了others\original_programes\code_pvnet_20241212ms\main_nn.py，这个代码是没有框架的，就是暂时测试思路的，但是现在要写在新建的框架下。写伪代码指导，要专业，也要指导我为了后续别的模型训练我应该做什么准备，让框架更有扩展性。你觉得要加东西，你可以知道我在哪路径下加东西

我的代码路径如下：
configs
configs\db
configs\db\gogoal_db.yaml
configs\db\local_db_configs.yaml
configs\db\prod_db.yaml
configs\db\table_config.yaml
configs\db\test_tdsql_db.yaml
configs\db\wind_db.yaml
configs\field_mappings
configs\field_mappings\financial_data.yaml
configs\field_mappings\fund_data.yaml
configs\field_mappings\index_data.yaml
configs\field_mappings\local_db_data.yaml
configs\field_mappings\macro_data.yaml
configs\field_mappings\market_data.yaml
configs\models
configs\models\cnn
configs\models\ensemble
configs\models\mlp
configs\models\rnn
configs\models\rnn\gru
configs\models\rnn\gru\basic.yaml
configs\models\rnn\gru\dfzq.yaml
configs\models\rnn\lstm
configs\models\transformer
configs\nas_disk
configs\nas_disk\nas_config.yaml
configs\strategies
configs\strategies\base_strategy.yaml
configs\strategies\ensemble_strategy.yaml
configs\strategies\ml_strategy.yaml
configs\field_mapping.yaml
data
data\processed
data\processed\label.2023.pkl
data\raw
data\strategies
docs
docs\algorithms.md
docs\architecture.md
docs\background.md
docs\database_specification.md
docs\local_db.md
docs\usage.md
logs
notebooks
others\original_programes
others\original_programes\code_pvnet_20241212ms
others\original_programes\code_pvnet_20241212ms\collect_perfs_lgbgru.csv
others\original_programes\code_pvnet_20241212ms\collect_perfsumm_lgbgru.csv
others\original_programes\code_pvnet_20241212ms\collect_records_lgbgru.csv
others\original_programes\code_pvnet_20241212ms\crontab_lic.txt
others\original_programes\code_pvnet_20241212ms\main_dt.py
others\original_programes\code_pvnet_20241212ms\main_nn.py
others\original_programes\code_pvnet_20241212ms\merge_alpha.py
others\original_programes\code_pvnet_20241212ms\model_nn.py
others\original_programes\code_pvnet_20241212ms\prepare_data.py
others\original_programes\code_pvnet_20241212ms\run_product.sh
others\original_programes\code_pvnet_20241212ms\run_retrain.sh
others\original_programes\get_data_and_save
others\original_programes\transformer-master
others\original_programes\transformer-master\__pycache__
others\original_programes\transformer-master\.idea
others\original_programes\transformer-master\image
others\original_programes\transformer-master\models
others\original_programes\transformer-master\paper
others\original_programes\transformer-master\saved
others\original_programes\transformer-master\util
others\original_programes\transformer-master\.gitignore
others\original_programes\transformer-master\conf.py
others\original_programes\transformer-master\data.py
others\original_programes\transformer-master\graph.py
others\original_programes\transformer-master\README.md
others\original_programes\transformer-master\train.py
src
src\__pycache__
src\backtesting
src\data_service
src\data_service\__pycache__
src\data_service\data_engineering
src\data_service\data_engineering\__pycache__
src\data_service\data_engineering\__init__.py
src\data_service\data_engineering\features_engineering.py
src\data_service\data_engineering\label_adjusters.py
src\data_service\data_engineering\labels_engineering.py
src\data_service\data_loading
src\data_service\data_loading\__pycache__
src\data_service\data_loading\logs
src\data_service\data_loading\company_profile.py
src\data_service\data_loading\event_data.py
src\data_service\data_loading\financial_data.py
src\data_service\data_loading\forbid_data.py
src\data_service\data_loading\get_data.py
src\data_service\data_loading\index_data.py
src\data_service\data_loading\local_testdb_data.py
src\data_service\data_loading\macro_data.py
src\data_service\data_loading\market_data.py
src\data_service\data_loading\model_training_data.py
src\data_service\data_loading\pretrain_data.py
src\data_service\data_loading\quant_factor_data.py
src\data_service\data_saving
src\data_service\data_saving\__pycache__
src\data_service\data_saving\__init__.py
src\data_service\data_saving\data_to_testdb.py
src\data_service\preprocessing
src\data_service\preprocessing\__pycache__
src\data_service\preprocessing\methods
src\data_service\preprocessing\methods\__pycache__
src\data_service\preprocessing\methods\correlation_utils.py
src\data_service\preprocessing\methods\dataframe_utils.py
src\data_service\preprocessing\methods\encoder.py
src\data_service\preprocessing\methods\future_returns_utils.py
src\data_service\preprocessing\methods\missing_value.py
src\data_service\preprocessing\methods\normalizer.py
src\data_service\preprocessing\methods\outlier.py
src\data_service\preprocessing\methods\standardizer.py
src\data_service\preprocessing\__init__.py
src\data_service\preprocessing\pipeline.py
src\data_service\tests
src\data_service\__init__.py
src\data_service\data_pipeline.py
src\data_service\DATA_README.md
src\dataset
src\dataset\__init__.py
src\dataset\pv_training_dataset.py
src\evaluations
src\models
src\models\__pycache__
src\models\rnn
src\models\rnn\gru\dfzq_gru
src\models\rnn\gru\dfzq_gru\__pycache__
src\models\rnn\gru\dfzq_gru\models
src\models\rnn\gru\dfzq_gru\models\__init__.py
src\models\rnn\gru\dfzq_gru\__init__.py
src\models\rnn\gru\dfzq_gru\dfzq_gru.py
src\models\rnn\gru\dfzq_gru\get_configs.py
src\models\rnn\gru\dfzq_gru\test_dfzq_gru.py
src\models\rnn\lstm
src\models\transformer
src\models\__init__.py
src\models\base_model.py
src\models\model_factory.py
src\models\model.py
src\scheduler
src\scheduler\__pycache__
src\scheduler\__init__.py
src\scheduler\Dfzq_gru_scheduler.py
src\scheduler\job_definitions.py
src\scheduler\job_runner.py
src\scheduler\nas_get_data_Scheduler.py
src\strategies
src\tasks
src\train
src\train\Neural_networks
src\train\Neural_networks\RNN
src\train\Neural_networks\RNN\DFZQ_GRU
src\train\Neural_networks\RNN\DFZQ_GRU\saved
src\train\Neural_networks\RNN\DFZQ_GRU\__init__.py
src\train\Neural_networks\RNN\DFZQ_GRU\dfzq_Dataloader.py
src\train\Neural_networks\RNN\DFZQ_GRU\dfzq_gru_trainer.py
src\train\Neural_networks\RNN\DFZQ_GRU\train_dfzq_gru.py
src\train\Neural_networks\RNN\DFZQ_GRU\Train_framework.md
src\train\Neural_networks\__init__.py
src\train\trainer.py
src\utils
src\utils\__pycache__
src\utils\logs
src\utils\config_loader.py
src\utils\db_connection.py
src\utils\logger.py
src\utils\nas_connection.py
src\utils\table_schema.py
src\utils\visualization.py
src\__init__.py
tests
tests\logs
tests\data_standerliaze.py
tests\data_to_testdb_test.py
tests\main_nas_test.py
tests\test_label_generation.py
tests\test_normalization.py
tests\test_scheduler.py
tests\TestDBManager.py
tests\Tget_testdb_tester.py
visualizations
.gitignore
data_test.py
dataset_test.py
init_data.py
main_test.py
main.py
nas_pipeline_20250423.log
nas_pipeline_20250424.log
pip_test.py
README.md
run_daily_data_pipeline.py
run_nas_data_pipeline.py
Tget_testdb_tester.py
TOOLS_USE.md

我将参考others\original_programes\code_pvnet_20241212ms\main_nn.py的run_backtest_daybar_byyear函数和others\original_programes\code_pvnet_20241212ms\model_nn.py的AGRUModel的部分进行训练。

## 数据

首先数据部分，数据结构请看 docs\local_db.md

**X的部分**：
我会取 市场数据归一化表 (`intermediate_training_factors_market_normalize_lag30_countday1`)的adj_open_lag_N、adj_high_lag_N、adj_low_lag_N、adj_close_lag_N、vwap_lag_N、amount_lag_N、turnover_rate_lag_N ，（注意 N是0到29的），其中adj_open_lag_N、adj_high_lag_N、adj_low_lag_N、adj_close_lag_N、vwap_lag_N 需要标准化参数表 (`inter_train_factors_std_l30_d1_2002_2012`)的mean、std、lower、upper来进行计算进一步标准化（原先数据已经归一化了）。 

再取restricted_stock_pool data， 用来过滤对应交易日的股票，训练时需要过滤掉signal为1的股票。

取数运用的工具是LocalTestDBDataProvider，具体参考Tget_testdb_tester.py，取的日期范围是 2003-01-01 到 2013-12-31。

这就是X的部分。


**y的部分**：
数据来自training_label_ls10_adj_topcor_cr30_cw240表，取当日X对应日期和股票的 filed_name为tc_t10_n30_adj的数据

## Dataset& DataLoader
请在src\dataset\pv_training_dataset.py搭建dataset  参考others\original_programes\code_pvnet_20241212ms\main_nn.py，我看原来的代码是按照日期
DataLoader 到时候写在train里面 不单独写了 可以放在 src\train\Neural_networks\RNN\DFZQ_GRU\dfzq_Dataloader.py

注意我有一个疑惑，就是每个日期 对应的股票都是不一样的，他用的 for i in range(len(train_dates)): 所以没有遇到这个对不齐的问题，但是如果我这边的训练我有可能会遇到不同日期股票不一致的问题，这个要注意下，可能DataLoader要注意调整到一致，虽然我还没有一个调整思路。

## Model 
请写在src\models\rnn\gru\dfzq_gru\dfzq_gru.py  。我已经写了，你可以优化，但是我觉得可能已经不错了


## train
请写在src\train\Neural_networks\RNN\DFZQ_GRU\train_dfzq_gru.py


## Evaluation
目前写在 src\train\Neural_networks\RNN\DFZQ_GRU\dfzq_gru_trainer.py上面吧


## 模型保存与加载（Checkpoint）
放在 src\train\Neural_networks\RNN\DFZQ_GRU\saved\model_ckp

## 量化因子（最后再做，训练完成后再思考，先不做）
完成后将20140101-现在最新的 的量化因子结果放在src\train\Neural_networks\RNN\DFZQ_GRU\saved\result（后续再改）


### 搭建思路：



Price-Volume GRU 框架落地蓝图
关键词：离线预处理 · Parquet + DuckDB · IterableDataset · GRU + Self-Attention

1 Dataset 体系
1.1 目录版本化
text
复制
编辑
data/
└─ Dataset/
   ├─ pv_v1/                    # ← Price-Volume 数据集第 1 版
   │   ├─ meta/
   │   │   ├─ schema.json       # 字段名 / dtype / 描述
   │   │   └─ splits.parquet    # (trade_date, stock_code, split)
   │   ├─ shards/               # 分片 Parquet：yyyy/yyyyMM.parquet
   │   └─ stats.parquet         # 二阶段标准化参数 (mean, std, lower, upper)
   └─ tmp/                      # 中间文件
按月分片：200 ~ 400 MB/文件，方便并行读取与增量追加。

pv_v*：特征或标签有重大调整时复制成新目录，历史模型可回溯。

1.2 离线 Builder src/data_service/pipelines/build_pv_dataset.py
python
复制
编辑
def build(start_date="20030101", end_date="20131231", lag=30):
    # ① 拉 wide-X（7✕lag）+ label
    # ② align & mask → 过滤禁买股、去 NaN
    # ③ z-score + 可选剪裁（lower/upper）
    # ④ 按 (year, month) 写 Parquet
    # ⑤ 产出 meta / stats.parquet / splits.parquet
一次运行即可生成完整 pv_v1；日后增量→追加新分片即可。

1.3 在线 Loader src/dataset/parquet_pv_dataset.py
python
复制
编辑
class ParquetPVDataset(IterableDataset):
    def __init__(..., shuffle=True):
        self.index = load_splits(...)
        if shuffle: random.shuffle(self.index)       # 每 epoch 再洗
        self.con   = duckdb.connect(":memory:")
        self.ds    = ds.dataset(shards_path, "parquet")
        self.con.register('pv', self.ds)
DuckDB 零拷贝 SQL；IterableDataset 天然多-Worker。

Shuffle 发生在 索引层，既随机又省内存。

⚙️ TODO
[ ] 编写 Builder [ ] 生成小批数据测试 [ ] 完整跑出 pv_v1

2 模型：src/models/rnn/gru/dfzq_gru/dfzq_gru.py

组件	设计
RNN	nn.GRU(d_feat, hidden, layers, bidirectional)
Self-Attention	单头 MLP Score → Softmax over L
融合	cat(last_state, attn_out) → LayerNorm → FC
正则	选配 Dropout 0.1；返回 features 供 Orthogonal loss
python
复制
编辑
def forward(self, x):          # x: [B, F, L]
    x = x.permute(0, 2, 1)     # → [B, L, F]
    h, _ = self.rnn(x)         # [B, L, H]
    a    = self.attn(h)        # [B, H]
    cat  = torch.cat([h[:, -1], a], dim=1)
    y_hat = self.fc(cat)       # [B, 1]
    return y_hat, cat
⚙️ TODO
[ ] 加 LayerNorm [ ] Attention 维度自适应双向
[ ] Dropout / Xavier Init [ ] 单元测试 dummy [B,7,30]

3 Training Pipeline
3.1 DataLoader dfzq_Dataloader.py
python
复制
编辑
train_dl, val_dl = get_train_valid_loaders(
    batch=4096, workers=8, lag=30, shuffle=True)
90 / 10 随机切分；pin_memory=True 提升 GPU feed 速率

3.2 训练脚本 train_dfzq_gru.py
python
复制
编辑
for epoch in range(cfg.epochs):
    loss = train_one_epoch(...)
    ic   = evaluate(...)
    if ic > best: save_checkpoint()
Loss 建议用 负 Spearman ρ；Early-stop 监控验证 IC.

3.3 Alpha Dump dfzq_gru_trainer.py
加载最佳权重 → 2014-01-01 以后跑预测 → dfzq_gru_alpha.csv

⚙️ TODO
[ ] DataLoader 实现 [ ] train / eval 循环 OK
[ ] Checkpoint 存取 [ ] Alpha CSV 输出

4 配置示例 configs/models/rnn/gru/dfzq.yaml
yaml
复制
编辑
lag:            30
hidden_size:    64
num_layers:     2
bidirectional:  false
dropout:        0.1
batch_size:     4096
num_workers:    8
lr:             1.0e-3
epochs:         200
early_stop:     20
shuffle:        true
5 常见变体与扩展

需求	改动
换 LSTM / Transformer	新建模型文件 + YAML；model_factory 自动加载
新标签窗口	Builder 改列名 → pv_v2
"同日整批" 训练	自定义 GroupedBatchSampler
大规模随机	torchdata.Shuffler(buffer_size=N)
HPO	src/train/hpo/optuna_xxx.py + 写回 YAML
6 实施清单
 Dataset：Builder + Parquet Dataset

 Model：LayerNorm / Dropout / init

 Loader：train / valid split

 Trainer：loss=–IC，早停，ckpt

 Evaluation：预测→alpha CSV

 单测：DFZQ_TRAIN_TEST.py 覆盖核心逻辑

 CI：小样本跑通；大样本 benchmark

 文档：更新 docs/usage.md

完成以上步骤后，即可把旧目录
others/original_programes/code_pvnet_20241212ms 归档或删除，项目正式切换到新框架。



建议：
1 为什么 _apply_std() 里只用 mean / std，没用 upper / lower？
在传统 z-score 标准化 里

𝑧
=
𝑥
−
𝜇
𝜎
z= 
σ
x−μ
​
 
就足够把不同特征放到零均值、单位方差的共同尺度。
upper / lower（我在表里叫 robust-clip 或 "up / down"）通常用来 裁剪极端值 ，防止长尾分布对模型造成冲击，不是必须步骤，只是"可选增强"。

什么时候要裁剪？

数据有明显尖峰、厚尾，且模型对离群点非常敏感（比如线性回归、MSE loss）。

训练早期发现梯度经常爆炸或者 IC 波动极大。

怎么加？
在 x = (x-μ)/σ 之后再做

python
复制
编辑
x = x.clip(lower_bound, upper_bound)
其中

python
复制
编辑
lower_bound = (lower - mu) / (sigma + 1e-12)
upper_bound = (upper - mu) / (sigma + 1e-12)
这样就和数据库里 lower/upper 的"原始量纲"保持一致。
你可以把我的 _apply_std() 改成：

python
复制
编辑
for col in df.filter(like="_lag_").columns:
    mu, sigma, lo, hi = mapper.loc[col]
    z = (df[col] - mu) / (sigma + 1e-12)
    df[col] = z.clip((lo-mu)/sigma, (hi-mu)/sigma)
先跑一版只用 mean/std，如果训练稳定、IC 曲线平滑，就暂时别裁剪；
如果发现个别特征值动辄 ±30，而 IC 时好时坏，再打开剪裁开关。

2 你贴的 DFZQGRU 模型代码 —— 可以用，但有几个小优化

区域	建议	为什么
输入 fc + Tanh	在金融因子里 LayerNorm 经常比 BatchNorm1d 效果更稳，数据量大时尤其明显。可以把 nn.Tanh() 后面接 nn.LayerNorm(self.hidden_size).	BN 对 batch 维度敏感，横截面大小不一时波动大；LayerNorm 穿透时间维度更友好。
GRU bidirectional	你把 bidirectional 暴露在 config 里很好，但如果设为 True，后面 attention 的输入维度就要用 hidden_size*2（你已经在 gru_output_size 里处理了，OK）。	避免 shape mismatch。
注意力权重	现在 Softmax(dim=1) 在序列维度 OK；如果开启双向 GRU，最好把 nn.Linear(hidden_size*2, ...)，不然特征维度不符。	兼容双向。
输出部分	x = torch.mean(res, dim=1, keepdim=True) 再做 bn2 会把 batch_size 维度压成 [B,1]，这其实与你 output_size=1 是重复的。可以直接： ```python	
out = self.bn2(self.fc1(combined)) return out, res```	简化并避免多余的均值操作。	
损失时用的 features (res)	你返回 res 用来做 RankIC / orthogonal penalty，这很好；如果要计算相关性约束记得先做行归一化 (res-res.mean(0))/res.std(0).	避免 scale 影响惩罚项。
Dropout	你只在注意力 MLP 里加了 Dropout；建议在 fc 和 GRU 层之间也加一点（如 nn.Dropout(0.1)），样本比较小时能减少过拟合。	常规正则化。
参数初始化	PyTorch 默认 init 足够；想更可控可以： ```python	
for m in self.modules():		
scss
复制
编辑
if isinstance(m, (nn.Linear, nn.GRU)):
    nn.init.xavier_uniform_(m.weight)
|
复制
编辑
| **CUDA friendly** | forward 里别用 `torch.mean(res, dim=1, keepdim=True)` 再 BN；会触发 rank-1 tensor BN 警告。 | 稳定性。 |

**小结**

```python
# 关键修改示意
self.fc1 = nn.Linear(gru_output_size * 2, self.hidden_size)
self.dropout1 = nn.Dropout(self.dropout)
self.out_bn = nn.BatchNorm1d(self.config.output_size, affine=False)

...
combined = torch.cat((gru_output[:, -1, :], attended_output), dim=1)
res = self.dropout1(torch.tanh(self.fc1(combined)))
out = self.out_bn(res.mean(1, keepdim=True))
return out, res
整体架构是没问题的，可以直接在新 Dataset + DataLoader 上试跑；
先把 batch 改小（比如 256）验证 forward / loss 正常，再放大。

下一步动作
在 _apply_std() 里是否裁剪 —— 用前 5 万条样本画个直方图看看；决定要不要 clip().

把上面这些模型微调项合并进 src/models/rnn/gru/dfzq_gru/dfzq_gru.py.

跑 train_dfzq_gru.py 小 epoch（5 ~ 10）确认 loss、IC、ρ 都能降。
```

### Implementation TODO List (Integrated Offline Build + Parquet IterableDataset)

**Phase 1: Offline Dataset Building Logic (within `src/data_service/pipelines/build_pv_dataset.py`)**

- [x] **1. Implement Offline Builder Function (`build_pv_dataset`)**
    - [x] Define a function `build_pv_dataset(output_dir, start_date, end_date, lag, ...)`. This function will encapsulate the logic previously planned for the standalone script.
    - [x] **Ensure Output Directory**: The function should first ensure the `output_dir` (e.g., `data/Dataset/pv_v1/`) and its subdirectories (`meta/`, `shards/`) exist, creating them if necessary.
    - [x] **Load Raw Data**: Use `LocalTestDBDataProvider` to fetch wide-format features (X) and long-format labels (y) for the specified historical range.
    - [x] **Preprocessing & Alignment**: Implement `align_mask_standardize` logic:
        - [x] Merge X and y based on `trade_date`, `stock_code`.
        - [x] Filter restricted stocks using data from `ai_is.restricted_stock_pool`.
        - [x] Handle NaNs.
        - [x] Apply Z-score standardization (using stats from `ai_is.inter_train_factors_std_l30_d1_2002_2012`). Decide on clipping.
        - [x] **Store Standardization Stats**: Calculate and save standardization parameters to `{output_dir}/stats.parquet`.
        - [x] **Reshape Features**: Prepare features for storage (e.g., keep as wide columns).
    - [x] **Generate Train/Valid/Test Splits**: Define split logic (e.g., by date ranges) and create a DataFrame containing `(trade_date, stock_code, split)`.
        - [x] Save split index to `{output_dir}/meta/splits.parquet`.
    - [x] **Write Sharded Data**: Group processed data by year/month.
        - [x] Write each group to `{output_dir}/shards/YYYY/YYYYMM.parquet` using `pyarrow.dataset.write_dataset`.
    - [x] **Save Schema** (Optional but Recommended): Save schema to `{output_dir}/meta/schema.json`.
    - [x] **(Optional) Add Standalone Script**: Create a thin wrapper script `src/data_service/pipelines/build_pv_dataset.py` that imports and calls the `build_pv_dataset` function, allowing manual dataset generation via command line if needed.
    - [x] **Test Builder Function**: Unit test the `build_pv_dataset` function with a small date range, verifying output files.

**Phase 2: Online Data Loading & Training (within Training Scripts)**

- [x] **2. Implement Online Loader (`src/dataset/parquet_pv_dataset.py`)**
    - [x] Implement `ParquetPVDataset(IterableDataset)` (mostly unchanged from previous plan).
    - [x] `__init__`: Takes dataset root path, `split`, `num_features`, `lag`, `shuffle` flag. Loads index from `meta/splits.parquet`, shuffles if needed, initializes `duckdb`.
    - [x] `__iter__`: Handles worker splitting, queries rows via `duckdb`, reshapes features online, yields `(x_tensor, y_tensor, date, code)`.
    - [x] **Test (`DFZQ_TRAIN_TEST.py`)**: Test `ParquetPVDataset` (requires dummy Parquet files).

- [x] **3. Implement DataLoader (`src/train/Neural_networks/RNN/DFZQ_GRU/dfzq_Dataloader.py`)**
    - [x] Implement `get_train_valid_loaders` function.
    - [x] Instantiate `ParquetPVDataset` for 'train' and 'valid' splits.
    - [x] Create `DataLoader` instances.
    - [x] **Test (`DFZQ_TRAIN_TEST.py`)**: Test loader creation and batch output.

- [ ] **4. Model Refinement (`src/models/rnn/gru/dfzq_gru/dfzq_gru.py`)**
    - [ ] (No changes needed from previous list, focus on GRU/Attention details).
    - [ ] **Test (`DFZQ_TRAIN_TEST.py`)**: Ensure model tests pass.

- [ ] **5. Training Script (`src/train/Neural_networks/RNN/DFZQ_GRU/train_dfzq_gru.py`)**
    - [ ] **Dataset Check & Build Logic**: Implement logic at the start:
        - [ ] Read dataset path from config.
        - [ ] Check if the dataset path and key files (`meta/splits.parquet`, `stats.parquet`) exist.
        - [ ] If not exists, or if `--rebuild-data` flag is present:
            - Log information about building the dataset.
            - Import and call the `build_pv_dataset` function from `src.data_service.pipelines.build_pv_dataset`.
            - Handle potential errors during the build process.
    - [ ] **Load Data**: Import and use `get_train_valid_loaders` with `ParquetPVDataset`.
    - [ ] **Training Loop**: Implement `train_one_epoch`, `evaluate`, main loop (loss, optimizer, metric, early stopping, checkpointing).
    - [ ] **Configuration**: Load hyperparameters from `configs/models/rnn/gru/dfzq.yaml`.
    - [ ] **Test (`DFZQ_TRAIN_TEST.py`)**: Update tests for the main training script.
        - [ ] Test the dataset check logic (mocking file existence).
        - [ ] Test that the builder is called when data is missing (mock the builder function).
        - [ ] Test the training loop runs for 1-2 epochs with a small pre-built Parquet dataset.

- [ ] **6. Checkpoint Handling (within `train_dfzq_gru.py`)**
    - [ ] (No changes needed from previous list).
    - [ ] **Test (`DFZQ_TRAIN_TEST.py`)**: Ensure checkpoint tests pass.

- [ ] **7. Evaluation & Alpha Generation (`src/train/Neural_networks/RNN/DFZQ_GRU/dfzq_gru_trainer.py` - Refactor)**
    - [ ] Refactor to use `ParquetPVDataset`.
    - [ ] **Dataset Check**: Similar check as in the training script, but likely *fail* if the dataset doesn't exist (evaluation usually assumes data is ready).
    - [ ] Load prediction data, load best model, generate and save alpha factors.
    - [ ] **Test (`DFZQ_TRAIN_TEST.py` or dedicated test file)**: Update tests.



- [ ] **9. Documentation & Cleanup**
    - [ ] Add docstrings for `build_pv_dataset` function and `ParquetPVDataset` class.
    - [ ] Update relevant `README` or `docs` explaining the integrated build-and-train workflow.
    - [ ] Clean up old dataset code/tests/scripts when stable.