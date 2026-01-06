# training_monitor.py - 精简高效的训练监控模块
import time
import warnings
from typing import Dict, List, Optional, Tuple, Any, Union, Sequence
from collections import defaultdict
from pathlib import Path

import torch
import torch.nn as nn
import numpy as np
from torch.utils.tensorboard import SummaryWriter
from torch.cuda.amp import GradScaler


class TrainingMonitor:
    """
    精简高效的训练监控器 - 三大页签架构
    
    🎯 Core: 核心训练指标 (loss/lr/grad_norm/amp_scale)
    🔍 LayerDiag: 层级诊断 (按模型顺序：weight→activation→grad→grad_norm)  
    ⚠️ Alerts: 稳定性预警 (NaN/Inf/梯度爆炸/AMP异常)
    
    性能目标：监控开销 <2ms/step，写盘点位 <100个
    """
    
    def __init__(
        self,
        writer: SummaryWriter,
        config: Optional[Dict[str, Any]] = None
    ):
        self.writer = writer
        
        # 精简配置 - 专注核心功能
        default_config = {
            # 监控频率控制
            "detailed_log_freq": 100,           # LayerDiag监控频率  
            "full_monitor_batches": 5,          # 前N个batch完全监控
            
            # 异常检测阈值
            "grad_explosion_threshold": 10.0,   # 梯度爆炸阈值
            "grad_vanishing_threshold": 1e-4,   # 梯度消失阈值
            "loss_explosion_threshold": 1000.0, # 损失爆炸阈值
            "amp_scale_min_threshold": 2.0,     # AMP缩放最小阈值
            
            # 性能优化
            "activation_to_cpu": True,          # 激活值缓存到CPU
            "activation_sample_size": 1000,     # 大张量采样数量

            # 新增：诊断增强
            "ema_decay": 0.9,                   # 梯度范数EMA平滑系数
            "enable_histograms": False,         # 是否记录直方图（成本高）
            "histogram_freq": 500,              # 直方图记录频率
            "act_zero_epsilon": 1e-6,           # 激活近零判定阈值
            "act_large_sigma": 3.0              # 激活大幅度(>k*std)判定阈值
        }
        
        self.config = {**default_config, **(config or {})}
        
        # 核心状态
        self.monitored_layers = []          # 按模型顺序的层名列表
        self.activation_hooks = []          # Forward hooks
        self.activation_cache = {}          # 激活值缓存
        self.last_amp_scale = None          # 上次AMP缩放值
        self._current_batch_idx = 0         # 当前batch索引
        self._grad_norm_ema: Dict[str, float] = {}  # 分层梯度范数EMA
        
        # 性能统计
        self.monitoring_time = 0.0
        self.total_writes = 0
        
        print("🚀 精简高效监控器初始化完成")
        print(f"   📊 监控策略: Core + LayerDiag + Alerts")
        print(f"   ⚡ 目标性能: <2ms/step, <100个写盘点位")
    
    def setup_model_monitoring(self, model: nn.Module):
        """设置模型监控 - 智能识别关键层（包括GRU等）"""
        print(f"\n🔧 设置层级监控...")
        
        # === 1. 智能识别重要层（参考multi_step_activation_analysis.py） ===
        self.monitored_layers = []
        important_layer_types = [
            # RNN相关
            torch.nn.GRU, torch.nn.LSTM, torch.nn.RNN,
            # 基础层
            torch.nn.Linear, torch.nn.Conv1d, torch.nn.Conv2d,
            # 规范化层
            torch.nn.LayerNorm, torch.nn.BatchNorm1d, torch.nn.BatchNorm2d,
            # 激活层（如果需要监控）
            torch.nn.ReLU, torch.nn.GELU, torch.nn.Tanh
        ]
        
        # 按模型顺序收集层
        for name, module in model.named_modules():
            # 方法1: 检查是否是重要层类型
            is_important_type = any(isinstance(module, layer_type) for layer_type in important_layer_types)
            
            # 方法2: 检查是否有可训练参数
            has_trainable_params = any(p.requires_grad for p in module.parameters(recurse=False))
            
            # 方法3: 检查特定名称模式
            important_name_patterns = ['gru', 'lstm', 'attention', 'fc', 'linear', 'head', 'proj', 'norm', 'mlp']
            is_important_name = any(pattern in name.lower() for pattern in important_name_patterns)
            
            # 满足任一条件即监控
            if is_important_type or (has_trainable_params and is_important_name):
                self.monitored_layers.append(name)
        
        # 去重并按名称排序（保持一定的顺序）
        self.monitored_layers = sorted(list(set(self.monitored_layers)))
        
        print(f"   📋 识别到 {len(self.monitored_layers)} 个关键层")
        if len(self.monitored_layers) <= 10:
            print(f"   📍 监控层: {self.monitored_layers}")
        else:
            print(f"   📍 前5层: {self.monitored_layers[:5]}")
            print(f"   📍 后5层: {self.monitored_layers[-5:]}")
        
        # === 2. 注册激活值监控hooks ===
        self._register_activation_hooks(model)
        
        estimated_writes = len(self.monitored_layers) * 4 + 10  # 每层4个指标 + 核心指标
        print(f"   📊 预计写盘点位: ~{estimated_writes} 个")
        print("📊 精简高效监控器设置完成")
    
    def _register_activation_hooks(self, model: nn.Module):
        """注册激活值监控hooks"""
        hook_count = 0
        
        def make_activation_hook(layer_name):
            def hook_fn(module, input, output):
                if self._should_monitor_activations():
                    try:
                        # 处理不同类型的输出
                        if isinstance(output, tuple):
                            activation = output[0]  # RNN返回(output, hidden)
                        else:
                            activation = output
                        
                        if isinstance(activation, torch.Tensor):
                            # 性能优化：大张量采样 + 移到CPU
                            if activation.numel() > self.config["activation_sample_size"]:
                                # 随机采样
                                flat_act = activation.flatten()
                                indices = torch.randperm(flat_act.numel())[:self.config["activation_sample_size"]]
                                sampled_act = flat_act[indices]
                            else:
                                sampled_act = activation.flatten()
                            
                            # 移到CPU节省GPU显存
                            if self.config["activation_to_cpu"]:
                                sampled_act = sampled_act.detach().cpu()
                            else:
                                sampled_act = sampled_act.detach()
                            
                            self.activation_cache[layer_name] = sampled_act
                            
                    except Exception as e:
                        warnings.warn(f"激活值hook错误 {layer_name}: {e}")
            return hook_fn
        
        # 为所有监控层注册hooks
        for layer_name in self.monitored_layers:
            for name, module in model.named_modules():
                if name == layer_name:
                    hook = module.register_forward_hook(make_activation_hook(layer_name))
                    self.activation_hooks.append(hook)
                    hook_count += 1
                    break
        
        print(f"   🎯 注册了 {hook_count} 个激活值hooks")
    
    def _should_monitor_activations(self) -> bool:
        """判断是否应该监控激活值"""
        # 🔥 修复性能问题：大幅减少激活值监控频率
        return hasattr(self, '_current_batch_idx') and self._current_batch_idx % self.config["detailed_log_freq"] == 0
    
    def monitor_core_metrics(
        self,
        loss: float,
        grad_norm: float,
        lr: float,
        scaler: Optional[GradScaler],
        step: int,
        grad_clip_threshold: Optional[float] = None,
        was_clipped: Optional[bool] = None,
        preclip_grad_norm: Optional[float] = None,
        postclip_grad_norm: Optional[float] = None
    ):
        """监控核心指标 - Core页签"""
        start_time = time.time()
        
        # === Core/loss ===
        if np.isfinite(loss):
            self.writer.add_scalar("Core/loss", loss, step)
        else:
            self._alert_nan_loss(step)
        
        # === Core/lr ===
        self.writer.add_scalar("Core/lr", lr, step)
        
        # === Core/grad_norm ===
        self.writer.add_scalar("Core/grad_norm", grad_norm, step)

        # === Core/grad_clip ===
        if grad_clip_threshold is not None:
            self.writer.add_scalar("Core/grad_clip_threshold", float(grad_clip_threshold), step)
            if was_clipped is not None:
                self.writer.add_scalar("Core/grad_was_clipped", 1.0 if was_clipped else 0.0, step)
            if preclip_grad_norm is not None:
                self.writer.add_scalar("Core/grad_norm_preclip", float(preclip_grad_norm), step)
                clip_ratio = float(preclip_grad_norm) / max(float(grad_clip_threshold), 1e-12)
                self.writer.add_scalar("Core/grad_clip_ratio", clip_ratio, step)
            if postclip_grad_norm is not None:
                self.writer.add_scalar("Core/grad_norm_postclip", float(postclip_grad_norm), step)
        
        # === Core/amp_scale ===
        if scaler is not None:
            current_scale = scaler.get_scale()
            self.writer.add_scalar("Core/amp_scale", current_scale, step)
            
            # AMP缩放监控
            if self.last_amp_scale is not None:
                scale_ratio = current_scale / max(self.last_amp_scale, 1e-10)
                self.writer.add_scalar("Core/amp_scale_ratio", scale_ratio, step)
            
            self.last_amp_scale = current_scale
        
        # 粗略估计写盘数
        self.total_writes += 4
        self.monitoring_time += time.time() - start_time
    
    def monitor_layer_diagnostics(
        self, 
        model: nn.Module, 
        step: int,
        batch_idx: int = 0
    ):
        """监控层级诊断 - LayerDiag页签"""
        # 频率控制：只在特定step监控详细信息
        if not self._should_monitor_detailed(batch_idx):
            return
            
        start_time = time.time()
        
        try:
            with torch.no_grad():
                # 按层顺序监控
                for layer_name in self.monitored_layers:
                    self._monitor_single_layer(model, layer_name, step)
        
        except Exception as e:
            warnings.warn(f"层级诊断监控错误: {e}")
        
        # 清空激活值缓存
        self.activation_cache.clear()
        
        self.monitoring_time += time.time() - start_time
    
    def _monitor_single_layer(self, model: nn.Module, layer_name: str, step: int):
        """监控单个层的扩展指标：
        weight_std → activation_std/zero_frac/large_frac → grad_std/grad_norm → grad/weight比值 → EMA对比
        """
        
        # 找到对应的模块和参数
        module = dict(model.named_modules())[layer_name]
        
        # 🎯 修复：处理不同类型的层
        weight = None
        
        # 方法1: 标准层（Linear、Conv等）- 有单一weight属性
        if hasattr(module, 'weight') and module.weight is not None:
            weight = module.weight
        
        # 方法2: RNN层（GRU、LSTM等）- 选择主要权重
        elif isinstance(module, (torch.nn.GRU, torch.nn.LSTM, torch.nn.RNN)):
            # 对于RNN层，选择第一层的input-hidden权重作为代表
            if hasattr(module, 'weight_ih_l0'):
                weight = module.weight_ih_l0
            elif hasattr(module, 'weight_ih'):
                weight = module.weight_ih
        
        # 方法3: 其他特殊层 - 寻找第一个可训练参数
        else:
            for param_name, param in module.named_parameters(recurse=False):
                if param.requires_grad and 'weight' in param_name.lower():
                    weight = param
                    break
        
        # 如果找不到合适的权重参数，跳过监控
        if weight is None:
            return
            
        prefix = f"L/{layer_name}"
        
        # === 1. L/<layer>/w_std ===
        w_std = self._safe_std(weight)
        self.writer.add_scalar(f"{prefix}/w_std", w_std, step)
        # 记录权重范数，便于比值
        try:
            w_norm = weight.norm().item()
        except Exception:
            w_norm = float("nan")
        
        # === 2. L/<layer>/act_std ===
        if layer_name in self.activation_cache:
            activation = self.activation_cache[layer_name]
            act_std = self._safe_std(activation)
            self.writer.add_scalar(f"{prefix}/act_std", act_std, step)

            # 2.1 激活近零/大幅度占比（范式无关诊断）
            with torch.no_grad():
                try:
                    abs_act = activation.abs()
                    zero_eps = float(self.config["act_zero_epsilon"])  # type: ignore
                    zero_frac = (abs_act < zero_eps).float().mean().item() if activation.numel() > 0 else 0.0
                    self.writer.add_scalar(f"{prefix}/act_zero_frac", zero_frac, step)
                    # 大幅度：|x| > k * std
                    sigma_k = float(self.config["act_large_sigma"])  # type: ignore
                    thr = sigma_k * (act_std if act_std > 0 else 1.0)
                    large_frac = (abs_act > thr).float().mean().item() if activation.numel() > 0 else 0.0
                    self.writer.add_scalar(f"{prefix}/act_large_frac", large_frac, step)
                except Exception:
                    pass
            
            # 2.2 特定激活饱和诊断（如ReLU/Tanh）
            try:
                if isinstance(module, torch.nn.ReLU):
                    dead_frac = (activation <= 0).float().mean().item()
                    self.writer.add_scalar(f"{prefix}/relu_dead_frac", dead_frac, step)
                elif isinstance(module, torch.nn.Tanh):
                    tanh_sat = (activation.abs() > 0.99).float().mean().item()
                    self.writer.add_scalar(f"{prefix}/tanh_sat_frac", tanh_sat, step)
            except Exception:
                pass
        
        # === 3. L/<layer>/g_std ===  
        # === 4. L/<layer>/g_norm ===
        if weight.is_leaf and weight.grad is not None:
            g_std = self._safe_std(weight.grad)
            g_norm = weight.grad.norm().item()
            
            self.writer.add_scalar(f"{prefix}/g_std", g_std, step)
            self.writer.add_scalar(f"{prefix}/g_norm", g_norm, step)
            
            # 检查梯度异常
            self._check_gradient_anomalies(layer_name, weight.grad, step)

            # === 5. 梯度/权重 比值（尺度不变）：便于诊断消失/爆炸 ===
            if np.isfinite(w_norm) and w_norm > 0:
                g_w_norm_ratio = g_norm / max(w_norm, 1e-12)
                self.writer.add_scalar(f"{prefix}/g_w_norm_ratio", g_w_norm_ratio, step)
            if w_std > 0:
                g_w_std_ratio = g_std / max(w_std, 1e-12)
                self.writer.add_scalar(f"{prefix}/g_w_std_ratio", g_w_std_ratio, step)

            # === 6. 梯度范数EMA与相对变化（突变检测） ===
            decay = float(self.config.get("ema_decay", 0.9))
            prev = self._grad_norm_ema.get(layer_name, g_norm)
            ema = decay * prev + (1.0 - decay) * g_norm
            self._grad_norm_ema[layer_name] = ema
            self.writer.add_scalar(f"{prefix}/g_norm_ema", ema, step)
            rel_change = g_norm / max(ema, 1e-12)
            self.writer.add_scalar(f"{prefix}/g_norm_over_ema", rel_change, step)

            # === 7. 可选直方图（成本较高） ===
            if self.config.get("enable_histograms", False) and self._should_log_histogram():
                try:
                    self.writer.add_histogram(f"{prefix}/grad_hist", weight.grad, step)
                except Exception:
                    pass
        
        self.total_writes += 4  # 基础指标计数（实际略多）
    
    def _safe_std(self, tensor: torch.Tensor) -> float:
        """安全计算标准差"""
        try:
            if tensor.numel() <= 1:
                return 0.0
            return tensor.std().item()
        except:
            return 0.0
    
    def _should_monitor_detailed(self, batch_idx: int) -> bool:
        """判断是否进行详细监控"""
        # 前几个batch完全监控
        if batch_idx < self.config["full_monitor_batches"]:
            return True
        
        # 按频率监控
        return batch_idx % self.config["detailed_log_freq"] == 0

    def _should_log_histogram(self) -> bool:
        """是否记录直方图"""
        freq = int(self.config.get("histogram_freq", 500))
        return hasattr(self, '_current_batch_idx') and freq > 0 and (self._current_batch_idx % freq == 0)
    
    def monitor_stability_alerts(
        self,
        loss: float,
        predictions: torch.Tensor,
        labels: torch.Tensor,
        step: int,
        scaler: Optional[GradScaler] = None
    ):
        """监控稳定性预警 - Alerts页签"""
        start_time = time.time()
        
        # === 检查数值异常 ===
        anomalies = self._check_tensor_anomalies([predictions, labels])
        
        if anomalies["nan"] > 0:
            self.writer.add_scalar("Alerts/nan_count", anomalies["nan"], step)
            print(f"⚠️ NaN检测: Step {step}, 数量={anomalies['nan']}")
        
        if anomalies["inf"] > 0:
            self.writer.add_scalar("Alerts/inf_count", anomalies["inf"], step)
            print(f"⚠️ Inf检测: Step {step}, 数量={anomalies['inf']}")
        
        # === 损失异常 ===
        if not np.isfinite(loss):
            self._alert_nan_loss(step)
        elif loss > self.config["loss_explosion_threshold"]:
            self.writer.add_scalar("Alerts/loss_explosion", 1.0, step)
            print(f"🔥 损失爆炸: Step {step}, Loss={loss}")
        
        # === AMP异常 ===
        if scaler is not None:
            current_scale = scaler.get_scale()
            if current_scale < self.config["amp_scale_min_threshold"]:
                self.writer.add_scalar("Alerts/amp_scale_drop", 1.0, step)
                print(f"📉 AMP缩放异常: Step {step}, Scale={current_scale}")
        
        self.monitoring_time += time.time() - start_time
    
    def _check_tensor_anomalies(self, tensors: Sequence[torch.Tensor]) -> Dict[str, int]:
        """检查张量的NaN/Inf异常"""
        nan_count, inf_count = 0, 0
        
        for tensor in tensors:
            if tensor is not None and isinstance(tensor, torch.Tensor):
                nan_count += torch.isnan(tensor).sum().item()
                inf_count += torch.isinf(tensor).sum().item()
        
        return {"nan": nan_count, "inf": inf_count}
    
    def _check_gradient_anomalies(self, layer_name: str, grad: torch.Tensor, step: int):
        """检查单层梯度异常"""
        grad_norm = grad.norm().item()
        
        # 梯度爆炸
        if grad_norm > self.config["grad_explosion_threshold"]:
            self.writer.add_scalar(f"Alerts/grad_explosion_{layer_name}", grad_norm, step)
            print(f"💥 梯度爆炸: {layer_name}, norm={grad_norm:.3f}")
        
        # 梯度消失
        if grad_norm < self.config["grad_vanishing_threshold"]:
            self.writer.add_scalar(f"Alerts/grad_vanishing_{layer_name}", grad_norm, step)
    
    def _alert_nan_loss(self, step: int):
        """发出NaN损失警报"""
        self.writer.add_scalar("Alerts/loss_nan", 1.0, step)
        print(f"💀 Loss NaN检测: Step {step}")
    
    def log_epoch_summary(
        self,
        epoch: int,
        train_metrics: Dict[str, float],
        val_metrics: Dict[str, float]
    ):
        """记录epoch级别汇总"""
        # 训练指标
        for key, value in train_metrics.items():
            self.writer.add_scalar(f"Epoch_Train/{key}", value, epoch)
        
        # 验证指标
        for key, value in val_metrics.items():
            self.writer.add_scalar(f"Epoch_Val/{key}", value, epoch)
        
        # 监控性能统计
        if self.monitoring_time > 0:
            avg_time_per_step = self.monitoring_time * 1000 / max(self.total_writes, 1)  # ms
            self.writer.add_scalar("Monitor/avg_time_ms_per_step", avg_time_per_step, epoch)
            self.writer.add_scalar("Monitor/total_writes", self.total_writes, epoch)
            
            print(f"📊 监控性能统计 Epoch {epoch}:")
            print(f"   ⏱️  平均监控时间: {avg_time_per_step:.2f}ms/step")
            print(f"   📝 总写盘次数: {self.total_writes}")
            
            # 重置计数器
            self.monitoring_time = 0.0
            self.total_writes = 0
    
    def cleanup_hooks(self):
        """清理所有hooks"""
        for hook in self.activation_hooks:
            hook.remove()
        self.activation_hooks.clear()
        self.activation_cache.clear()
        
        print("🧹 监控hooks清理完成")
    
    def get_monitoring_summary(self) -> Dict[str, Any]:
        """获取监控摘要"""
        return {
            "monitored_layers": len(self.monitored_layers),
            "registered_hooks": len(self.activation_hooks),
            "config": self.config,
            "performance": {
                "total_monitoring_time": self.monitoring_time,
                "total_writes": self.total_writes
            }
        }

    # ==================== 简化的辅助方法 ====================
    
    def monitor_basic_metrics(
        self,
        loss: float,
        grad_norm: float,
        lr: float,
        predictions: torch.Tensor,
        labels: torch.Tensor,
        step: int,
        scaler: Optional[GradScaler] = None,
        grad_clip_threshold: Optional[float] = None,
        was_clipped: Optional[bool] = None,
        preclip_grad_norm: Optional[float] = None,
        postclip_grad_norm: Optional[float] = None
    ):
        """一站式基础监控 - 整合Core + Alerts"""
        # Core指标
        self.monitor_core_metrics(
            loss,
            grad_norm,
            lr,
            scaler,
            step,
            grad_clip_threshold=grad_clip_threshold,
            was_clipped=was_clipped,
            preclip_grad_norm=preclip_grad_norm,
            postclip_grad_norm=postclip_grad_norm,
        )
        
        # 稳定性预警
        self.monitor_stability_alerts(loss, predictions, labels, step, scaler)
        
        # 预测分布（简化版）- 放在Core页签
        with torch.no_grad():
            self.writer.add_scalar("Core/pred_std", predictions.std().item(), step)
            self.writer.add_scalar("Core/label_std", labels.std().item(), step)
            
            # pred-label偏差
            pred_bias = (predictions - labels).mean().item()
            self.writer.add_scalar("Core/pred_bias", pred_bias, step)
    
    def monitor_comprehensive(
        self,
        model: nn.Module,
        loss: float,
        grad_norm: float,
        lr: float,
        predictions: torch.Tensor,
        labels: torch.Tensor,
        step: int,
        batch_idx: int = 0,
        scaler: Optional[GradScaler] = None,
        grad_clip_threshold: Optional[float] = None,
        was_clipped: Optional[bool] = None,
        preclip_grad_norm: Optional[float] = None,
        postclip_grad_norm: Optional[float] = None
    ):
        """全面监控入口 - Core + LayerDiag + Alerts"""
        
        # 🔥 更新当前batch索引
        self._current_batch_idx = batch_idx
        
        # 基础监控（每step都执行，轻量级）
        self.monitor_basic_metrics(
            loss,
            grad_norm,
            lr,
            predictions,
            labels,
            step,
            scaler,
            grad_clip_threshold=grad_clip_threshold,
            was_clipped=was_clipped,
            preclip_grad_norm=preclip_grad_norm,
            postclip_grad_norm=postclip_grad_norm,
        )
        
        # 层级诊断（按频率执行，重量级）
        if self._should_monitor_detailed(batch_idx):
            self.monitor_layer_diagnostics(model, step, batch_idx) 