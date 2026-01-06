import warnings
import torch.nn as nn


def build_native_encoder(d_model: int, nhead: int, ffn_mult: int,
                         dropout: float, norm_first: bool, depth: int,
                         attn_dropout: float):
    if attn_dropout and attn_dropout > 0:
        warnings.warn("attn_dropout 在 native encoder 中不单独生效（由 TransformerEncoderLayer.dropout 控制）")
    enc_layer = nn.TransformerEncoderLayer(
        d_model=d_model, nhead=nhead, dim_feedforward=ffn_mult*d_model,
        dropout=dropout, activation='gelu', batch_first=True, norm_first=norm_first
    )
    return nn.TransformerEncoder(enc_layer, num_layers=depth)


