import torch
import torch.nn as nn
from typing import Optional, List, Dict, Union
from .get_configs import SELFATTConfig


class SELFATT(nn.Module):
    """SELFATT — 论文原型的 PyTorch 实现 (PyTorch implementation of the SELFATT architecture)

    ▶ **输入格式 (Input Format)**
      * **x** : `Tensor`，形状 **[batch_size, num_stocks, input_size]**
        *batch_size* 表示一次可并行处理多个横截面（月/周/日），常为 1；
        *num_stocks* (N) 是截面中股票数量；
        *input_size* (J) 是特征数。
      * **stock_ids** *(可选)* : `List[str]`，长度 = num_stocks，用于一一对应模型输出与股票代码。

    ▶ **输出格式 (Output Format)**
      * 若 `return_dict=False` (默认)：返回 `Tensor`，形状 **[batch_size, num_stocks, output_size]**，顺序与传入 `x` 的第二维一致。
      * 若 `return_dict=True` 并提供 `stock_ids`：返回 `List[Dict[str, float]]`，列表长度=batch_size，列表中每个字典将股票代码映射到预测值，便于直接落地到 DataFrame。

    ▶ **模型架构说明 (Architecture Overview)**
    遵循论文 Figure-1 的完整架构流程：
    1. **Factor Embedding** (蓝色块)：两层线性变换 + ReLU，将 J 维特征映射到 S 维嵌入空间
    2. **Normalisation** (绿色块)：嵌入后的归一化层，支持 LayerNorm/BatchNorm/None
    3. **Multi-head Self-Attention** (紫色块)：捕捉股票间交互、特征非线性与异质性
    4. **Feed-forward** (橙色块)：Transformer风格的前馈网络，扩展模型表达能力
    5. **Output Linear** (红色块)：最终输出层，映射到预测收益

    ▶ **时间序列处理说明 (Time Series Processing)**
    论文采用 *smoothing / sliding window* 策略：
    1. 在时点 *t-1*，收集 **过去 W (=120) 个月**的 (char<sub>t-k-1</sub>, r<sub>t-k</sub>) 训练样本，独立拟合一组模型；
    2. 将 W 组模型预测结果简单平均，得到 *t* 期的横截面预测。

    因此，**SELFATT 本身只处理单期横截面**，不在网络内部显式建模时间序列依赖；时间维度通过 *rolling fit* + *ensemble* 机制在训练/推理循环中体现。
    """

    def __init__(self, config: SELFATTConfig):
        super().__init__()
        self.cfg = config

        # ------------------------------------------------------------------
        # 1. Factor Embedding Layer (蓝色块 - Equation 2)
        #    将 J 维原始特征映射到 S 维嵌入空间
        # ------------------------------------------------------------------
        self.embedding = nn.Sequential(
            nn.Linear(self.cfg.input_size, self.cfg.embedding_size),
            nn.ReLU(),
            nn.Linear(self.cfg.embedding_size, self.cfg.embedding_size),
        )

        # ------------------------------------------------------------------
        # 2. Normalisation Layer (绿色块 - 嵌入后归一化)
        #    支持 LayerNorm/BatchNorm/None 三种策略
        # ------------------------------------------------------------------
        if self.cfg.norm == "layer":
            self.norm = nn.LayerNorm(self.cfg.embedding_size)
        elif self.cfg.norm == "batch":
            # BatchNorm1d 期望 (B*N, S) 形状 - 在 forward 中重塑
            self.norm = nn.BatchNorm1d(self.cfg.embedding_size, affine=True)
        else:
            self.norm = nn.Identity()

        # ------------------------------------------------------------------
        # 3. Multi-head Self-Attention (紫色块)
        #    捕捉股票间交互、特征非线性与异质性
        #    Q=K=V=embedding(x) 符合论文 self-attention 描述
        # ------------------------------------------------------------------
        self.attn = nn.MultiheadAttention(
            embed_dim=self.cfg.embedding_size,
            num_heads=self.cfg.num_heads,
            dropout=self.cfg.dropout,
            batch_first=True,  # 省去 transpose，输入输出维持 [B, N, S]
        )

        # ------------------------------------------------------------------
        # 4. Feed-forward Network (橙色块 - Transformer风格)
        #    扩展模型表达能力，从 S 维映射到 D_model 维
        # ------------------------------------------------------------------
        ff_hidden = self.cfg.d_model * self.cfg.ff_multiple
        self.feedforward = nn.Sequential(
            nn.Linear(self.cfg.embedding_size, ff_hidden),
            nn.ReLU(),
            nn.Dropout(self.cfg.dropout),
            nn.Linear(ff_hidden, self.cfg.d_model),
        )

        # ------------------------------------------------------------------
        # 5. Output Linear Layer (红色块)
        #    D_model ➜ output_size (通常为1，表示预期收益)
        # ------------------------------------------------------------------
        self.out_proj = nn.Linear(self.cfg.d_model, self.cfg.output_size)

        # 权重初始化
        self._init_weights()

    # ----------------------------------------------------------------------
    # 权重初始化 (Weight Initialization)
    # ----------------------------------------------------------------------
    def _init_weights(self):
        """权重初始化：使用 Xavier 初始化保证训练稳定性"""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    # ----------------------------------------------------------------------
    # 前向传播 (Forward Pass)
    # ----------------------------------------------------------------------
    def forward(
        self,
        x: torch.Tensor,
        stock_ids: Optional[List[str]] = None,
        return_dict: bool = False,
    ) -> Union[torch.Tensor, List[Dict[str, float]]]:
        """前向传播 - 完整的 SELFATT 架构流程

        Parameters
        ----------
        x : Tensor
            Shape [B, N, J] — 横截面特征矩阵批量
            B: batch_size (通常为1)
            N: num_stocks (截面股票数量)
            J: input_size (特征维度)
        stock_ids : List[str] | None
            若想直接获得 *股票代码→预测值* 的映射，可传入股票代码列表；长度需与 N 一致。
        return_dict : bool, default=False
            是否返回 `List[Dict]` 模式（需要同时提供 `stock_ids`）。

        Returns
        -------
        Union[torch.Tensor, List[Dict[str, float]]]
            若 return_dict=False: Tensor [B, N] 或 [B, N, output_size]
            若 return_dict=True: List[Dict[str, float]]，每个dict包含股票代码到预测值的映射
        """
        B, N, _ = x.shape

        # ── 1. Factor Embedding ────────────────────────────────────────
        #    将 J 维原始特征映射到 S 维嵌入空间 (Equation 2)
        x_emb = self.embedding(x)                # [B, N, S]

        # ── 2. Normalisation ────────────────────────────────────────────
        #    嵌入后归一化，提升训练稳定性
        if isinstance(self.norm, nn.BatchNorm1d):
            # BatchNorm1d 需要 (B*N, S) 形状
            x_norm = self.norm(x_emb.view(-1, self.cfg.embedding_size)).view(B, N, -1)
        else:
            # LayerNorm 或 Identity
            x_norm = self.norm(x_emb)

        # ── 3. Multi-head Self-Attention ───────────────────────────────
        #    捕捉股票间交互，Q=K=V=normalized_embedding
        attn_out, _ = self.attn(x_norm, x_norm, x_norm)  # [B, N, S]

        # ── 4. Feed-forward Network ────────────────────────────────────
        #    Transformer风格前馈网络，扩展表达能力
        encoded = self.feedforward(attn_out)     # [B, N, D_model]

        # ── 5. Output Linear ───────────────────────────────────────────
        #    映射到最终预测值
        pred = self.out_proj(encoded)            # [B, N, output_size]
        
        # squeeze(-1) 保持与论文"单值预测"对应
        pred = pred.squeeze(-1) if self.cfg.output_size == 1 else pred

        # ──────────────────────────────────────────────────────────────────
        # 6. 可选：转为 {stock_id: value} 的字典列表
        #    便于直接落地到 DataFrame 或其他下游应用
        # ──────────────────────────────────────────────────────────────────
        if return_dict:
            assert stock_ids is not None and len(stock_ids) == N, "stock_ids 长度必须与 num_stocks 一致"
            # 每个 batch 生成一个 dict
            dict_list: List[Dict[str, float]] = []
            for b in range(B):
                dict_list.append({sid: float(pred[b, i].item()) for i, sid in enumerate(stock_ids)})
            return dict_list

        return pred  # Tensor [B, N] or [B, N, output_size]
