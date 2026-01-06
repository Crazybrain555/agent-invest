"""
Step: 信号构建

职责：
- 调用 build_signals 生成因子表达式
- 返回 AlphaExpression 列表
"""

import logging
from typing import TYPE_CHECKING, List

from backtest.expression.expression import AlphaExpression

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig

logger = logging.getLogger(__name__)


def run_step_signal(
    cfg: "ModelBacktestConfig",
    base_factor: str = "model_gru_factor"
) -> List[AlphaExpression]:
    """
    构建因子信号表达式
    
    Args:
        cfg: ModelBacktestConfig 配置对象
        base_factor: 基础因子名称
    
    Returns:
        List[AlphaExpression]: 策略表达式列表
    """
    logger.info("Step Signal: 构建因子信号...")
    
    # 导入 build_signals（延迟导入避免循环依赖）
    from src.data_service.pipelines.factor_utils import build_signals
    
    # 获取因子表达式字符串列表
    factor_expressions = build_signals(
        base_factor=base_factor,
        shift=getattr(cfg, 'factor_shift', 1),
        negative=getattr(cfg, 'signal_negative', False)
    )
    
    logger.info(f"   因子表达式: {factor_expressions}")
    
    # 转换为 AlphaExpression 对象
    alpha_expressions = []
    for i, expression in enumerate(factor_expressions):
        alpha = AlphaExpression(
            name=f"expression_{i+1}",
            expression=expression.strip()
        )
        alpha_expressions.append(alpha)
    
    logger.info(f"   生成 {len(alpha_expressions)} 个策略表达式")
    
    return alpha_expressions
