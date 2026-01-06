import torch
import torch.nn as nn
from typing import Optional, Tuple

from .get_configs import DFZQGRUConfig

class DFZQGRU(nn.Module):
    """东方证券GRU模型
    
    基于GRU的深度学习模型，包含注意力机制和特征相关性约束。
    主要特点：
    1. 输入层：特征转换和激活
    2. GRU层：序列建模
    3. 注意力层：自适应权重分配
    4. 输出层：预测和归一化
    
    使用方法：
    应从TrainingConfig构造DFZQGRUConfig，再传给本模型。确保以下参数一致：
    - input_size：每个时间步的特征数（TrainingConfig.input_size）
    - hidden_size：隐藏层维度（TrainingConfig.hidden_size）
    - num_layers：GRU层数（TrainingConfig.num_layers）
    - dropout：Dropout比率（TrainingConfig.dropout）
    """
    def __init__(self, config: DFZQGRUConfig):
        super().__init__()
        self.config = config
        
        # 保存关键参数
        self.hidden_size = self.config.hidden_size
        self.input_size = self.config.input_size
        self.dropout = self.config.dropout
        self.num_layers = self.config.num_layers
        self.bidirectional = self.config.bidirectional
        self.output_size = self.config.output_size
        self.attention = self.config.attention
        
        # 构建模型
        self._build_model()
    
    def _build_model(self):
        """构建模型各个组件"""
        # 输入层
        self.net = nn.Sequential(
            nn.Linear(self.input_size, self.hidden_size),
            nn.Tanh()
        )
        
        # GRU层
        self.gru = nn.GRU(
            input_size=self.hidden_size,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,  # 使用实例变量
            batch_first=True,
            dropout=self.dropout,        # 使用实例变量
            bidirectional=self.bidirectional
        )
        
        # 注意力层 - 只在配置中启用时构建
        gru_output_size = self.hidden_size * 2 if self.bidirectional else self.hidden_size
        if self.attention:
            self.attention = nn.Sequential(
                nn.Linear(gru_output_size, gru_output_size // 2),
                nn.Dropout(self.dropout),
                nn.Tanh(),
                nn.Linear(gru_output_size // 2, 1, bias=False),
                nn.Softmax(dim=1)
            )
        
        # 输出层
        self.fc1 = nn.Linear(gru_output_size * 2, self.hidden_size)
        self.bn1 = nn.BatchNorm1d(self.hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(self.output_size, affine=False)

        # 权重初始化
        self._init_weights()
    
    def _init_weights(self):
        """对Linear和GRU权重做Xavier初始化"""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.GRU):
                for name, param in m.named_parameters():
                    if 'weight' in name:
                        nn.init.xavier_uniform_(param)
                    elif 'bias' in name:
                        nn.init.zeros_(param)
    
    def forward(self, x: torch.Tensor, h_0: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, torch.Tensor]:
        """前向传播
        
        Args:
            x: 输入序列，形状为 [batch_size, seq_len, input_size]
            h_0: 初始隐藏状态，可选
            
        Returns:
            Tuple[torch.Tensor, torch.Tensor]: 
                - 预测结果，形状为 [batch_size, output_size]
                - 中间特征，形状为 [batch_size, hidden_size]
        """
        
        # 数据格式检查：现在期望输入已经是 [B, T, D] 格式
        # 注释掉原有的转换逻辑，因为dataloader已经处理了
        if x.dim() == 3:
            B, dim1, dim2 = x.shape
            # 根据维度大小判断：通常特征维度 < 时间维度
            if dim1 < dim2:  # [B, D, T] -> [B, T, D]
                x = x.transpose(1, 2)
        
        
        # 输入层处理
        x = self.net(x)
        
        # GRU层处理
        gru_output, _ = self.gru(x, h_0)
        
        # 注意力机制 - 只在配置中启用时使用
        if self.config.attention:
            attention_scores = self.attention(gru_output)
            attended_output = torch.mul(gru_output, attention_scores)
            attended_output = torch.sum(attended_output, dim=1)
        else:
            # 不使用注意力时，直接使用最后一个时间步
            attended_output = gru_output[:, -1, :]
        
        # 合并最后时间步输出和注意力输出
        combined = torch.cat((gru_output[:, -1, :], attended_output), dim=1)
        
        # 输出层处理
        res = self.fc1(combined)
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        # x = self.bn2(x)   #对 [B,1] 做 BN2，收益非常有限，所以我觉得 可以删掉。我认为BN2 的作用几乎可以被学习到的最后一层偏置（bias）所替代。
        
        return x, res