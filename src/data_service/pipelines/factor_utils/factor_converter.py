#!/usr/bin/env python3
"""
因子格式转换器 - 负责将模型预测结果转换为不同目标系统的格式
单一入口：convert(df_pred, target="backtest") -> df_factor

设计原则：
- 可插拔的转换器设计，便于扩展新的目标格式
- 保持现有回测逻辑完全不变
- 为未来的 Wind、实盘等系统预留接口
"""
import pandas as pd
from typing import Literal, Dict, Any, Optional


def convert(df_pred: pd.DataFrame,
            target: Literal["backtest", "wind", "live"] = "backtest",
            cfg: Optional[Any] = None,
            **kwargs) -> pd.DataFrame:
    """
    因子格式转换主入口
    
    Args:
        df_pred: 模型预测结果，包含 ['trade_date', 'stock_code', 'model_pred'] 列
        target: 目标格式 ("backtest", "wind", "live")
        cfg: 配置对象（某些转换器可能需要）
        **kwargs: 额外参数
        
    Returns:
        pd.DataFrame: 转换后的因子数据
        
    Examples:
        >>> # 转换为回测格式（默认）
        >>> df_factor = convert(df_pred)
        
        >>> # 转换为 Wind 组合管理格式（将来实现）
        >>> df_factor = convert(df_pred, target="wind")
        
        >>> # 转换为实盘交易格式（将来实现）
        >>> df_factor = convert(df_pred, target="live", cfg=config)
    """
    if target == "backtest":
        return _convert_to_backtest(df_pred, cfg=cfg, **kwargs)
    elif target == "wind":
        # TODO: Wind 组合管理格式转换
        return _convert_to_wind(df_pred, cfg=cfg, **kwargs)
    elif target == "live":
        # TODO: 实盘交易系统接口格式转换
        return _convert_to_live(df_pred, cfg=cfg, **kwargs)
    else:
        raise ValueError(f"未知的目标格式: {target}. 支持的格式: backtest, wind, live")


def _convert_to_backtest(df_pred: pd.DataFrame, cfg: Optional[Any] = None, **kwargs) -> pd.DataFrame:
    """
    转换为回测系统格式
    
    原来的 _convert_predictions_to_backtest_format 方法的内容，
    保证与原逻辑完全一致，确保现有功能不受影响
    
    Args:
        df_pred: 模型预测结果
        cfg: 配置对象
        
    Returns:
        pd.DataFrame: 回测格式的因子数据，包含 ['trade_date', 'stock_code', 'name', 'value'] 列
    """
    try:
        print("🔄 转换预测结果为回测格式...")
        
        # 1. 检查必要列
        required_columns = ['trade_date', 'stock_code', 'model_pred']
        for col in required_columns:
            if col not in df_pred.columns:
                raise ValueError(f"预测数据缺少必要列: {col}")
        
        # 2. 创建回测格式数据
        backtest_data = df_pred.copy()
        
        # 3. 标准化列名和格式
        factor_name = 'model_gru_factor'  # 使用我们自己的因子名称
        backtest_data['name'] = factor_name
        backtest_data['value'] = backtest_data['model_pred']  # 因子值
        
        # 3.5 添加股票代码交易所后缀（这是关键！）
        def add_exchange_suffix(stock_code):
            """为股票代码添加交易所后缀"""
            if not isinstance(stock_code, str):
                stock_code = str(stock_code)
            
            if '.' in stock_code:  # 已经有后缀
                return stock_code
            
            # 根据股票代码判断交易所
            if stock_code.startswith('00') or stock_code.startswith('30'):
                return f"{stock_code}.SZ"  # 深交所
            elif stock_code.startswith('60') or stock_code.startswith('68'):
                return f"{stock_code}.SH"  # 上交所  
            elif stock_code.startswith('8') or stock_code.startswith('4'):
                return f"{stock_code}.BJ"  # 北交所
            else:
                return f"{stock_code}.SZ"  # 默认深交所
        
        backtest_data['stock_code'] = backtest_data['stock_code'].apply(add_exchange_suffix)
        print(f"   🏢 已添加交易所后缀，示例: {backtest_data['stock_code'].head(3).tolist()}")
        
        # 4. 日期格式转换（保持YYYY-MM-DD格式，符合示例数据）
        backtest_data['trade_date'] = pd.to_datetime(backtest_data['trade_date']).dt.strftime('%Y-%m-%d')
        
        # 5. 过滤回测时间范围（如果提供了配置）
        if cfg is not None and hasattr(cfg, 'start_date') and hasattr(cfg, 'end_date'):
            start_date = cfg.start_date  # YYYYMMDD 格式
            end_date = cfg.end_date      # YYYYMMDD 格式
            
            # 将配置中的YYYYMMDD格式转换为YYYY-MM-DD格式进行比较
            start_date_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
            end_date_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
            
            before_filter = len(backtest_data)
            backtest_data = backtest_data[
                (backtest_data['trade_date'] >= start_date_formatted) & 
                (backtest_data['trade_date'] <= end_date_formatted)
            ]
            after_filter = len(backtest_data)
            
            print(f"   时间过滤: {before_filter} -> {after_filter} 条记录 ({start_date_formatted} 至 {end_date_formatted})")
        
        # 6. 数据清理和验证
        # 移除空值
        before_clean = len(backtest_data)
        backtest_data = backtest_data.dropna(subset=['value'])
        after_clean = len(backtest_data)
        
        if before_clean != after_clean:
            print(f"   清理空值: {before_clean} -> {after_clean} 条记录")
        
        # 7. 选择最终需要的列
        backtest_data = backtest_data[['trade_date', 'stock_code', 'name', 'value']].copy()
        
        # 8. 数据统计
        if len(backtest_data) > 0:
            print(f"✅ 格式转换完成:")
            print(f"   - 因子名称: {factor_name}")
            print(f"   - 数据条数: {len(backtest_data):,}")
            print(f"   - 时间范围: {backtest_data['trade_date'].min()} - {backtest_data['trade_date'].max()}")
            print(f"   - 股票数量: {backtest_data['stock_code'].nunique()}")
            print(f"   - 因子值统计: 均值={backtest_data['value'].mean():.4f}, 标准差={backtest_data['value'].std():.4f}")
            
            # 验证因子名称一致性
            unique_names = backtest_data['name'].unique()
            print(f"   - 数据中的因子名称: {unique_names.tolist()}")
        else:
            raise ValueError("过滤后无数据，请检查时间范围设置")
        
        return backtest_data
        
    except Exception as e:
        print(f"❌ 回测格式转换失败: {str(e)}")
        raise e


