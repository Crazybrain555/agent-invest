import torch
from torch import nn
from torch.utils.data import DataLoader
from typing import Dict, Any, Optional
import logging
import os
from datetime import datetime

class BaseTrainer:
    def __init__(
        self,
        model: nn.Module,
        criterion: nn.Module,
        optimizer: torch.optim.Optimizer,
        train_loader: DataLoader,
        val_loader: Optional[DataLoader] = None,
        scheduler: Optional[torch.optim.lr_scheduler._LRScheduler] = None,
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
        save_dir: str = "checkpoints"
    ):
        self.model = model
        self.criterion = criterion
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.scheduler = scheduler
        self.device = device
        self.save_dir = save_dir
        
        # 创建保存目录
        os.makedirs(save_dir, exist_ok=True)
        
        # 设置日志
        self.setup_logging()
        
    def setup_logging(self):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            fh = logging.FileHandler(os.path.join(self.save_dir, 'training.log'))
            fh.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)
            
    def train_epoch(self, epoch: int) -> Dict[str, float]:
        self.model.train()
        total_loss = 0
        for batch_idx, batch in enumerate(self.train_loader):
            self.optimizer.zero_grad()
            loss = self._process_batch(batch)
            loss.backward()
            self.optimizer.step()
            total_loss += loss.item()
            
            if batch_idx % 100 == 0:
                self.logger.info(f'Train Epoch: {epoch} [{batch_idx}/{len(self.train_loader)} '
                               f'({100. * batch_idx / len(self.train_loader):.0f}%)]\tLoss: {loss.item():.6f}')
                
        avg_loss = total_loss / len(self.train_loader)
        return {"train_loss": avg_loss}
    
    def validate_epoch(self) -> Dict[str, float]:
        if self.val_loader is None:
            return {}
        
        self.model.eval()
        total_loss = 0
        with torch.no_grad():
            for batch in self.val_loader:
                loss = self._process_batch(batch)
                total_loss += loss.item()
                
        avg_loss = total_loss / len(self.val_loader)
        return {"val_loss": avg_loss}
    
    def _process_batch(self, batch) -> torch.Tensor:
        """需要在具体的训练器中实现"""
        raise NotImplementedError
    
    def save_checkpoint(self, epoch: int, metrics: Dict[str, float]):
        """保存模型检查点"""
        checkpoint = {
            'epoch': epoch,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'metrics': metrics
        }
        if self.scheduler is not None:
            checkpoint['scheduler_state_dict'] = self.scheduler.state_dict()
            
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'checkpoint_epoch_{epoch}_{timestamp}.pth'
        save_path = os.path.join(self.save_dir, filename)
        torch.save(checkpoint, save_path)
        self.logger.info(f'Saved checkpoint: {save_path}')
    
    def train(self, epochs: int, save_frequency: int = 1):
        """完整的训练流程"""
        for epoch in range(epochs):
            train_metrics = self.train_epoch(epoch)
            val_metrics = self.validate_epoch()
            
            metrics = {**train_metrics, **val_metrics}
            
            # 记录训练信息
            metrics_str = ", ".join(f"{k}: {v:.4f}" for k, v in metrics.items())
            self.logger.info(f"Epoch {epoch}: {metrics_str}")
            
            # 更新学习率
            if self.scheduler is not None:
                if isinstance(self.scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                    self.scheduler.step(metrics.get("val_loss", metrics["train_loss"]))
                else:
                    self.scheduler.step()
            
            # 保存检查点
            if (epoch + 1) % save_frequency == 0:
                self.save_checkpoint(epoch, metrics) 