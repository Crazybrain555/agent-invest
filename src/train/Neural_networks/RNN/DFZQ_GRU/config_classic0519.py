from dataclasses import dataclass
from typing import Optional

@dataclass
class TrainingConfig:
    """
    配置训练过程的所有超参数与路径，便于统一管理与实验复现。
    """
    # ---------------- 数据 & I/O ----------------
    # 数据集路径
    dataset_path: str = "data/Dataset/pv_v1"
    # 输出根目录 (例如：outputs/Train_sample_model001)
    output_root: str = "outputs/DFZQ_GRU_MODEL"
    # 缓存标签统计文件名
    label_stats_file: str = "label_stats.json"
    # 训练集比例 (用于未来可能的自定义数据划分)
    train_ratio: float = 0.9 
    # Dataloader: 最大样本数 (None for all samples)
    max_samples_train: Optional[int] = None
    max_samples_valid: Optional[int] = None
    max_samples_test: Optional[int] = None
    # Dataloader: 是否保留元数据
    keep_meta_train: bool = False
    keep_meta_eval: bool = False # For valid and test sets
    # 是否使用固定索引保证数据顺序一致性
    use_fixed_indices: bool = True

    # ---------------- 模型结构参数 ----------------
    # 每个时间步的特征维度
    input_size: int = 7
    # GRU 隐藏层维度
    hidden_size: int = 64
    # GRU 层数
    num_layers: int = 2
    # GRU Dropout
    dropout: float = 0.1
    # 输出维度（默认为 1）
    output_size: int = 1

    # ---------------- 训练超参数 ----------------
    # Batch 大小
    batch_size: int = 256*4
    # DataLoader 并行 worker 数
    num_workers: int = 4
    # 是否对数据进行随机打乱 (applies to all splits: train/valid/test)
    shuffle: bool = False
    # 数据流读取时每次块大小 (rows)
    chunk_size: int = 32768
    # DuckDB 内存限制，用于流式读取
    memory_limit: Optional[str] = None
    # 是否对标签按日期进行标准化
    standardize_labels_by_date: bool = True
    # 学习率
    lr: float = 2.5e-5
    # 最大训练轮数
    max_epochs: int = 300
    # 早停耐心轮数
    patience: int = 30
    # 随机数种子
    seed: int = 42
    # 是否强制使用 CPU（即使有 GPU 也不启用）
    force_cpu: bool = False

    # ---------------- 数值稳定 & 正则 ----------------
    # 相关性惩罚权重 α
    alpha_corr: float = 0.5
    # 是否启用 AMP（自动混合精度）
    use_amp: bool = True
    # 权重衰减系数
    weight_decay: float = 1e-4

    # ---------------- 梯度裁剪 ----------------
    # 梯度 norm 裁剪阈值
    grad_clip_norm: float = 1.5

    # ---------------- 学习率调度 ----------------
    # 学习率调度器类型: "plateau", "warm_cos"
    lr_scheduler_type: str = "warm_cos"
    # Warmup+CosineAnnealing: 预热轮数
    lr_scheduler_warmup_epochs: int = 20
    # Warmup+CosineAnnealing: 预热起始学习率因子
    lr_scheduler_warmup_start_factor: float = 0.1
    # ReduceLROnPlateau 调度：耐心轮数
    lr_scheduler_patience: int = 5
    # 学习率衰减因子
    lr_scheduler_factor: float = 0.5
    # 最小学习率
    lr_scheduler_min_lr: float = 1e-7
    
    # ----------------loss的正则化 ----------------
    # 方差正则权重 β (Variance Regularization weight)
    var_reg_beta: float = 0
    # 目标预测标准差 σ* (Target prediction standard deviation)
    target_std: float = 1.0

    # ---------------- 多卡 / 分布式 ----------------
    # world_size > 1 时可开启 DDP（暂不实现）
    world_size: int = 1
