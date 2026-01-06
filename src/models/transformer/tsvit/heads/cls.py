import torch
import torch.nn as nn
from typing import Union, Tuple
from .cosine_scorer import CosineScorer


class CLSHead(nn.Module):
    """CLS 表示 → LN → 线性/余弦打分（稳定版本，去掉BN依赖）"""
    def __init__(self, Dh: int, use_cosine: bool = False, temperature: float = 1.0):
        super().__init__()
        self.use_cosine = use_cosine
        if use_cosine:
            self.scorer = CosineScorer(Dh, temperature=temperature, learnable_tau=True)
        else:
            self.ln = nn.LayerNorm(Dh)
            self.w = nn.Linear(Dh, 1)
            nn.init.xavier_uniform_(self.w.weight)
            nn.init.zeros_(self.w.bias)

    def forward(self, enc_with_cls: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        fv_raw = enc_with_cls[:, 0, :]                    # [B,Dh] - CLS token的原始语义向量
        pred = self.scorer(fv_raw) if self.use_cosine else self.w(self.ln(fv_raw)).squeeze(-1)
        
        if return_fv:
            return pred, fv_raw  # 返回LN前的raw向量
        else:
            return pred


# 为了向后兼容，保留原名称的别名
CLSHeadStable = CLSHead


