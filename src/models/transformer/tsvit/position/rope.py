import torch


def _rotate_half(x: torch.Tensor) -> torch.Tensor:
    x1, x2 = x[..., ::2], x[..., 1::2]
    return torch.stack((-x2, x1), dim=-1).flatten(-2)


def apply_rope(q: torch.Tensor, k: torch.Tensor, rope_dim: int, theta: float = 10000.0):
    """在注意力前对 q,k 注入旋转位置编码（RoPE）。

    参数:
        q, k:     [B, H, L, d_head]
        rope_dim: 应用的前缀维度，必须为偶数且 ≤ d_head
        theta:    周期缩放基数（默认 10000.0）
    返回:
        q', k':   与输入等形状的张量，前 rope_dim 维包含位置相位旋转
    """
    if rope_dim == 0:
        return q, k
    B, H, L, Dh = q.shape
    assert rope_dim % 2 == 0 and rope_dim <= Dh
    pos = torch.arange(L, device=q.device, dtype=torch.float32)
    inv = 1.0 / (theta ** (torch.arange(0, rope_dim, 2, device=q.device).float() / rope_dim))
    freqs = torch.outer(pos, inv)  # [L, rope_dim/2]
    cos = freqs.cos().repeat_interleave(2, dim=-1).view(1, 1, L, rope_dim).to(q.dtype)
    sin = freqs.sin().repeat_interleave(2, dim=-1).view(1, 1, L, rope_dim).to(q.dtype)
    q1, q2 = q[..., :rope_dim], q[..., rope_dim:]
    k1, k2 = k[..., :rope_dim], k[..., rope_dim:]
    q1 = q1 * cos + _rotate_half(q1) * sin
    k1 = k1 * cos + _rotate_half(k1) * sin
    return torch.cat([q1, q2], dim=-1), torch.cat([k1, k2], dim=-1)


