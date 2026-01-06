"""
实验工具模块 - 用于生成唯一的实验目录和运行名称
"""

import os
import time
import json
from pathlib import Path
from typing import Optional, Dict, Any


def _normalize_output_dir(path_str: str) -> Path:
    """
    Normalize experiment output path strings so Windows-style paths also work in WSL.
    """
    raw = str(path_str)

    if os.name != "nt" and len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        drive = raw[0].lower()
        remainder = raw[2:].lstrip("\\/")
        raw = f"/mnt/{drive}/{remainder}"

    raw = raw.replace("\\", "/")
    return Path(raw).expanduser()


def generate_experiment_name(
    hidden_size: int,
    num_layers: int,
    lr: float,
    weight_decay: float,
    attention: bool = False,
    bidirectional: bool = False,
    custom_name: Optional[str] = None,
    dataset_name: Optional[str] = None
) -> str:
    """
    生成实验名称
    
    Args:
        hidden_size: 隐藏层大小
        num_layers: 层数
        lr: 学习率
        attention: 是否使用注意力
        bidirectional: 是否双向
        custom_name: 自定义名称
        dataset_name: 数据集名称
    
    Returns:
        实验名称字符串
    """
    if custom_name:
        return custom_name
    
    # 兼容字符串/数值类型
    try:
        lr_f = float(lr)
    except Exception:
        lr_f = 0.0
    try:
        wd_f = float(weight_decay)
    except Exception:
        wd_f = 0.0

    # 命名包含核心超参：hidden_size/num_layers/lr/wd
    exp_name = f"h{hidden_size}_l{num_layers}_lr{lr_f:.0e}_wd{wd_f:.0e}"
    if attention:
        exp_name += "_attn"
    if bidirectional:
        exp_name += "_bi"
    
    # 添加数据集名称
    if dataset_name:
        exp_name += f"_{dataset_name}"
    
    return exp_name


def generate_output_dir(
    base_output_root: str,
    experiment_name: Optional[str] = None,
    auto_timestamp: bool = True,
    output_format: str = "{base}_{exp_name}_{timestamp}",
    dataset_path: Optional[str] = None,
    **model_params
) -> str:
    """
    生成唯一的输出目录路径
    
    Args:
        base_output_root: 基础输出目录
        experiment_name: 实验名称
        auto_timestamp: 是否自动添加时间戳
        output_format: 输出目录格式
        dataset_path: 数据集路径，用于提取数据集名称
        **model_params: 模型参数，用于自动生成实验名称
    
    Returns:
        完整的输出目录路径
    """
    root = _normalize_output_dir(base_output_root)
    
    # 从数据集路径提取数据集名称
    dataset_name = None
    if dataset_path:
        dataset_name = Path(dataset_path).name
    
    # 生成实验名称
    if experiment_name is None:
        # 将dataset_name传递给generate_experiment_name
        exp_name = generate_experiment_name(dataset_name=dataset_name, **model_params)
    else:
        exp_name = experiment_name
    
    # 生成时间戳
    if auto_timestamp:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dir_name = output_format.format(
            exp_name=exp_name,
            timestamp=timestamp
        )
    else:
        dir_name = exp_name

    # 返回完整路径：始终嵌套到 base_output_root 下
    return str(root / dir_name)


def generate_run_name(
    experiment_name: Optional[str] = None,
    auto_timestamp: bool = True,
    **model_params
) -> str:
    """
    生成wandb运行名称
    
    Args:
        experiment_name: 实验名称
        auto_timestamp: 是否自动添加时间戳
        **model_params: 模型参数，用于自动生成名称
    
    Returns:
        wandb运行名称
    """
    if experiment_name:
        base_name = experiment_name
    else:
        hidden_size = model_params.get('hidden_size', 64)
        num_layers = model_params.get('num_layers', 2)
        base_name = f"dfzq_gru_h{hidden_size}_l{num_layers}"
    
    if auto_timestamp:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        return f"{base_name}_{timestamp}"
    else:
        return base_name


def create_experiment_dirs(output_dir: str) -> dict:
    """
    创建实验所需的所有目录
    
    Args:
        output_dir: 输出目录路径
    
    Returns:
        包含各个子目录路径的字典
    """
    root = _normalize_output_dir(output_dir)
    
    dirs = {
        'root': root,
        'ckpt': root / "ckpt",
        'logs': root / "logs", 
        'bt_results': root / "bt_results",
        'wandb': root / "logs_wandb"
    }
    
    # 创建所有目录
    for dir_path in dirs.values():
        dir_path.mkdir(parents=True, exist_ok=True)
    
    return {k: str(v) for k, v in dirs.items()}


def get_experiment_summary(config) -> dict:
    """
    获取实验摘要信息
    
    Args:
        config: 训练配置对象
    
    Returns:
        实验摘要字典
    """
    model_params = {
        'hidden_size': config.hidden_size,
        'num_layers': config.num_layers,
        'lr': config.lr,
        'weight_decay': getattr(config, 'weight_decay', 0.0),
        'attention': getattr(config, 'attention', False),
        'bidirectional': getattr(config, 'bidirectional', False),
    }
    
    # 从数据集路径提取数据集名称
    dataset_name = None
    if hasattr(config, 'dataset_path') and config.dataset_path:
        dataset_name = Path(config.dataset_path).name
    
    # 确保 exp_tag 优先影响输出目录命名（即使在 from_yaml 之后被覆盖）
    output_format = getattr(config, 'output_format', None) or "{exp_name}_{timestamp}"
    if getattr(config, 'exp_tag', None):
        output_format = f"{config.exp_tag}_{{exp_name}}_{{timestamp}}"

    output_dir = generate_output_dir(
        base_output_root=config.output_root,
        experiment_name=config.experiment_name,
        auto_timestamp=config.auto_timestamp,
        output_format=output_format,
        dataset_path=getattr(config, 'dataset_path', None),
        **model_params
    )
    
    run_name = generate_run_name(
        experiment_name=config.experiment_name,
        auto_timestamp=config.auto_timestamp,
        **model_params
    )
    
    return {
        'output_dir': output_dir,
        'run_name': run_name,
        'experiment_name': config.experiment_name or generate_experiment_name(dataset_name=dataset_name, **model_params),
        'model_params': model_params
    } 


