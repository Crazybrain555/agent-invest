#!/usr/bin/env python3
"""
因子保存器 - 负责因子数据的持久化存储
支持多种格式和保存策略，便于后续扩展
"""
import os
import pandas as pd
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod


class BaseFactorSaver(ABC):
    """因子保存器基类"""
    
    def __init__(self, cfg):
        self.cfg = cfg
        
    @abstractmethod
    def save(self, df_factor: pd.DataFrame, save_path: str) -> Dict[str, Any]:
        """保存因子数据的抽象方法"""
        pass


class CSVFactorSaver(BaseFactorSaver):
    """CSV格式因子保存器"""
    
    def save(self, df_factor: pd.DataFrame, save_path: str) -> Dict[str, Any]:
        """
        保存因子数据为CSV格式
        
        Args:
            df_factor: 因子数据DataFrame，包含 ['trade_date', 'stock_code', 'model_pred'] 列
            save_path: 保存目录路径
            
        Returns:
            Dict: 保存结果信息
        """
        try:
            print("💾 开始保存因子CSV文件...")
            
            # 创建因子保存目录
            factor_dir = os.path.join(save_path, 'factors')
            os.makedirs(factor_dir, exist_ok=True)
            
            saved_files = []
            
            # 1. 按年度分别保存因子文件
            df_factor_copy = df_factor.copy()
            df_factor_copy['year'] = pd.to_datetime(df_factor_copy['trade_date']).dt.year
            
            for year, df_year in df_factor_copy.groupby('year'):
                # 格式化为CSV格式：trade_date, stock_code, model_pred
                factor_file = os.path.join(factor_dir, f'model_factor_{year}.csv')
                df_year[['trade_date', 'stock_code', 'model_pred']].to_csv(
                    factor_file, 
                    index=False
                )
                saved_files.append({
                    'file': factor_file,
                    'year': year,
                    'records': len(df_year)
                })
                print(f"   📁 保存{year}年因子: {factor_file} (共{len(df_year)}条记录)")
            
            # 2. 生成总的因子文件（可选）
            save_total = getattr(self.cfg, "factor_save_total", True)
            if save_total:
                total_factor_file = os.path.join(factor_dir, 'model_factor_total.csv')
                df_factor[['trade_date', 'stock_code', 'model_pred']].to_csv(
                    total_factor_file,
                    index=False
                )
                saved_files.append({
                    'file': total_factor_file,
                    'year': 'total',
                    'records': len(df_factor)
                })
                print(f"   📁 保存总因子文件: {total_factor_file} (共{len(df_factor)}条记录)")
            
            print(f"✅ CSV因子文件保存完成，保存至: {factor_dir}")
            
            return {
                'status': 'success',
                'format': 'csv',
                'save_directory': factor_dir,
                'files': saved_files,
                'total_records': len(df_factor)
            }
            
        except Exception as e:
            print(f"❌ 保存CSV因子文件失败: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                'status': 'failed',
                'format': 'csv',
                'error': str(e)
            }


class ParquetFactorSaver(BaseFactorSaver):
    """Parquet格式因子保存器（预留扩展）"""
    
    def save(self, df_factor: pd.DataFrame, save_path: str) -> Dict[str, Any]:
        """
        保存因子数据为Parquet格式
        """
        try:
            print("💾 开始保存因子Parquet文件...")
            
            # 创建因子保存目录
            factor_dir = os.path.join(save_path, 'factors')
            os.makedirs(factor_dir, exist_ok=True)
            
            # 保存为parquet格式（更高效的存储）
            parquet_file = os.path.join(factor_dir, 'model_factor_total.parquet')
            df_factor.to_parquet(parquet_file, index=False)
            
            print(f"✅ Parquet因子文件保存完成: {parquet_file}")
            
            return {
                'status': 'success',
                'format': 'parquet',
                'save_directory': factor_dir,
                'files': [{'file': parquet_file, 'records': len(df_factor)}],
                'total_records': len(df_factor)
            }
            
        except Exception as e:
            print(f"❌ 保存Parquet因子文件失败: {str(e)}")
            return {
                'status': 'failed',
                'format': 'parquet',
                'error': str(e)
            }


class DatabaseFactorSaver(BaseFactorSaver):
    """数据库因子保存器（预留扩展）"""
    
    def __init__(self, cfg, connection_config: Optional[Dict] = None):
        super().__init__(cfg)
        self.connection_config = connection_config or {}
    
    def save(self, df_factor: pd.DataFrame, save_path: str) -> Dict[str, Any]:
        """
        保存因子数据到数据库
        """
        # 这里可以实现数据库保存逻辑
        print("💾 数据库保存功能待实现...")
        return {
            'status': 'not_implemented',
            'format': 'database',
            'message': '数据库保存功能待实现'
        }


class FactorSaverFactory:
    """因子保存器工厂类"""
    
    _savers = {
        'csv': CSVFactorSaver,
        'parquet': ParquetFactorSaver,
        'database': DatabaseFactorSaver
    }
    
    @classmethod
    def create_saver(cls, format_type: str, cfg, **kwargs) -> BaseFactorSaver:
        """
        创建指定格式的因子保存器
        
        Args:
            format_type: 保存格式类型 ('csv', 'parquet', 'database')
            cfg: 配置对象
            **kwargs: 额外参数
            
        Returns:
            BaseFactorSaver: 因子保存器实例
        """
        if format_type not in cls._savers:
            raise ValueError(f"不支持的保存格式: {format_type}. 支持的格式: {list(cls._savers.keys())}")
        
        saver_class = cls._savers[format_type]
        return saver_class(cfg, **kwargs)
    
    @classmethod
    def get_available_formats(cls):
        """获取支持的保存格式列表"""
        return list(cls._savers.keys())


class FactorSaverManager:
    """因子保存管理器 - 支持多格式同时保存"""
    
    def __init__(self, cfg):
        self.cfg = cfg
        self.savers = []
    
    def add_saver(self, format_type: str, **kwargs):
        """添加保存器"""
        saver = FactorSaverFactory.create_saver(format_type, self.cfg, **kwargs)
        self.savers.append(saver)
        return self
    
    def save_all(self, df_factor: pd.DataFrame, save_path: str) -> Dict[str, Any]:
        """使用所有已添加的保存器保存因子数据"""
        results = {}
        
        for i, saver in enumerate(self.savers):
            saver_type = saver.__class__.__name__
            print(f"\n🔄 使用保存器 {i+1}/{len(self.savers)}: {saver_type}")
            
            result = saver.save(df_factor, save_path)
            results[saver_type] = result
        
        return results


# 便捷函数
def save_factor_csv(df_factor: pd.DataFrame, cfg, save_path: str) -> Dict[str, Any]:
    """便捷函数：保存因子为CSV格式"""
    saver = CSVFactorSaver(cfg)
    return saver.save(df_factor, save_path)


def save_factor_multi_format(df_factor: pd.DataFrame, cfg, save_path: str, 
                            formats: list = None) -> Dict[str, Any]:
    """便捷函数：多格式保存因子数据"""
    if formats is None:
        formats = ['csv']  # 默认只保存CSV
    
    manager = FactorSaverManager(cfg)
    for fmt in formats:
        manager.add_saver(fmt)
    
    return manager.save_all(df_factor, save_path)
