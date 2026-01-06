import torch
import torch.nn as nn
from ..utils.patches import num_patches


class PatchEmbedSeparableLinear(nn.Module):
    """可分线性 Patch 嵌入（先按时间，再按特征混合）。

    - 输入:   x ∈ [B, T, D]
    - unfold: p ∈ [B, D, N, P]
    - 时间投影:  若 share_timeproj=True，time_proj: [P -> dt] 施加于每个 (B*D,N,P)
                 得 z ∈ [B*D, N, dt]
                 否则为每个通道单独线性层，等价 z ∈ [B, D, N, dt]
    - 重排:   z -> [B, N, D, dt] 并展平为 [B, N, D*dt]
    - 混合:   mix_proj: [D*dt -> Dh]，得到 tokens ∈ [B, N, Dh]
    """
    def __init__(self, T: int, D: int, P: int, S: int, Dh: int, dt: int = 8, share_timeproj: bool = True):
        super().__init__()
        self.T, self.D, self.P, self.S, self.Dh, self.dt = T, D, P, S, Dh, dt
        self.share = share_timeproj
        self.time_proj = nn.Linear(P, dt) if share_timeproj else nn.ModuleList([nn.Linear(P, dt) for _ in range(D)])
        self.mix_proj = nn.Linear(D * dt, Dh)

    def num_tokens(self) -> int:
        return num_patches(self.T, self.P, self.S)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, D = x.shape                             # [B,T,D]
        assert (T, D) == (self.T, self.D)
        p = x.permute(0, 2, 1).unfold(2, self.P, self.S)     # [B,D,N,P]
        B_, D_, N, P = p.shape
        if self.share:
            z = self.time_proj(p.reshape(B_*D_, N, P))       # [B*D,N,dt]
        else:
            W = torch.stack([m.weight for m in self.time_proj], 0)      # [D,dt,P]
            b = torch.stack([m.bias for m in self.time_proj], 0)        # [D,dt]
            z = torch.einsum('bdnp,dtp->bdnt', p, W) + b[None, :, None, :]  # [B,D,N,dt]
            z = z.reshape(B_*D_, N, self.dt)
        z = z.view(B_, D_, N, self.dt).permute(0, 2, 1, 3).contiguous()  # [B,N,D,dt]
        out = self.mix_proj(z.view(B_, N, D_*self.dt))                  # [B,N,Dh]
        return out


