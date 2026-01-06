import csv
import logging
from typing import Union, Dict, Any, List
import os
from datetime import datetime

import numpy as np
import pandas as pd

from backtest.configs.performance_metrics import PerformanceMetrics
from backtest.data.data_manager import DataManager
from backtest.configs.backtest_config import BacktestConfig
from backtest.expression.expression import AlphaExpression
from backtest.metrics.risk import RiskManager
from backtest.metrics.ic_calculator import ICCalculator
from backtest.metrics.factor_return_calculator import FactorReturnCalculator
from backtest.metrics.performance_calc import calculate_portfolio_metrics
from backtest.portfolio.portfolio_constructor import PortfolioConstructor
from backtest.transaction.cost import TransactionCostModel
from backtest.engine.engine import VectorizedEngine


class Backtester:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.data_manager = DataManager(config)
        self.portfolio_constructor = PortfolioConstructor(config)
        self.cost_model = TransactionCostModel(config)
        self.risk_manager = RiskManager(config)
        self.ic_calculator = ICCalculator(
            ic_period=config.ic_calculation_period,
            ic_method=config.ic_method,
        )
        self.factor_return_calculator = FactorReturnCalculator(
            period=config.factor_return_period,
            calculation_frequency=config.factor_return_calculation_frequency
        )
        self.engine = VectorizedEngine()

        self.current_positions = pd.Series(dtype=float)  # 持仓权重
        self.current_shares = pd.Series(dtype=float)  # 实际股票数量
        self.portfolio_values = []
        self.trade_log = []
        self.risk_log = []

        # 详细交易记录
        self.detailed_trading_records = []

        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)

        # 如果启用详细日志，创建日志目录
        if self.config.enable_detailed_log:
            self._setup_detailed_logging()

    def _setup_detailed_logging(self):
        """设置详细交易记录日志"""
        log_dir = os.path.dirname(self.config.detailed_log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 添加时间戳到文件名，避免覆盖
        base_name, ext = os.path.splitext(self.config.detailed_log_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.detailed_log_file = f"{base_name}_{timestamp}{ext}"

        self.logger.info(f"详细交易记录将保存到: {self.detailed_log_file}")

    def _log_detailed_trading_record(self,
                                     date: pd.Timestamp,
                                     portfolio_value: float,
                                     target_weights: pd.Series,
                                     trades: pd.Series,
                                     costs: Dict[str, float],
                                     prices: pd.Series):
        """记录详细的交易信息"""
        if not self.config.enable_detailed_log:
            return

        # 基础调仓信息
        base_record = {
            'date': date.strftime('%Y-%m-%d'),
            'portfolio_value_before': portfolio_value + costs.get('total_cost', 0),  # portfolio_value已经是扣费后的
            'portfolio_value_after': portfolio_value,
            'total_transaction_cost': costs.get('total_cost', 0),
            'commission_cost': costs.get('commission', 0),
            'slippage_cost': costs.get('slippage', 0),
            'trade_value': costs.get('trade_value', 0),  # 交易总价值
            'commission_rate': costs.get('commission_rate', self.config.trade_cost_rate),
            'slippage_rate': costs.get('slippage_rate', self.config.slippage_ratio),
            'num_holdings': len(target_weights[target_weights > 0]) if target_weights is not None else 0,
            'num_trades': len(trades) if not trades.empty else 0,
            'turnover_ratio': trades.abs().sum() / portfolio_value if portfolio_value > 0 and not trades.empty else 0,
            'cost_ratio': costs.get('total_cost', 0) / portfolio_value if portfolio_value > 0 else 0  # 费用率
        }

        # 获取所有涉及的股票（包括新持仓、旧持仓和交易的股票）
        all_stocks = set()
        if target_weights is not None:
            all_stocks.update(target_weights.index)
        if not self.current_positions.empty:
            all_stocks.update(self.current_positions.index)
        if not trades.empty:
            all_stocks.update(trades.index)

        if not all_stocks:
            # 如果没有任何股票，记录现金
            record = base_record.copy()
            record['stock_code'] = 'CASH'
            record['weight'] = 1.0
            record['position_value'] = portfolio_value
            record['weight_rank'] = 1
            record['price'] = 1.0
            record['shares'] = portfolio_value
            record['trade_amount'] = 0
            record['trade_type'] = '无交易'
            record['trade_shares'] = 0
            record['trade_ratio'] = 0
            self.detailed_trading_records.append(record)
            return

        # 为每个涉及的股票创建记录
        for stock_code in all_stocks:
            record = base_record.copy()
            record['stock_code'] = stock_code

            # 目标权重和持仓价值
            target_weight = target_weights.get(stock_code, 0) if target_weights is not None else 0
            record['weight'] = target_weight
            record['position_value'] = target_weight * portfolio_value

            # 权重排名（只对有持仓的股票排名）
            if target_weight > 0 and target_weights is not None:
                sorted_weights = target_weights[target_weights > 0].sort_values(ascending=False)
                record['weight_rank'] = list(sorted_weights.index).index(stock_code) + 1
            else:
                record['weight_rank'] = 0  # 无持仓

            # 价格和股数信息
            if stock_code in prices.index:
                record['price'] = prices.loc[stock_code]
                # 显示调仓后的目标股票数量
                if target_weight > 0 and prices.loc[stock_code] > 0:
                    record['shares'] = (target_weight * portfolio_value) / prices.loc[stock_code]
                else:
                    record['shares'] = 0
            else:
                record['price'] = 0
                record['shares'] = 0

            # 交易信息
            if not trades.empty and stock_code in trades.index:
                trade_amount = trades.loc[stock_code]
                record['trade_amount'] = trade_amount

                # 更精确的交易类型判断
                if trade_amount > 0:
                    record['trade_type'] = '买入'
                elif trade_amount < 0:
                    record['trade_type'] = '卖出'
                else:
                    record['trade_type'] = '无交易'

                # 计算实际的股票数量变化
                current_shares = self.current_shares.get(stock_code, 0) if not self.current_shares.empty else 0
                if target_weight > 0 and stock_code in prices.index and prices.loc[stock_code] > 0:
                    target_shares = (target_weight * portfolio_value) / prices.loc[stock_code]
                else:
                    target_shares = 0

                record['trade_shares'] = abs(target_shares - current_shares)
                record['trade_ratio'] = abs(trade_amount) / costs.get('trade_value', 1) if costs.get('trade_value',
                                                                                                     0) > 0 else 0
            else:
                record['trade_amount'] = 0
                record['trade_type'] = '无交易'
                record['trade_shares'] = 0
                record['trade_ratio'] = 0

            # 添加持仓变化信息
            old_weight = self.current_positions.get(stock_code, 0) if not self.current_positions.empty else 0
            record['old_weight'] = old_weight
            record['old_position_value'] = old_weight * (portfolio_value + costs.get('total_cost', 0))
            record['weight_change'] = target_weight - old_weight

            # 只记录有意义的记录（有持仓、有交易、或权重变化）
            if target_weight > 0 or old_weight > 0 or record['trade_amount'] != 0:
                self.detailed_trading_records.append(record)

    def _save_detailed_trading_log(self):
        """保存详细交易记录到文件"""
        if not self.config.enable_detailed_log or not self.detailed_trading_records:
            return

        try:
            df = pd.DataFrame(self.detailed_trading_records)
            df.to_csv(self.detailed_log_file, index=False, encoding='utf-8-sig')
            self.logger.info(f"详细交易记录已保存到: {self.detailed_log_file}")
            self.logger.info(f"共记录 {len(self.detailed_trading_records)} 次调仓记录")
        except Exception as e:
            self.logger.error(f"保存详细交易记录失败: {str(e)}")

    def _compute_alpha_signals(self, alpha_expression: AlphaExpression) -> pd.DataFrame:
        """计算alpha信号"""
        try:
            # 准备数据字典，包含所有需要的基础数据
            data_dict = {
                'inds': self.data_manager.inds,
                'vwap': self.data_manager.vwap,
                'adj_close': self.data_manager.adj_close,
                'market_cap': self.data_manager.market_cap
            }

            # 使用AlphaExpression计算因子值
            alpha_signals = alpha_expression.calculate_factor(data_dict)

            # 确保信号的格式正确
            if alpha_signals is None or alpha_signals.empty:
                raise ValueError("Alpha表达式计算结果为空")

            # 处理异常值
            alpha_signals = alpha_signals.replace([np.inf, -np.inf], np.nan)
            alpha_signals = alpha_signals.fillna(0)

            self.logger.info(f"Alpha signals computed for {alpha_expression.name}")
            return alpha_signals

        except Exception as e:
            self.logger.error(f"计算Alpha信号失败: {str(e)}")
            # 返回空的DataFrame，避免程序崩溃
            return pd.DataFrame(
                0,
                index=self.data_manager.trading_dates
            )

    def _get_rebalance_dates(self) -> List[pd.Timestamp]:
        """根据调仓频率获取调仓日期"""
        all_dates = pd.DatetimeIndex(self.data_manager.trading_dates)

        # 解析调仓频率
        freq = self.config.rebalance_frequency

        self.logger.info(f"调仓频率: {freq}, 总交易日数: {len(all_dates)}")
        if len(all_dates) > 0:
            self.logger.info(
                f"交易日期范围: {all_dates[0].strftime('%Y-%m-%d')} 到 {all_dates[-1].strftime('%Y-%m-%d')}")

        if freq == "1D":
            # 每日调仓
            return list(all_dates)
        elif freq == "5D":
            # 每5个交易日调仓（每周）
            rebalance_dates = []
            for i in range(0, len(all_dates), 5):
                rebalance_dates.append(all_dates[i])
            return rebalance_dates
        elif freq == "10D":
            # 每10个交易日调仓（双周）
            rebalance_dates = []
            for i in range(0, len(all_dates), 10):
                rebalance_dates.append(all_dates[i])
            return rebalance_dates
        elif freq == "1M":
            # 每月调仓
            return list(all_dates.groupby(pd.Grouper(freq='BM')).first().dropna())
        elif freq == "1Q":
            # 每季度调仓
            return list(all_dates.groupby(pd.Grouper(freq='BQ')).first().dropna())
        elif freq.endswith("D"):
            # 自定义天数调仓
            try:
                n_days = int(freq[:-1])
                rebalance_dates = []
                for i in range(0, len(all_dates), n_days):
                    rebalance_dates.append(all_dates[i])

                # 添加调仓日期的详细日志
                self.logger.info(f"每{n_days}个交易日调仓，共生成{len(rebalance_dates)}个调仓日期:")
                for idx, date in enumerate(rebalance_dates[:5]):  # 只显示前5个
                    trading_day_num = all_dates.get_loc(date) + 1
                    self.logger.info(f"  第{idx + 1}次调仓: {date.strftime('%Y-%m-%d')} (第{trading_day_num}个交易日)")
                if len(rebalance_dates) > 5:
                    self.logger.info(f"  ... 还有{len(rebalance_dates) - 5}个调仓日期")

                return rebalance_dates
            except ValueError:
                self.logger.warning(f"无法解析调仓频率: {freq}，使用默认5天")
                return self._get_default_rebalance_dates(all_dates)
        else:
            # 默认每5个交易日调仓
            self.logger.warning(f"未识别的调仓频率: {freq}，使用默认5天")
            return self._get_default_rebalance_dates(all_dates)

    def _get_default_rebalance_dates(self, all_dates) -> List[pd.Timestamp]:
        """默认调仓日期（每5个交易日）"""
        rebalance_dates = []
        for i in range(0, len(all_dates), 5):
            rebalance_dates.append(all_dates[i])
        return rebalance_dates

    def _calculate_trades(self, target_weights: pd.Series, portfolio_value: float, prices: pd.Series) -> pd.Series:
        """计算交易金额（基于股票数量跟踪）

        Args:
            target_weights: 目标权重
            portfolio_value: 当前组合价值
            prices: 当前股票价格

        Returns:
            交易金额Series (正数为买入，负数为卖出)
        """
        if len(self.current_shares) == 0:
            # 如果没有当前持仓，所有目标权重都是买入
            return target_weights * portfolio_value

        # 获取所有相关股票（包括当前持仓和目标持仓）
        all_stocks = set()
        all_stocks.update(target_weights.index)
        all_stocks.update(self.current_shares.index)

        # 创建所有股票的目标持仓和当前持仓金额
        target_positions = pd.Series(0.0, index=list(all_stocks))
        current_positions = pd.Series(0.0, index=list(all_stocks))

        # 填入目标权重对应的金额
        for stock in target_weights.index:
            target_positions[stock] = target_weights[stock] * portfolio_value

        # 基于实际股票数量计算当前持仓金额
        for stock in self.current_shares.index:
            if stock in prices.index and prices.loc[stock] > 0:
                current_positions[stock] = self.current_shares[stock] * prices.loc[stock]
            else:
                # 如果价格缺失，使用权重方法估算（备用方案）
                current_positions[stock] = self.current_positions.get(stock, 0) * portfolio_value

        # 计算交易金额
        trades = target_positions - current_positions

        # 只返回非零交易
        return trades[abs(trades) > 1e-6]  # 使用更小的阈值避免舍入误差

    def _update_share_positions(self, target_weights: pd.Series, portfolio_value: float, prices: pd.Series):
        """更新实际股票数量

        Args:
            target_weights: 目标权重
            portfolio_value: 当前组合价值（扣费后）
            prices: 当前股票价格
        """
        new_shares = pd.Series(dtype=float)

        for stock in target_weights.index:
            if target_weights[stock] > 0 and stock in prices.index and prices.loc[stock] > 0:
                # 计算目标持仓金额
                target_value = target_weights[stock] * portfolio_value
                # 计算应持有的股票数量
                shares = target_value / prices.loc[stock]
                new_shares[stock] = shares

        self.current_shares = new_shares

    def _backtest_loop(self, alpha_signals: pd.DataFrame):
        portfolio_value = self.config.initial_capital

        # 将trading_dates转换为DatetimeIndex以便于操作
        trading_dates = pd.DatetimeIndex(self.data_manager.trading_dates)

        # 使用Series来记录每日组合价值
        portfolio_history = pd.Series(index=trading_dates, dtype=float)
        portfolio_history.iloc[0] = portfolio_value

        self.current_positions = pd.Series(dtype=float)
        self.current_shares = pd.Series(dtype=float)
        rebalance_dates = self._get_rebalance_dates()

        # 检查首交易日因子值是否全部为空，如果是则将所有调仓日期推迟
        first_date = trading_dates[0]
        if first_date in alpha_signals.index:
            first_day_signals = alpha_signals.loc[first_date]
            if first_day_signals.isna().all() or (first_day_signals == 0).all():
                self.logger.info(
                    f"首交易日 {first_date.strftime('%Y-%m-%d')} 因子值全部为空，将所有调仓日期推迟1个交易日")

                # 将所有调仓日期向后推迟1个交易日
                delayed_rebalance_dates = []
                for rebalance_date in rebalance_dates:
                    # 找到当前调仓日期在交易日列表中的位置
                    if rebalance_date in trading_dates:
                        current_idx = trading_dates.get_loc(rebalance_date)
                        # 推迟到下一个交易日
                        if current_idx + 1 < len(trading_dates):
                            delayed_rebalance_dates.append(trading_dates[current_idx + 1])
                        # 如果是最后一个交易日，则保持不变
                        else:
                            delayed_rebalance_dates.append(rebalance_date)

                rebalance_dates = delayed_rebalance_dates
                self.logger.info(
                    f"调仓日期已推迟，新的首次调仓日期: {rebalance_dates[0].strftime('%Y-%m-%d') if rebalance_dates else '无'}")

        # 关键修复：确保第一天也进行调仓检查
        for i, date in enumerate(trading_dates):
            # 从第二天开始计算当日收益（基于前一天的持仓）
            if i > 0 and not self.current_positions.empty:
                daily_returns = self.data_manager.return_data.loc[date]
                # 计算加权收益
                weighted_returns = (daily_returns * self.current_positions.reindex(daily_returns.index).fillna(0)).sum()
                portfolio_value *= (1 + weighted_returns)

            # 检查是否为调仓日（包括第一天）
            if date in rebalance_dates:
                current_signals = alpha_signals.loc[date]
                current_mcap = self.data_manager.market_cap.loc[date]

                target_weights = self.portfolio_constructor.construct_portfolio(
                    alpha_scores=current_signals,
                    market_cap=current_mcap
                )

                # === ① 获取价格并计算 trades & 成本 =============================
                prices = self.data_manager.vwap.loc[date]
                trades = self._calculate_trades(target_weights, portfolio_value, prices)
                costs = self.cost_model.calculate_costs(trades, prices, market_cap=current_mcap)

                # === ② 扣掉费用，得到净资产 =======================================
                original_portfolio_value = portfolio_value  # 保存扣费前的值
                portfolio_value -= costs['total_cost']

                # === ③ 更新portfolio_history以反映交易费用的影响 ==================
                # 重要：调仓日的最终组合价值应该是扣费后的值
                portfolio_history.loc[date] = portfolio_value

                # === ④ 更新持仓权重和股票数量 ===================================
                if portfolio_value > 0:
                    self.current_positions = target_weights
                    # 重要：同时更新实际股票数量
                    self._update_share_positions(target_weights, portfolio_value, prices)
                else:
                    self.current_positions = pd.Series(dtype=float)
                    self.current_shares = pd.Series(dtype=float)

                last_rebalance_date = date

                # 记录日志
                self.trade_log.append({
                    'date': date,
                    'portfolio_value': portfolio_value,
                    'num_positions': len(self.current_positions) if not self.current_positions.empty else 0,
                    'turnover': trades.abs().sum() / original_portfolio_value if original_portfolio_value > 0 and not trades.empty else 0,
                    'transaction_costs': costs['total_cost']
                })

                risk_metrics = self.risk_manager.check_portfolio_risk(
                    self.current_positions,
                    self.data_manager.return_data.loc[:date],
                    current_mcap
                )
                self.risk_log.append({
                    'date': date,
                    **risk_metrics.get('concentration', {}),
                    **risk_metrics.get('volatility', {})
                })

                # 记录详细交易记录
                if self.config.enable_detailed_log:
                    self._log_detailed_trading_record(
                        date,
                        portfolio_value,
                        self.current_positions,  # 使用目标权重
                        trades,
                        costs,
                        prices
                    )

            # 记录当日最终组合价值
            portfolio_history.loc[date] = portfolio_value

        final_value = portfolio_history.iloc[-1]
        total_return = (final_value / self.config.initial_capital) - 1

        # 保存详细交易记录
        self._save_detailed_trading_log()

        return {
            'portfolio_history': portfolio_history.dropna(),
            'final_value': final_value,
            'total_return': total_return
        }

    def _calculate_performance(self, results: Dict[str, Any], trading_dates: pd.DatetimeIndex,
                               alpha_signals: pd.DataFrame = None) -> tuple:
        """
        计算性能指标
        
        Returns:
            tuple: (PerformanceMetrics, ic_analysis, factor_return_analysis)
            - PerformanceMetrics: 绩效指标对象
            - ic_analysis: IC分析完整结果（含 ic_series）
            - factor_return_analysis: 因子收益分析完整结果（含 factor_return_series）
        """
        # 初始化空的 analysis 字典
        ic_analysis = {
            'ic_series': pd.Series(dtype=float),
            'ic_metrics': {'mean_ic': 0.0, 'ic_std': 0.0, 'ic_ir': 0.0, 'ic_hit_rate': 0.0},
            'ic_decay': pd.DataFrame(),
            'monthly_ic': pd.Series(dtype=float),
            'yearly_ic': pd.Series(dtype=float),
            'config': {}
        }
        factor_return_analysis = {
            'factor_return_series': pd.Series(dtype=float),
            'factor_return_total': 0.0,
            'factor_return_mean': 0.0,
            'factor_return_std': 0.0,
            'factor_return_t_stat': 0.0
        }

        if 'portfolio_history' not in results or results['portfolio_history'].empty:
            return PerformanceMetrics(), ic_analysis, factor_return_analysis

        portfolio_values = results['portfolio_history']

        # ========== 使用公共模块计算 portfolio 类指标 ==========
        portfolio_metrics = calculate_portfolio_metrics(portfolio_values)

        # ========== IC 指标计算 ==========
        ic_metrics = {'mean_ic': 0.0, 'ic_std': 0.0, 'ic_ir': 0.0, 'ic_hit_rate': 0.0}
        if alpha_signals is not None and not alpha_signals.empty:
            try:
                price_data = self.data_manager.adj_close
                if price_data is not None and not price_data.empty:
                    ic_analysis = self.ic_calculator.analyze_ic_performance(alpha_signals, price_data)
                    ic_metrics = ic_analysis['ic_metrics']
                    self.logger.info(f"IC计算完成: IC均值={ic_metrics['mean_ic']:.4f}, IR={ic_metrics['ic_ir']:.4f}")
            except Exception as e:
                self.logger.warning(f"IC计算失败: {str(e)}")

        # ========== 因子收益率计算 ==========
        if alpha_signals is not None and not alpha_signals.empty:
            try:
                price_data = self.data_manager.adj_close
                if price_data is not None and not price_data.empty:
                    factor_return_analysis = self.factor_return_calculator.calculate_factor_returns(
                        alpha_signals, price_data
                    )
                    self.logger.info(
                        f"因子收益率计算完成 (收益周期={self.config.factor_return_period}天, "
                        f"计算频率={self.config.factor_return_calculation_frequency}天): "
                        f"总收益={factor_return_analysis.get('factor_return_total', 0):.6f}, "
                        f"均值={factor_return_analysis.get('factor_return_mean', 0):.6f}, "
                        f"T值={factor_return_analysis.get('factor_return_t_stat', 0):.4f}"
                    )
            except Exception as e:
                self.logger.warning(f"因子收益率计算失败: {str(e)}")

        # ========== 换手率统计计算 ==========
        turnover_metrics = self._calculate_turnover_metrics(trading_dates)

        performance = PerformanceMetrics(
            total_return=portfolio_metrics.total_return,
            annual_return=portfolio_metrics.annual_return,
            volatility=portfolio_metrics.volatility,
            sharpe_ratio=portfolio_metrics.sharpe_ratio,
            max_drawdown=portfolio_metrics.max_drawdown,
            calmar_ratio=portfolio_metrics.calmar_ratio,
            hit_rate=portfolio_metrics.hit_rate,
            profit_loss_ratio=portfolio_metrics.profit_loss_ratio,
            var_95=portfolio_metrics.var_95,
            cvar_95=portfolio_metrics.cvar_95,
            # IC相关指标
            mean_ic=ic_metrics['mean_ic'],
            ic_std=ic_metrics['ic_std'],
            ic_ir=ic_metrics['ic_ir'],
            ic_hit_rate=ic_metrics['ic_hit_rate'],
            # 因子收益率相关指标
            factor_return_total=factor_return_analysis.get('factor_return_total', 0.0),
            factor_return_mean=factor_return_analysis.get('factor_return_mean', 0.0),
            factor_return_std=factor_return_analysis.get('factor_return_std', 0.0),
            factor_return_t_stat=factor_return_analysis.get('factor_return_t_stat', 0.0),
            # 换手率相关指标
            turnover_mean=turnover_metrics['turnover_mean'],
            turnover_std=turnover_metrics['turnover_std'],
            turnover_total=turnover_metrics['turnover_total']
        )

        return performance, ic_analysis, factor_return_analysis

    def _calculate_turnover_metrics(self, trading_dates: pd.DatetimeIndex) -> Dict[str, float]:
        """计算换手率相关统计指标"""

        if not self.trade_log:
            return {
                'turnover_mean': 0.0,
                'turnover_std': 0.0,
                'turnover_total': 0.0
            }

        # 从 trade_log 中提取换手率数据
        turnover_data = [log_entry['turnover'] for log_entry in self.trade_log if 'turnover' in log_entry]

        if not turnover_data:
            return {
                'turnover_mean': 0.0,
                'turnover_std': 0.0,
                'turnover_total': 0.0
            }

        turnover_series = pd.Series(turnover_data)

        # 计算平均换手率和标准差
        turnover_mean = turnover_series.mean()
        turnover_std = turnover_series.std()
        turnover_total = turnover_series.sum()

        return {
            'turnover_mean': turnover_mean,
            'turnover_std': turnover_std,
            'turnover_total': turnover_total
        }

    def run_backtest(self,
                     alpha_expression: AlphaExpression,
                     data_source: Union[str, pd.DataFrame] = None
                     ) -> Dict[str, Any]:
        """
        运行回测并返回结果
        
        Returns:
            Dict 包含：
            - alpha: AlphaExpression
            - config: BacktestConfig
            - performance: PerformanceMetrics
            - portfolio_history: pd.Series (组合市值序列)
            - trade_log: pd.DataFrame
            - risk_log: pd.DataFrame
            - ic_analysis: dict (含 ic_series, ic_metrics 等，用于年度切片)
            - factor_return_analysis: dict (含 factor_return_series 等，用于年度切片)
        """
        self.logger.info(f"Starting backtest for {alpha_expression.name}")

        self.data_manager.load_data(data_source)
        alpha_signals = self._compute_alpha_signals(alpha_expression)

        if alpha_signals.empty:
            self.logger.warning("Alpha signals are empty. Skipping backtest.")
            return {
                'alpha': alpha_expression,
                'config': self.config,
                'performance': PerformanceMetrics(),
                'portfolio_history': pd.Series(dtype=float),
                'trade_log': pd.DataFrame(),
                'risk_log': pd.DataFrame(),
                'ic_analysis': {
                    'ic_series': pd.Series(dtype=float),
                    'ic_metrics': {'mean_ic': 0.0, 'ic_std': 0.0, 'ic_ir': 0.0, 'ic_hit_rate': 0.0},
                    'ic_decay': pd.DataFrame(),
                    'monthly_ic': pd.Series(dtype=float),
                    'yearly_ic': pd.Series(dtype=float),
                    'config': {}
                },
                'factor_return_analysis': {
                    'factor_return_series': pd.Series(dtype=float),
                    'factor_return_total': 0.0,
                    'factor_return_mean': 0.0,
                    'factor_return_std': 0.0,
                    'factor_return_t_stat': 0.0
                }
            }

        # 运行回测循环
        backtest_results = self._backtest_loop(alpha_signals)

        # 获取回测期间的交易日
        start_date = self.data_manager.trading_dates[0]
        end_date = self.data_manager.trading_dates[-1]
        trading_dates_in_range = pd.DatetimeIndex(self.data_manager.trading_dates)

        # 计算性能指标（返回三元组）
        performance, ic_analysis, factor_return_analysis = self._calculate_performance(
            backtest_results, trading_dates_in_range, alpha_signals
        )

        self.logger.info(f"Backtest completed. Sharpe Ratio: {performance.sharpe_ratio:.3f}")

        return {
            'alpha': alpha_expression,
            'config': self.config,
            'performance': performance,
            'portfolio_history': backtest_results.get('portfolio_history'),
            'trade_log': pd.DataFrame(self.trade_log),
            'risk_log': pd.DataFrame(self.risk_log),
            # 新增：支持年度切片聚合（不重跑）
            'ic_analysis': ic_analysis,
            'factor_return_analysis': factor_return_analysis
        }

