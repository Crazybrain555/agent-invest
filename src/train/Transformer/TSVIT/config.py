# -*- coding: utf-8 -*-
"""
TSViT 配置加载器 - 统一从 YAML 读取训练和模型参数
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class TSViTConfig:
    """TSViT 统一配置类"""
    
    # ===== 模型参数（仅类型定义，值由 YAML 提供） =====
    # 数据
    T: Optional[int] = None
    D: Optional[int] = None
    lead: Optional[int] = None
    
    # Patch
    P: Optional[int] = None
    S: Optional[int] = None
    pre_keep_len: Optional[int] = 0
    patch_mode: Optional[str] = None
    hidden_size: Optional[int] = None
    dt: Optional[int] = None
    share_timeproj: Optional[bool] = None
    ms_branches: Optional[list] = None
    
    # Position encoding
    pos_encoding: Optional[str] = None
    rope_pct: Optional[float] = None
    rope_theta: Optional[float] = None
    rpb_max_dist: Optional[int] = None
    pos_dropout: Optional[float] = None
    
    # Encoder
    nheads: Optional[int] = None
    num_layers: Optional[int] = None
    ffn_mult: Optional[int] = None
    dropout: Optional[float] = None
    attn_dropout: Optional[float] = None
    norm_first: Optional[bool] = None
    drop_path_rate: Optional[float] = None
    encoder_impl: Optional[str] = None
    
    # Head & token 级正则
    use_cls: Optional[bool] = None
    head_type: Optional[str] = None
    token_drop_p: Optional[float] = None
    fv_bn: Optional[bool] = None
    
    # 新头部额外参数
    head_cls_use_cosine: Optional[bool] = None      # CLS头是否使用余弦打分
    head_temperature: Optional[float] = None        # 余弦/softmax温度参数（通用）
    head_pool_use_rff: Optional[bool] = None        # Pool头是否使用轻量rFF
    head_pma_k: Optional[int] = None                # PMA头seed数量
    head_pma_use_rff_kv: Optional[bool] = None      # PMA头KV是否使用rFF
    # 预测归一化（横截面）
    pred_norm: Optional[str] = None                 # 'none' | 'batchnorm' | 'zscore'
    pred_norm_affine: Optional[bool] = None
    pred_norm_eps: Optional[float] = None
    
    # ===== 训练参数 =====
    optimizer: Optional[str] = None
    lr: Optional[float] = None
    weight_decay: Optional[float] = None
    # 分组权重衰减因子
    wd_factor_attention: Optional[float] = None
    wd_factor_head: Optional[float] = None
    wd_factor_patch_embed: Optional[float] = None
    batch_size: Optional[int] = None
    epochs: Optional[int] = None
    grad_clip: Optional[float] = None
    
    # 调度器
    scheduler_name: Optional[str] = None
    warmup_epochs: Optional[int] = None
    scheduler_warmup_start_factor: Optional[float] = None
    scheduler_min_lr: Optional[float] = None
    
    # 损失（固定为组合损失：wic + huber）
    wic_mode: Optional[str] = None     # corr | cov
    loss_delta: Optional[float] = None
    lambda_wic: Optional[float] = None
    huber_tau: Optional[float] = None
    loss_focus: Optional[str] = None   # symmetric | long_top | topk | label_pos
    loss_topk: Optional[float] = None  # 比例(0~1)或个数(>1)
    use_orthogonality_penalty: Optional[bool] = None
    alpha_corr: Optional[float] = None
    
    # ===== 数据加载参数 =====
    dataset_path: Optional[str] = None
    loader_backend: Optional[str] = None  # 'duckseq' | 'legacy'
    seq_len: Optional[int] = None         # 序列长度（优先于 schema）
    num_workers: Optional[int] = None
    shuffle: Optional[bool] = None
    seed: Optional[int] = None
    memory_limit: Optional[str] = None
    use_fixed_indices: Optional[bool] = None
    use_ok_indices: Optional[bool] = None
    prefetch_factor: Optional[int] = None
    chunk_size: Optional[int] = None
    # DataLoader 性能参数
    pin_memory: Optional[bool] = None
    persistent_workers: Optional[bool] = None
    # DuckDB 参数
    duck_threads: Optional[int] = None
    duck_memory: Optional[str] = None
    duck_cache: Optional[str] = None
    duck_materialize: Optional[bool] = None
    duck_persist_conn: Optional[bool] = None
    duck_max_temp: Optional[str] = None
    skip_order_by: Optional[bool] = None
    
    # 日期范围
    use_custom_splits: Optional[bool] = None
    date_ranges: Optional[dict] = None
    
    # 特征和样本限制
    selected_factors: Optional[list] = None
    max_samples_train: Optional[int] = None
    max_samples_valid: Optional[int] = None
    max_samples_test: Optional[int] = None
    
    # batch分组方式（新增）
    batch_by: Optional[str] = None           # batch分组方式：'date'=按日期分组（单日batch），'chunk'=按样本数分组（默认）
    
    # ===== 数据加载优化参数（新增） =====
    cpu_queue_size: Optional[int] = None     # TwoStagePrefetcher CPU预取队列深度（默认2）
    io_half: Optional[bool] = None           # 启用fp16 I/O传输（H2D时自动转换，减半带宽）
    opt_log_freq: Optional[int] = None       # 优化器统计日志频率（默认500，降低以减少CPU开销）
    step_log_freq: Optional[int] = None      # 训练步日志频率（默认100，降低以减少CPU开销）
    days_per_fetch: Optional[int] = None     # Streaming dataset一次读取的交易日数量（默认10）
    part_pad: Optional[str] = None           # 宽表分区目录是否补零 (auto/padded/unpadded)
    
    # ===== 其他训练设置 =====
    use_amp: Optional[bool] = None
    force_cpu: Optional[bool] = None
    patience: Optional[int] = None
    
    # 输出
    output_root: Optional[str] = None
    exp_tag: Optional[str] = None  # 自定义实验标注
    # 与 experiment_utils 兼容的字段
    experiment_name: Optional[str] = None
    auto_timestamp: Optional[bool] = None
    output_format: Optional[str] = None
    # 为 experiment_utils 提供的映射字段
    attention: Optional[bool] = None
    bidirectional: Optional[bool] = None
    
    @classmethod
    def from_yaml(cls, yaml_path: str) -> 'TSViTConfig':
        """从 YAML 文件加载配置"""
        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {yaml_path}")
        
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        # 提取模型参数
        model_params = data.get('model', {})
        train_params = data.get('train', {})
        loss_params = data.get('loss', {})
        
        # 创建配置实例（不带默认值，全部从YAML/CLI注入）
        config = cls()
        
        # 设置模型参数
        for key, value in model_params.items():
            if key == 'module':  # 跳过模块路径
                continue
            if hasattr(config, key):
                setattr(config, key, value)
        
        # 设置训练参数
        for key, value in train_params.items():
            if key == 'scheduler':
                # 处理调度器嵌套配置
                config.scheduler_name = value.get('name', config.scheduler_name or 'cosine')
                config.warmup_epochs = value.get('warmup_epochs', config.warmup_epochs or 5)
                config.scheduler_warmup_start_factor = value.get(
                    'warmup_start_factor',
                    config.scheduler_warmup_start_factor if config.scheduler_warmup_start_factor is not None else 0.1
                )
                config.scheduler_min_lr = value.get(
                    'min_lr',
                    config.scheduler_min_lr if config.scheduler_min_lr is not None else max(1e-6, float(config.lr) * 0.01)
                )
            elif hasattr(config, key):
                setattr(config, key, value)
        
        # 设置数据参数
        data_params = data.get('data', {})
        for key, value in data_params.items():
            if key == 'date_ranges' and isinstance(value, dict):
                # 处理日期范围的特殊格式转换
                config.date_ranges = {k: tuple(v) for k, v in value.items()}
            elif hasattr(config, key):
                setattr(config, key, value)
        
        # 设置输出参数
        output_params = data.get('output', {})
        for key, value in output_params.items():
            if hasattr(config, key):
                setattr(config, key, value)
        
        # 设置损失参数
        if 'wic_mode' in loss_params:
            config.wic_mode = loss_params['wic_mode']
        if 'delta' in loss_params:
            config.loss_delta = loss_params['delta']
        if 'lambda_wic' in loss_params:
            config.lambda_wic = loss_params['lambda_wic']
        if 'tau' in loss_params:
            config.huber_tau = loss_params['tau']
        if 'focus' in loss_params:
            config.loss_focus = loss_params['focus']
        if 'topk' in loss_params:
            config.loss_topk = loss_params['topk']
        if 'use_orthogonality_penalty' in loss_params:
            config.use_orthogonality_penalty = loss_params['use_orthogonality_penalty']
        if 'alpha_corr' in loss_params:
            config.alpha_corr = loss_params['alpha_corr']

        # 设置experiment_utils兼容字段
        config.experiment_name = config.experiment_name  # 保持None以触发自动参数生成
        config.auto_timestamp = config.auto_timestamp if config.auto_timestamp is not None else True
        
        # 如果有exp_tag，修改output_format来包含标注
        if hasattr(config, 'exp_tag') and config.exp_tag:
            config.output_format = f"{config.exp_tag}_{{exp_name}}_{{timestamp}}"
        else:
            config.output_format = config.output_format or "{exp_name}_{timestamp}"
        
        # 映射字段给experiment_utils使用
        config.attention = True
        config.bidirectional = False
        
        return config
    
    def get_model_params(self) -> Dict[str, Any]:
        """获取模型初始化参数"""
        return {
            'T': self.T,
            'D': self.D,
            'lead': self.lead,
            'P': self.P,
            'S': self.S,
            'pre_keep_len': self.pre_keep_len,
            'patch_mode': self.patch_mode,
            'hidden_size': self.hidden_size,
            'dt': self.dt,
            'share_timeproj': self.share_timeproj,
            'ms_branches': self.ms_branches,
            'pos_encoding': self.pos_encoding,
            'rope_pct': self.rope_pct,
            'rope_theta': self.rope_theta,
            'rpb_max_dist': self.rpb_max_dist,
            'pos_dropout': self.pos_dropout,
            'nheads': self.nheads,
            'num_layers': self.num_layers,
            'ffn_mult': self.ffn_mult,
            'dropout': self.dropout,
            'attn_dropout': self.attn_dropout,
            'norm_first': self.norm_first,
            'drop_path_rate': self.drop_path_rate,
            'encoder_impl': self.encoder_impl,
            'use_cls': self.use_cls,
            'head_type': self.head_type,
            'token_drop_p': self.token_drop_p,
            'fv_bn': self.fv_bn,
            'head_cls_use_cosine': self.head_cls_use_cosine,
            'head_temperature': self.head_temperature,
            'head_pool_use_rff': self.head_pool_use_rff,
            'head_pma_k': self.head_pma_k,
            'head_pma_use_rff_kv': self.head_pma_use_rff_kv,
            'pred_norm': self.pred_norm,
            'pred_norm_affine': self.pred_norm_affine,
            'pred_norm_eps': self.pred_norm_eps,
        }
