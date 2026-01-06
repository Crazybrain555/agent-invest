#!/usr/bin/env python3
"""
Training script for PyTorch nn.Transformer Based Encoder-Only Model

This script trains an encoder-only transformer model using PyTorch's built-in
torch.nn.Transformer module instead of custom implementation. It maintains the same
input/output/loss structure as the existing models for compatibility.
"""

import os
import sys
import yaml
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
from pathlib import Path
import numpy as np
from typing import Dict, Any, Tuple, Optional, List
from collections import defaultdict
import warnings
from tqdm import tqdm
from scipy.stats import spearmanr, pearsonr

# Add the src directory to the Python path
sys.path.append(str(Path(__file__).parent / "src"))

from src.models.transformer.torch.torch_nn_transformer import TorchTransformerEncoder
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_train_valid_test_loaders
from src.utils.experiment_utils import get_experiment_summary, create_experiment_dirs
from src.train.Neural_networks.RNN.DFZQ_GRU.training_monitor import TrainingMonitor


# ===============================================
# 🎯 Loss Functions and Metrics (same as existing)
# ===============================================

def orthogonality_penalty(fv: torch.Tensor, eps: float = 1e-3) -> torch.Tensor:
    """稳定的正交惩罚实现"""
    if fv.size(0) <= 1:
        return torch.tensor(0.0, device=fv.device)
        
    # 中心化
    x = fv - fv.mean(dim=0, keepdim=True)
    
    # 标准化
    var = x.var(dim=0, unbiased=False).clamp(min=eps)
    std = torch.sqrt(var)
    x_norm = x / std.unsqueeze(0)
    
    # 相关矩阵
    n = x.size(0)
    corr = (x_norm.T @ x_norm) / n
    
    # 去除对角线
    eye = torch.eye(corr.size(0), device=corr.device)
    corr = corr - eye
    
    return (corr ** 2).mean()


def linear_weighted_cov_loss(
        preds: torch.Tensor,
        labels: torch.Tensor,
        mask: Optional[torch.Tensor] = None,
        eps: float = 1e-8,
) -> torch.Tensor:
    """
    线性加权协方差损失（返回负值以供最小化）
    权重 wi = (n-1-rank)/(n-1)，0 ≤ rank ≤ n-1，rank 按预测值降序
    """
    preds, labels = preds.squeeze(-1), labels.squeeze(-1)

    if mask is not None:                       # 可选 NaN / 有效性 mask
        keep = mask.squeeze(-1).bool()
        preds, labels = preds[keep], labels[keep]

    n = preds.numel()
    if n <= 1:
        return preds.new_zeros(())

    order = torch.argsort(-preds)              # 预测值从大到小
    rank  = torch.arange(n, device=preds.device).float()
    w     = (n - 1 - rank) / (n - 1)           # 最好样本 w=1，最差 w=0
    w     = w[order]                           # ⚠️ 权重对齐到排序后样本

    x, y  = preds[order], labels[order]

    mu_x  = (w * x).sum() / (w.sum() + eps)    # 加权均值
    mu_y  = (w * y).sum() / (w.sum() + eps)

    cov_xy = (w * (x - mu_x) * (y - mu_y)).sum() / (w.sum() + eps)
    
    return -cov_xy


