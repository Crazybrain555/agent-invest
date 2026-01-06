import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union, Any
import os
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, project_root)

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.correlation_utils import CorrelationCalculator
from src.utils.logger import setup_logger
from src.utils.config_loader import ConfigLoader

logger = setup_logger(__name__)

class FactorCorrelationAnalyzer:
    """
    因子相关性分析器
    
    用于分析因子库中各个因子之间的相关性，支持静态和滚动相关性分析。
    """
    
    def __init__(self, 
                 table_name: str = "ai_is.inter_train_factors_mkt_norm_academic_dcount1",
                 use_gpu: bool = False,
                 device: str = 'cuda'):
        """
        初始化因子相关性分析器
        
        Args:
            table_name: 因子数据表名
            use_gpu: 是否使用GPU加速计算
            device: 计算设备 ('cuda' 或 'cpu')
        """
        self.table_name = table_name
        self.use_gpu = use_gpu
        self.device = device
        
        # 初始化数据提供者
        self.data_provider = LocalTestDBDataProvider()
        
        # 初始化配置加载器
        self.config_loader = ConfigLoader()
        
        # 存储数据
        self.raw_data = None
        self.factor_data = None  # 宽表格式的因子数据
        self.correlation_results = {}
        
        logger.info(f"Initialized FactorCorrelationAnalyzer for table: {table_name}")
    
    def load_factor_data(self,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None,
                        stock_codes: Optional[List[str]] = None,
                        factor_names: Optional[List[str]] = None,
                        lag: int = 0) -> pd.DataFrame:
        """
        从数据库加载因子数据
        
        Args:
            start_date: 开始日期 (YYYYMMDD 格式)
            end_date: 结束日期 (YYYYMMDD 格式)
            stock_codes: 股票代码列表
            factor_names: 因子名称列表
            lag: 滞后期
            
        Returns:
            pd.DataFrame: 长表格式的原始数据
        """
        try:
            logger.info("Loading factor data from database...")
            
            # 从数据库加载数据
            self.raw_data = self.data_provider.fetch_data(
                table=self.table_name,
                start_date=start_date,
                end_date=end_date,
                stock_codes=stock_codes,
                format='long'  # 使用长表格式
            )
            
            if self.raw_data.empty:
                raise ValueError("No data loaded from database")
            
            # 过滤滞后期
            if 'lag' in self.raw_data.columns:
                self.raw_data = self.raw_data[self.raw_data['lag'] == lag]
            
            # 过滤因子名称
            if factor_names:
                if 'factor_name' in self.raw_data.columns:
                    self.raw_data = self.raw_data[self.raw_data['factor_name'].isin(factor_names)]
                elif 'field_name' in self.raw_data.columns:
                    self.raw_data = self.raw_data[self.raw_data['field_name'].isin(factor_names)]
            
            logger.info(f"Loaded {len(self.raw_data)} records")
            logger.info(f"Date range: {self.raw_data['trade_date'].min()} to {self.raw_data['trade_date'].max()}")
            
            return self.raw_data
            
        except Exception as e:
            logger.error(f"Error loading factor data: {str(e)}")
            raise
    
    def prepare_factor_matrix(self) -> pd.DataFrame:
        """
        将长表格式的因子数据转换为宽表格式（因子矩阵）
        
        Returns:
            pd.DataFrame: 宽表格式的因子数据 (index=date*stock_code, columns=factor_names)
        """
        if self.raw_data is None or self.raw_data.empty:
            raise ValueError("No raw data available. Please load data first.")
        
        try:
            logger.info("Converting long format to wide format (factor matrix)...")
            
            # 确定字段名
            factor_name_col = 'factor_name' if 'factor_name' in self.raw_data.columns else 'field_name'
            factor_value_col = 'factor_value' if 'factor_value' in self.raw_data.columns else 'value'

            data_copy = self.raw_data.copy()

            if 'z_windows' in data_copy.columns:
                data_copy['combined_factor'] = data_copy[factor_name_col].astype(str) + '_zwin' + data_copy['z_windows'].astype(str)
                pivot_column = 'combined_factor'
                logger.info("Using combined factor identifier")
            else:
                pivot_column = factor_name_col
                logger.info("Using fieldname identifier")

            self.factor_data = data_copy.pivot_table(
                index=['trade_date', 'stock_code'],
                columns=pivot_column,
                values=factor_value_col,
                aggfunc='first'
            )

            # 重置索引，但保持MultiIndex用于后续分析
            self.factor_data = self.factor_data.reset_index()
            
            # 记录信息
            n_dates = self.factor_data['trade_date'].nunique()
            n_stocks = self.factor_data['stock_code'].nunique()
            n_factors = len([col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']])
            
            logger.info(f"Factor matrix created: {n_dates} dates, {n_stocks} stocks, {n_factors} factors")
            logger.info(f"Factor names: {[col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']]}")
            
            return self.factor_data
            
        except Exception as e:
            logger.error(f"Error preparing factor matrix: {str(e)}")
            raise
    
    def calculate_factor_correlation(self,
                                   correlation_type: str = "pearson",
                                   min_periods: int = 30,
                                   method: str = "cross_sectional") -> pd.DataFrame:
        """
        计算因子间的相关性
        
        Args:
            correlation_type: 相关性类型 ("pearson" 或 "spearman")
            min_periods: 最小观测数
            method: 计算方法 ("cross_sectional": 截面相关性, "time_series": 时间序列相关性)
            
        Returns:
            pd.DataFrame: 因子相关性矩阵
        """
        if self.factor_data is None or self.factor_data.empty:
            raise ValueError("No factor matrix available. Please prepare factor matrix first.")
        
        try:
            logger.info(f"Calculating {method} {correlation_type} correlation...")
            
            # 获取因子列（排除日期和股票代码）
            factor_cols = [col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']]
            
            if method == "cross_sectional":
                # 截面相关性：每个日期计算因子间相关性，然后平均
                correlations_by_date = []
                
                for date in self.factor_data['trade_date'].unique():
                    daily_data = self.factor_data[self.factor_data['trade_date'] == date][factor_cols]
                    
                    # 计算当天的相关性矩阵
                    if correlation_type == "pearson":
                        daily_corr = daily_data.corr(method='pearson', min_periods=min_periods)
                    else:
                        daily_corr = daily_data.corr(method='spearman', min_periods=min_periods)
                    
                    correlations_by_date.append(daily_corr)
                
                # 计算平均相关性矩阵
                correlation_matrix = pd.concat(correlations_by_date).groupby(level=0).mean()
                
            else:  # time_series
                # 时间序列相关性：每只股票的因子时间序列之间的相关性，然后平均
                correlations_by_stock = []
                
                for stock in self.factor_data['stock_code'].unique():
                    stock_data = self.factor_data[self.factor_data['stock_code'] == stock][factor_cols]
                    
                    # 计算该股票的时间序列相关性矩阵
                    if correlation_type == "pearson":
                        stock_corr = stock_data.corr(method='pearson', min_periods=min_periods)
                    else:
                        stock_corr = stock_data.corr(method='spearman', min_periods=min_periods)
                    
                    correlations_by_stock.append(stock_corr)
                
                # 计算平均相关性矩阵
                correlation_matrix = pd.concat(correlations_by_stock).groupby(level=0).mean()
            
            # 存储结果
            result_key = f"{method}_{correlation_type}"
            self.correlation_results[result_key] = correlation_matrix
            
            logger.info(f"Correlation calculation completed. Matrix shape: {correlation_matrix.shape}")
            
            return correlation_matrix
            
        except Exception as e:
            logger.error(f"Error calculating factor correlation: {str(e)}")
            raise
    
    def calculate_rolling_correlation(self,
                                    window: int = 60,
                                    correlation_type: str = "pearson",
                                    min_periods: int = 30,
                                    target_dates: Optional[List[str]] = None) -> Dict[pd.Timestamp, pd.DataFrame]:
        """
        计算滚动因子相关性
        
        Args:
            window: 滚动窗口大小（天数）
            correlation_type: 相关性类型
            min_periods: 最小观测数
            target_dates: 目标日期列表
            
        Returns:
            Dict: 日期到相关性矩阵的映射
        """
        if self.factor_data is None or self.factor_data.empty:
            raise ValueError("No factor matrix available. Please prepare factor matrix first.")
        
        try:
            logger.info(f"Calculating rolling correlation with window={window}...")
            
            # 获取因子列
            factor_cols = [col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']]
            
            # 对每个日期计算截面相关性的滚动平均
            unique_dates = sorted(self.factor_data['trade_date'].unique())
            rolling_correlations = {}
            
            """# 转换目标日期
            if target_dates:
                target_dates = [pd.to_datetime(date) for date in target_dates]
            
            for i, current_date in enumerate(unique_dates):
                if target_dates and current_date not in target_dates:
                    continue"""

            target_dates_converted = None
            if target_dates:
                target_dates_converted = [pd.to_datetime(date) for date in target_dates]

            for i, current_date in enumerate(unique_dates):
                if target_dates_converted and current_date not in target_dates_converted:
                    continue
                
                # 获取窗口内的日期
                start_idx = max(0, i - window + 1)
                window_dates = unique_dates[start_idx:i + 1]
                
                if len(window_dates) < min_periods:
                    continue
                
                # 获取窗口内的数据
                window_data = self.factor_data[self.factor_data['trade_date'].isin(window_dates)]
                
                # 计算窗口内每个日期的截面相关性，然后平均
                daily_correlations = []
                for date in window_dates:
                    daily_data = window_data[window_data['trade_date'] == date][factor_cols]
                    
                    if len(daily_data) >= min_periods:
                        if correlation_type == "pearson":
                            daily_corr = daily_data.corr(method='pearson', min_periods=min_periods)
                        else:
                            daily_corr = daily_data.corr(method='spearman', min_periods=min_periods)
                        
                        daily_correlations.append(daily_corr)
                
                if daily_correlations:
                    # 计算平均相关性矩阵
                    avg_correlation = pd.concat(daily_correlations).groupby(level=0).mean()
                    rolling_correlations[current_date] = avg_correlation
            
            # 存储结果
            result_key = f"rolling_{window}_{correlation_type}"
            self.correlation_results[result_key] = rolling_correlations
            
            logger.info(f"Rolling correlation calculated for {len(rolling_correlations)} dates")
            
            return rolling_correlations
            
        except Exception as e:
            logger.error(f"Error calculating rolling correlation: {str(e)}")
            raise
    
    def analyze_high_correlations(self,
                                correlation_matrix: pd.DataFrame,
                                threshold: float = 0.7,
                                exclude_self: bool = True) -> pd.DataFrame:
        """
        分析高相关性因子对
        
        Args:
            correlation_matrix: 相关性矩阵
            threshold: 相关性阈值
            exclude_self: 是否排除自相关
            
        Returns:
            pd.DataFrame: 高相关性因子对列表
        """
        try:
            logger.info(f"Analyzing high correlations with threshold={threshold}")
            
            high_corr_pairs = []
            
            for i in range(len(correlation_matrix.columns)):
                for j in range(i if not exclude_self else i + 1, len(correlation_matrix.columns)):
                    factor1 = correlation_matrix.columns[i]
                    factor2 = correlation_matrix.columns[j]
                    corr_value = correlation_matrix.iloc[i, j]
                    
                    # if not pd.isna(corr_value) and abs(corr_value) >= threshold:
                    if not pd.isna(corr_value) and isinstance(corr_value, (int, float)) and abs(float(corr_value)) >= threshold:
                        high_corr_pairs.append({
                            'factor1': factor1,
                            'factor2': factor2,
                            # 'correlation': corr_value,
                            # 'abs_correlation': abs(corr_value)
                            'correlation': float(corr_value),
                            'abs_correlation' : abs(float(corr_value))
                        })
            
            high_corr_df = pd.DataFrame(high_corr_pairs)
            
            if not high_corr_df.empty:
                # 按绝对相关性排序
                high_corr_df = high_corr_df.sort_values('abs_correlation', ascending=False)
                logger.info(f"Found {len(high_corr_df)} factor pairs with |correlation| >= {threshold}")
            else:
                logger.info(f"No factor pairs found with |correlation| >= {threshold}")
            
            return high_corr_df
            
        except Exception as e:
            logger.error(f"Error analyzing high correlations: {str(e)}")
            raise
    
    def plot_correlation_heatmap(self,
                               correlation_matrix: pd.DataFrame,
                               title: str = "Factor Correlation Matrix",
                               figsize: Tuple[int, int] = (12, 10),
                               save_path: Optional[str] = None) -> None:
        """
        绘制相关性热力图
        
        Args:
            correlation_matrix: 相关性矩阵
            title: 图表标题
            figsize: 图表大小
            save_path: 保存路径
        """
        try:
            logger.info("Plotting correlation heatmap...")
            
            plt.figure(figsize=figsize)
            
            # 创建热力图
            mask = np.triu(np.ones_like(correlation_matrix, dtype=bool))  # 只显示下三角
            sns.heatmap(
                correlation_matrix,
                mask=mask,
                annot=True,
                cmap='RdBu_r',
                center=0,
                square=True,
                fmt='.2f',
                cbar_kws={"shrink": .8}
            )
            
            plt.title(title, fontsize=16, pad=20)
            plt.xticks(rotation=45, ha='right')
            plt.yticks(rotation=0)
            plt.tight_layout()
            
            if save_path:
                plt.savefig(save_path, dpi=300, bbox_inches='tight')
                logger.info(f"Heatmap saved to: {save_path}")
            
            plt.show()
            
        except Exception as e:
            logger.error(f"Error plotting correlation heatmap: {str(e)}")
            raise
    
    def plot_correlation_distribution(self,
                                    correlation_matrix: pd.DataFrame,
                                    title: str = "Factor Correlation Distribution",
                                    figsize: Tuple[int, int] = (10, 6),
                                    save_path: Optional[str] = None) -> None:
        """
        绘制相关性分布直方图
        
        Args:
            correlation_matrix: 相关性矩阵
            title: 图表标题
            figsize: 图表大小
            save_path: 保存路径
        """
        try:
            logger.info("Plotting correlation distribution...")
            
            # 提取上三角矩阵的相关性值（排除对角线）
            mask = np.triu(np.ones_like(correlation_matrix, dtype=bool), k=1)
            correlations = correlation_matrix.values[mask]
            correlations = correlations[~np.isnan(correlations)]
            
            plt.figure(figsize=figsize)
            
            # 绘制直方图
            plt.hist(correlations, bins=50, alpha=0.7, edgecolor='black')
            plt.axvline(correlations.mean(), color='red', linestyle='--', 
                       label=f'Mean: {correlations.mean():.3f}')
            """plt.axvline(np.median(correlations), color='green', linestyle='--', 
                       label=f'Median: {np.median(correlations):.3f}')"""
            plt.axvline(float(np.median(correlations)), color='green', linestyle='--',
                       label=f'Median: {np.median(correlations):.3f}')

            plt.xlabel('Correlation Coefficient')
            plt.ylabel('Frequency')
            plt.title(title, fontsize=14)
            plt.legend()
            plt.grid(True, alpha=0.3)
            
            # 添加统计信息
            stats_text = f'Count: {len(correlations)}\n'
            stats_text += f'Std: {correlations.std():.3f}\n'
            stats_text += f'Min: {correlations.min():.3f}\n'
            stats_text += f'Max: {correlations.max():.3f}'
            
            plt.text(0.02, 0.98, stats_text, transform=plt.gca().transAxes, 
                    verticalalignment='top', bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
            
            plt.tight_layout()
            
            if save_path:
                plt.savefig(save_path, dpi=300, bbox_inches='tight')
                logger.info(f"Distribution plot saved to: {save_path}")
            
            plt.show()
            
        except Exception as e:
            logger.error(f"Error plotting correlation distribution: {str(e)}")
            raise
    
    def export_results(self, output_dir: str = "analysis_output") -> None:
        """
        导出分析结果
        
        Args:
            output_dir: 输出目录
        """
        try:
            # 创建输出目录
            output_path = Path(output_dir)
            output_path.mkdir(exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            logger.info(f"Exporting results to {output_dir}...")
            
            # 导出相关性矩阵
            for key, correlation_matrix in self.correlation_results.items():
                if isinstance(correlation_matrix, pd.DataFrame):
                    file_path = output_path / f"correlation_matrix_{key}_{timestamp}.csv"
                    correlation_matrix.to_csv(file_path)
                    logger.info(f"Exported {key} correlation matrix to: {file_path}")
            
            # 如果有原始数据，导出数据概要
            if self.raw_data is not None:
                summary_data = {
                    'total_records': len(self.raw_data),
                    'date_range': f"{self.raw_data['trade_date'].min()} to {self.raw_data['trade_date'].max()}",
                    'unique_dates': self.raw_data['trade_date'].nunique(),
                    'unique_stocks': self.raw_data['stock_code'].nunique(),
                    'unique_factors': self.raw_data['factor_name'].nunique() if 'factor_name' in self.raw_data.columns else self.raw_data['field_name'].nunique(),
                }
                
                summary_path = output_path / f"data_summary_{timestamp}.txt"
                with open(summary_path, 'w') as f:
                    for key, value in summary_data.items():
                        f.write(f"{key}: {value}\n")
                
                logger.info(f"Exported data summary to: {summary_path}")
            
        except Exception as e:
            logger.error(f"Error exporting results: {str(e)}")
            raise
    
    def get_factor_statistics(self) -> pd.DataFrame:
        """
        获取因子统计信息
        
        Returns:
            pd.DataFrame: 因子统计信息
        """
        if self.factor_data is None:
            raise ValueError("No factor matrix available. Please prepare factor matrix first.")
        
        try:
            factor_cols = [col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']]
            
            stats = []
            for factor in factor_cols:
                factor_values = self.factor_data[factor].dropna()
                
                stats.append({
                    'factor_name': factor,
                    'count': len(factor_values),
                    'mean': factor_values.mean(),
                    'std': factor_values.std(),
                    'min': factor_values.min(),
                    'max': factor_values.max(),
                    'missing_rate': (len(self.factor_data) - len(factor_values)) / len(self.factor_data),
                    'is_combined_factor': '_zwin' in factor
                })
            
            return pd.DataFrame(stats)
            
        except Exception as e:
            logger.error(f"Error getting factor statistics: {str(e)}")
            raise

    def parse_combined_factor_name(self, combined_factor_name: str) -> Dict[str, Optional[str]]:
        if "_zwin" in combined_factor_name:
            parts = combined_factor_name.split("_zwin")
            return {
                'field_name': parts[0],
                'z_windows': parts[1]
            }
        else:
            return {
                'field_name': combined_factor_name,
                'z_windows': None
            }

    def get_factors_by_window(self, z_window: str) -> List[str]:
        if self.factor_data is None:
            raise ValueError("No factor matrix available")

        factor_cols = [col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']]

        return [col for col in factor_cols if col.endswith(f'_zwin{z_window}')]

    def get_factor_windows_summary(self) -> pd.DataFrame:
        if self.factor_data is None:
            raise ValueError("No factor matrix available")

        factor_cols = [col for col in self.factor_data.columns if col not in ['trade_date', 'stock_code']]
        windows_summary = {}
        for factor in factor_cols:
            parsed = self.parse_combined_factor_name(factor)
            window = parsed['z_windows']

            if window not in windows_summary:
                windows_summary[window] = {
                    'z_windows': window,
                    'factor_count': 0,
                    'example_factors': []
                }

            windows_summary[window]['factor_count'] += 1
            if len(windows_summary[window]['example_factors']) <5:
                windows_summary[window]['example_factors'].append(parsed['field_name'])

        summary_data = []
        for window, info in windows_summary.items():
            summary_data.append({
                'z_windows': window if window else 'None',
                'factor_count': info['factor_count'],
                'example_factors': ', '.join(info['example_factors'])
            })
        return pd.DataFrame(summary_data).sort_values('factor_count', ascending=False)

def main():
    """
    主函数 - 演示用法
    """
    # 创建分析器
    analyzer = FactorCorrelationAnalyzer(
        table_name="ai_is.quantitative_other_signals",
        use_gpu=False
    )
    
    try:
        # 加载数据（最近3个月的数据作为示例）
        # end_date = datetime.now().strftime("%Y%m%d")
        # start_date = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

        end_date = datetime(2024,12,31)
        start_date = (end_date - timedelta(days=300)).strftime("%Y%m%d")
        end_date = end_date.strftime("%Y%m%d")

        raw_data = analyzer.load_factor_data(
            start_date=start_date,
            end_date=end_date,
            lag=0
        )
        
        print(f"Loaded {len(raw_data)} records")
        # print(f"Unique factors: {raw_data['factor_name'].nunique()}")
        print(f"Unique factors: {raw_data['field_name'].nunique()}")

        factor_name_col = 'factor_name' if 'factor_name' in raw_data.columns else 'field_name'
        
        # 准备因子矩阵
        factor_matrix = analyzer.prepare_factor_matrix()
        print(f"Factor matrix shape: {factor_matrix.shape}")

        factor_cols = [col for col in factor_matrix.columns if col not in ['trade_date', 'stock_code']]
        for i, factor in enumerate(factor_matrix.columns[:10]):
            print(f"  {i+1}, {factor}")
        
        # 计算截面相关性
        corr_matrix = analyzer.calculate_factor_correlation(
            correlation_type="pearson",
            method="cross_sectional"
        )
        
        print(f"Correlation matrix shape: {corr_matrix.shape}")
        
        # 分析高相关性
        high_corr = analyzer.analyze_high_correlations(corr_matrix, threshold=0.5)
        print(f"High correlation pairs: {len(high_corr)}")
        if not high_corr.empty:
            print("Top 5 high correlation pairs")
            print(high_corr.head())
        
        # 获取因子统计信息
        stats = analyzer.get_factor_statistics()
        print("\nFactor Statistics:")
        print(f"Total factors: {len(stats)}")
        print(f"Combined factors: {len(stats['is_combined_factor'])}")
        print(f"Simple factors: {len(stats[~stats['is_combined_factor']])}")

        print("Example statistics:")
        print(stats.head())

        if any('_zwin' in factor for factor in factor_cols):
            window_summary = analyzer.get_factor_windows_summary()
            print(f"\nFactor Windows Summary:")
            print(window_summary)

            if len(window_summary) > 0:
                first_window = window_summary.iloc[0]['z_windows']
                if first_window != 'None':
                    window_factors = analyzer.get_factors_by_window(first_window)
                    print(f"\nFactors in z_window={first_window}:")
                    for factor in window_factors[:5]:
                        parsed = analyzer.parse_combined_factor_name(factor)
                        print(f" - {factor} -> field_name: {parsed['field_name']}, z_windows: {parsed['z_windows']}")
        
        # 绘制图表
        analyzer.plot_correlation_heatmap(corr_matrix, save_path="analysis_output/factor_correlation_heatmap.png")
        analyzer.plot_correlation_distribution(corr_matrix, save_path="analysis_output/correlation_distribution.png")
        
        # 导出结果
        analyzer.export_results()
        
        print("Analysis completed successfully!")
        
    except Exception as e:
        logger.error(f"Analysis failed: {str(e)}")
        raise

if __name__ == "__main__":
    main()