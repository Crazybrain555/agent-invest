import pandas as pd
from typing import Dict, Any
import tushare as ts

class FundamentalDataProvider:
    """基本面数据提供者类"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化基本面数据提供者
        
        Args:
            config: 配置字典
        """
        self.config = config
        self._init_provider()
        
    def _init_provider(self):
        """初始化数据源"""
        provider_config = self.config['data']['data_sources']['fundamental_data']
        if provider_config['provider'] == 'tushare':
            ts.set_token(provider_config['api_key'])
            self.provider = ts.pro_api()
        else:
            raise ValueError(f"Unsupported fundamental data provider: {provider_config['provider']}")
    
    def fetch_data(self, 
                   symbol: str, 
                   report_date: str) -> pd.DataFrame:
        """获取基本面数据
        
        Args:
            symbol: 股票代码
            report_date: 报告日期
            
        Returns:
            基本面数据DataFrame
        """
        try:
            # 获取财务指标数据
            financial_data = self._fetch_financial_data(symbol, report_date)
            
            # 获取公司基本信息
            company_info = self._fetch_company_info(symbol)
            
            # 合并数据
            result = pd.merge(financial_data, company_info, on='ts_code', how='left')
            
            return result
        except Exception as e:
            raise Exception(f"Failed to fetch fundamental data: {str(e)}")
    
    def _fetch_financial_data(self, 
                            symbol: str, 
                            report_date: str) -> pd.DataFrame:
        """获取财务指标数据
        
        Args:
            symbol: 股票代码
            report_date: 报告日期
            
        Returns:
            财务指标数据DataFrame
        """
        try:
            # 获取财务指标
            financial = self.provider.financial_indicator(
                ts_code=symbol,
                period=report_date
            )
            
            # 获取资产负债表
            balance = self.provider.balancesheet(
                ts_code=symbol,
                period=report_date
            )
            
            # 获取利润表
            income = self.provider.income(
                ts_code=symbol,
                period=report_date
            )
            
            # 获取现金流量表
            cashflow = self.provider.cashflow(
                ts_code=symbol,
                period=report_date
            )
            
            # 合并所有财务数据
            result = pd.merge(financial, balance, on=['ts_code', 'period'], how='left')
            result = pd.merge(result, income, on=['ts_code', 'period'], how='left')
            result = pd.merge(result, cashflow, on=['ts_code', 'period'], how='left')
            
            return result
        except Exception as e:
            raise Exception(f"Failed to fetch financial data: {str(e)}")
    
    def _fetch_company_info(self, symbol: str) -> pd.DataFrame:
        """获取公司基本信息
        
        Args:
            symbol: 股票代码
            
        Returns:
            公司基本信息DataFrame
        """
        try:
            return self.provider.stock_basic(
                ts_code=symbol,
                fields='ts_code,symbol,name,area,industry,list_date'
            )
        except Exception as e:
            raise Exception(f"Failed to fetch company info: {str(e)}") 