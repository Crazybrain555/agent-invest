from dataclasses import dataclass
from typing import Optional


@dataclass
class DFZQGRUConfig:
    """DFZQ GRU模型配置类 - 简化版
    
    专注于核心模型参数，避免过度复杂的配置项。
    实际使用时主要参数会从TrainingConfig传入。
    """
    
    # ============ 核心模型架构参数 ============
    input_size: int = 28                    # 输入特征维度 (18个因子 + 2个mask)
    hidden_size: int = 64                  # GRU隐藏层维度
    num_layers: int = 2                    # GRU层数
    output_size: int = 1                   # 输出维度
    
    # ============ 模型选项 ============
    bidirectional: bool = False             # 是否使用双向GRU
    attention: bool = True                 # 是否使用注意力机制
    dropout: float = 0.1                   # Dropout比例
    
    # ============ 残差MLP配置 ============
    input_hidden_dim: Optional[int] = 40    # 输入残差块隐藏维度，默认为hidden_size
    head_hidden_dim: Optional[int] = None     # 头部残差块隐藏维度，默认为hidden_size*2
    
    # ============ 归一化配置 ============
    input_norm_type: str = None            # 输入残差块归一化类型: "batch" | "layer" | "instance" | None
    feature_norm_type: str = "layer"      # 特征残差块归一化类型: "batch" | "layer" | "group" | None  
    use_pre_gru_norm: bool = False         # 是否在GRU前添加额外的BatchNorm1d
    
    # ============ 其他配置 ============
    feature_extractor: bool = True         # 是否启用特征提取器
    attn_tau: float = 2                  # 注意力温度参数 (增大以减少激活值爆炸)
    learnable_tau: bool = False             # 注意力温度参数是否可学习
    monitor_gates: bool = True            # 是否全局启用GRU门控监控（仅调试/诊断时建议开启）
    output_head_type: str = 'simple'      # 输出头类型: "simple" | "mlp"
    
 
    
    @classmethod
    def default(cls) -> 'DFZQGRUConfig':
        """返回默认配置"""
        return cls()


if __name__ == "__main__":
    # 创建默认配置
    config = DFZQGRUConfig.default()
    print(config)

