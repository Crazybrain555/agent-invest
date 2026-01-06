import torch
import torch.nn as nn
import torch.nn.functional as F

from ..position.rope import apply_rope
from ..position.rpb import RelativePositionBias


class SelfAttention(nn.Module):
    """支持 RoPE / RPB 的多头自注意力。

    输入/输出:
      - x: [B, L, C]
      - out: [B, L, C]
    """
    def __init__(self, d_model: int, nhead: int, attn_dropout: float = 0.0,
                 use_rope: bool = False, rope_pct: float = 1.0, rope_theta: float = 10000.0,
                 use_rpb: bool = False, rpb_max_dist: int = 128):
        super().__init__()
        assert d_model % nhead == 0
        self.nhead = nhead
        self.head_dim = d_model // nhead
        self.q = nn.Linear(d_model, d_model)
        self.k = nn.Linear(d_model, d_model)
        self.v = nn.Linear(d_model, d_model)
        self.o = nn.Linear(d_model, d_model)
        self.drop = nn.Dropout(attn_dropout)
        self.use_rope = use_rope
        self.rope_pct = rope_pct
        self.rope_theta = rope_theta
        self.use_rpb = use_rpb
        self.rpb = RelativePositionBias(nhead, rpb_max_dist) if use_rpb else None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, L, C = x.shape
        q = self.q(x).view(B, L, self.nhead, self.head_dim).permute(0, 2, 1, 3)
        k = self.k(x).view(B, L, self.nhead, self.head_dim).permute(0, 2, 1, 3)
        v = self.v(x).view(B, L, self.nhead, self.head_dim).permute(0, 2, 1, 3)

        if self.use_rope:
            rope_dim = int(self.head_dim * self.rope_pct)
            rope_dim -= rope_dim % 2
            q, k = apply_rope(q, k, rope_dim, self.rope_theta)

        q = q.reshape(B * self.nhead, L, self.head_dim)
        k = k.reshape(B * self.nhead, L, self.head_dim)
        v = v.reshape(B * self.nhead, L, self.head_dim)

        attn_mask = None
        if self.use_rpb and self.rpb is not None:
            attn_mask = self.rpb(L, L).repeat(B, 1, 1).to(q.dtype)  # [B*H,L,L]

        out = F.scaled_dot_product_attention(
            q, k, v,
            attn_mask=attn_mask,
            dropout_p=self.drop.p if self.training else 0.0,
            is_causal=False,
        )
        out = out.view(B, self.nhead, L, self.head_dim).permute(0, 2, 1, 3).reshape(B, L, C)
        return self.o(out)


