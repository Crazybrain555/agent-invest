import torch
import torch.nn as nn
from typing import Union, Tuple
from .cosine_scorer import CosineScorer


class CosineMeanHead(nn.Module):
    """mean → LN → 余弦打分（极简稳定版本）"""
    def __init__(self, Dh: int, temperature: float = 1.0):
        super().__init__()
        self.scorer = CosineScorer(Dh, temperature=temperature, learnable_tau=True)
    
    def forward(self, enc_tokens: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        fv_raw = enc_tokens.mean(dim=1)                  # [B,Dh] - 时间维平均池化的原始语义向量
        pred = self.scorer(fv_raw)                       # [B]
        
        if return_fv:
            return pred, fv_raw  # 返回LN前的raw向量
        else:
            return pred

