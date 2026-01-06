# -*- coding: utf-8 -*-
"""
TSViT: ViT-style Time Series Transformer (RoPE / RPB / DropPath / TokenDrop)

- 输入张量:  x ∈ [B, T, D]
  - B: batch size
  - T: 时间长度
  - D: 特征维度

- 主流程:  Patch-Embed -> TokenDrop -> PositionEncoding -> Encoder(native/custom) -> Head
  - Patch-Embed: 将 [B, T, D] 切成 N 个 patch（N = (T-P)//S + 1），并映射到隐藏维 Dh，得到 tokens ∈ [B, N, Dh]
  - TokenDrop: 训练时按 token（序列维）随机丢弃，tokens 形状不变
  - PositionEncoding: 
      * abs: 显式加法位置向量，形状对齐 [1, N(+1), Dh]
      * rope/rpb/rope_rpb: 见注意力内部注入（q,k 上应用）
  - Encoder: 编码 tokens -> h ∈ [B, N(+1), Dh]
    * native（官方 TransformerEncoder）或 custom（支持 RoPE/RPB/DropPath）
  - Head: 将 h 变换为标量预测 y ∈ [B]

Position encoding 选项 (pos_encoding):
- 'none':    不使用位置编码
- 'abs':     绝对位置向量 (learnable)
- 'rope':    Rotary Position Embedding（注意力内旋转）
- 'rpb':     Relative Position Bias（T5-style）
- 'rope_rpb': RoPE 与 RPB 同时启用

RoPE 额外参数（仅在包含 'rope' 时生效，提供默认值）:
- rope_pct:   应用到每个注意力头维度的比例，需为偶数维（默认 1.0）
- rope_theta: 周期缩放基数（默认 10000.0）
"""

from typing import Literal, Optional, List, Tuple
import torch
import torch.nn as nn

# Modular imports
from .embeddings import (
    PatchEmbedFlatten,
    PatchEmbedSeparableLinear,
    PatchEmbedSeparableConv1d,
    MultiScaleSepConvEmbed,
)
from .regularization import TokenDrop
from .position import AbsPositionalEmbedding, SinusoidalPositionalEmbedding
from .heads import (
    QueryHead, PoolHead, CLSHead, BaselineHead,
    PMAHead, PoolHeadStable, CLSHeadStable,
    CosineMeanHead, SoftmaxPoolHead, GatedMixHead
)
from .utils.pred_normalizer import PredNormalizer
from .encoder.native import build_native_encoder
from .encoder.custom import TSViTEncoderCustom
from .validators import decide_encoder_impl


# removed: in-file attention/encoder moved to attention/ and encoder/


