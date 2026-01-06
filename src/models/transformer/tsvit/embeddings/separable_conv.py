import torch
import torch.nn as nn
from ..utils.patches import num_patches


class PatchEmbedSeparableConv1d(nn.Module):
    """可分卷积 Patch 嵌入（Depthwise + Pointwise）。

    - 输入:   x ∈ [B, T, D]
    - 转置:   x^C ∈ [B, D, T]
    - 深度卷积: depth: [D -> D*dt]，步长 S，核 P，得 [B, D*dt, N]
    - 逐点卷积: point: [D*dt -> Dh]，得 [B, Dh, N]
    - 转置:   tokens ∈ [B, N, Dh]
    """
    def __init__(self, T: int, D: int, P: int, S: int, Dh: int, dt: int = 8):
        super().__init__()
        self.T, self.D, self.P, self.S, self.Dh, self.dt = T, D, P, S, Dh, dt
        self.depth = nn.Conv1d(D, D*dt, kernel_size=P, stride=S, padding=0, groups=D, bias=True)
        self.point = nn.Conv1d(D*dt, Dh, kernel_size=1, stride=1, padding=0, groups=1, bias=True)
        nn.init.kaiming_normal_(self.depth.weight, nonlinearity='relu'); nn.init.zeros_(self.depth.bias)
        nn.init.xavier_uniform_(self.point.weight); nn.init.zeros_(self.point.bias)

    def num_tokens(self) -> int:
        return num_patches(self.T, self.P, self.S)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = x.permute(0, 2, 1)       # [B,D,T]
        z = self.depth(z)            # [B,D*dt,N]
        z = self.point(z)            # [B,Dh,N]
        out = z.permute(0, 2, 1).contiguous()  # [B,N,Dh]
        return out


