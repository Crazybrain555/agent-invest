import pandas as pd
import numpy as np
from typing import Dict, Any
from .base_strategy import BaseStrategy

class MLStrategy(BaseStrategy):
    """基于机器学习的投资策略"""
    
    def __init__(self, config: Dict[str, Any], model: Any = None):
        """初始化策略
        
        Args:
            config: 策略配置字典
            model: 预测模型实例
        """
        super().__init__(config, model)
        self.signal_config = config['strategy']['signal_generation']
        self.risk_config = config['strategy']['risk_control']
    
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号
        
        Args:
            data: 市场数据
            
        Returns:
            包含交易信号的数据框
        """
        # 使用模型进行预测
        predictions = self.model.predict(data)
        
        # 生成交易信号
        signals = self._generate_trading_signals(predictions)
        
        # 应用风险控制
        signals = self._apply_risk_control(signals, data)
        
        # 信号平滑
        signals = self._smooth_signals(signals)
        
        return signals
    
    def _generate_trading_signals(self, predictions: np.ndarray) -> pd.Series:
        """生成交易信号
        
        Args:
            predictions: 模型预测结果
            
        Returns:
            交易信号序列
        """
        threshold = self.signal_config['threshold']
        
        # 根据预测值和阈值生成信号
        signals = pd.Series(index=predictions.index)
        signals[predictions > threshold] = 1  # 买入信号
        signals[predictions < -threshold] = -1  # 卖出信号
        signals[(predictions >= -threshold) & (predictions <= threshold)] = 0  # 持有信号
        
        return signals
    
    def _apply_risk_control(self, 
                          signals: pd.Series,
                          data: pd.DataFrame) -> pd.Series:
        """应用风险控制
        
        Args:
            signals: 交易信号
            data: 市场数据
            
        Returns:
            经过风险控制的信号
        """
        # 计算波动率
        volatility = data['close'].pct_change().rolling(window=20).std()
        
        # 计算VaR
        var = data['close'].pct_change().rolling(window=20).quantile(0.05)
        
        # 根据风险指标调整信号
        risk_adjusted_signals = signals.copy()
        
        # 当波动率过高时降低仓位
        high_vol_mask = volatility > self.risk_config['var_limit']
        risk_adjusted_signals[high_vol_mask] *= 0.5
        
        # 当VaR过高时降低仓位
        high_var_mask = var < -self.risk_config['var_limit']
        risk_adjusted_signals[high_var_mask] *= 0.5
        
        return risk_adjusted_signals
    
    def _smooth_signals(self, signals: pd.Series) -> pd.Series:
        """平滑交易信号
        
        Args:
            signals: 原始信号
            
        Returns:
            平滑后的信号
        """
        window = self.signal_config['smoothing_window']
        return signals.rolling(window=window, min_periods=1).mean()
    
    def rebalance_portfolio(self, 
                          data: pd.DataFrame,
                          timestamp: pd.Timestamp) -> None:
        """重新平衡投资组合
        
        Args:
            data: 市场数据
            timestamp: 当前时间
        """
        # 生成新的交易信号
        signals = self.generate_signals(data)
        
        # 获取当前价格
        current_prices = data.loc[timestamp, 'close']
        
        # 执行交易
        for symbol in signals.index:
            if symbol in current_prices:
                self.execute_trade(
                    symbol=symbol,
                    signal=signals[symbol],
                    price=current_prices[symbol],
                    timestamp=timestamp
                )
        
        # 更新组合价值
        self.update_portfolio_value(current_prices, timestamp)
    
    def get_position_weights(self) -> Dict[str, float]:
        """获取当前持仓权重
        
        Returns:
            持仓权重字典
        """
        total_value = self.portfolio_value
        weights = {}
        
        for symbol, size in self.positions.items():
            weights[symbol] = size / total_value
            
        return weights
    
    def get_risk_metrics(self) -> Dict[str, float]:
        """获取风险指标
        
        Returns:
            风险指标字典
        """
        history_df = self.get_trade_history()
        
        # 计算波动率
        returns = history_df['portfolio_value'].pct_change()
        volatility = returns.std() * np.sqrt(252)
        
        # 计算VaR
        var = returns.quantile(0.05)
        
        # 计算最大回撤
        cummax = history_df['portfolio_value'].cummax()
        drawdown = (history_df['portfolio_value'] - cummax) / cummax
        max_drawdown = drawdown.min()
        
        return {
            'volatility': volatility,
            'var': var,
            'max_drawdown': max_drawdown
        } 