import pandas as pd
import numpy as np
from typing import Dict, Any, List
from ..strategies.base_strategy import BaseStrategy
from ..data_service.data_service import DataService

class Backtester:
    """回测器类"""
    
    def __init__(self, 
                 strategy: BaseStrategy,
                 data_service: DataService,
                 config: Dict[str, Any]):
        """初始化回测器
        
        Args:
            strategy: 交易策略实例
            data_service: 数据服务实例
            config: 回测配置字典
        """
        self.strategy = strategy
        self.data_service = data_service
        self.config = config
        self.results = {}
        
    def run_backtest(self, 
                    symbols: List[str],
                    start_date: str,
                    end_date: str) -> Dict[str, Any]:
        """运行回测
        
        Args:
            symbols: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            回测结果字典
        """
        # 获取历史数据
        data = self._get_historical_data(symbols, start_date, end_date)
        
        # 按时间顺序遍历数据
        for timestamp in data.index.unique():
            # 获取当前时间点的数据
            current_data = data.loc[timestamp]
            
            # 更新策略
            self.strategy.rebalance_portfolio(current_data, timestamp)
        
        # 计算回测结果
        self.results = self._calculate_results()
        
        return self.results
    
    def _get_historical_data(self, 
                           symbols: List[str],
                           start_date: str,
                           end_date: str) -> pd.DataFrame:
        """获取历史数据
        
        Args:
            symbols: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            历史数据DataFrame
        """
        all_data = []
        
        for symbol in symbols:
            # 获取市场数据
            market_data = self.data_service.fetch_market_data(
                symbol, start_date, end_date
            )
            
            # 获取基本面数据
            fundamental_data = self.data_service.fetch_fundamental_data(
                symbol, end_date
            )
            
            # 合并数据
            data = pd.merge(
                market_data,
                fundamental_data,
                on='ts_code',
                how='left'
            )
            
            all_data.append(data)
        
        # 合并所有股票的数据
        combined_data = pd.concat(all_data)
        
        # 按时间排序
        combined_data = combined_data.sort_index()
        
        return combined_data
    
    def _calculate_results(self) -> Dict[str, Any]:
        """计算回测结果
        
        Returns:
            回测结果字典
        """
        # 获取交易历史
        trade_history = self.strategy.get_trade_history()
        
        # 计算策略表现指标
        performance_metrics = self.strategy.get_performance_metrics()
        
        # 计算风险指标
        risk_metrics = self.strategy.get_risk_metrics()
        
        # 获取持仓权重
        position_weights = self.strategy.get_position_weights()
        
        # 计算交易统计
        trade_stats = self._calculate_trade_statistics(trade_history)
        
        return {
            'performance_metrics': performance_metrics,
            'risk_metrics': risk_metrics,
            'position_weights': position_weights,
            'trade_statistics': trade_stats,
            'trade_history': trade_history
        }
    
    def _calculate_trade_statistics(self, trade_history: pd.DataFrame) -> Dict[str, Any]:
        """计算交易统计
        
        Args:
            trade_history: 交易历史
            
        Returns:
            交易统计字典
        """
        # 计算交易次数
        total_trades = len(trade_history[trade_history['action'].isin(['buy', 'sell'])])
        
        # 计算买入和卖出次数
        buy_trades = len(trade_history[trade_history['action'] == 'buy'])
        sell_trades = len(trade_history[trade_history['action'] == 'sell'])
        
        # 计算平均交易成本
        avg_cost = trade_history['cost'].mean()
        
        # 计算总交易成本
        total_cost = trade_history['cost'].sum()
        
        return {
            'total_trades': total_trades,
            'buy_trades': buy_trades,
            'sell_trades': sell_trades,
            'average_cost': avg_cost,
            'total_cost': total_cost
        }
    
    def plot_results(self) -> None:
        """绘制回测结果"""
        import matplotlib.pyplot as plt
        
        # 获取交易历史
        history = self.results['trade_history']
        
        # 创建图形
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
        
        # 绘制组合价值
        portfolio_values = history[history['action'] == 'portfolio_update']['portfolio_value']
        ax1.plot(portfolio_values.index, portfolio_values.values)
        ax1.set_title('Portfolio Value Over Time')
        ax1.set_xlabel('Date')
        ax1.set_ylabel('Value')
        
        # 绘制持仓权重
        weights = pd.DataFrame(self.results['position_weights'], index=[0])
        weights.plot(kind='bar', ax=ax2)
        ax2.set_title('Position Weights')
        ax2.set_xlabel('Symbol')
        ax2.set_ylabel('Weight')
        
        plt.tight_layout()
        plt.show()
    
    def save_results(self, path: str) -> None:
        """保存回测结果
        
        Args:
            path: 保存路径
        """
        import json
        
        # 将结果转换为可序列化的格式
        serializable_results = {
            'performance_metrics': self.results['performance_metrics'],
            'risk_metrics': self.results['risk_metrics'],
            'position_weights': self.results['position_weights'],
            'trade_statistics': self.results['trade_statistics']
        }
        
        # 保存为JSON文件
        with open(path, 'w') as f:
            json.dump(serializable_results, f, indent=4)
        
        # 保存交易历史为CSV文件
        history_path = path.replace('.json', '_history.csv')
        self.results['trade_history'].to_csv(history_path) 