import torch
import torch.nn as nn
from typing import List, Tuple
from ..utils.patches import num_patches


class MultiScaleSepConvEmbed(nn.Module):
    """多尺度可分卷积 Patch 嵌入（保留，默认不用）。

    - 对若干 (P, S, dt) 分支并行：每支输出 [B, N_i, Dh]
    - 拼接 token 维: 输出 tokens ∈ [B, sum_i N_i, Dh]
    """
    def __init__(self, T: int, D: int, Dh: int, branches: List[Tuple[int,int,int]], point_groups: int = 1):
        super().__init__()
        self.branches = nn.ModuleList()
        self.N_list = []
        self.Dh = Dh
        for (P, S, dt) in branches:
            depth = nn.Conv1d(D, D*dt, kernel_size=P, stride=S, padding=0, groups=D, bias=True)
            point = nn.Conv1d(D*dt, Dh, kernel_size=1, stride=1, padding=0, groups=point_groups, bias=True)
            nn.init.kaiming_normal_(depth.weight, nonlinearity='relu'); nn.init.zeros_(depth.bias)
            nn.init.xavier_uniform_(point.weight); nn.init.zeros_(point.bias)
            self.branches.append(nn.ModuleDict({'depth': depth, 'point': point}))
            self.N_list.append(num_patches(T, P, S))

    def num_tokens(self) -> int:
        return int(sum(self.N_list))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        xC = x.permute(0, 2, 1)        # [B,D,T]
        outs = []                      # list of [B,Ni,Dh]
        for m in self.branches:
            z = m['depth'](xC)        # [B,D*dt,Ni]
            z = m['point'](z)         # [B,Dh,Ni]
            outs.append(z.permute(0, 2, 1))  # [B,Ni,Dh]
        out = torch.cat(outs, dim=1)  # [B,sum_i Ni,Dh]
        return out


