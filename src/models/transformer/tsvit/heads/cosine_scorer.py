import torch
import torch.nn as nn
import torch.nn.functional as F


class CosineScorer(nn.Module):
    """LayerNorm + 余弦打分（带可学习温度tau）"""
    def __init__(self, D: int, temperature: float = 1.0, learnable_tau: bool = True):
        super().__init__()
        self.w = nn.Parameter(torch.randn(D))
        if learnable_tau:
            self.tau = nn.Parameter(torch.tensor(float(temperature)))
        else:
            self.register_buffer('tau', torch.tensor(float(temperature)))
        nn.init.normal_(self.w, std=1.0 / (D ** 0.5))
        self.ln = nn.LayerNorm(D)

    def forward(self, f: torch.Tensor) -> torch.Tensor:  # f: [B,D]
        f = self.ln(f)
        f = F.normalize(f, dim=-1)
        w = F.normalize(self.w, dim=-1)
        return self.tau * (f @ w)                        # [B]


