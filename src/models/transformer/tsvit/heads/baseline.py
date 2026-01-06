import torch
import torch.nn as nn
from typing import Union, Tuple


class BaselineHead(nn.Module):
    """Baseline 头：精确复现基线TorchTransformerEncoder的输出流程。

    流程（与基线完全一致）:
      1) 时间维平均池化 → [B, Dh]
      2) 两层MLP投影 → [B, feature_dim]  
      3) BatchNorm1d(affine=False) → [B, feature_dim]
      4) 特征向量均值作为预测 → [B]

    输入/输出:
      - enc_tokens: [B, L, Dh]
      - 输出:       [B] 或 ([B], [B, feature_dim])
    """
    def __init__(self, Dh: int, feature_dim: int = None, dropout: float = 0.1, fv_bn: bool = True):
        super().__init__()
        # 默认feature_dim = Dh // 2，与基线一致
        self.feature_dim = feature_dim or (Dh // 2)
        
        # 两层MLP投影（与基线output_projection一致）
        self.output_projection = nn.Sequential(
            nn.Linear(Dh, self.feature_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(self.feature_dim, self.feature_dim)
        )
        
        # BatchNorm（与基线bn1一致）
        self.bn1 = nn.BatchNorm1d(self.feature_dim, affine=False, track_running_stats=True) if fv_bn else nn.Identity()
        
        # 初始化权重（与基线一致）
        self._init_weights()
    
    def _init_weights(self):
        """Initialize weights same as baseline."""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
    
    def forward(self, enc_tokens: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        # 1) 时间维平均池化（与基线一致）
        pooled = enc_tokens.mean(dim=1)  # [B, Dh]
        
        # 2) 两层MLP投影（与基线output_projection一致）
        fv_raw = self.output_projection(pooled)  # [B, feature_dim]
        
        # 3) BatchNorm（与基线bn1一致）
        fv_normalized = self.bn1(fv_raw)  # [B, feature_dim]
        
        # 4) 特征向量均值作为预测（与基线一致）
        pred = fv_normalized.mean(dim=1)  # [B] - 不需要keepdim，直接返回[B]
        
        if return_fv:
            # 返回BN前的raw特征向量用于正交惩罚
            return pred, fv_raw
        else:
            return pred


