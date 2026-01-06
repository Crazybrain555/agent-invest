#!/usr/bin/env python3
"""
因子信号处理工具
负责构建回测所需的因子表达式
"""
from typing import List


def build_signals(base_factor: str,
                  shift: int = 1,
                  negative: bool = False) -> List[str]:
    """
    构建因子信号表达式，可直接送入 backtest.expression 使用

    Args:
        base_factor (str): 基础因子名称，如 "model_gru_factor"
        shift (int): 因子滞后期数，用于防止前视偏差，默认1期
        negative (bool): 是否对信号取反，用于反向交易策略

    Returns:
        List[str]: 因子表达式列表，可直接用于回测

    Examples:
        >>> build_signals("model_gru_factor")
        ['delay(model_gru_factor,1)']
        
        >>> build_signals("model_gru_factor", shift=2, negative=True)
        ['-(delay(model_gru_factor,2))']
        
        >>> build_signals("model_gru_factor", shift=0, negative=False)
        ['model_gru_factor']
    """
    sig = base_factor
    
    # 应用信号变换
    if negative:
        print(f"   🔄 应用信号取反...")
        sig = f"-({sig})"
    
    if shift > 0:
        print(f"   ⏰ 应用因子滞后 {shift} 期...")
        sig = f"delay({sig},{shift})"
    
    return [sig]


def get_factor_name_from_data(df_factor) -> str:
    """
    从因子数据中提取因子名称
    
    Args:
        df_factor: 包含因子数据的DataFrame，需要有'name'列
        
    Returns:
        str: 因子名称
    """
    if 'name' in df_factor.columns:
        unique_names = df_factor['name'].unique()
        if len(unique_names) == 1:
            return unique_names[0]
        elif len(unique_names) > 1:
            print(f"⚠️ 发现多个因子名称: {unique_names.tolist()}，使用第一个")
            return unique_names[0]
        else:
            print("⚠️ name列为空，使用默认因子名称")
            return 'model_gru_factor'
    else:
        print("⚠️ 数据中缺少name列，使用默认因子名称")
        return 'model_gru_factor'


def generate_factor_signals(df_factor, shift: int = 1, negative: bool = False) -> List[str]:
    """
    从因子数据生成信号表达式（便捷函数）
    
    Args:
        df_factor: 因子数据DataFrame
        shift: 因子滞后期数
        negative: 是否信号取反
        
    Returns:
        List[str]: 因子表达式列表
    """
    print("🎯 构建因子信号表达式...")
    
    try:
        # 1. 从数据中提取因子名称
        base_factor_name = get_factor_name_from_data(df_factor)
        print(f"   使用因子名称: {base_factor_name}")
        
        # 2. 构建因子表达式
        factor_expressions = build_signals(
            base_factor_name, 
            shift=shift, 
            negative=negative
        )
        
        print(f"✅ 因子表达式构建完成: {factor_expressions}")
        return factor_expressions
        
    except Exception as e:
        print(f"❌ 因子信号生成失败: {str(e)}")
        return ["model_gru_factor"]  # 返回默认因子