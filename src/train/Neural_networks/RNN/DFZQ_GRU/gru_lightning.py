from __future__ import annotations
import torch, torch.nn as nn
import lightning.pytorch as pl
from scipy.stats import spearmanr

from src.models.rnn.gru.dfzq_gru.dfzq_gru import DFZQGRU
from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig

# ────────────── utils ───────────────────
def orthogonality_penalty(fv: torch.Tensor) -> torch.Tensor:
    corr = torch.corrcoef(fv.T) if torch.__version__ >= "2.1" else None
    if corr is None:        # 手动
        n = fv.size(0)
        fv_norm = (fv - fv.mean(0)) / (fv.std(0) + 1e-12)
        corr = (fv_norm.T @ fv_norm) / n
    corr = corr - torch.diag_embed(torch.diag(corr))
    return (corr ** 2).mean()

def spearman_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    ic, _ = spearmanr(pred.detach().cpu().squeeze(), label.detach().cpu(), nan_policy="omit")
    return float(ic)

# ────────────── LightningModule ─────────
class GRULightning(pl.LightningModule):
    def __init__(
        self,
        model_cfg: DFZQGRUConfig,
        train_cfg,
        label_mean: float,
        label_std: float,
    ):
        super().__init__()
        self.save_hyperparameters(ignore=["model_cfg"])   # 方便 checkpoint 里保存 cfg
        self.net = DFZQGRU(model_cfg)
        self.alpha_corr = train_cfg.alpha_corr
        self.lr = train_cfg.lr
        self.label_mean = label_mean
        self.label_std = label_std

    # ---------- forward ----------
    def forward(self, x):
        return self.net(x)

    # ---------- shared step -------
    def _step(self, batch):
        feats, labels = batch
        feats = feats.permute(0, 2, 1).float()
        labels = labels.unsqueeze(1).float()
        labels = (labels - self.label_mean) / self.label_std

        preds, fv = self.net(feats)
        loss = -(preds * labels).mean() + self.alpha_corr * orthogonality_penalty(fv)
        ic = spearman_ic(fv.mean(1, keepdim=True), labels)

        return loss, ic

    def training_step(self, batch, _):
        loss, ic = self._step(batch)
        self.log("train_loss", loss, prog_bar=True)
        self.log("train_ic", ic, prog_bar=False)
        return loss

    def validation_step(self, batch, _):
        _, ic = self._step(batch)
        self.log("val_ic", ic, prog_bar=True, sync_dist=True)

    def test_step(self, batch, _):
        _, ic = self._step(batch)
        self.log("test_ic", ic, prog_bar=True)

    # ---------- optim --------------
    def configure_optimizers(self):
        opt = torch.optim.Adam(self.parameters(), lr=self.lr)
        sched = torch.optim.lr_scheduler.ReduceLROnPlateau(
            opt, mode="max", patience=5, factor=0.5
        )
        return {
            "optimizer": opt,
            "lr_scheduler": {"scheduler": sched, "monitor": "val_ic"},
        }
