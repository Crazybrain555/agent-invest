from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

@dataclass
class TrainingConfig:
    """
    训练配置类 - TensorBoard监控版本
    移除wandb相关配置，专注于TensorBoard监控
    """
    
    # ================== 数据相关配置 ==================
    dataset_path: str = "data/Dataset/pv_v2_6"
    # dataset_path: str = "data/Dataset/pv_v1"
    output_root: str = "outputs/DFZQ_GRU_MODEL"
    
    # ================== 数据分割配置 ==================
    # 日期范围配置 - 如果设置了，会覆盖数据集内建的split
    date_ranges: Optional[Dict[str, Tuple[str, str]]] = None          # 格式: {"train": ("start", "end"), "valid": ("start", "end"), "test": ("start", "end")}
    use_custom_splits: bool = False             # 是否使用自定义日期分割
    
    # 实验标识配置
    experiment_name: Optional[str] = None    # 实验名称，None时自动生成
    auto_timestamp: bool = True              # 是否自动添加时间戳
    output_format: str = "{base}_{exp_name}_{timestamp}"  # 输出目录格式
    

    
    # ================== 特征选择配置 ==================
    selected_factors: Optional[List[str]] = None  # 🚀 新增：选择的特征列表，None时使用全部特征
    
    # ================== 数据加载配置 ==================
    batch_size: Optional[int] = None             # 废弃：现在由chunk_size控制实际GPU批次大小
    num_workers: int = 0                         # 数据加载进程数（单进程模式，减少内存消耗）
    shuffle: bool = False                         # 是否打乱数据
    chunk_size: int = 1024*2                     # 🚀 真正的批次大小 - 控制每次送入GPU的样本数
    memory_limit: Optional[str] = "16GB"           # DuckDB内存限制 - 增加内存缓冲
    use_fixed_indices: bool = True               # 使用固定索引确保数据一致性
    prefetch_factor: int = 4                     # 预取批次数（num_workers * prefetch_factor 批会被预取）
    

    
    # 数据量限制（用于调试）
    max_samples_train: Optional[int] = None
    max_samples_valid: Optional[int] = None  
    max_samples_test: Optional[int] = None
    
    # 是否保留元数据
    keep_meta_train: bool = False
    keep_meta_eval: bool = False
    
    # ================== 模型架构参数 ==================
    base_input_size: Optional[int] = None      # 基础输入特征维度，None时自动从数据集检测
    hidden_size: int = 64                       # GRU隐藏层维度
    num_layers: int = 2                         # GRU层数
    dropout: float = 0.2                        # Dropout比例
    output_size: int = 1                        # 输出维度
    bidirectional: bool = False                  # 是否使用双向GRU
    attention: bool = True                      # 是否使用注意力机制
    sequence_length: int = 30                   # 序列长度
    
    # 残差MLP配置
    input_hidden_dim: Optional[int] = None      # 输入残差块隐藏维度
    head_hidden_dim: Optional[int] = None       # 头部残差块隐藏维度
    
    # ================== 训练超参数 ==================
    lr: float = 1e-4                           # 学习率（从4e-5提升，解决梯度过小）
    weight_decay: float = 5e-2                # 权重衰减（从2e-3降低，减少过度正则化）
    max_epochs: int = 300                      # 最大训练轮数
    patience: int = 100                         # 早停耐心轮数
    seed: int =67                             # 随机种子
    force_cpu: bool = False                    # 强制使用CPU
    
    # ================== 损失函数和正则化 ==================
    alpha_corr: float = 0.5                  # 正交惩罚权重（暂时关闭排除干扰）
    lambda_ic: float = 1              # 🎯 新增：Pearson IC损失权重 [0,1]，控制IC和Huber损失比例
    lambda_wic: float = 1                  # 🎯 新增：Linear-weighted Cov损失权重 [0,1]，控制WIC和Huber损失比例
    lambda_var: float = 0.00               # 🎯 方差守门员惩罚权重：防止预测方差过度压缩（从0.05增加到0.2）
    target_std: float = 1.0                # 🎯 目标预测标准差：方差守门员的目标值
    use_amp: bool = True                       # 是否使用混合精度
    grad_clip_norm: float =5.0                # 梯度裁剪阈值（从3.0提升到5.0）
    
    # ================== 标签标准化 ==================
    standardize_labels_by_date: bool = False    # 是否按日期标准化标签（开启解决尺度问题）
    
    # ================== 学习率调度 ==================
    lr_scheduler_type: str = "warm_cos"        # 学习率调度器类型
    lr_scheduler_warmup_epochs: int = 5       # 预热轮数
    lr_scheduler_warmup_start_factor: float = 0.1 # 预热起始因子
    lr_scheduler_patience: int = 5             # ReduceLROnPlateau耐心轮数
    lr_scheduler_factor: float = 0.5           # 学习率衰减因子
    lr_scheduler_min_lr: float = 5e-6          # 最小学习率
    
    # ================== 梯度监控配置 ==================
    gradient_explosion_threshold: float = 10.0   # 梯度爆炸阈值
    gradient_vanishing_threshold: float = 1e-4   # 梯度消失阈值
    monitor_layer_gradients: bool = True       # 是否监控层级梯度
    monitor_activations: bool = True           # 是否监控激活统计
    batch_log_freq: int = 200                   # 批次级别日志频率 - 从50提升到200，减少写入频率
    monitor_gates: bool = True                 # 关闭GRU门控监控（仅调试时开启，性能影响大）
    
    # ================== 深度监控配置 ==================
    # 基础监控控制
    enable_detailed_monitoring: bool = True        # 是否开启详细监控
    detailed_log_freq: int = 2000                   # 详细监控频率 - 从100提升到500，大幅减少开销
    full_monitor_batches: int = 5                 # 前N个batch完全监控 - 从10减少到5
    
    # 精简的分布统计监控 - 按照用户建议优化
    monitor_distributions: bool = True             # 监控分布统计
    monitor_quantiles: bool = False                # 关闭分位数监控（噪音太多）
    distribution_stats: List[str] = None           # 统计类型：["mean", "std"]（精简版）
    
    # 关键层监控 - 精简到核心层
    monitor_weights: bool = True                   # 监控权重分布
    monitor_gradients_detailed: bool = True        # 监控梯度分布
    monitor_norm_params: bool = False              # 关闭归一化参数监控（价值较低）
    monitor_activations: bool = True              # 激活值监控（可选，开销较大）
    
    # 精简的层选择策略
    monitor_layer_selection: str = "key_layers"    # "all", "important", "key_layers", "custom"
    monitor_key_layers: List[str] = None           # 关键层定义
    monitor_custom_layers: List[str] = None        # 自定义监控层名
    # ================== 其他配置 ==================
    world_size: int = 1                       # 分布式训练world size

    def __post_init__(self):
        """初始化后处理"""
        if self.monitor_custom_layers is None:
            self.monitor_custom_layers = []
        
        if self.monitor_key_layers is None:
            # 设置关键层默认值（按用户建议）
            self.monitor_key_layers = [
                "gru",                             # GRU模块
                "pred_layer", "prediction", "head", # 预测层的可能名称
                "attention", "attn",               # 注意力层
                "output", "final"                  # 输出层
            ]
        
        if self.distribution_stats is None:
            # 精简的统计类型（按用户建议：只保留mean+std）
            self.distribution_stats = ["mean", "std"]
            
        # 如果设置了date_ranges，自动启用use_custom_splits
        if self.date_ranges is not None and len(self.date_ranges) > 0:
            self.use_custom_splits = True

    @property
    def input_size(self) -> Optional[int]:
        """动态计算输入维度"""
        if self.base_input_size is None:
            return None  # 需要从数据集自动检测
        return self.base_input_size
