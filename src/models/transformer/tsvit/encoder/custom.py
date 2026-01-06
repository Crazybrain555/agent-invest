import torch
import torch.nn as nn

from .layer import TSViTEncoderLayer


class TSViTEncoderCustom(nn.Module):
    """自定义 Encoder 堆叠，支持 RoPE / RPB / DropPath。

    输入/输出:
      - x: [B, L, Dh]
      - out: [B, L, Dh]
    """
    def __init__(self, depth: int, d_model: int, nhead: int, ffn_mult: int,
                 dropout: float, attn_dropout: float, norm_first: bool,
                 drop_path_rate: float,
                 use_rope: bool, rope_pct: float, rope_theta: float,
                 use_rpb: bool, rpb_max_dist: int):
        super().__init__()
        dpr = torch.linspace(0, drop_path_rate, depth).tolist()
        self.layers = nn.ModuleList([
            TSViTEncoderLayer(
                d_model=d_model, nhead=nhead, ffn_mult=ffn_mult, dropout=dropout,
                attn_dropout=attn_dropout, drop_path=dpr[i], norm_first=norm_first,
                use_rope=use_rope, rope_pct=rope_pct, rope_theta=rope_theta,
                use_rpb=use_rpb, rpb_max_dist=rpb_max_dist
            ) for i in range(depth)
        ])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for layer in self.layers:
            x = layer(x)
        return x


