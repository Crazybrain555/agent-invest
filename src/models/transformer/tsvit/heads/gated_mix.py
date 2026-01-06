import torch
import torch.nn as nn
from typing import Union, Tuple


class GatedMixHead(nn.Module):
    """mean 与 Query 聚合的门控混合（自适应选择聚合策略）"""
    def __init__(self, Dh: int, nhead: int = 4, dropout: float = 0.0):
        super().__init__()
        self.ln = nn.LayerNorm(Dh)
        self.q  = nn.Parameter(torch.randn(1, 1, Dh))
        nn.init.normal_(self.q, std=0.02)
        self.attn = nn.MultiheadAttention(Dh, nhead, dropout=dropout, batch_first=True)
        self.gate = nn.Sequential(nn.LayerNorm(Dh), nn.Linear(Dh, 1))  # 基于 mean 的门控
        self.out  = nn.Linear(Dh, 1)
        nn.init.xavier_uniform_(self.out.weight)
        nn.init.zeros_(self.out.bias)

    def forward(self, enc_tokens: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        x = self.ln(enc_tokens)                                   # [B,L,Dh]
        mean_f = x.mean(dim=1)                                    # [B,Dh] - mean聚合
        B = x.size(0)
        q = self.q.expand(B, 1, -1)
        attn_f, _ = self.attn(q, x, x)                            # [B,1,Dh]
        attn_f = attn_f.squeeze(1)                                # [B,Dh] - 注意力聚合
        a = torch.sigmoid(self.gate(mean_f))                      # [B,1] - 门控权重
        fv_raw = a * mean_f + (1 - a) * attn_f                   # [B,Dh] - 自适应混合的原始语义向量
        pred = self.out(fv_raw).squeeze(-1)
        
        if return_fv:
            return pred, fv_raw  # 返回混合前的raw向量（这里返回混合后的fv_raw作为语义表示）
        else:
            return pred
