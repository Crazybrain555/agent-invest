import torch
import torch.nn as nn


class RelativePositionBias(nn.Module):
    """Relative Position Bias（T5-style）。

    输入:
      - Lq, Lk: query/key 序列长度
    参数:
      - num_heads: 注意力头数
      - max_distance: 偏置截断最大距离
    返回:
      - [H, Lq, Lk] 的偏置矩阵，可广播到 [B*H, Lq, Lk]
    """
    def __init__(self, num_heads: int, max_distance: int = 128):
        super().__init__()
        self.num_heads = num_heads
        self.max_distance = max_distance
        self.bias = nn.Parameter(torch.zeros(num_heads, 2 * max_distance - 1))
        nn.init.zeros_(self.bias)

    def forward(self, Lq: int, Lk: int) -> torch.Tensor:
        q_ids = torch.arange(Lq, device=self.bias.device)
        k_ids = torch.arange(Lk, device=self.bias.device)
        rel = (q_ids[:, None] - k_ids[None, :]).clamp(-self.max_distance + 1, self.max_distance - 1)
        idx = rel + (self.max_distance - 1)  # [Lq,Lk] -> [0, 2*max-2]
        return self.bias[:, idx]  # [H,Lq,Lk]


