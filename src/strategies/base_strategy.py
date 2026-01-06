from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from ..models.base_model import BaseModel

class BaseStrategy(ABC):
    """策略基类"""
    
    def __init__(self, config: Dict[str, Any], model: Optional[BaseModel] = None):
        """初始化策略
        
        Args:
            config: 策略配置字典
            model: 预测模型实例
        """
        self.config = config
        self.model = model
        self.positions = {}  # 当前持仓
        self.cash = self.config['strategy']['backtesting']['initial_capital']
        self.portfolio_value = self.cash
        self.trade_history = []  # 交易历史
        
    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号
        
        Args:
            data: 市场数据
            
        Returns:
            包含交易信号的数据框
        """
        pass
    
    def calculate_position_size(self, 
                              signal: float,
                              price: float) -> int:
        """计算仓位大小
        
        Args:
            signal: 交易信号
            price: 当前价格
            
        Returns:
            仓位大小（股数）
        """
        position_config = self.config['strategy']['trading']
        max_position_value = self.portfolio_value * position_config['position_size']
        return int(max_position_value / price)
    
    def execute_trade(self, 
                     symbol: str,
                     signal: float,
                     price: float,
                     timestamp: pd.Timestamp) -> None:
        """执行交易
        
        Args:
            symbol: 股票代码
            signal: 交易信号
            price: 当前价格
            timestamp: 交易时间
        """
        # 计算交易成本
        transaction_cost = self._calculate_transaction_cost(price)
        
        # 计算目标仓位
        target_size = self.calculate_position_size(signal, price)
        current_size = self.positions.get(symbol, 0)
        
        # 计算需要交易的股数
        trade_size = target_size - current_size
        
        if trade_size != 0:
            # 计算交易金额
            trade_amount = trade_size * price
            
            # 检查是否有足够的现金
            if trade_amount + transaction_cost <= self.cash:
                # 更新持仓
                self.positions[symbol] = target_size
                
                # 更新现金
                self.cash -= (trade_amount + transaction_cost)
                
                # 记录交易
                self.trade_history.append({
                    'timestamp': timestamp,
                    'symbol': symbol,
                    'action': 'buy' if trade_size > 0 else 'sell',
                    'size': abs(trade_size),
                    'price': price,
                    'cost': transaction_cost
                })
    
    def _calculate_transaction_cost(self, price: float) -> float:
        """计算交易成本
        
        Args:
            price: 交易价格
            
        Returns:
            交易成本
        """
        cost_config = self.config['strategy']['transaction_cost']
        commission = price * cost_config['commission_rate']
        slippage = price * cost_config['slippage']
        return commission + slippage
    
    def update_portfolio_value(self, 
                             prices: Dict[str, float],
                             timestamp: pd.Timestamp) -> None:
        """更新组合价值
        
        Args:
            prices: 各股票当前价格
            timestamp: 更新时间
        """
        # 计算持仓市值
        position_value = sum(
            size * prices[symbol]
            for symbol, size in self.positions.items()
        )
        
        # 更新组合总价值
        self.portfolio_value = self.cash + position_value
        
        # 记录每日组合价值
        self.trade_history.append({
            'timestamp': timestamp,
            'action': 'portfolio_update',
            'portfolio_value': self.portfolio_value
        })
    
    def get_performance_metrics(self) -> Dict[str, float]:
        """计算策略表现指标
        
        Returns:
            表现指标字典
        """
        # 将交易历史转换为DataFrame
        history_df = pd.DataFrame(self.trade_history)
        
        # 计算收益率序列
        returns = history_df[history_df['action'] == 'portfolio_update']['portfolio_value'].pct_change()
        
        # 计算年化收益率
        annual_return = (1 + returns.mean()) ** 252 - 1
        
        # 计算夏普比率
        sharpe_ratio = np.sqrt(252) * returns.mean() / returns.std()
        
        # 计算最大回撤
        cummax = history_df['portfolio_value'].cummax()
        drawdown = (history_df['portfolio_value'] - cummax) / cummax
        max_drawdown = drawdown.min()
        
        # 计算信息比率
        benchmark_returns = pd.Series([0.0001] * len(returns))  # 假设基准日收益率为0.01%
        excess_returns = returns - benchmark_returns
        information_ratio = np.sqrt(252) * excess_returns.mean() / excess_returns.std()
        
        return {
            'annual_return': annual_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'information_ratio': information_ratio
        }
    
    def get_trade_history(self) -> pd.DataFrame:
        """获取交易历史
        
        Returns:
            交易历史DataFrame
        """
        return pd.DataFrame(self.trade_history) 