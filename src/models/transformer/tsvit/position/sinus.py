import torch
import torch.nn as nn


class SinusoidalPositionalEmbedding(nn.Module):
    """固定正弦位置嵌入。

    - 形状: 输入/输出 [B, L, Dh]
    - buffer: pe [1, L, Dh]
    """
    def __init__(self, length: int, dim: int, theta: float = 10000.0):
        super().__init__()
        pe = torch.zeros(length, dim)
        position = torch.arange(0, length, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, dim, 2).float() * (-torch.log(torch.tensor(theta)) / dim))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe.unsqueeze(0), persistent=False)  # [1, L, dim]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, :x.size(1), :].to(x.dtype)


