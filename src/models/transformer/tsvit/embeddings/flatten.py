import torch
import torch.nn as nn
from ..utils.patches import num_patches


class PatchEmbedFlatten(nn.Module):
    """Flatten Patch 嵌入。

    - 输入:  x ∈ [B, T, D]
    - 切片:  unfold -> p ∈ [B, D, N, P]
    - 重排:  p -> [B, N, P, D]
    - 映射:  Linear(P*D -> Dh) 得到 tokens ∈ [B, N, Dh]
    """
    def __init__(self, T: int, D: int, P: int, S: int, Dh: int):
        super().__init__()
        self.T, self.D, self.P, self.S, self.Dh = T, D, P, S, Dh
        self.proj = nn.Linear(P * D, Dh)

    def num_tokens(self) -> int:
        return num_patches(self.T, self.P, self.S)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, D = x.shape                 # [B,T,D]
        assert (T, D) == (self.T, self.D)
        p = x.permute(0, 2, 1).unfold(2, self.P, self.S)      # [B,D,N,P]
        p = p.permute(0, 2, 3, 1).contiguous()                # [B,N,P,D]
        out = self.proj(p.view(B, p.size(1), -1))             # [B,N,Dh]
        return out


