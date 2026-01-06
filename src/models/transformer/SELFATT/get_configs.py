from typing import Optional


class SELFATTConfig:
    """SELFATT 模型配置类 - Configuration for the SELFATT cross‑sectional return predictor

    Parameters
    ----------
    input_size : int
        每支股票在单个截面上的特征数量 J；论文中为 14。
        Number of raw firm characteristics **J** (14 in the paper).
    embedding_size : int, default=64
        嵌入层维度 S，对应论文中先将 J 维特征映射到 S 维。
        Dimension **S** of the characteristic embedding (paper uses 64).
    num_heads : int, default=8
        多头自注意力的头数 H（论文经验值 8）。
        Number of attention heads **H** (paper uses 8).
    d_model : int, default=64
        Self-attention 最终输出维度 D_model（论文经验值 64）。
        Model hidden size after the multi‑head concat; also the feed‑forward
        input size (**D_model** in the paper). 64 matches the authors' choice.
    ff_multiple : int, default=4
        前馈网络隐藏层扩展倍数（Transformer风格）。
        Expansion factor for the feed‑forward hidden layer (Transformer‑style).
    dropout : float, default=0.0
        Dropout 比率，可视情况开启以防止过拟合。
        Dropout probability applied to attention & feed‑forward.
    output_size : int, default=1
        输出维度；横截面收益预测通常为 1。
        Prediction dimension (1 = expected return).
    norm : str, {"layer", "batch", "none"}, default="layer"
        嵌入层后的归一化策略。
        Normalisation strategy right after the embedding layer.
    """

    def __init__(
        self,
        input_size: int,
        embedding_size: int = 64,
        num_heads: int = 8,
        d_model: int = 64,
        ff_multiple: int = 4,
        dropout: float = 0.0,
        output_size: int = 1,
        norm: str = "layer",
    ):
        self.input_size = input_size
        self.embedding_size = embedding_size
        self.num_heads = num_heads
        self.d_model = d_model
        self.ff_multiple = ff_multiple
        self.dropout = dropout
        self.output_size = output_size
        self.norm = norm.lower()

    @classmethod
    def default(cls, input_size: int = 20) -> 'SELFATTConfig':
        """返回默认配置
        
        Parameters
        ----------
        input_size : int, default=20
            输入特征维度，需要根据实际数据设置
        """
        return cls(input_size=input_size)


if __name__ == "__main__":
    # 创建默认配置
    config = SELFATTConfig.default()
    print(f"SELFATTConfig: input_size={config.input_size}, embedding_size={config.embedding_size}, "
          f"num_heads={config.num_heads}, d_model={config.d_model}, ff_multiple={config.ff_multiple}, "
          f"dropout={config.dropout}, output_size={config.output_size}, norm={config.norm}") 