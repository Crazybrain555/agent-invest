import torch
import torch.nn as nn
from typing import Union, Tuple


class PoolHead(nn.Module):
    """mean 池化 → LN → 线性；可选轻量 rFF 放在 LN 之后（稳定版本，去掉BN依赖）"""
    def __init__(self, Dh: int, use_rff: bool = False, hidden: int = None, dropout: float = 0.0):
        super().__init__()
        self.ln = nn.LayerNorm(Dh)
        self.use_rff = use_rff and (hidden is not None)
        if self.use_rff:
            self.rff = nn.Sequential(
                nn.Linear(Dh, hidden), nn.GELU(), nn.Dropout(dropout),
                nn.Linear(hidden, Dh)
            )
            for m in self.rff:
                if isinstance(m, nn.Linear):
                    nn.init.xavier_uniform_(m.weight)
                    nn.init.zeros_(m.bias)
        self.out = nn.Linear(Dh, 1)
        nn.init.xavier_uniform_(self.out.weight)
        nn.init.zeros_(self.out.bias)

    def forward(self, enc_tokens: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        fv_raw = enc_tokens.mean(dim=1)                   # [B,Dh] - 时间维平均池化的原始语义向量
        z = self.ln(fv_raw)
        if self.use_rff:
            z = self.rff(z)
        pred = self.out(z).squeeze(-1)
        
        if return_fv:
            return pred, fv_raw  # 返回LN前的raw向量
        else:
            return pred


# 为了向后兼容，保留原名称的别名
PoolHeadStable = PoolHead