def save_experiment_config(output_dir: str, config: Any, model_config: Any = None) -> str:
    """
    保存实验配置到输出目录
    
    Args:
        output_dir: 输出目录路径
        config: 训练配置对象
        model_config: 模型配置对象
    
    Returns:
        配置文件路径
    """
    config_root = _normalize_output_dir(output_dir)
    config_path = config_root / "experiment_config.json"
    
    # 提取训练配置
    training_config = {}
    
    # 基本训练参数
    # 1) 基础字段（稳定）
    basic_attrs = [
        # 数据与IO
        'dataset_path', 'output_root', 'use_custom_splits', 'date_ranges',
        'selected_factors', 'duck_threads', 'duck_memory', 'duck_cache',
        'chunk_size', 'batch_size', 'num_workers', 'shuffle', 'prefetch_factor',
        'memory_limit', 'use_fixed_indices',
        # 训练超参
        'lr', 'weight_decay', 'max_epochs', 'seed', 'use_amp',
        'grad_clip_norm', 'alpha_corr', 'lambda_ic', 'lambda_wic', 'lambda_var',
        'target_std', 'patience',
        # 学习率调度
        'lr_scheduler_type', 'lr_scheduler_warmup_epochs', 'lr_scheduler_warmup_start_factor',
        'lr_scheduler_patience', 'lr_scheduler_factor', 'lr_scheduler_min_lr',
        # 监控/日志
        'gradient_explosion_threshold', 'gradient_vanishing_threshold',
        'monitor_layer_gradients', 'monitor_activations', 'batch_log_freq', 'monitor_gates',
        'enable_detailed_monitoring', 'detailed_log_freq', 'full_monitor_batches',
        'monitor_distributions', 'monitor_quantiles', 'distribution_stats',
        'monitor_weights', 'monitor_gradients_detailed', 'monitor_norm_params',
        'monitor_layer_selection', 'monitor_key_layers', 'monitor_custom_layers',
        # 其他
        'world_size', 'base_input_size', 'sequence_length', 'standardize_labels_by_date',
        # 因子/DB 相关（用于推理/补齐对齐）
        'features_tables', 'labels_table', 'stats_table', 'restricted_table',
        'clip_std', 'factor_based_nan_handling', 'consecutive_nan_threshold',
        'winsorise_labels', 'label_shift'
    ]
    
    for attr in basic_attrs:
        if hasattr(config, attr):
            value = getattr(config, attr)
            # 确保值可以JSON序列化
            if isinstance(value, (str, int, float, bool, list, dict, type(None))):
                training_config[attr] = value
            else:
                training_config[attr] = str(value)

    # 2) 自动补全：将 config 中其他可序列化字段一并写入（避免未来新增遗漏）
    try:
        for key, value in vars(config).items():
            if key in training_config:
                continue
            if isinstance(value, (str, int, float, bool, list, dict, type(None))):
                training_config[key] = value
            else:
                try:
                    # 尝试将不可直接序列化的对象转为字符串（兜底）
                    training_config[key] = str(value)
                except Exception:
                    # 实在不行则忽略
                    pass
    except Exception:
        # 某些配置对象可能不支持 vars()，忽略
        pass
    
    # 提取模型配置
    model_params = {}
    if model_config:
        model_attrs = [
            'input_size', 'hidden_size', 'num_layers', 'dropout', 'output_size',
            'bidirectional', 'attention', 'input_hidden_dim', 'head_hidden_dim'
        ]
        
        for attr in model_attrs:
            if hasattr(model_config, attr):
                value = getattr(model_config, attr)
                if isinstance(value, (str, int, float, bool, type(None))):
                    model_params[attr] = value
                else:
                    model_params[attr] = str(value)
    
    # 其他模型参数从训练配置中获取
    model_attrs_from_train = ['hidden_size', 'num_layers', 'attention', 'bidirectional']
    for attr in model_attrs_from_train:
        if hasattr(config, attr) and attr not in model_params:
            value = getattr(config, attr)
            if isinstance(value, (str, int, float, bool, type(None))):
                model_params[attr] = value
    
    # 计算实际的input_size（考虑selected_factors）
    if hasattr(config, 'selected_factors') and config.selected_factors:
        model_params['input_size'] = len(config.selected_factors)
        model_params['actual_features'] = config.selected_factors
    
    # 构建完整配置
    experiment_config = {
        'experiment_info': {
            'created_at': time.strftime("%Y-%m-%d %H:%M:%S"),
            'output_dir': output_dir,
            'dataset_path': getattr(config, 'dataset_path', None)
        },
        'training_config': training_config,
        'model_config': model_params,
        'version': '1.0'  # 配置文件版本
    }
    
    # 保存配置文件
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(experiment_config, f, indent=2, ensure_ascii=False)
    
    return str(config_path)


def load_experiment_config(output_dir: str) -> Optional[Dict[str, Any]]:
    """
    从输出目录加载实验配置
    
    Args:
        output_dir: 输出目录路径
    
    Returns:
        配置字典，如果文件不存在则返回None
    """
    config_root = _normalize_output_dir(output_dir)
    config_path = config_root / "experiment_config.json"
    
    if not config_path.exists():
        return None
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"加载配置文件失败: {e}")
        return None
