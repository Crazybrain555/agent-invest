#!/usr/bin/env python3
"""
回测结果格式化工具
负责格式化、打印和保存回测结果

DEPRECATED: 此模块已被 backtest/backtest_result_pipeline/report/excel_report.py 替代。
Pipeline 模式下不再使用此模块。仅保留供旧脚本/控制台展示使用。
"""
import os
import warnings
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Any

# 发出废弃警告
warnings.warn(
    "ResultFormatter 已废弃。Pipeline 模式请使用 "
    "backtest.backtest_result_pipeline.report.excel_report 模块。",
    DeprecationWarning,
    stacklevel=2
)


class ResultFormatter:
    """
    结果格式化器 - 负责：
    - 汇总 overall & yearly dict  
    - 打印漂亮的 console 表格  
    - 存 Excel
    """
    
    def __init__(self, cfg):
        self.cfg = cfg
    
    def format_results(self, overall_results: Dict, yearly_results: Dict) -> Dict[str, pd.DataFrame]:
        """格式化回测结果为DataFrame字典"""
        print("正在格式化结果...")
        
        formatted_results = {}
        
        # 1. 总体结果表
        if overall_results:
            overall_df = self._dict_to_dataframe(overall_results, "总体表现")
            formatted_results["总体表现"] = overall_df
        
        # 2. 年度结果汇总表
        if yearly_results:
            yearly_summary = []
            
            for year, result in yearly_results.items():
                if result:
                    for strategy_name, metrics in result.items():
                        row = {"年份": year, "策略": strategy_name}
                        row.update(metrics)
                        yearly_summary.append(row)
            
            if yearly_summary:
                yearly_df = pd.DataFrame(yearly_summary)
                formatted_results["年度表现"] = yearly_df
        
        # 3. 详细年度结果
        for year, result in yearly_results.items():
            if result:
                year_df = self._dict_to_dataframe(result, f"{year}年表现")
                formatted_results[f"{year}年详细"] = year_df
        
        return formatted_results
    
    def _dict_to_dataframe(self, results_dict: Dict, sheet_name: str) -> pd.DataFrame:
        """将结果字典转换为DataFrame"""
        formatted_data = []
        
        for strategy_name, metrics in results_dict.items():
            row = {"策略名称": strategy_name}
            if isinstance(metrics, dict):
                row.update(metrics)
            formatted_data.append(row)
        
        return pd.DataFrame(formatted_data)
    
    def display_results(self, formatted_results: Dict[str, pd.DataFrame]):
        """打印结果，重点突出关键指标"""
        if not self.cfg.print_results:
            return
        
        print("\n" + "="*80)
        print("模型回测结果汇总")
        print("="*80)
        
        # 1. 优先显示核心指标汇总
        if "总体表现" in formatted_results and "年度表现" in formatted_results:
            self._print_key_metrics_summary(formatted_results)
        
        # 2. 显示详细结果
        for sheet_name, df in formatted_results.items():
            print(f"\n【{sheet_name}】")
            print("-" * 60)
            
            if not df.empty:
                if sheet_name == "年度表现":
                    self._print_yearly_performance(df)
                else:
                    # 设置显示选项
                    pd.set_option('display.max_columns', None)
                    pd.set_option('display.width', None)
                    pd.set_option('display.max_colwidth', 15)
                    
                    print(df.to_string(index=False))
            else:
                print("无数据")
        
        print("\n" + "="*80)
    
    def _print_key_metrics_summary(self, formatted_results: Dict[str, pd.DataFrame]):
        """打印核心指标对比摘要"""
        print("\n【核心指标对比】")
        print("-" * 60)
        
        try:
            overall_df = formatted_results["总体表现"]
            yearly_df = formatted_results["年度表现"]
            
            if not overall_df.empty:
                # 提取总体关键指标
                overall_data = overall_df.iloc[0] if len(overall_df) > 0 else {}
                
                print("总体表现:")
                key_metrics = ["总收益率", "夏普比率", "最大回撤", "Calmar比率", "IC均值", "IC胜率"]
                for metric in key_metrics:
                    value = overall_data.get(metric, "N/A")
                    print(f"  {metric}: {value}")
                
                # 年度表现统计
                if not yearly_df.empty and "年份" in yearly_df.columns:
                    print(f"\n年度表现统计 ({len(yearly_df)} 年):")
                    
                    # 计算年度收益率的统计信息
                    yearly_returns = []
                    for _, row in yearly_df.iterrows():
                        return_str = row.get("总收益率", "0%")
                        try:
                            # 解析百分比字符串
                            return_val = float(return_str.replace("%", "")) / 100
                            yearly_returns.append(return_val)
                        except:
                            continue
                    
                    if yearly_returns:
                        print(f"  年均收益率: {np.mean(yearly_returns):.2%}")
                        print(f"  最好年份: {max(yearly_returns):.2%}")
                        print(f"  最差年份: {min(yearly_returns):.2%}")
                        print(f"  收益率标准差: {np.std(yearly_returns):.2%}")
                        print(f"  盈利年份占比: {sum(1 for r in yearly_returns if r > 0) / len(yearly_returns):.1%}")
        
        except Exception as e:
            print(f"打印核心指标摘要失败: {str(e)}")
    
    def _print_yearly_performance(self, yearly_df: pd.DataFrame):
        """专门格式化打印年度表现"""
        if yearly_df.empty:
            print("无年度数据")
            return
        
        # 重新排列列的顺序，突出重要指标
        important_cols = ["年份", "策略", "总收益率", "夏普比率", "最大回撤", "IC均值", "IC胜率"]
        
        # 筛选存在的列
        available_cols = [col for col in important_cols if col in yearly_df.columns]
        other_cols = [col for col in yearly_df.columns if col not in available_cols]
        
        # 重新排序DataFrame
        reordered_df = yearly_df[available_cols + other_cols]
        
        # 设置显示格式
        pd.set_option('display.max_columns', None)
        pd.set_option('display.width', None)
        pd.set_option('display.max_colwidth', 12)
        
        print(reordered_df.to_string(index=False))
    
    def save_to_excel(self, formatted_results: Dict[str, pd.DataFrame]):
        """保存结果到Excel，包含格式化和汇总信息"""
        if not self.cfg.save_excel:
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_path = os.path.join(
            self.cfg.backtest_result_path, 
            f"模型回测结果_{timestamp}.xlsx"
        )
        
        try:
            with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                # 1. 保存配置信息
                config_df = self._create_config_sheet()
                config_df.to_excel(writer, sheet_name="配置信息", index=False)
                
                # 2. 保存汇总表（重点关注的指标）
                summary_df = self._create_summary_sheet(formatted_results)
                if summary_df is not None:
                    summary_df.to_excel(writer, sheet_name="核心指标汇总", index=False)
                
                # 3. 保存详细结果
                for sheet_name, df in formatted_results.items():
                    if not df.empty:
                        # Excel工作表名称限制
                        safe_sheet_name = sheet_name[:31]  # Excel限制31个字符
                        df.to_excel(writer, sheet_name=safe_sheet_name, index=False)
                        
                        # 应用格式化
                        self._format_excel_sheet(writer, safe_sheet_name, df)
            
            print(f"\n结果已保存至: {excel_path}")
            
        except Exception as e:
            print(f"保存Excel文件失败: {str(e)}")
    
    def _create_config_sheet(self) -> pd.DataFrame:
        """创建配置信息表"""
        config_info = [
            ["回测配置", ""],
            ["开始日期", self.cfg.start_date],
            ["结束日期", self.cfg.end_date],
            ["调仓频率", self.cfg.rebalance_frequency],
            ["初始资金", f"{self.cfg.initial_capital:,.0f}"],
            ["最大持仓比例", f"{self.cfg.max_position_size:.1%}"],
            ["交易费率", f"{self.cfg.trade_cost_rate:.4%}"],
            ["最大持股数", f"{self.cfg.max_stocks}"],
            ["因子滞后期", f"{self.cfg.factor_shift}"],
            ["", ""],
            ["路径配置", ""],
            ["模型路径", self.cfg.model_path],
            ["数据路径", self.cfg.dataset_path],
            ["结果路径", self.cfg.backtest_result_path],
            ["", ""],
            ["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ]
        
        return pd.DataFrame(config_info, columns=["参数", "值"])
    
    def _create_summary_sheet(self, formatted_results: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """创建核心指标汇总表"""
        try:
            summary_data = []
            
            # 提取关键指标
            key_metrics = ["总收益率", "夏普比率", "最大回撤", "Calmar比率", "胜率", "IC均值", "IC胜率"]
            
            # 1. 总体表现
            if "总体表现" in formatted_results:
                overall_df = formatted_results["总体表现"]
                if not overall_df.empty:
                    for _, row in overall_df.iterrows():
                        summary_row = {"时期": "总体", "策略": row.get("策略名称", "未知")}
                        for metric in key_metrics:
                            summary_row[metric] = row.get(metric, "N/A")
                        summary_data.append(summary_row)
            
            # 2. 年度表现
            if "年度表现" in formatted_results:
                yearly_df = formatted_results["年度表现"]
                if not yearly_df.empty:
                    for _, row in yearly_df.iterrows():
                        summary_row = {"时期": f"{row.get('年份', 'N/A')}年", "策略": row.get("策略", "未知")}
                        for metric in key_metrics:
                            summary_row[metric] = row.get(metric, "N/A")
                        summary_data.append(summary_row)
            
            if summary_data:
                return pd.DataFrame(summary_data)
            else:
                return None
                
        except Exception as e:
            print(f"创建汇总表失败: {str(e)}")
            return None
    
    def _format_excel_sheet(self, writer, sheet_name: str, df: pd.DataFrame):
        """格式化Excel工作表"""
        try:
            from openpyxl.styles import Font, PatternFill, Alignment
            
            workbook = writer.book
            worksheet = workbook[sheet_name]
            
            # 设置表头格式
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill("solid", fgColor="366092")
            
            for cell in worksheet[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")
            
            # 自动调整列宽
            for column in worksheet.columns:
                max_length = 0
                column_letter = column[0].column_letter
                
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                
                adjusted_width = min(max_length + 2, 50)
                worksheet.column_dimensions[column_letter].width = adjusted_width
            
        except Exception as e:
            print(f"Excel格式化失败: {str(e)}")