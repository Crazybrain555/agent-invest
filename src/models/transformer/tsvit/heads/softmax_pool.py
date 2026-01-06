import torch
import torch.nn as nn
from typing import Union, Tuple


class SoftmaxPoolHead(nn.Module):
    """softmax 权重池化（log-sum-exp pooling 的可学习版，温度控制的注意力池化）"""
    def __init__(self, Dh: int, temperature: float = 1.0):
        super().__init__()
        self.u = nn.Parameter(torch.randn(Dh))
        nn.init.normal_(self.u, std=1.0 / (Dh ** 0.5))
        self.tau = nn.Parameter(torch.tensor(float(temperature)))
        self.ln = nn.LayerNorm(Dh)
        self.out = nn.Linear(Dh, 1)
        nn.init.xavier_uniform_(self.out.weight)
        nn.init.zeros_(self.out.bias)

    def forward(self, enc_tokens: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        # 注意力权重：a_i ∝ exp( (h_i·u)/tau )
        scores = (enc_tokens @ self.u) / self.tau.clamp_min(1e-6)   # [B,L]
        w = torch.softmax(scores, dim=1).unsqueeze(-1)              # [B,L,1]
        fv_raw = (w * enc_tokens).sum(dim=1)                        # [B,Dh] - 加权聚合的原始语义向量
        pred = self.out(self.ln(fv_raw)).squeeze(-1)                # [B]
        
        if return_fv:
            return pred, fv_raw  # 返回LN前的raw向量
        else:
            return pred
