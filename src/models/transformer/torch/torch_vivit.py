import math
import torch
import torch.nn as nn
import torch.nn.functional as F

class TubeletEmbedder(nn.Module):
    """
    Tubelet embedding over (time, samples). Treats features F as channels.
    Input:  x [B, S, F, T]
    Output: tokens [B, N, D], where N = T' * S'
    """
    def __init__(self, features: int, embed_dim: int,
                 t_patch: int = 2, s_patch: int = 4, bias: bool = True):
        super().__init__()
        self.t_patch = t_patch
        self.s_patch = s_patch
        # Map (F, T, S, 1) -> D with 3D conv over (T,S,1)
        # We add a dummy width=1 so Conv3d works as 3D tubelets.
        self.proj = nn.Conv3d(
            in_channels=features,
            out_channels=embed_dim,
            kernel_size=(t_patch, s_patch, 1),
            stride=(t_patch, s_patch, 1),
            bias=bias
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, S, F, T] -> [B, F, T, S, 1]
        x = x.permute(0, 2, 3, 1).unsqueeze(-1).contiguous()
        # conv: [B, D, T', S', 1]
        x = self.proj(x)
        B, D, Tp, Sp, _ = x.shape
        # flatten tokens: [B, N, D], N = T'*S'
        x = x.view(B, D, Tp * Sp).transpose(1, 2).contiguous()
        return x, Tp, Sp  # tokens, temporal_tokens, spatial_tokens


class PositionalEmbedding(nn.Module):
    """
    Learned absolute pos-embedding with optional factorized (time + sample) sum.
    """
    def __init__(self, num_tokens: int, embed_dim: int, use_factorized=False,
                 Tp: int = None, Sp: int = None):
        super().__init__()
        self.use_factorized = use_factorized
        if use_factorized:
            assert Tp is not None and Sp is not None
            self.time_pe   = nn.Parameter(torch.zeros(1, Tp, 1, embed_dim))
            self.sample_pe = nn.Parameter(torch.zeros(1, 1, Sp, embed_dim))
            nn.init.trunc_normal_(self.time_pe, std=0.02)
            nn.init.trunc_normal_(self.sample_pe, std=0.02)
        else:
            self.pe = nn.Parameter(torch.zeros(1, num_tokens, embed_dim))
            nn.init.trunc_normal_(self.pe, std=0.02)

    def forward(self, x: torch.Tensor, Tp: int, Sp: int) -> torch.Tensor:
        # x: [B, 1+N, D]  (CLS prepended)
        if self.use_factorized:
            # exclude CLS for reshape
            cls, tok = x[:, :1, :], x[:, 1:, :]
            B, N, D = tok.shape
            assert N == Tp * Sp
            tok = tok.view(B, Tp, Sp, D)
            tok = tok + (self.time_pe + self.sample_pe)  # broadcast
            tok = tok.view(B, N, D)
            return torch.cat([cls, tok], dim=1)
        else:
            return x + self.pe


class TransformerEncoder(nn.Module):
    def __init__(self, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4.0,
                 attn_dropout=0.0, dropout=0.0):
        super().__init__()
        self.layers = nn.ModuleList()
        self.norm = nn.LayerNorm(embed_dim)
        for _ in range(depth):
            self.layers.append(
                nn.ModuleDict(dict(
                    ln1=nn.LayerNorm(embed_dim),
                    attn=nn.MultiheadAttention(embed_dim, num_heads, dropout=attn_dropout, batch_first=True),
                    ln2=nn.LayerNorm(embed_dim),
                    mlp=nn.Sequential(
                        nn.Linear(embed_dim, int(embed_dim * mlp_ratio)),
                        nn.GELU(),
                        nn.Dropout(dropout),
                        nn.Linear(int(embed_dim * mlp_ratio), embed_dim),
                        nn.Dropout(dropout),
                    )
                ))
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, 1+N, D]
        for blk in self.layers:
            # MHSA
            h = blk["ln1"](x)
            attn_out, _ = blk["attn"](h, h, h, need_weights=False)
            x = x + attn_out
            # MLP
            h = blk["ln2"](x)
            x = x + blk["mlp"](h)
        return self.norm(x)


class ViViTModel1_ST(nn.Module):
    """
    ViViT Model 1 (joint space-time attention) adapted for inputs (B, S, F, T).
    - Tubelet embedding over (T, S)
    - Learned CLS token
    - Absolute or factorized (time+sample) positional embeddings
    Heads:
      - frame: per-time logits [B, T', C]  (spatially pooled)
      - token: per-token logits [B, T', S', C]
      - cls:   clip-level logits [B, C]
    """
    def __init__(self,
                 num_classes: int,
                 features: int,
                 embed_dim: int = 768,
                 depth: int = 12,
                 num_heads: int = 12,
                 mlp_ratio: float = 4.0,
                 t_patch: int = 2,
                 s_patch: int = 4,
                 use_factorized_pe: bool = True,
                 head_type: str = "frame"):
        super().__init__()
        self.embed_dim = embed_dim
        self.head_type = head_type
        self.t_patch = t_patch
        self.s_patch = s_patch

        self.embedder = TubeletEmbedder(features, embed_dim, t_patch, s_patch)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        nn.init.trunc_normal_(self.cls_token, std=0.02)

        # Pos-embed is created lazily after first forward (needs Tp, Sp)
        self.pos_embed = None
        self.encoder = TransformerEncoder(embed_dim, depth, num_heads, mlp_ratio)

        if head_type == "frame":
            self.head = nn.Linear(embed_dim, num_classes)
        elif head_type == "token":
            self.head = nn.Linear(embed_dim, num_classes)
        elif head_type == "cls":
            self.head = nn.Linear(embed_dim, num_classes)
        else:
            raise ValueError("head_type must be 'frame', 'token', or 'cls'")

        self._use_factorized_pe = use_factorized_pe

    def forward(self, x: torch.Tensor):
        """
        x: [B, S, F, T]
        returns:
          - 'frame': [B, T', C]
          - 'token': [B, T', S', C]
          - 'cls'  : [B, C]
        """
        B = x.size(0)
        tok, Tp, Sp = self.embedder(x)       # [B, N, D], N=Tp*Sp
        # prepend CLS
        cls = self.cls_token.expand(B, -1, -1)
        z = torch.cat([cls, tok], dim=1)     # [B, 1+N, D]

        # build positional embedding if needed
        if self.pos_embed is None:
            num_tokens = 1 + Tp * Sp
            self.pos_embed = PositionalEmbedding(
                num_tokens, self.embed_dim, use_factorized=self._use_factorized_pe, Tp=Tp, Sp=Sp
            ).to(x.device)

        z = self.pos_embed(z, Tp, Sp)
        z = self.encoder(z)                  # [B, 1+N, D]

        if self.head_type == "cls":
            cls_out = z[:, 0, :]
            return self.head(cls_out)        # [B, C]

        # reshape tokens
        tok_out = z[:, 1:, :].view(B, Tp, Sp, self.embed_dim)  # [B, T', S', D]

        if self.head_type == "frame":
            f = tok_out.mean(dim=2)          # pool over S' -> [B, T', D]
            return self.head(f)              # [B, T', C]

        if self.head_type == "token":
            out = self.head(tok_out)         # [B, T', S', C]
            return out
