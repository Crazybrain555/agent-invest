import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from typing import Dict, Any, List, Optional
from ..backtesting.backtest import Backtester

class Visualizer:
    """可视化工具类"""
    
    def __init__(self, backtester: Optional[Backtester] = None):
        """初始化可视化器
        
        Args:
            backtester: 回测器实例
        """
        self.backtester = backtester
        plt.style.use('seaborn')
    
    def plot_portfolio_performance(self, 
                                 portfolio_values: pd.Series,
                                 benchmark_values: Optional[pd.Series] = None) -> None:
        """绘制投资组合表现
        
        Args:
            portfolio_values: 投资组合价值序列
            benchmark_values: 基准指数价值序列（可选）
        """
        plt.figure(figsize=(12, 6))
        
        # 绘制投资组合价值
        plt.plot(portfolio_values.index, portfolio_values.values, label='Portfolio')
        
        # 如果提供了基准指数，则绘制基准指数
        if benchmark_values is not None:
            plt.plot(benchmark_values.index, benchmark_values.values, label='Benchmark')
        
        plt.title('Portfolio Performance')
        plt.xlabel('Date')
        plt.ylabel('Value')
        plt.legend()
        plt.grid(True)
        plt.show()
    
    def plot_drawdown(self, portfolio_values: pd.Series) -> None:
        """绘制回撤图
        
        Args:
            portfolio_values: 投资组合价值序列
        """
        # 计算回撤
        cummax = portfolio_values.cummax()
        drawdown = (portfolio_values - cummax) / cummax
        
        plt.figure(figsize=(12, 6))
        plt.plot(drawdown.index, drawdown.values)
        plt.title('Portfolio Drawdown')
        plt.xlabel('Date')
        plt.ylabel('Drawdown')
        plt.grid(True)
        plt.show()
    
    def plot_position_weights(self, weights: Dict[str, float]) -> None:
        """绘制持仓权重
        
        Args:
            weights: 持仓权重字典
        """
        plt.figure(figsize=(10, 6))
        
        # 创建条形图
        plt.bar(weights.keys(), weights.values())
        
        plt.title('Position Weights')
        plt.xlabel('Symbol')
        plt.ylabel('Weight')
        plt.xticks(rotation=45)
        plt.grid(True)
        plt.tight_layout()
        plt.show()
    
    def plot_trade_distribution(self, trade_history: pd.DataFrame) -> None:
        """绘制交易分布
        
        Args:
            trade_history: 交易历史DataFrame
        """
        # 提取买入和卖出交易
        buy_trades = trade_history[trade_history['action'] == 'buy']
        sell_trades = trade_history[trade_history['action'] == 'sell']
        
        plt.figure(figsize=(12, 6))
        
        # 绘制买入和卖出交易的时间分布
        plt.scatter(buy_trades.index, buy_trades['price'], 
                   color='green', label='Buy', alpha=0.5)
        plt.scatter(sell_trades.index, sell_trades['price'], 
                   color='red', label='Sell', alpha=0.5)
        
        plt.title('Trade Distribution')
        plt.xlabel('Date')
        plt.ylabel('Price')
        plt.legend()
        plt.grid(True)
        plt.show()
    
    def plot_correlation_matrix(self, returns: pd.DataFrame) -> None:
        """绘制相关性矩阵
        
        Args:
            returns: 收益率DataFrame
        """
        plt.figure(figsize=(10, 8))
        sns.heatmap(returns.corr(), annot=True, cmap='coolwarm', center=0)
        plt.title('Correlation Matrix')
        plt.tight_layout()
        plt.show()
    
    def plot_risk_metrics(self, risk_metrics: Dict[str, float]) -> None:
        """绘制风险指标
        
        Args:
            risk_metrics: 风险指标字典
        """
        plt.figure(figsize=(10, 6))
        
        # 创建条形图
        plt.bar(risk_metrics.keys(), risk_metrics.values())
        
        plt.title('Risk Metrics')
        plt.xlabel('Metric')
        plt.ylabel('Value')
        plt.xticks(rotation=45)
        plt.grid(True)
        plt.tight_layout()
        plt.show()
    
    def plot_feature_importance(self, 
                              feature_importance: Dict[str, float],
                              top_n: int = 10) -> None:
        """绘制特征重要性
        
        Args:
            feature_importance: 特征重要性字典
            top_n: 显示前N个特征
        """
        # 选择前N个特征
        top_features = dict(sorted(
            feature_importance.items(),
            key=lambda x: x[1],
            reverse=True
        )[:top_n])
        
        plt.figure(figsize=(12, 6))
        
        # 创建条形图
        plt.bar(top_features.keys(), top_features.values())
        
        plt.title(f'Top {top_n} Feature Importance')
        plt.xlabel('Feature')
        plt.ylabel('Importance')
        plt.xticks(rotation=45)
        plt.grid(True)
        plt.tight_layout()
        plt.show()
    
    def plot_model_performance(self, 
                             train_loss: List[float],
                             val_loss: List[float]) -> None:
        """绘制模型性能
        
        Args:
            train_loss: 训练损失列表
            val_loss: 验证损失列表
        """
        plt.figure(figsize=(10, 6))
        
        plt.plot(train_loss, label='Training Loss')
        plt.plot(val_loss, label='Validation Loss')
        
        plt.title('Model Performance')
        plt.xlabel('Epoch')
        plt.ylabel('Loss')
        plt.legend()
        plt.grid(True)
        plt.show() 