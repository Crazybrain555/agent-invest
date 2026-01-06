import logging
import pandas as pd
from typing import Optional, Dict, Any

from backtest.configs.backtest_config import BacktestConfig
from backtest.expression.expression import AlphaExpression
from backtest.backtester.backtester import Backtester



class BacktestRunner:
    """回测运行器 - 提供高级接口

    支持单个和批量策略回测，可选择启用详细交易记录功能。
    详细交易记录包括：
    - 每次调仓的组合价值变化
    - 交易费用明细（佣金、滑点等）
    - 持仓数量和权重详情
    - 前10大持仓信息
    - 最大买入/卖出交易详情
    """

    def __init__(self, config: Optional[BacktestConfig] = None):
        """初始化回测运行器"""
        self.config = config or self._get_default_config()
        self.results = {}
        self.setup_logging()

    def setup_logging(self):
        """设置日志"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def _get_default_config(self) -> BacktestConfig:
        """获取默认配置"""
        return BacktestConfig(
            start_date="20200101",
            end_date="20231231",
            rebalance_frequency="5D",
            initial_capital=1000000.0,
            max_position_size=0.05,
            trade_cost_rate=0.001
        )

    def run_single_backtest(self,
                            alpha_expression: AlphaExpression,
                            data_source: Optional[str] = None,
                            plot_results: bool = False,
                            enable_detailed_log: bool = False,
                            detailed_log_path: Optional[str] = None) -> Dict[str, Any]:
        """运行单个策略回测

        Args:
            alpha_expression: Alpha表达式/策略
            data_source: 数据源路径或DataFrame
            plot_results: 是否绘制结果图表
            enable_detailed_log: 是否启用详细交易记录
            detailed_log_path: 详细日志文件路径（可选，如不指定则自动生成）

        Returns:
            回测结果字典，包含性能指标、组合历史等
        """

        self.logger.info(f"开始回测策略: {alpha_expression.name}")

        # 如果需要启用详细日志，更新配置
        if enable_detailed_log:
            self.config.enable_detailed_log = True
            if detailed_log_path:
                self.config.detailed_log_path = detailed_log_path
            else:
                # 使用策略名称创建默认文件名
                safe_name = "".join(c for c in alpha_expression.name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                safe_name = safe_name.replace(' ', '_')
                self.config.detailed_log_path = f"logs/detailed_trading_log_{safe_name}.csv"

            self.logger.info(f"已启用详细交易记录，文件路径: {self.config.detailed_log_path}")

        # 创建回测器
        backtester = Backtester(self.config)

        # 运行回测
        results = backtester.run_backtest(alpha_expression, data_source)

        # 保存结果
        self.results[alpha_expression.name] = results

        self.logger.info(f"策略 {alpha_expression.name} 回测完成")
        # self._print_performance_summary(results)

        if plot_results:
            self._plot_performance_curve(results)

        return results

    def run_multiple_backtests(self,
                               alpha_expressions: list,
                               data_source: Optional[str] = None,
                               enable_detailed_log: bool = False,
                               detailed_log_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """运行多个策略回测

        Args:
            alpha_expressions: Alpha表达式/策略列表
            data_source: 数据源路径或DataFrame
            enable_detailed_log: 是否启用详细交易记录
            detailed_log_dir: 详细日志文件目录（可选，每个策略生成独立文件）

        Returns:
            所有策略的回测结果字典
        """

        all_results = {}

        for alpha_expr in alpha_expressions:
            try:
                # 为每个策略设置不同的日志文件
                log_path = None
                if enable_detailed_log:
                    if detailed_log_dir:
                        safe_name = "".join(c for c in alpha_expr.name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                        safe_name = safe_name.replace(' ', '_')
                        log_path = f"{detailed_log_dir}/detailed_trading_log_{safe_name}.csv"

                result = self.run_single_backtest(
                    alpha_expr,
                    data_source,
                    enable_detailed_log=enable_detailed_log,
                    detailed_log_path=log_path
                )
                all_results[alpha_expr.name] = result
            except Exception as e:
                self.logger.error(f"策略 {alpha_expr.name} 回测失败: {str(e)}")
                continue

        self._compare_strategies(all_results)
        return all_results


    def _print_performance_summary(self, results: Dict[str, Any]):
        """打印性能摘要"""
        perf = results['performance']

        print(f"\n=== 策略回测结果 ===")
        print(f"策略名称: {results['alpha'].name}")
        print(f"总收益率: {perf.total_return:.2%}")
        print(f"年化收益率: {perf.annual_return:.2%}")
        print(f"年化波动率: {perf.volatility:.2%}")
        print(f"夏普比率: {perf.sharpe_ratio:.3f}")
        print(f"最大回撤: {perf.max_drawdown:.2%}")
        print(f"Calmar比率: {perf.calmar_ratio:.3f}")
        print(f"胜率: {perf.hit_rate:.2%}")
        print(f"盈亏比: {perf.profit_loss_ratio:.3f}")
        print(f"VaR(95%): {perf.var_95:.2%}")
        print(f"CVaR(95%): {perf.cvar_95:.2%}")
        print(f"\n=== IC指标分析 ===")
        print(f"IC均值: {perf.mean_ic:.4f}")
        print(f"IC标准差: {perf.ic_std:.4f}")
        print(f"IC信息比率(IR): {perf.ic_ir:.4f}")
        print(f"IC胜率: {perf.ic_hit_rate:.2%}")
        print(
            f"\n=== 因子收益率分析 (周期: {results['config'].factor_return_period}天, 频率: {results['config'].factor_return_calculation_frequency}天) ===")
        print(f"因子收益总和: {perf.factor_return_total:.6f}")
        print(f"因子收益均值: {perf.factor_return_mean:.6f}")
        print(f"因子收益T值: {perf.factor_return_t_stat:.4f}")
        print("=" * 30)

    def _plot_performance_curve(self, results: Dict[str, Any]):
        """绘制策略收益曲线"""
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import FuncFormatter
        except ImportError:
            self.logger.warning("Matplotlib not installed. Skipping plot. Please install with 'pip install matplotlib'")
            return

        if 'portfolio_history' not in results:
            self.logger.warning("No portfolio history found. Skipping plot.")
            return

        portfolio_history = results['portfolio_history']
        strategy_name = results['alpha'].name

        plt.style.use('seaborn-v0_8-darkgrid')
        fig, ax = plt.subplots(figsize=(15, 8))

        ax.plot(portfolio_history.index, portfolio_history.values, label=strategy_name)

        # Format Y-axis as currency
        def currency_formatter(x, pos):
            if x >= 1e6:
                return f'¥{x / 1e6:,.1f}M'
            elif x >= 1e3:
                return f'¥{x / 1e3:,.1f}K'
            return f'¥{x:,.0f}'

        ax.yaxis.set_major_formatter(FuncFormatter(currency_formatter))

        ax.set_title(f'Strategy Performance', fontsize=16)
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('Portfolio Value', fontsize=12)
        ax.grid(True)

        fig.autofmt_xdate()
        plt.show()

    def _compare_strategies(self, all_results: Dict[str, Dict[str, Any]]):
        """比较多个策略"""
        if len(all_results) < 2:
            return

        print(f"\n=== 策略对比 ===")
        comparison_data = []

        for strategy_name, result in all_results.items():
            perf = result['performance']
            comparison_data.append({
                '策略名称': strategy_name,
                '总收益率': f"{perf.total_return:.2%}",
                '年化收益率': f"{perf.annual_return:.2%}",
                '夏普比率': f"{perf.sharpe_ratio:.3f}",
                '最大回撤': f"{perf.max_drawdown:.2%}",
                'Calmar比率': f"{perf.calmar_ratio:.3f}",
                'IC均值': f"{perf.mean_ic:.4f}",
                'IC-IR': f"{perf.ic_ir:.4f}",
                'IC胜率': f"{perf.ic_hit_rate:.2%}",
                '因子收益均值': f"{perf.factor_return_mean:.6f}",
                '因子收益T值': f"{perf.factor_return_t_stat:.4f}"
            })

        comparison_df = pd.DataFrame(comparison_data)
        print(comparison_df.to_string(index=False))
        print("=" * 100)
