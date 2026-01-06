import torch
import torch.nn as nn


class PredNormalizer(nn.Module):
    """
    Prediction normalizer applied on the cross-sectional dimension (batch items).

    Modes:
      - 'none':    no normalization
      - 'batchnorm': BatchNorm1d over a single channel with track_running_stats=False
      - 'zscore':  (x - mean) / (std + eps) computed per-batch

    Notes:
      - Expected input shape: [B] or [B, 1]. Output matches input shape.
      - BatchNorm path uses batch statistics both in train/eval (track_running_stats=False),
        aligning with per-day cross-sectional normalization semantics.
    """

    def __init__(self, mode: str = 'batchnorm', affine: bool = False, eps: float = 1e-6):
        super().__init__()
        mode = (mode or 'none').lower()
        assert mode in ('none', 'batchnorm', 'zscore'), f"Unsupported pred_norm mode: {mode}"
        self.mode = mode
        self.eps = float(eps)

        if self.mode == 'batchnorm':
            # Single-channel BN over batch axis; no running stats so eval uses batch stats as well
            self.bn = nn.BatchNorm1d(1, affine=bool(affine), eps=self.eps, track_running_stats=False)
        else:
            self.bn = None

    def forward(self, pred: torch.Tensor) -> torch.Tensor:
        if self.mode == 'none':
            return pred

        # Ensure shape [B, 1]
        orig_shape = pred.shape
        if pred.dim() == 1:
            x = pred.view(-1, 1)
        elif pred.dim() == 2 and pred.size(1) == 1:
            x = pred
        else:
            # Unexpected shape; try to squeeze to [B]
            x = pred.squeeze(-1)
            x = x.view(-1, 1)

        if self.mode == 'batchnorm':
            y = self.bn(x)
        else:  # zscore
            mean = x.mean(dim=0, keepdim=True)
            var = x.var(dim=0, unbiased=False, keepdim=True)
            std = (var + self.eps).sqrt()
            y = (x - mean) / std

        # Restore shape
        if len(orig_shape) == 1:
            return y.view(-1)
        elif len(orig_shape) == 2 and orig_shape[1] == 1:
            return y
        else:
            return y.view(*orig_shape)


