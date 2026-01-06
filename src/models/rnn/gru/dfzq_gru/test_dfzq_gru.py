import os
import sys
import torch

# 添加项目根目录到 Python 路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.."))
sys.path.insert(0, project_root)

from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.models.rnn.gru.dfzq_gru.dfzq_gru import DFZQGRU

def test_model():
    """测试 DFZQGRU 模型"""
    # 创建默认配置
    config = DFZQGRUConfig.default()
    
    # 创建模型
    model = DFZQGRU(config)
    
    # 创建测试数据
    batch_size = 32
    seq_len = 30
    input_size = config.input_size
    
    # 生成随机输入数据
    x = torch.randn(batch_size, seq_len, input_size)
    
    # 测试前向传播
    try:
        output, features = model(x)
        print("模型测试成功！")
        print(f"输入形状: {x.shape}")
        print(f"输出形状: {output.shape}")
        print(f"特征形状: {features.shape}")
    except Exception as e:
        print(f"模型测试失败: {str(e)}")
    
    # 打印模型结构
    print("\n模型结构:")
    print(model)
    
    # 打印模型参数数量
    total_params = sum(p.numel() for p in model.parameters())
    print(f"\n总参数量: {total_params:,}")

if __name__ == "__main__":
    test_model() 