def _convert_to_wind(df_pred: pd.DataFrame, cfg: Optional[Any] = None, **kwargs) -> pd.DataFrame:
    """
    转换为 Wind 组合管理系统格式
    
    TODO: 待实现 - Wind 组合管理系统的数据格式规范
    预期格式可能包括：
    - 股票代码格式：可能需要特定的代码映射
    - 权重字段：可能需要根据因子值计算权重
    - 行业分类：可能需要添加行业信息
    - 时间格式：可能需要特定的时间戳格式
    
    Args:
        df_pred: 模型预测结果
        cfg: 配置对象
        
    Returns:
        pd.DataFrame: Wind 格式的因子数据
        
    Raises:
        NotImplementedError: 功能尚未实现
    """
    # TODO: 实现 Wind 格式转换逻辑
    # 可能的实现步骤：
    # 1. 检查输入数据格式
    # 2. 转换股票代码格式（如：000001.SZ -> 000001）
    # 3. 计算权重或排名
    # 4. 添加行业分类信息
    # 5. 格式化时间字段
    # 6. 生成 Wind 系统要求的列结构
    
    raise NotImplementedError(
        "Wind 组合管理格式转换尚未实现。\n"
        "预计需要实现的功能：\n"
        "- 股票代码格式转换\n"
        "- 权重计算\n"
        "- 行业分类添加\n"
        "- 时间格式标准化"
    )


def _convert_to_live(df_pred: pd.DataFrame, cfg: Optional[Any] = None, **kwargs) -> pd.DataFrame:
    """
    转换为实盘交易系统格式
    
    TODO: 待实现 - 实盘交易系统的接口格式
    预期功能可能包括：
    - API 接口格式：符合交易系统的 REST/gRPC 接口规范
    - 实时数据流：支持流式数据推送格式
    - 风控字段：添加风险控制相关字段
    - 订单字段：可能需要转换为订单执行格式
    
    Args:
        df_pred: 模型预测结果
        cfg: 配置对象
        
    Returns:
        pd.DataFrame: 实盘交易格式的因子数据
        
    Raises:
        NotImplementedError: 功能尚未实现
    """
    # TODO: 实现实盘交易格式转换逻辑
    # 可能的实现步骤：
    # 1. 验证数据实时性
    # 2. 添加风控检查字段
    # 3. 转换为订单格式
    # 4. 添加执行优先级
    # 5. 生成交易系统 API 格式
    # 6. 支持流式数据推送格式
    
    raise NotImplementedError(
        "实盘交易格式转换尚未实现。\n"
        "预计需要实现的功能：\n"
        "- 交易 API 格式适配\n"
        "- 风控字段添加\n"
        "- 订单格式转换\n"
        "- 实时数据流支持"
    )


def get_supported_formats() -> list:
    """获取支持的转换格式列表"""
    return ["backtest", "wind", "live"]


def validate_df_pred(df_pred: pd.DataFrame) -> bool:
    """
    验证输入的 df_pred 格式是否正确
    
    Args:
        df_pred: 待验证的预测数据
        
    Returns:
        bool: 验证是否通过
        
    Raises:
        ValueError: 格式验证失败
    """
    required_columns = ['trade_date', 'stock_code', 'model_pred']
    
    for col in required_columns:
        if col not in df_pred.columns:
            raise ValueError(f"df_pred 缺少必要列: {col}")
    
    if len(df_pred) == 0:
        raise ValueError("df_pred 不能为空")
    
    # 检查数据类型
    if not pd.api.types.is_numeric_dtype(df_pred['model_pred']):
        raise ValueError("model_pred 列必须是数值类型")
    
    return True


# 便捷函数
def convert_to_backtest(df_pred: pd.DataFrame, cfg: Optional[Any] = None) -> pd.DataFrame:
    """便捷函数：转换为回测格式"""
    return convert(df_pred, target="backtest", cfg=cfg)


def convert_to_wind(df_pred: pd.DataFrame, cfg: Optional[Any] = None) -> pd.DataFrame:
    """便捷函数：转换为 Wind 格式（待实现）"""
    return convert(df_pred, target="wind", cfg=cfg)


def convert_to_live(df_pred: pd.DataFrame, cfg: Optional[Any] = None) -> pd.DataFrame:
    """便捷函数：转换为实盘格式（待实现）"""
    return convert(df_pred, target="live", cfg=cfg)