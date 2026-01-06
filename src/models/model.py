import torch
import torch.nn as nn
from typing import Dict, Any
from .base_model import BaseModel

class InvestmentModel(BaseModel):
    """投资预测模型"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化模型
        
        Args:
            config: 模型配置字典
        """
        super().__init__(config)
        
        # 获取模型配置
        model_config = config['model_architecture']
        input_size = model_config['input_size']
        hidden_size = model_config['hidden_size']
        num_layers = model_config['num_layers']
        dropout = model_config['dropout']
        output_size = model_config['output_size']
        
        # 定义模型架构
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0
        )
        
        self.fc = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, output_size)
        )
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """前向传播
        
        Args:
            x: 输入张量，形状为 (batch_size, sequence_length, input_size)
            
        Returns:
            输出张量，形状为 (batch_size, output_size)
        """
        # LSTM层
        lstm_out, _ = self.lstm(x)
        
        # 只使用最后一个时间步的输出
        last_hidden = lstm_out[:, -1, :]
        
        # 全连接层
        output = self.fc(last_hidden)
        
        return output
    
    def predict(self, x: torch.Tensor) -> torch.Tensor:
        """预测
        
        Args:
            x: 输入张量
            
        Returns:
            预测结果
        """
        self.eval()
        with torch.no_grad():
            return self(x)
    
    def get_attention_weights(self, x: torch.Tensor) -> torch.Tensor:
        """获取注意力权重
        
        Args:
            x: 输入张量
            
        Returns:
            注意力权重
        """
        # LSTM层
        lstm_out, _ = self.lstm(x)
        
        # 计算注意力权重
        attention_weights = torch.softmax(
            torch.matmul(lstm_out, lstm_out.transpose(-1, -2)),
            dim=-1
        )
        
        return attention_weights
    
    def get_feature_importance(self, x: torch.Tensor) -> torch.Tensor:
        """获取特征重要性
        
        Args:
            x: 输入张量
            
        Returns:
            特征重要性分数
        """
        # 获取LSTM输出
        lstm_out, _ = self.lstm(x)
        
        # 计算特征重要性
        feature_importance = torch.mean(torch.abs(lstm_out), dim=1)
        
        return feature_importance 