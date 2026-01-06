import torch
import torch.nn as nn

from ..attention.self_attention import SelfAttention
from ..regularization.drop_path import DropPath


class FeedForward(nn.Module):
    def __init__(self, d_model: int, mult: int = 4, dropout: float = 0.1):
        super().__init__()
        hid = mult * d_model
        self.fc1 = nn.Linear(d_model, hid)
        self.act = nn.GELU()
        self.drop1 = nn.Dropout(dropout)
        self.fc2 = nn.Linear(hid, d_model)
        self.drop2 = nn.Dropout(dropout)
        nn.init.xavier_uniform_(self.fc1.weight)
        nn.init.zeros_(self.fc1.bias)
        nn.init.xavier_uniform_(self.fc2.weight)
        nn.init.zeros_(self.fc2.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.drop2(self.fc2(self.drop1(self.act(self.fc1(x)))))


class TSViTEncoderLayer(nn.Module):
    def __init__(self, d_model: int, nhead: int, ffn_mult: int, dropout: float,
                 attn_dropout: float, drop_path: float, norm_first: bool,
                 use_rope: bool, rope_pct: float, rope_theta: float,
                 use_rpb: bool, rpb_max_dist: int):
        super().__init__()
        self.norm_first = norm_first
        self.ln1 = nn.LayerNorm(d_model)
        self.ln2 = nn.LayerNorm(d_model)
        self.attn = SelfAttention(d_model, nhead, attn_dropout, use_rope, rope_pct, rope_theta, use_rpb, rpb_max_dist)
        self.ffn = FeedForward(d_model, mult=ffn_mult, dropout=dropout)
        self.dp1 = DropPath(drop_path)
        self.dp2 = DropPath(drop_path)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.norm_first:
            x = x + self.dp1(self.attn(self.ln1(x)))
            x = x + self.dp2(self.ffn(self.ln2(x)))
        else:
            x = self.ln1(x + self.dp1(self.attn(x)))
            x = self.ln2(x + self.dp2(self.ffn(x)))
        return x