# -----------------------------
# TSViT Main
# -----------------------------
class TSViT(nn.Module):
    """TSViT 主模型。

    维度约定:
      - 输入:  x ∈ [B, T, D]
      - Patch: tokens ∈ [B, N, Dh]，N = (T-P)//S + 1
      - Encoder 输出: h ∈ [B, N(+1), Dh]
      - Head 输出: y ∈ [B]

    关键参数：
      - encoder_impl: 'auto' | 'native' | 'custom'
        * auto: 若 pos_encoding∈{none,abs} 且 drop_path_rate==0，则走 native，否则 custom。
      - patch_mode: 'flatten' | 'separable_linear' | 'separable_conv' | 'multiscale_separable_conv'
    """
    def __init__(
        self,
        T: int, D: int, lead: int = 10,
        # Patch
        P: int = 32, S: int = 16,
        pre_keep_len: int = 0,
        patch_mode: Literal['flatten','separable_linear','separable_conv','multiscale_separable_conv'] = 'separable_conv',
        Dh: Optional[int] = None, hidden_size: Optional[int] = None, dt: int = 8, share_timeproj: bool = True,
        ms_branches: Optional[List[Tuple[int,int,int]]] = None,
        # Position encoding (single switch)
        pos_encoding: Literal['none','abs','rope','rpb','rope_rpb','sinus'] = 'rope',
        rope_pct: float = 1.0,
        rope_theta: float = 10000.0,
        rpb_max_dist: int = 128,
        pos_dropout: float = 0.0,
        # Encoder
        nheads: int = 8, depth: Optional[int] = None, num_layers: Optional[int] = None, ffn_mult: int = 4,
        dropout: float = 0.1, attn_dropout: float = 0.0, norm_first: bool = True,
        drop_path_rate: float = 0.0, encoder_impl: Literal['auto','native','custom'] = 'auto',
        # Head / token regularization
        use_cls: bool = False, 
        head_type: Literal['query','pool','cls','baseline','pma','pool_stable','cls_stable','cosine_mean','softmax_pool','gated_mix'] = 'query',
        token_drop_p: float = 0.0,
        fv_bn: bool = True,
        # 新头部的额外参数
        head_cls_use_cosine: bool = False,      # CLS头是否使用余弦打分
        head_temperature: float = 1.0,          # 余弦/softmax温度参数（通用）
        head_pool_use_rff: bool = False,        # Pool头是否使用轻量rFF
        head_pma_k: int = 1,                    # PMA头seed数量
        head_pma_use_rff_kv: bool = False,      # PMA头KV是否使用rFF
        # 预测归一化
        pred_norm: str = 'none',                # 'none' | 'batchnorm' | 'zscore'
        pred_norm_affine: bool = False,
        pred_norm_eps: float = 1e-6,
    ):
        super().__init__()
        self.T, self.D, self.lead = T, D, lead
        self.pre_keep_len = int(pre_keep_len)
        self.pos_encoding = pos_encoding
        # 由 head_type 决定是否启用 CLS token（忽略外部 use_cls）
        self.use_cls = (head_type == 'cls')
        # 统一尺寸命名
        Dh = Dh if Dh is not None else (hidden_size if hidden_size is not None else 256)
        depth = depth if depth is not None else (num_layers if num_layers is not None else 3)

        if self.pre_keep_len < 0:
            raise ValueError("pre_keep_len must be >= 0")
        if self.pre_keep_len >= T:
            raise ValueError(f"pre_keep_len({self.pre_keep_len}) must be < T({T})")
        T_patch = T - self.pre_keep_len

        # Patch
        if patch_mode == 'flatten':
            self.patch_embed = PatchEmbedFlatten(T_patch, D, P, S, Dh)
        elif patch_mode == 'separable_linear':
            self.patch_embed = PatchEmbedSeparableLinear(T_patch, D, P, S, Dh, dt=dt, share_timeproj=share_timeproj)
        elif patch_mode == 'separable_conv':
            self.patch_embed = PatchEmbedSeparableConv1d(T_patch, D, P, S, Dh, dt=dt)
        elif patch_mode == 'multiscale_separable_conv':
            assert ms_branches and len(ms_branches) > 0, "ms_branches must be provided"
            self.patch_embed = MultiScaleSepConvEmbed(T, D, Dh, ms_branches)
        else:
            raise ValueError(f"Unknown patch_mode: {patch_mode}")

        if self.pre_keep_len > 0:
            self.pre_embed = PatchEmbedFlatten(self.pre_keep_len, D, 1, 1, Dh)
        else:
            self.pre_embed = None

        N_patch = self.patch_embed.num_tokens()
        self.N = N_patch + (self.pre_keep_len if self.pre_keep_len > 0 else 0)

        # 位置编码（加法类：abs/sinus；其余在 attention 内部）
        pos_len = self.N + (1 if self.use_cls else 0)
        if self.pos_encoding == 'abs':
            self.pos_add = AbsPositionalEmbedding(pos_len, Dh)
        elif self.pos_encoding == 'sinus':
            self.pos_add = SinusoidalPositionalEmbedding(pos_len, Dh)
        else:
            self.pos_add = nn.Identity()
        self.pos_drop = nn.Dropout(pos_dropout)

        # [CLS]
        if self.use_cls:
            self.cls_token = nn.Parameter(torch.randn(1, 1, Dh))
            nn.init.normal_(self.cls_token, mean=0.0, std=0.02)
        else:
            self.register_parameter('cls_token', None)

        # TokenDrop
        self.token_drop = TokenDrop(token_drop_p)

        # 选择 encoder 实现（并做前置约束校验）
        impl = decide_encoder_impl(encoder_impl=encoder_impl, pos_encoding=self.pos_encoding, drop_path_rate=drop_path_rate)

        if impl == 'native':
            self.encoder = build_native_encoder(
                d_model=Dh, nhead=nheads, ffn_mult=ffn_mult,
                dropout=dropout, norm_first=norm_first, depth=depth,
                attn_dropout=attn_dropout,
            )
            self.custom = None
        else:
            use_rope = self.pos_encoding in ('rope', 'rope_rpb')
            use_rpb  = self.pos_encoding in ('rpb', 'rope_rpb')
            self.custom = TSViTEncoderCustom(
                depth=depth, d_model=Dh, nhead=nheads, ffn_mult=ffn_mult,
                dropout=dropout, attn_dropout=attn_dropout, norm_first=norm_first,
                drop_path_rate=drop_path_rate,
                use_rope=use_rope, rope_pct=rope_pct, rope_theta=rope_theta,
                use_rpb=use_rpb, rpb_max_dist=rpb_max_dist,
            )
            self.encoder = None

        self.ln_out = nn.LayerNorm(Dh)

        # Head
        if head_type == 'baseline':
            # 保持基线不变
            self.head = BaselineHead(Dh, feature_dim=Dh//2, dropout=dropout, fv_bn=fv_bn)
        elif head_type == 'query':
            # 原有的query head（现在是PMA风格）
            self.head = QueryHead(Dh, nhead=nheads, dropout=attn_dropout, k=1, use_rff_kv=False)
        elif head_type == 'pool':
            # 原有的pool head（现在是稳定版本）
            self.head = PoolHead(Dh, use_rff=head_pool_use_rff, hidden=2*Dh if head_pool_use_rff else None, dropout=dropout)
        elif head_type == 'cls':
            # 原有的cls head（现在是稳定版本）
            self.head = CLSHead(Dh, use_cosine=head_cls_use_cosine, temperature=head_temperature)
        elif head_type == 'pma':
            # PMA 头（显式版本）
            self.head = PMAHead(Dh, nhead=nheads, dropout=attn_dropout, k=head_pma_k, use_rff_kv=head_pma_use_rff_kv)
        elif head_type == 'pool_stable':
            # 稳定池化头（显式版本）
            self.head = PoolHeadStable(Dh, use_rff=head_pool_use_rff, hidden=2*Dh if head_pool_use_rff else None, dropout=dropout)
        elif head_type == 'cls_stable':
            # 稳定CLS头（显式版本）
            self.head = CLSHeadStable(Dh, use_cosine=head_cls_use_cosine, temperature=head_temperature)
        elif head_type == 'cosine_mean':
            # 余弦均值头
            self.head = CosineMeanHead(Dh, temperature=head_temperature)
        elif head_type == 'softmax_pool':
            # Softmax池化头
            self.head = SoftmaxPoolHead(Dh, temperature=head_temperature)
        elif head_type == 'gated_mix':
            # 门控混合头
            self.head = GatedMixHead(Dh, nhead=nheads, dropout=attn_dropout)
        else:
            raise ValueError(f"Unknown head_type: {head_type}")

        # 预测归一化模块（统一接到 head 输出后）
        self.pred_normalizer = PredNormalizer(mode=pred_norm, affine=pred_norm_affine, eps=pred_norm_eps)

    @torch.no_grad()
    def count_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    def forward_features(self, x: torch.Tensor) -> torch.Tensor:
        """从输入特征提取到编码表示。

        流程与形状:
          1) Patch-Embed:  x ∈ [B, T, D] → tokens ∈ [B, N, Dh]
          2) TokenDrop:   训练期按 token 丢弃，形状不变 [B, N, Dh]
          3) [CLS] 可选:  拼接成 [B, N+1, Dh]
          4) ABS 位置:    若启用，与 tokens 相加（广播到 [1, N(+1), Dh]）
          5) Dropout:     形状不变
          6) Encoder:     native/custom，输出 h ∈ [B, N(+1), Dh]
          7) LayerNorm:   形状不变
        """
        if self.pre_keep_len > 0:
            # x: [B, T, D]，时间维从旧到新
            # 最近 pre_keep_len 天（尾部）保留原始时间粒度，更早的部分做 Patch 下采样
            x_pre = x[:, -self.pre_keep_len:, :]        # [B, pre_keep_len, D] 最近K天
            x_rest = x[:, :-self.pre_keep_len, :]       # [B, T-pre_keep_len, D] 更早历史
            pre_tokens = self.pre_embed(x_pre)          # [B, pre_keep_len, Dh]
            patch_tokens = self.patch_embed(x_rest)     # [B, N_patch, Dh]
            # 保持时间顺序：先历史 patch，再最近 K 天
            tokens = torch.cat([patch_tokens, pre_tokens], dim=1)
        else:
            tokens = self.patch_embed(x)              # [B,N,Dh] / [B,sum N_i,Dh]
        tokens = self.token_drop(tokens)
        if self.use_cls:
            cls = self.cls_token.expand(tokens.size(0), -1, -1)  # [B,1,Dh]
            tokens = torch.cat([cls, tokens], dim=1)             # [B,N+1,Dh]
        tokens = self.pos_add(tokens)  # abs/sinus 加法或 identity
        tokens = self.pos_drop(tokens)

        if self.encoder is not None:
            h = self.encoder(tokens)  # [B,N(+1),Dh]
        else:
            h = self.custom(tokens)   # [B,N(+1),Dh]
        return self.ln_out(h)                                     # [B,N(+1),Dh]

    def forward(self, x: torch.Tensor, return_fv: bool = False):
        """端到端前向。

        - 输入:  x ∈ [B, T, D]
        - 输出:  y ∈ [B] 或 (y ∈ [B], fv ∈ [B, Dh])
        """
        enc = self.forward_features(x)          # [B,N(+1),Dh]
        out = self.head(enc, return_fv=return_fv)
        if return_fv:
            pred, fv = out
            pred = self.pred_normalizer(pred)
            return pred, fv
        else:
            pred = out
            pred = self.pred_normalizer(pred)
            return pred
