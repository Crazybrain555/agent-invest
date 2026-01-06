from abc import ABC, abstractmethod
import torch
import torch.nn as nn
from typing import Dict, Any, Optional

class BaseModel(ABC, nn.Module):
    """模型基类"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化模型
        
        Args:
            config: 模型配置字典
        """
        super().__init__()
        self.config = config
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
    @abstractmethod
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """前向传播
        
        Args:
            x: 输入张量
            
        Returns:
            输出张量
        """
        pass
    
    def save(self, path: str) -> None:
        """保存模型
        
        Args:
            path: 保存路径
        """
        torch.save({
            'model_state_dict': self.state_dict(),
            'config': self.config
        }, path)
    
    def load(self, path: str) -> None:
        """加载模型
        
        Args:
            path: 模型文件路径
        """
        checkpoint = torch.load(path)
        self.load_state_dict(checkpoint['model_state_dict'])
        self.config = checkpoint['config']
    
    def to_device(self) -> None:
        """将模型移动到指定设备"""
        self.to(self.device)
    
    def get_optimizer(self) -> torch.optim.Optimizer:
        """获取优化器
        
        Returns:
            优化器实例
        """
        optimizer_config = self.config['training']['optimizer']
        if optimizer_config['type'] == 'Adam':
            return torch.optim.Adam(
                self.parameters(),
                lr=optimizer_config['learning_rate'],
                weight_decay=optimizer_config['weight_decay']
            )
        else:
            raise ValueError(f"Unsupported optimizer type: {optimizer_config['type']}")
    
    def get_scheduler(self, optimizer: torch.optim.Optimizer) -> torch.optim.lr_scheduler._LRScheduler:
        """获取学习率调度器
        
        Args:
            optimizer: 优化器实例
            
        Returns:
            学习率调度器实例
        """
        scheduler_config = self.config['training']['scheduler']
        if scheduler_config['type'] == 'ReduceLROnPlateau':
            return torch.optim.lr_scheduler.ReduceLROnPlateau(
                optimizer,
                mode='min',
                factor=scheduler_config['factor'],
                patience=scheduler_config['patience'],
                min_lr=scheduler_config['min_lr']
            )
        else:
            raise ValueError(f"Unsupported scheduler type: {scheduler_config['type']}")
    
    def get_loss_function(self) -> nn.Module:
        """获取损失函数
        
        Returns:
            损失函数实例
        """
        loss_type = self.config['training']['loss_function']
        if loss_type == 'MSE':
            return nn.MSELoss()
        elif loss_type == 'BCE':
            return nn.BCELoss()
        else:
            raise ValueError(f"Unsupported loss function type: {loss_type}")
    
    def get_metrics(self) -> Dict[str, callable]:
        """获取评估指标
        
        Returns:
            评估指标字典
        """
        metrics = {}
        for metric_name in self.config['evaluation']['metrics']:
            if metric_name == 'MSE':
                metrics['MSE'] = lambda y_true, y_pred: torch.mean((y_true - y_pred) ** 2)
            elif metric_name == 'MAE':
                metrics['MAE'] = lambda y_true, y_pred: torch.mean(torch.abs(y_true - y_pred))
            elif metric_name == 'R2':
                metrics['R2'] = self._r2_score
            else:
                raise ValueError(f"Unsupported metric: {metric_name}")
        return metrics
    
    def _r2_score(self, y_true: torch.Tensor, y_pred: torch.Tensor) -> torch.Tensor:
        """计算R2分数
        
        Args:
            y_true: 真实值
            y_pred: 预测值
            
        Returns:
            R2分数
        """
        ss_tot = torch.sum((y_true - torch.mean(y_true)) ** 2)
        ss_res = torch.sum((y_true - y_pred) ** 2)
        return 1 - (ss_res / ss_tot) 