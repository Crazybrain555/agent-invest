import torch
import torch.nn as nn


class AbsPositionalEmbedding(nn.Module):
    """可学习绝对位置嵌入。

    - 形状: 输入/输出 [B, L, Dh]
    - 参数: pos_emb [1, L, Dh]
    """
    def __init__(self, length: int, dim: int, std: float = 0.02):
        super().__init__()
        self.pos_emb = nn.Parameter(torch.randn(1, length, dim))
        nn.init.normal_(self.pos_emb, mean=0.0, std=std)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pos_emb[:, :x.size(1), :].to(x.dtype)


