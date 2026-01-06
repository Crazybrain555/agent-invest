import torch
import torch.nn as nn


class TokenDrop(nn.Module):
    """Drop tokens (seq axis) during training.

    输入/输出:
      - x: [B, N, Dh]
      - 输出: [B, N, Dh]
    """
    def __init__(self, p: float = 0.0):
        super().__init__()
        self.p = float(p)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if (not self.training) or self.p <= 0.0:
            return x
        B, N, Dh = x.shape
        keep = torch.empty(B, N, 1, device=x.device, dtype=x.dtype).bernoulli_(1.0 - self.p)
        return x * keep / (1.0 - self.p)


