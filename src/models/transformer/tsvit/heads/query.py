import torch
import torch.nn as nn
from typing import Union, Tuple


class QueryHead(nn.Module):
    """PMA：learnable seed(s) 在时间维上聚合 → LN → 线性（Set Transformer 风格，去掉BN依赖）"""
    def __init__(self, Dh: int, nhead: int, dropout: float = 0.0, k: int = 1, use_rff_kv: bool = False):
        super().__init__()
        assert k >= 1
        self.k = k
        self.seeds = nn.Parameter(torch.randn(1, k, Dh))
        nn.init.normal_(self.seeds, mean=0.0, std=0.02)
        self.attn   = nn.MultiheadAttention(Dh, nhead, dropout=dropout, batch_first=True)
        self.ln_kv  = nn.LayerNorm(Dh)          # MAB里对KV的前置LN
        self.use_rff_kv = use_rff_kv
        if use_rff_kv:
            self.rff_kv = nn.Sequential(nn.Linear(Dh, Dh), nn.GELU(), nn.Linear(Dh, Dh))
            for m in self.rff_kv:
                if isinstance(m, nn.Linear):
                    nn.init.xavier_uniform_(m.weight)
                    nn.init.zeros_(m.bias)
        self.ln_out = nn.LayerNorm(Dh if k == 1 else Dh * k)
        self.proj   = nn.Linear(Dh if k == 1 else Dh * k, 1)
        nn.init.xavier_uniform_(self.proj.weight)
        nn.init.zeros_(self.proj.bias)

    def forward(self, enc_tokens: torch.Tensor, return_fv: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        B = enc_tokens.size(0)
        kv = self.ln_kv(enc_tokens)
        if self.use_rff_kv:
            kv = self.rff_kv(kv)
        q = self.seeds.expand(B, self.k, -1)             # [B,k,Dh]
        y, _ = self.attn(q, kv, kv)                      # [B,k,Dh]
        if self.k == 1:
            fv_raw = y.squeeze(1)                        # [B,Dh] - 注意力聚合的原始语义向量
            z = self.ln_out(fv_raw)
        else:
            z = self.ln_out(y.reshape(B, -1))            # [B,k*Dh]
            fv_raw = y.mean(dim=1)                       # 定义一个 [B,Dh] 的原始向量返回
        pred = self.proj(z).squeeze(-1)                  # [B]
        
        if return_fv:
            return pred, fv_raw  # 返回LN前的raw向量  
        else:
            return pred


# 为了向后兼容，保留原名称的别名
PMAHead = QueryHead


