from typing import Dict, Any
from .models import LSTM, Transformer, CNN, MLP

class ModelFactory:
    """模型工厂类，用于创建不同类型的模型"""
    
    @staticmethod
    def create_model(model_type: str, config: Dict[str, Any]):
        """
        根据配置创建模型实例
        
        Args:
            model_type: 模型类型
            config: 模型配置
        
        Returns:
            model: 模型实例
        """
        model_config = config['models'][model_type]
        
        if model_type == "LSTM":
            return LSTM(**model_config)
        elif model_type == "Transformer":
            return Transformer(**model_config)
        elif model_type == "CNN":
            return CNN(**model_config)
        elif model_type == "MLP":
            return MLP(**model_config)
        else:
            raise ValueError(f"Unsupported model type: {model_type}") 