def combined_loss(preds: torch.Tensor, labels: torch.Tensor, 
                 lambda_wic: float = 1, huber_delta: float = 1.0, 
                 mask: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    🎯 新组合损失：λ_wic * Linear_weighted_IC_loss + (1-λ_wic) * Huber_loss
    """
    # Linear-weighted Cov损失：优化排序能力（不做方差归一化）
    wic_loss = linear_weighted_cov_loss(preds, labels, mask)
    
    # Huber损失：保持数值尺度
    if mask is not None:
        # 如果有mask，只对有效样本计算Huber损失
        valid_preds = preds[mask]
        valid_labels = labels[mask]
        if valid_preds.numel() > 0:
            huber_loss = nn.functional.huber_loss(valid_preds, valid_labels, delta=huber_delta)
        else:
            huber_loss = torch.tensor(0.0, device=preds.device)
    else:
        huber_loss = nn.functional.huber_loss(preds, labels, delta=huber_delta)
    
    # 零惩罚（保持接口兼容，原方差惩罚已移除）
    zero_penalty = torch.tensor(0.0, device=preds.device)
    
    # 加权组合
    total_loss = lambda_wic * wic_loss + (1.0 - lambda_wic) * huber_loss
    
    return total_loss, wic_loss, huber_loss, zero_penalty


def spearman_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    """计算Spearman相关系数"""
    try:
        pred_np = pred.detach().cpu().numpy().flatten()
        label_np = label.detach().cpu().numpy().flatten()
        corr_stat, _ = spearmanr(pred_np, label_np, nan_policy="omit")  # type: ignore
        return float(corr_stat) if not np.isnan(corr_stat) else 0.0  # type: ignore
    except Exception:
        return 0.0


def pearson_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    """计算Pearson相关系数"""
    try:
        pred_np = pred.detach().cpu().numpy().flatten()
        label_np = label.detach().cpu().numpy().flatten()
        valid_mask = ~(np.isnan(pred_np) | np.isnan(label_np))
        if np.sum(valid_mask) < 2:
            return 0.0
        corr_stat, _ = pearsonr(pred_np[valid_mask], label_np[valid_mask])  # type: ignore
        return float(corr_stat) if not np.isnan(corr_stat) else 0.0  # type: ignore
    except Exception:
        return 0.0


class ConfigLoader:
    """Load and validate configuration from YAML file."""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config()
        self._resolve_preset()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config
    
    def _resolve_preset(self):
        """Resolve preset configuration if specified."""
        preset = self.config.get('preset', 'medium')
        presets = self.config.get('presets', {})
        
        if preset in presets:
            print(f"📋 Using preset: {preset} - {presets[preset].get('description', '')}")
            preset_config = presets[preset]
            
            # Merge preset configuration with base configuration
            if 'architecture' in preset_config:
                self.config['architecture'] = preset_config['architecture']
            if 'training' in preset_config:
                self.config['training'] = preset_config['training']
        elif preset != 'custom':
            print(f"⚠️  Warning: Preset '{preset}' not found, using custom configuration")
    
    def get_architecture_config(self) -> Dict[str, Any]:
        """Get architecture configuration."""
        return self.config.get('architecture', {})
    
    def get_training_config(self) -> Dict[str, Any]:
        """Get training configuration."""
        return self.config.get('training', {})
    
    def get_data_config(self) -> Dict[str, Any]:
        """Get data configuration."""
        return self.config.get('data', {})
    
    def get_saving_config(self) -> Dict[str, Any]:
        """Get saving configuration."""
        return self.config.get('saving', {})
    
    def get_preset_name(self) -> str:
        """Get the current preset name."""
        return self.config.get('preset', 'custom')


class TorchTransformerTrainer:
    """Trainer class for the PyTorch nn.Transformer based encoder-only model."""
    
    def __init__(self, config_path: str):
        self.config_loader = ConfigLoader(config_path)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Load configurations
        self.arch_config = self.config_loader.get_architecture_config()
        self.train_config = self.config_loader.get_training_config()
        self.data_config = self.config_loader.get_data_config()
        self.save_config = self.config_loader.get_saving_config()
        
        # Get preset name for logging (needed early for setup methods)
        self.preset_name = self.config_loader.get_preset_name()
        
        # 🚀 新增：特征选择配置
        self.selected_factors = self.data_config.get('selected_factors', None)
        
        # Create experiment config object for consistent directory structure
        self._create_experiment_config()
        
        # Setup experiment directories
        self._setup_experiment_directories()
        
        # Create dataloaders first to get actual input size
        self.train_loader, self.valid_loader, self.test_loader = self._create_dataloaders()
        
        
        # Create model
        self.model = self._create_model()
        self.model.to(self.device)
        
        # Setup training components
        self.optimizer = self._create_optimizer()
        self.scheduler = self._create_scheduler()
        # Gradient clipping threshold (for stability diagnostics)
        self.grad_clip_norm = float(self.train_config.get('grad_clip_norm', 1.0))
        
        # Loss configuration (needed before logging setup)
        self.lambda_wic = 1  # Linear-weighted IC loss weight
        self.alpha_corr = 0.01  # Orthogonality penalty weight
        
        # Noise configuration
        self.noise_config = self.train_config.get('noise', {})
        self.noise_enabled = self.noise_config.get('enabled', False)
        self.noise_std = self.noise_config.get('gaussian_std', 0.01)
        self.noise_apply_to_features = self.noise_config.get('apply_to_features', True)
        self.noise_apply_to_labels = self.noise_config.get('apply_to_labels', False)
        
        # Training state
        self.current_epoch = 0
        self.best_val_ic = -float('inf')  # Track highest combined IC instead of lowest loss
        self.patience_counter = 0
        
        # Setup logging (after experiment directories are set up)
        self.writer = self._setup_logging()
        
        # Setup TrainingMonitor for comprehensive monitoring
        self.monitor = self._setup_training_monitor()

        # Keyboard control flags and listener
        self._stop_training_requested = False
        self._test_on_stop_requested = False
        self._start_keyboard_monitor()
        
        print(f"✅ TorchTransformerTrainer initialized successfully!")
        print(f"   Device: {self.device}")
        print(f"   Preset: {self.preset_name}")
        print(f"   Experiment directory: {self.output_dir}")
        print(f"   Model parameters: {sum(p.numel() for p in self.model.parameters()):,}")
        print(f"   Train batches: {len(self.train_loader)}")
        print(f"   Valid batches: {len(self.valid_loader)}")
        print(f"   Test batches: {len(self.test_loader)}")
        if self.selected_factors:
            print(f"   🎯 Selected factors: {len(self.selected_factors)} factors")
        else:
            print(f"   📊 Using all available factors")
        print(f"   Noise augmentation: {'Enabled' if self.noise_enabled else 'Disabled'}")
        if self.noise_enabled:
            print(f"     - Gaussian std: {self.noise_std}")
            print(f"     - Apply to features: {self.noise_apply_to_features}")
            print(f"     - Apply to labels: {self.noise_apply_to_labels}")
        # 🚀 新增：显示DuckDB配置
        duck_threads = self.data_config.get('duck_threads', 16)
        duck_memory = self.data_config.get('duck_memory', '16GB')
        duck_cache = self.data_config.get('duck_cache', '4GB')
        prefetch_factor = self.data_config.get('prefetch_factor', 4)
        print(f"   🦆 DuckDB配置: {duck_threads}线程, {duck_memory}内存, {duck_cache}缓存, 预取{prefetch_factor}")
    
    def _create_experiment_config(self):
        """Create experiment config object for consistent directory structure."""
        # Create a simple config object that mimics the structure expected by experiment_utils
        class ExperimentConfig:
            def __init__(self, arch_config, train_config, data_config, save_config, preset_name, selected_factors):
                # Map transformer config to GRU-style naming for experiment utils
                # Required attributes for experiment_utils.py
                self.hidden_size = arch_config['d_model']  # Map d_model to hidden_size
                self.num_layers = arch_config['num_encoder_layers']  # Map num_encoder_layers to num_layers
                self.lr = train_config['optimizer']['learning_rate']
                self.attention = True  # Transformers always use attention
                self.bidirectional = False  # Transformers are not bidirectional
                
                # Dataset and output configuration
                self.dataset_path = data_config['dataset_path']
                self.output_root = save_config['save_dir']
                self.experiment_name = f"TorchTransformer_{preset_name}"
                self.auto_timestamp = True
                self.output_format = "{base}_{exp_name}_{timestamp}"
                
                # 🚀 Date ranges configuration
                self.date_ranges = data_config.get('date_ranges', None)
                self.use_custom_splits = data_config.get('use_custom_splits', False)
                
                # 🚀 新增：特征选择配置
                self.selected_factors = selected_factors
                
                # Update output directory to include date range information
                if self.date_ranges and self.use_custom_splits:
                    valid_range = self.date_ranges.get("valid", ["", ""])
                    train_range = self.date_ranges.get("train", ["", ""])
                    self.output_root = f"outputs/encoder_only_transformer_vd_{valid_range[0]}_{valid_range[1]}_t_{train_range[0]}_{train_range[1]}"
                
                # 🚀 新增：如果使用特征选择，在输出目录中包含特征信息
                if self.selected_factors:
                    factors_suffix = f"_factors_{len(self.selected_factors)}"
                    self.output_root += factors_suffix
        
        self.experiment_config = ExperimentConfig(
            self.arch_config, 
            self.train_config, 
            self.data_config, 
            self.save_config,
            self.config_loader.get_preset_name(),
            self.selected_factors
        )
    
    def _setup_experiment_directories(self):
        """Setup experiment directories using the same structure as GRU training."""
        experiment_info = get_experiment_summary(self.experiment_config)
        self.output_dir = experiment_info['output_dir']
        self.run_name = experiment_info['run_name']
        
        # Create experiment directories
        self.dirs = create_experiment_dirs(self.output_dir)
        # Convert to Path objects for proper path operations
        self.ckpt_dir = Path(self.dirs['ckpt'])
        self.log_dir = Path(self.dirs['logs'])
        self.bt_dir = Path(self.dirs['bt_results'])
        
        print(f"📁 Experiment directory: {self.output_dir}")
        print(f"📊 Log directory: {self.log_dir}")
        print(f"💾 Checkpoint directory: {self.ckpt_dir}")
        
        # Show date range information if available
        if hasattr(self.experiment_config, 'date_ranges') and self.experiment_config.date_ranges:
            print(f"🗓️  Date ranges: {self.experiment_config.date_ranges}")
        
        # Show feature selection information if available
        if hasattr(self.experiment_config, 'selected_factors') and self.experiment_config.selected_factors:
            print(f"🎯  Selected factors: {len(self.experiment_config.selected_factors)} factors")
    
    def _create_dataloaders(self):
        """Create train/validation/test dataloaders."""
        # Prepare dataloader config
        dataloader_config = {
            "dataset_path": self.data_config['dataset_path'],
            "batch_size": self.train_config['batch_size'],
            "num_workers": 4,
            "seed": 42,
            "chunk_size": self.data_config['chunk_size'],
            "memory_limit": self.data_config['memory_limit'],
            "use_fixed_indices": self.data_config['use_fixed_indices'],
            "reverse_seq": self.data_config['reverse_seq'],
            # 🚀 添加自定义日期范围配置
            "use_custom_splits": self.data_config.get('use_custom_splits', False),
            "date_ranges": self.data_config.get('date_ranges', None),
            # 🚀 新增：添加特征选择配置
            "selected_factors": self.selected_factors,
                    # 🚀 新增：DuckDB性能调优配置
        "duck_threads": self.data_config.get('duck_threads', 16),
        "duck_memory": self.data_config.get('duck_memory', '16GB'),
        "duck_cache": self.data_config.get('duck_cache', '4GB'),
        "prefetch_factor": self.data_config.get('prefetch_factor', 4)
        }
        
        # Create dataloaders
        print(f"📊 Creating dataloaders with config: {dataloader_config}")
        if dataloader_config.get('use_custom_splits') and dataloader_config.get('date_ranges'):
            print(f"🗓️  Using custom date ranges: {dataloader_config['date_ranges']}")
        if dataloader_config.get('selected_factors'):
            print(f"🎯  Using selected factors: {len(dataloader_config['selected_factors'])} factors")
        print(f"🦆 DuckDB配置: {dataloader_config.get('duck_threads', 16)}线程, {dataloader_config.get('duck_memory', '16GB')}内存, {dataloader_config.get('duck_cache', '4GB')}缓存, 预取{dataloader_config.get('prefetch_factor', 4)}")
        
        train_loader, valid_loader, test_loader = get_train_valid_test_loaders(
            config=dataloader_config,
            keep_meta_train=False,  # No need for metadata during training
            keep_meta_eval=False,
            use_fixed_indices=dataloader_config['use_fixed_indices'],
            selected_factors=dataloader_config['selected_factors']  # 🚀 新增：传递特征选择参数
        )
        
        return train_loader, valid_loader, test_loader
            
    
    def _create_model(self) -> TorchTransformerEncoder:
        """Create the PyTorch nn.Transformer based encoder-only model."""
        model = TorchTransformerEncoder(
            input_size=self.arch_config['input_size'],
            seq_length=self.arch_config['seq_length'],
            d_model=self.arch_config['d_model'],
            nhead=self.arch_config['nhead'],
            num_encoder_layers=self.arch_config['num_encoder_layers'],
            dim_feedforward=self.arch_config['dim_feedforward'],
            dropout=self.arch_config['dropout'],
            activation=self.arch_config['activation'],
            positional_encoding=self.arch_config['positional_encoding'],
            embedding_type=self.arch_config['embedding_type'],
            norm_type=self.arch_config.get('norm_type', 'layer'),
            norm_first=self.arch_config.get('norm_first', True)
        )
        return model
    
    def _create_optimizer(self) -> optim.Optimizer:
        """Create optimizer."""
        opt_config = self.train_config['optimizer']
        
        # Ensure learning rate is a float
        learning_rate = float(opt_config['learning_rate'])
        weight_decay = float(opt_config['weight_decay'])
        beta1 = float(opt_config['beta1'])
        beta2 = float(opt_config['beta2'])
        
        if opt_config['type'] == "Adam":
            return optim.Adam(
                self.model.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay,
                betas=(beta1, beta2)
            )
        elif opt_config['type'] == "AdamW":
            return optim.AdamW(
                self.model.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay,
                betas=(beta1, beta2)
            )
        else:
            raise ValueError(f"Unsupported optimizer: {opt_config['type']}")
    
    def _create_scheduler(self):
        """Create learning rate scheduler."""
        scheduler_config = self.train_config.get('scheduler', {})
        if not scheduler_config:
            return None
            
        scheduler_type = scheduler_config.get('type', 'ReduceLROnPlateau')
        
        # Ensure scheduler parameters are floats
        factor = float(scheduler_config.get('factor', 0.9))
        patience = int(scheduler_config.get('patience', 10))
        min_lr = float(scheduler_config.get('min_lr', 1e-7))
        
        if scheduler_type == 'ReduceLROnPlateau':
            return optim.lr_scheduler.ReduceLROnPlateau(
                self.optimizer,
                mode='min',
                factor=factor,
                patience=patience,
                min_lr=min_lr
            )
        elif scheduler_type == 'CosineAnnealingLR':
            return optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=self.train_config['epochs'] - self.train_config['scheduler']['lr_scheduler_warmup_epochs'],
                eta_min=min_lr
            )
        elif scheduler_type == 'warm_cos':
            warmup_scheduler = optim.lr_scheduler.LinearLR(
                self.optimizer,
                start_factor=0.1,
                end_factor=1.0,
                total_iters=self.train_config['scheduler']['lr_scheduler_warmup_epochs']
            )
            cosine_scheduler = optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=self.train_config['epochs'] - self.train_config['scheduler']['lr_scheduler_warmup_epochs'],
                eta_min=min_lr
            )
            scheduler = optim.lr_scheduler.SequentialLR(
                self.optimizer,
                schedulers=[warmup_scheduler, cosine_scheduler],
                milestones=[self.train_config['scheduler']['lr_scheduler_warmup_epochs']]
            )
            return scheduler
        else:
            print(f"⚠️  Warning: Unknown scheduler type '{scheduler_type}', using no scheduler")
            return None
    
    def _setup_logging(self) -> SummaryWriter:
        """Setup TensorBoard logging."""
        # Choose TensorBoard log directory based on configuration
        use_experiment_dir = self.save_config.get('tensorboard_in_experiment_dir', True)
        
        if use_experiment_dir:
            # Use the same experiment directory structure as other outputs
            tensorboard_log_dir = self.log_dir
            print(f"📊 TensorBoard logs will be saved to experiment directory: {tensorboard_log_dir}")
        else:
            # Create custom TensorBoard log directory at parent/parent/tf-logs
            current_dir = Path(__file__).parent
            parent_parent_dir = current_dir.parent.parent
            tf_logs_dir = parent_parent_dir / "tf-logs"
            
            # Create the tf-logs directory if it doesn't exist
            tf_logs_dir.mkdir(parents=True, exist_ok=True)
            
            # Create a subdirectory for this specific run
            tensorboard_log_dir = tf_logs_dir / self.run_name
            print(f"📊 TensorBoard logs will be saved to tf-logs directory: {tensorboard_log_dir}")
        
        writer = SummaryWriter(
            log_dir=str(tensorboard_log_dir),
            comment=f"_{self.preset_name}_torch_transformer"
        )
        
        # Log hyperparameters (as text to avoid extra empty event file created by add_hparams)
        hparams = {
            'preset': self.preset_name,
            'd_model': self.arch_config['d_model'],
            'num_encoder_layers': self.arch_config['num_encoder_layers'],
            'nhead': self.arch_config['nhead'],
            'batch_size': self.train_config['batch_size'],
            'learning_rate': self.train_config['optimizer']['learning_rate'],
            'dropout': self.arch_config['dropout'],
            'lambda_wic': self.lambda_wic,
            'alpha_corr': self.alpha_corr,
            'noise_enabled': self.noise_enabled,
            'noise_std': self.noise_std if self.noise_enabled else 0.0
        }
        try:
            import yaml as _yaml_for_tb
            hparam_text = _yaml_for_tb.dump(hparams, default_flow_style=False, sort_keys=False)
        except Exception:
            hparam_text = str(hparams)
        writer.add_text('hparams', f"``\n{hparam_text}\n``")
        
        return writer

    def _keyboard_listener(self):
        """Simple keyboard listener for interactive control.
        Type 'q' + Enter to stop after current batch and finish the epoch summary.
        Type 't' + Enter to stop and run testing immediately after stopping.
        Type 'c' + Enter to clear any pending stop.
        """
        try:
            print("\n[Keyboard] Controls: 'q'+Enter stop, 't'+Enter stop+test, 'c'+Enter cancel")
            import sys
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd == 'q':
                    self._stop_training_requested = True
                    self._test_on_stop_requested = False
                    print("[Keyboard] Stop requested (no test)")
                elif cmd == 't':
                    self._stop_training_requested = True
                    self._test_on_stop_requested = True
                    print("[Keyboard] Stop+Test requested")
                elif cmd == 'c':
                    self._stop_training_requested = False
                    self._test_on_stop_requested = False
                    print("[Keyboard] Stop canceled")
        except Exception:
            # Non-interactive environment; ignore
            pass

    def _start_keyboard_monitor(self):
        """Start the keyboard monitor thread (non-blocking)."""
        import threading
        t = threading.Thread(target=self._keyboard_listener, daemon=True)
        t.start()
    
    def _setup_training_monitor(self) -> TrainingMonitor:
        """Setup training monitor for comprehensive monitoring."""
        monitor_config = {
            'batch_log_freq': 200,
            'detailed_log_freq': 500,
            'monitor_distributions': True,
            'monitor_gradients_detailed': True,
            'monitor_layer_selection': 'key_layers',
            'ema_decay': 0.95,
            'act_zero_epsilon': 1e-3
        }
        
        monitor = TrainingMonitor(self.writer, monitor_config)
        monitor.setup_model_monitoring(self.model)
        
        return monitor
    
    def _add_gaussian_noise(self, tensor: torch.Tensor, std: float) -> torch.Tensor:
        """Add gaussian noise to a tensor."""
        if not self.noise_enabled or std <= 0:
            return tensor
        
        noise = torch.randn_like(tensor) * std
        return tensor + noise
    
    def train_epoch(self) -> Tuple[float, float, float, float, float, float, float, float]:
        """Train for one epoch using the same loss calculation as GRU."""
        self.model.train()
        
        # Training statistics
        epoch_losses = []
        epoch_losses_main = []
        epoch_losses_ortho = []
        epoch_pearson_ics = []
        epoch_spearman_ics = []
        epoch_pred_means = []
        epoch_pred_stds = []
        epoch_grad_norms = []
        
        pbar = tqdm(self.train_loader, desc='Train', leave=False)
        early_stop_triggered = False
        for batch_idx, batch in enumerate(pbar):
            # Extract features and labels
            if len(batch) == 4:  # With metadata
                features, labels, _, _ = batch
            else:  # Without metadata
                features, labels = batch
            
            # Move to device and prepare
            features = features.to(self.device)
            labels = labels.unsqueeze(1).to(self.device).float()  # [B, 1]
            
            # Apply gaussian noise during training (if enabled)
            if self.noise_enabled:
                if self.noise_apply_to_features:
                    features = self._add_gaussian_noise(features, self.noise_std)
                if self.noise_apply_to_labels:
                    labels = self._add_gaussian_noise(labels, self.noise_std)
            
            # Forward pass
            self.optimizer.zero_grad()
            predictions, fv = self.model(features)  # [B, 1], [B, feature_dim]
            
            # 🎯 使用与GRU相同的损失计算
            # 主损失：Linear-weighted Cov + Huber
            loss_main, loss_wic_part, loss_huber_part, loss_var_part = combined_loss(
                predictions, labels, self.lambda_wic, huber_delta=1.0
            )
            
            # 正交惩罚（使用transformer的特征向量，与GRU保持一致）
            loss_ortho = self.alpha_corr * orthogonality_penalty(fv, eps=1e-3)
            
            # 总损失
            loss = loss_main + loss_ortho
            
            # Backward pass
            loss.backward()

            # Gradient clipping diagnostics
            preclip_grad_norm = torch.nn.utils.clip_grad_norm_(
                self.model.parameters(), max_norm=self.grad_clip_norm
            )
            try:
                preclip_grad_norm_value = float(preclip_grad_norm)
            except Exception:
                preclip_grad_norm_value = preclip_grad_norm.item() if hasattr(preclip_grad_norm, 'item') else float(preclip_grad_norm)

            # Compute post-clip grad norm for monitoring
            with torch.no_grad():
                postclip_sq_sum = 0.0
                for p in self.model.parameters():
                    if p.grad is not None:
                        postclip_sq_sum += float(p.grad.detach().data.norm().item() ** 2)
                postclip_grad_norm_value = postclip_sq_sum ** 0.5

            was_clipped = preclip_grad_norm_value > self.grad_clip_norm

            # Update weights
            self.optimizer.step()
            
            # Accumulate statistics
            epoch_losses.append(loss.item())
            epoch_losses_main.append(loss_main.item())
            epoch_losses_ortho.append(loss_ortho.item())
            epoch_grad_norms.append(preclip_grad_norm_value)
            
            # Calculate IC metrics
            batch_pearson_ic = pearson_ic(predictions.detach(), labels)
            batch_spearman_ic = spearman_ic(predictions.detach(), labels)
            epoch_pearson_ics.append(batch_pearson_ic)
            epoch_spearman_ics.append(batch_spearman_ic)
            
            # Calculate prediction distribution metrics
            batch_pred_mean = predictions.mean().item()
            batch_pred_std = predictions.std().item()
            epoch_pred_means.append(batch_pred_mean)
            epoch_pred_stds.append(batch_pred_std)
            
            # 🎯 Batch-level TensorBoard logging
            if self.writer is not None:
                global_step = (self.current_epoch - 1) * len(self.train_loader) + batch_idx
                self.writer.add_scalar('Train_Batch/loss_total', loss.item(), global_step)
                self.writer.add_scalar('Train_Batch/grad_norm_preclip', preclip_grad_norm_value, global_step)
                self.writer.add_scalar('Train_Batch/grad_norm_postclip', postclip_grad_norm_value, global_step)
                self.writer.add_scalar('Train_Batch/grad_was_clipped', 1.0 if was_clipped else 0.0, global_step)
                self.writer.add_scalar('Train_Batch/grad_clip_threshold', self.grad_clip_norm, global_step)
                self.writer.add_scalar('Train_Batch/pearson_ic', batch_pearson_ic, global_step)
                self.writer.add_scalar('Train_Batch/spearman_ic', batch_spearman_ic, global_step)
                self.writer.add_scalar('Train_Batch/pred_std', batch_pred_std, global_step)
            
            # 🚀 使用精简高效的TrainingMonitor进行监控
            if self.monitor:
                # 全面监控：Core + LayerDiag + Alerts
                self.monitor.monitor_comprehensive(
                    model=self.model,
                    loss=loss.item(),
                    grad_norm=preclip_grad_norm_value, 
                    lr=self.optimizer.param_groups[0]['lr'],
                    predictions=predictions.detach(),
                    labels=labels,
                    step=global_step,
                    batch_idx=batch_idx,
                    scaler=None,  # No scaler used in transformer training
                    grad_clip_threshold=self.grad_clip_norm,
                    was_clipped=was_clipped,
                    preclip_grad_norm=preclip_grad_norm_value,
                    postclip_grad_norm=postclip_grad_norm_value
                )
            
            # Update progress bar
            pbar.set_postfix(
                loss=loss.item(),
                grad_norm=preclip_grad_norm_value,
                pearson_ic=batch_pearson_ic,
                spearman_ic=batch_spearman_ic
            )

            # Keyboard stop check
            if self._stop_training_requested:
                early_stop_triggered = True
                print("\n[Keyboard] Early stop triggered. Finishing current epoch summary...")
                break
        
        # Calculate epoch averages
        avg_loss = float(np.mean(epoch_losses))
        avg_loss_main = float(np.mean(epoch_losses_main))
        avg_loss_ortho = float(np.mean(epoch_losses_ortho))
        avg_pearson_ic = float(np.mean(epoch_pearson_ics))
        avg_spearman_ic = float(np.mean(epoch_spearman_ics))
        avg_pred_std = float(np.mean(epoch_pred_stds))
        avg_pred_mean = float(np.mean(epoch_pred_means))
        avg_grad_norm = float(np.mean(epoch_grad_norms))
        
        return avg_loss, avg_loss_main, avg_loss_ortho, avg_pearson_ic, avg_spearman_ic, avg_pred_std, avg_pred_mean, avg_grad_norm
    
    def validate_epoch(self) -> Tuple[float, float, float, float, float, float]:
        """Validate for one epoch."""
        self.model.eval()
        
        # Validation statistics
        epoch_losses_main = []
        epoch_losses_ortho = []
        epoch_pearson_ics = []
        epoch_spearman_ics = []
        epoch_pred_stds = []
        epoch_pred_means = []
        
        pbar = tqdm(self.valid_loader, desc='Valid', leave=False)
        with torch.no_grad():
            for batch_idx, batch in enumerate(pbar):
                # Extract features and labels
                if len(batch) == 4:  # With metadata
                    features, labels, _, _ = batch
                else:  # Without metadata
                    features, labels = batch
                
                # Move to device and prepare
                features = features.to(self.device)
                labels = labels.unsqueeze(1).to(self.device).float()  # [B, 1]
                
                # Forward pass
                predictions, fv = self.model(features)  # [B, 1], [B, feature_dim]
                
                # 🎯 使用与GRU相同的损失计算
                # 主损失：Linear-weighted Cov + Huber
                loss_main, loss_wic_part, loss_huber_part, loss_var_part = combined_loss(
                    predictions, labels, self.lambda_wic, huber_delta=1.0
                )
                
                # 正交惩罚（使用transformer的特征向量，与GRU保持一致）
                loss_ortho = self.alpha_corr * orthogonality_penalty(fv, eps=1e-3)
                
                # Accumulate statistics
                epoch_losses_main.append(loss_main.item())
                epoch_losses_ortho.append(loss_ortho.item())
                
                # Calculate IC metrics
                batch_pearson_ic = pearson_ic(predictions, labels)
                batch_spearman_ic = spearman_ic(predictions, labels)
                epoch_pearson_ics.append(batch_pearson_ic)
                epoch_spearman_ics.append(batch_spearman_ic)
                
                # Calculate prediction distribution metrics
                epoch_pred_means.append(predictions.mean().item())
                epoch_pred_stds.append(predictions.std().item())
                
                # Update progress bar
                pbar.set_postfix(
                    loss_main=loss_main.item(),
                    loss_ortho=loss_ortho.item(),
                    pearson_ic=batch_pearson_ic,
                    spearman_ic=batch_spearman_ic
                )
        
        # Calculate epoch averages
        avg_loss_main = float(np.mean(epoch_losses_main))
        avg_loss_ortho = float(np.mean(epoch_losses_ortho))
        avg_pearson_ic = float(np.mean(epoch_pearson_ics))
        avg_spearman_ic = float(np.mean(epoch_spearman_ics))
        avg_pred_std = float(np.mean(epoch_pred_stds))
        avg_pred_mean = float(np.mean(epoch_pred_means))
        
        return avg_loss_main, avg_loss_ortho, avg_pearson_ic, avg_spearman_ic, avg_pred_std, avg_pred_mean
    
    def test_model(self, checkpoint_path: Optional[str] = None) -> Tuple[float, float, float, float, float, float]:
        """Test the model on test set."""
        # Load checkpoint if provided
        if checkpoint_path:
            if not os.path.exists(checkpoint_path):
                raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")
            
            print(f"🔧 Loading checkpoint for testing: {checkpoint_path}")
            checkpoint = torch.load(checkpoint_path, map_location=self.device, weights_only=False)
            self.model.load_state_dict(checkpoint["model_state_dict"])
            self.model.to(self.device)  # Ensure model is on correct device
            
            # Load checkpoint info
            epoch = checkpoint.get('epoch', 'N/A')
            val_loss = checkpoint.get('val_loss', 'N/A')
            print(f"✅ Checkpoint loaded - Epoch: {epoch}, Val Loss: {val_loss}")
            print(f"🎯 Model moved to device: {self.device}")
        else:
            # Use best model from current training
            best_ckpt_path = self.ckpt_dir / "best_model.pth"
            if best_ckpt_path.exists():
                print(f"🔧 Loading best model for testing: {best_ckpt_path}")
                checkpoint = torch.load(best_ckpt_path, map_location=self.device, weights_only=False)
                self.model.load_state_dict(checkpoint["model_state_dict"])
                self.model.to(self.device)  # Ensure model is on correct device
                
                epoch = checkpoint.get('epoch', 'N/A')
                best_val_ic = checkpoint.get('best_val_ic', 'N/A')
                print(f"✅ Best model loaded - Epoch: {epoch}, Best Val IC: {best_val_ic}")
                print(f"🎯 Model moved to device: {self.device}")
            else:
                print("⚠️  No best model found, using current model state")
        
        # Test evaluation
        self.model.eval()
        
        # Test statistics
        test_losses_main = []
        test_losses_ortho = []
        test_pearson_ics = []
        test_spearman_ics = []
        test_pred_stds = []
        test_pred_means = []
        
        print("🧪 Testing model on test set...")
        pbar = tqdm(self.test_loader, desc='Test', leave=False)
        with torch.no_grad():
            for batch_idx, batch in enumerate(pbar):
                # Extract features and labels
                if len(batch) == 4:  # With metadata
                    features, labels, _, _ = batch
                else:  # Without metadata
                    features, labels = batch
                
                # Move to device and prepare
                features = features.to(self.device)
                labels = labels.unsqueeze(1).to(self.device).float()  # [B, 1]
                
                # Forward pass
                predictions, fv = self.model(features)  # [B, 1], [B, feature_dim]
                
                # 🎯 使用与GRU相同的损失计算
                # 主损失：Linear-weighted Cov + Huber
                loss_main, loss_wic_part, loss_huber_part, loss_var_part = combined_loss(
                    predictions, labels, self.lambda_wic, huber_delta=1.0
                )
                
                # 正交惩罚（使用transformer的特征向量，与GRU保持一致）
                loss_ortho = self.alpha_corr * orthogonality_penalty(fv, eps=1e-3)
                
                # Accumulate statistics
                test_losses_main.append(loss_main.item())
                test_losses_ortho.append(loss_ortho.item())
                
                # Calculate IC metrics
                batch_pearson_ic = pearson_ic(predictions, labels)
                batch_spearman_ic = spearman_ic(predictions, labels)
                test_pearson_ics.append(batch_pearson_ic)
                test_spearman_ics.append(batch_spearman_ic)
                
                # Calculate prediction distribution metrics
                test_pred_means.append(predictions.mean().item())
                test_pred_stds.append(predictions.std().item())
                
                # Update progress bar
                pbar.set_postfix(
                    loss_main=loss_main.item(),
                    loss_ortho=loss_ortho.item(),
                    pearson_ic=batch_pearson_ic,
                    spearman_ic=batch_spearman_ic
                )
        
        # Calculate test averages
        test_loss_main = float(np.mean(test_losses_main))
        test_loss_ortho = float(np.mean(test_losses_ortho))
        test_pearson_ic = float(np.mean(test_pearson_ics))
        test_spearman_ic = float(np.mean(test_spearman_ics))
        test_pred_std = float(np.mean(test_pred_stds))
        test_pred_mean = float(np.mean(test_pred_means))
        
        # Calculate combined IC
        test_combined_ic = test_pearson_ic * 0.5 + test_spearman_ic * 0.5
        
        # Log test results to TensorBoard
        if self.writer is not None:
            epoch_for_logging = self.current_epoch if self.current_epoch > 0 else 1
            self.writer.add_scalar("Test/pearson_ic", test_pearson_ic, epoch_for_logging)
            self.writer.add_scalar("Test/spearman_ic", test_spearman_ic, epoch_for_logging)
            self.writer.add_scalar("Test/combined_ic", test_combined_ic, epoch_for_logging)
            self.writer.add_scalar("Test/loss_main", test_loss_main, epoch_for_logging)
            self.writer.add_scalar("Test/loss_ortho", test_loss_ortho, epoch_for_logging)
            self.writer.add_scalar("Test/pred_mean", test_pred_mean, epoch_for_logging)
            self.writer.add_scalar("Test/pred_std", test_pred_std, epoch_for_logging)
        
        # Print test results
        print(f"\n📊 Test Results:")
        print(f"   Test Loss: Main={test_loss_main:.6f}, Ortho={test_loss_ortho:.6f}")
        print(f"   Test IC: Pearson={test_pearson_ic:.6f}, Spearman={test_spearman_ic:.6f}")
        print(f"   Combined IC: {test_combined_ic:.6f}")
        print(f"   Pred Distribution: Mean={test_pred_mean:.6f}, Std={test_pred_std:.6f}")
        
        return test_loss_main, test_loss_ortho, test_pearson_ic, test_spearman_ic, test_pred_std, test_pred_mean
    
    def save_checkpoint(self, val_loss: float, val_combined_ic: float, is_best: bool = False):
        """Save model checkpoint."""
        checkpoint = {
            'epoch': self.current_epoch,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scheduler_state_dict': self.scheduler.state_dict() if self.scheduler else None,
            'val_loss': val_loss,
            'val_combined_ic': val_combined_ic,
            'best_val_ic': self.best_val_ic,
            'arch_config': self.arch_config,
            'train_config': self.train_config,
            'preset_name': self.preset_name
        }
        
        # Regular checkpoint
        if self.current_epoch % self.save_config.get('save_frequency', 10) == 0:
            checkpoint_path = self.ckpt_dir / f"checkpoint_epoch_{self.current_epoch}.pth"
            torch.save(checkpoint, checkpoint_path)
            
        # Best checkpoint
        if is_best:
            best_path = self.ckpt_dir / "best_model.pth"
            torch.save(checkpoint, best_path)
            print(f"💾 Best model saved: {best_path}")
        
        # Cleanup old checkpoints
        if self.current_epoch % self.save_config.get('save_frequency', 10) == 0:
            self._cleanup_old_checkpoints(self.ckpt_dir)
    
    def _cleanup_old_checkpoints(self, save_dir: Path):
        """Clean up old checkpoints to save space."""
        max_saved = self.save_config.get('max_saved_models', 5)
        
        # Get all checkpoint files
        checkpoint_files = list(save_dir.glob("checkpoint_epoch_*.pth"))
        
        if len(checkpoint_files) > max_saved:
            # Sort by creation time and remove oldest
            checkpoint_files.sort(key=lambda x: x.stat().st_ctime)
            for old_file in checkpoint_files[:-max_saved]:
                old_file.unlink()
                print(f"🗑️  Removed old checkpoint: {old_file}")
    
    def train(self):
        """Main training loop."""
        epochs = self.train_config['epochs']
        patience = self.train_config['early_stopping']['patience']
        
        print(f"\n🚀 Starting training for {epochs} epochs...")
        print(f"   Loss weights: λ_wic={self.lambda_wic}, α_corr={self.alpha_corr}")
        if self.noise_enabled:
            print(f"   Training noise: Gaussian std={self.noise_std} (features={self.noise_apply_to_features}, labels={self.noise_apply_to_labels})")
        else:
            print(f"   Training noise: Disabled")
        
        for epoch in range(epochs):
            self.current_epoch = epoch + 1
                
            # Train
            print(f"\n📚 Epoch {self.current_epoch}/{epochs}")
            (train_loss, train_loss_main, train_loss_ortho, 
            train_pearson_ic, train_spearman_ic, train_pred_std, train_pred_mean, train_grad_norm) = self.train_epoch()
                
            # Validate
            (val_loss_main, val_loss_ortho, val_pearson_ic, 
            val_spearman_ic, val_pred_std, val_pred_mean) = self.validate_epoch()
                
            # Calculate combined validation loss and IC for scheduler and early stopping
            val_loss = val_loss_main + val_loss_ortho
            val_combined_ic = (val_pearson_ic + val_spearman_ic) / 2.0  # Mean of Pearson and Spearman IC
                
            # Update learning rate scheduler (still uses loss for scheduler)
            if self.scheduler is not None:
                if isinstance(self.scheduler, optim.lr_scheduler.ReduceLROnPlateau):
                    self.scheduler.step(val_loss)
                else:
                    self.scheduler.step()
                
            # 🎯 Log metrics using exact naming convention from training guide
            # Loss (损失函数)
            self.writer.add_scalar('Loss/train', train_loss, epoch)
            self.writer.add_scalar('Loss/train_main', train_loss_main, epoch)
            self.writer.add_scalar('Loss/train_ortho', train_loss_ortho, epoch)
            self.writer.add_scalar('Loss/val_main', val_loss_main, epoch)
            self.writer.add_scalar('Loss/val_ortho', val_loss_ortho, epoch)
            self.writer.add_scalar('Loss/val_total', val_loss, epoch)
                
            # IC (信息系数)
            self.writer.add_scalar('IC/train_pearson', train_pearson_ic, epoch)
            self.writer.add_scalar('IC/train_spearman', train_spearman_ic, epoch)
            self.writer.add_scalar('IC/val_pearson', val_pearson_ic, epoch)
            self.writer.add_scalar('IC/val_spearman', val_spearman_ic, epoch)
            self.writer.add_scalar('IC/val_combined', val_combined_ic, epoch)
                
            # Prediction distribution
            self.writer.add_scalar('Pred_Dist/train_mean', train_pred_mean, epoch)
            self.writer.add_scalar('Pred_Dist/train_std', train_pred_std, epoch)
            self.writer.add_scalar('Pred_Dist/val_mean', val_pred_mean, epoch)
            self.writer.add_scalar('Pred_Dist/val_std', val_pred_std, epoch)
                
            # Training metrics
            self.writer.add_scalar('Training/grad_norm', train_grad_norm, epoch)
            self.writer.add_scalar('Training/lr', self.optimizer.param_groups[0]['lr'], epoch)
                
            # Print epoch summary
            print(f"📊 Epoch {self.current_epoch} Results:")
            print(f"   Train Loss: {train_loss:.6f} (Main: {train_loss_main:.6f}, Ortho: {train_loss_ortho:.6f})")
            print(f"   Val Loss: {val_loss:.6f} (Main: {val_loss_main:.6f}, Ortho: {val_loss_ortho:.6f})")
            print(f"   Train IC: Pearson={train_pearson_ic:.4f}, Spearman={train_spearman_ic:.4f}")
            print(f"   Val IC: Pearson={val_pearson_ic:.4f}, Spearman={val_spearman_ic:.4f}, Combined={val_combined_ic:.4f}")
            print(f"   Grad Norm: {train_grad_norm:.4f}, LR: {self.optimizer.param_groups[0]['lr']:.2e}")
                
            # Early stopping and checkpointing based on highest combined IC
            is_best = val_combined_ic > self.best_val_ic
            if is_best:
                self.best_val_ic = val_combined_ic
                self.patience_counter = 0
            else:
                self.patience_counter += 1
                
            # Save checkpoint
            self.save_checkpoint(val_loss, val_combined_ic, is_best)
                
            # Keyboard stop (takes priority)
            if self._stop_training_requested:
                print(f"⏹️  Manual stop requested at epoch {self.current_epoch}")
                print(f"   Best validation IC so far: {self.best_val_ic:.6f}")
                break

            # Early stopping
            if self.patience_counter >= patience:
                print(f"⏹️  Early stopping triggered after {epoch + 1} epochs")
                print(f"   Best validation IC: {self.best_val_ic:.6f}")
                break
        
        print(f"\n✅ Training completed!")
        print(f"   Final validation loss: {val_loss:.6f}")
        print(f"   Best validation IC: {self.best_val_ic:.6f}")
        print(f"   Model saved in: {self.ckpt_dir}")
        
        # Post-training testing logic
        if self._stop_training_requested and self._test_on_stop_requested:
            print(f"\n🧪 Manual test requested. Testing best model on test set...")
            self.test_model()
        else:
            print(f"\n🧪 Testing best model on test set...")
            self.test_model()
        
        # Close writer
        if self.writer is not None:
            self.writer.close()


def main():
    """Main function to run training."""
    import argparse
    
    # 🎯 Default checkpoint path - modify this to your specific checkpoint
    DEFAULT_CHECKPOINT = "outputs/encoder_only_transformer_TorchTransformer_custom_20250708_010731/ckpt/best_model.pth"
    
    parser = argparse.ArgumentParser(description='Train PyTorch nn.Transformer based encoder-only model')
    parser.add_argument('--config', type=str, default='configs/models/transformer/encoder_only.yaml',
                        help='Path to configuration file')
    parser.add_argument('--preset', type=str, default=None,
                        help='Override preset in config file')
    parser.add_argument('--test-only', action='store_true', default=False,
                        help='Only run testing on specified checkpoint (default: True)')
    parser.add_argument('--checkpoint', type=str, default=DEFAULT_CHECKPOINT,
                        help=f'Path to checkpoint file for testing (default: {DEFAULT_CHECKPOINT})')
    parser.add_argument('--train', action='store_true', default=True,
                        help='Run full training instead of test-only mode')
    
    # 🚀 新增：特征选择参数
    parser.add_argument('--selected-factors', type=str, nargs='*', default=None,
                       help="选择的特征列表，用空格分隔。例如：--selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
    parser.add_argument('--list-factors', action='store_true',
                       help="列出数据集中所有可用的特征名称")
    
    # 🚀 新增：DuckDB性能调优参数
    parser.add_argument('--duck-threads', type=int, default=16, help="DuckDB worker threads.")
    parser.add_argument('--duck-memory', type=str, default='16GB', help="DuckDB memory limit.")
    parser.add_argument('--duck-cache', type=str, default='4GB', help="DuckDB object cache size.")
    parser.add_argument('--prefetch-factor', type=int, default=4, help="DataLoader 每个 worker 预取批次数，默认为4")
    
    args = parser.parse_args()
    
    # Check if config file exists
    if not os.path.exists(args.config):
        print(f"❌ Configuration file not found: {args.config}")
        return
    
    # 🚀 新增：处理特征列表查询
    if args.list_factors:
        try:
            # 临时创建数据集实例来获取可用特征
            import json
            from pathlib import Path
            dataset_path = "data/Dataset/pv_v5_pv_v4_price&trade_pt10818"  # 默认数据集路径
            schema_path = Path(dataset_path) / "meta" / "schema.json"
            if schema_path.exists():
                with schema_path.open("r", encoding="utf-8") as fp:
                    schema_json = json.load(fp)
                expanded_factor_names = schema_json.get("expanded_factor_names", [])
                print("📊 数据集中可用的特征：")
                for i, factor in enumerate(expanded_factor_names, 1):
                    print(f"  {i:2d}. {factor}")
                print(f"\n总计：{len(expanded_factor_names)}个特征")
                print("\n💡 使用示例：")
                print("  python train_torch_transformer.py --selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
                print("  python train_torch_transformer.py --selected-factors adj_close_mar_w1 adj_open_mar_w1 adj_high_mar_w1 adj_low_mar_w1")
            else:
                print(f"❌ 未找到数据集schema文件: {schema_path}")
        except Exception as e:
            print(f"❌ 获取特征列表失败: {e}")
        return
    
    # Override test-only mode if --train is specified
    if args.train:
        args.test_only = False
    
    # Validate test-only mode arguments
    if args.test_only:
        if args.checkpoint is None:
            print(f"❌ --checkpoint is required when using --test-only mode")
            return
        if not os.path.exists(args.checkpoint):
            print(f"❌ Checkpoint file not found: {args.checkpoint}")
            print(f"💡 To run training instead, use: python train_torch_transformer.py --train")
            return
    
    # Override preset if specified
    if args.preset:
        print(f"🔧 Overriding preset to: {args.preset}")
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        config['preset'] = args.preset
        with open(args.config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    # 🚀 新增：处理特征选择参数
    if args.selected_factors:
        print(f"🔧 Overriding selected factors: {args.selected_factors}")
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        if 'data' not in config:
            config['data'] = {}
        config['data']['selected_factors'] = args.selected_factors
        with open(args.config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    # 🚀 新增：处理DuckDB性能调优参数
    if args.duck_threads or args.duck_memory or args.duck_cache or args.prefetch_factor:
        print(f"🔧 Overriding DuckDB settings: threads={args.duck_threads}, memory={args.duck_memory}, cache={args.duck_cache}, prefetch={args.prefetch_factor}")
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        if 'data' not in config:
            config['data'] = {}
        if args.duck_threads:
            config['data']['duck_threads'] = args.duck_threads
        if args.duck_memory:
            config['data']['duck_memory'] = args.duck_memory
        if args.duck_cache:
            config['data']['duck_cache'] = args.duck_cache
        if args.prefetch_factor:
            config['data']['prefetch_factor'] = args.prefetch_factor
        with open(args.config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    # Create trainer
    trainer = TorchTransformerTrainer(args.config)
    
    if args.test_only:
        # Run test-only mode
        print(f"🧪 Running test-only mode with checkpoint: {args.checkpoint}")
        trainer.test_model(args.checkpoint)
    else:
        # Run full training
        trainer.train()


if __name__ == "__main__":
    main()