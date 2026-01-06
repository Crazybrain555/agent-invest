import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from typing import Optional, List, Dict, Any, Tuple
from tqdm import tqdm
from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.logger import setup_logger
from src.utils.table_schema import TableSchemaBuilder
from src.utils.db_connection import db_config
from sqlalchemy import text

logger = setup_logger(__name__)

class ForbidPoolGenerationTask:
    """
    禁投池生成任务类 - 根据多种条件生成综合禁投池
    
    参考老版本 get_tot_forbid_pivot_data 的逻辑，包括：
    1. 新股筛选（上市不满一定天数）
    2. ST股票筛选
    3. 停牌股票筛选
    4. 涨跌停股票筛选
    5. 退市股票筛选（退市、退市整理、暂停上市、创业板暂停上市风险警示）
    6. 结合量化部门禁投池数据
    """
    
    def __init__(self, 
                 table_name: str = "forbid_pool_comprehensive",
                 ipo_days: int = 122,  # 上市不满122个交易日的新股
                 st_lookback_days: int = 20,  # ST股票查找回看天数
                 stock_code_prefixes: Optional[List[str]] = None,
                 numeric_precision: Tuple[int, int] = (15, 6)):
        """
        初始化禁投池生成任务
        
        Args:
            table_name: 禁投池表名
            ipo_days: 新股上市天数阈值，小于此天数的为新股
            st_lookback_days: ST股票筛选回看天数
            stock_code_prefixes: 股票代码前缀筛选
            numeric_precision: 数值精度设置
        """
        self.table_name = table_name
        self.ipo_days = ipo_days
        self.st_lookback_days = st_lookback_days
        self.numeric_precision = numeric_precision
        
        # 股票代码前缀筛选 - 默认筛选主板(0)、创业板(3)、科创板(6)
        if stock_code_prefixes is None:
            self.stock_code_prefixes = ['0', '3', '6']
        else:
            self.stock_code_prefixes = stock_code_prefixes
        
        # 初始化数据提供者和数据库管理器
        self.market_data_provider = MarketDataProvider()
        self.db_manager = TestDBManager()
        
        # 获取数据库连接
        self.wind_engine = db_config.get_wind_engine()
        self.gogoal_engine = db_config.get_gogoal_engine()
        
        logger.info(f"ForbidPoolGenerationTask initialized with table_name={table_name}, ipo_days={ipo_days}")

    def _get_trading_dates(self, start_date: str, end_date: str, lookback_days: int = 0) -> List[str]:
        """获取交易日历"""
        # 将开始日期往前推lookback_days天
        start_date_dt = datetime.strptime(start_date, '%Y%m%d')
        lookback_start_date = (start_date_dt - timedelta(days=lookback_days*1.5+7)).strftime('%Y%m%d')
        
        query = f"""
        SELECT TRADE_DAYS
        FROM wind_quant.dbo.AShareCalendar
        WHERE S_INFO_EXCHMARKET='SSE'
        AND TRADE_DAYS BETWEEN '{lookback_start_date}' AND '{end_date}'
        ORDER BY TRADE_DAYS
        """
        
        try:
            df = pd.read_sql(query, self.wind_engine)
            return df['TRADE_DAYS'].astype(str).tolist()
        except Exception as e:
            logger.error(f"Error fetching trading dates: {str(e)}")
            raise

    def _get_stock_pool(self, start_date: str, end_date: str) -> List[str]:
        """获取股票池"""
        query = f"""
        SELECT DISTINCT(S_INFO_WINDCODE) as stock_code
        FROM wind_quant.dbo.AShareEODPrices  
        WHERE TRADE_DT <= '{end_date}' AND TRADE_DT >= '{start_date}' 
        AND S_DQ_TRADESTATUSCODE = -1
        """
        
        try:
            df = pd.read_sql(query, self.wind_engine)
            # 过滤掉T、BJ、A等特殊股票
            df = df[~df['stock_code'].str.contains('T|BJ|A', na=False)]
            # 转换为纯数字格式
            df['stock_code'] = df['stock_code'].str.split('.').str[0]
            # 移除特定的退市股票
            df = df[~df['stock_code'].isin(['3', '556'])]
            
            # 按股票代码前缀筛选
            if self.stock_code_prefixes:
                pattern = '|'.join([f'^{prefix}' for prefix in self.stock_code_prefixes])
                df = df[df['stock_code'].str.match(pattern, na=False)]
            
            stock_pool = df['stock_code'].sort_values().tolist()
            logger.info(f"获取股票池，共{len(stock_pool)}只股票")
            return stock_pool
        except Exception as e:
            logger.error(f"Error fetching stock pool: {str(e)}")
            raise

    def _filter_new_stocks(self, trade_date: str, trading_dates: List[str]) -> List[str]:
        """筛选新股（上市未满指定天数）"""
        try:
            # 找到当前交易日在交易日历中的位置
            current_idx = trading_dates.index(trade_date)
            if current_idx < self.ipo_days:
                # 如果当前日期之前的交易日不足ipo_days天，使用所有可用的交易日
                new_stock_date = trading_dates[0]
            else:
                new_stock_date = trading_dates[current_idx - self.ipo_days]
            
            query = f"""
            SELECT S_INFO_WINDCODE as stock_code 
            FROM wind_quant.dbo.AShareDescription 
            WHERE S_INFO_LISTDATE >= '{new_stock_date}'
            """
            
            df = pd.read_sql(query, self.wind_engine)
            df = df[~df['stock_code'].str.contains('T|BJ|A', na=False)]
            if not df.empty:
                df['stock_code'] = df['stock_code'].str.split('.').str[0]
                return df['stock_code'].tolist()
            return []
        except Exception as e:
            logger.error(f"Error filtering new stocks for {trade_date}: {str(e)}")
            return []

    def _filter_st_stocks(self, trade_date: str, trading_dates: List[str]) -> List[str]:
        """筛选ST股票"""
        try:
            # 找到当前交易日在交易日历中的位置
            current_idx = trading_dates.index(trade_date)
            if current_idx < self.st_lookback_days:
                st_entry_date = trading_dates[0]
            else:
                st_entry_date = trading_dates[current_idx - self.st_lookback_days]
                
            query = f"""
            SELECT S_INFO_WINDCODE as stock_code, ENTRY_DT, REMOVE_DT 
            FROM wind_quant.dbo.AShareST 
            WHERE (ENTRY_DT <= '{st_entry_date}' AND REMOVE_DT > '{trade_date}') 
               OR (ENTRY_DT <= '{st_entry_date}' AND REMOVE_DT IS NULL)
            """
            
            df = pd.read_sql(query, self.wind_engine)
            df = df[~df['stock_code'].str.contains('T|BJ', na=False)]
            if not df.empty:
                df['stock_code'] = df['stock_code'].str.split('.').str[0]
                return df['stock_code'].tolist()
            return []
        except Exception as e:
            logger.error(f"Error filtering ST stocks for {trade_date}: {str(e)}")
            return []

    def _filter_suspended_stocks(self, trade_date: str) -> List[str]:
        """筛选停牌股票"""
        try:
            query = f"""
            SELECT S_INFO_WINDCODE as stock_code, S_DQ_TRADESTATUS
            FROM wind_quant.dbo.AShareEODPrices 
            WHERE S_DQ_TRADESTATUSCODE != -1 AND TRADE_DT = '{trade_date}'
            """
            
            df = pd.read_sql(query, self.wind_engine)
            df = df[~df['stock_code'].str.contains('T|BJ', na=False)]
            # 筛选停牌和上市首日的股票
            df = df[(df['S_DQ_TRADESTATUS'] == '停牌') | (df['S_DQ_TRADESTATUS'] == '上市首日')]
            if not df.empty:
                df['stock_code'] = df['stock_code'].str.split('.').str[0]
                return df['stock_code'].tolist()
            return []
        except Exception as e:
            logger.error(f"Error filtering suspended stocks for {trade_date}: {str(e)}")
            return []

    def _filter_limit_stocks(self, trade_date: str) -> List[str]:
        """筛选涨跌停股票"""
        try:
            query = f"""
            SELECT S_INFO_WINDCODE as stock_code 
            FROM wind_quant.dbo.AShareEODDerivativeIndicator 
            WHERE UP_DOWN_LIMIT_STATUS != 0 AND TRADE_DT = '{trade_date}'
            """
            
            df = pd.read_sql(query, self.wind_engine)
            df = df[~df['stock_code'].str.contains('T|BJ', na=False)]
            if not df.empty:
                df['stock_code'] = df['stock_code'].str.split('.').str[0]
                return df['stock_code'].tolist()
            return []
        except Exception as e:
            logger.error(f"Error filtering limit stocks for {trade_date}: {str(e)}")
            return []

    def _filter_delisted_stocks(self, trade_date: str) -> List[str]:
        """筛选退市股票（包括退市、退市整理、暂停上市等）"""
        try:
            query = f"""
            SELECT S_INFO_WINDCODE as stock_code, S_TYPE_ST, ENTRY_DT, REMOVE_DT 
            FROM wind_quant.dbo.AShareST 
            WHERE S_TYPE_ST IN ('T', 'L', 'Z', 'X') 
            AND ENTRY_DT <= '{trade_date}' 
            AND (REMOVE_DT IS NULL OR REMOVE_DT > '{trade_date}')
            """
            
            df = pd.read_sql(query, self.wind_engine)
            df = df[~df['stock_code'].str.contains('T|BJ', na=False)]
            if not df.empty:
                df['stock_code'] = df['stock_code'].str.split('.').str[0]
                delisted_stocks = df['stock_code'].tolist()
                logger.debug(f"找到{len(delisted_stocks)}只退市相关股票在{trade_date}")
                return delisted_stocks
            return []
        except Exception as e:
            logger.error(f"Error filtering delisted stocks for {trade_date}: {str(e)}")
            return []

    def _get_quantitative_forbid_pool(self) -> pd.DataFrame:
        """获取量化部门的禁投池数据"""
        try:
            # 根据 run_nas_data_pipeline.py，量化禁投数据保存在 restricted_stock_pool 表中
            nas_table_name = "restricted_stock_pool"
            
            try:
                if self.db_manager.check_table_exists(nas_table_name):
                    # 只获取 signal = 1 的禁投数据，并限制日期范围以提高性能
                    query = f"""
                    SELECT trade_date, stock_code, signal 
                    FROM {nas_table_name} 
                    WHERE signal = 1 
                    AND trade_date >= '2020-01-01'
                    ORDER BY trade_date, stock_code
                    """
                    
                    df = pd.read_sql(query, self.db_manager.engine)
                    
                    if not df.empty:
                        logger.info(f"从NAS量化禁投表 {nas_table_name} 获取到禁投数据，共{len(df)}条记录")
                        
                        # 数据格式转换
                        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
                        df['stock_code'] = df['stock_code'].astype(str)  # 确保股票代码为字符串
                        df['signal'] = df['signal'].fillna(0).astype(int)
                        
                        # 只保留信号为1的数据（禁投股票）
                        df = df[df['signal'] == 1].copy()
                        
                        if df.empty:
                            logger.warning("量化禁投表中没有 signal=1 的禁投数据")
                            return pd.DataFrame()
                        
                        # 转换为透视表格式，便于后续处理
                        # 对于禁投池，只需要知道某天某股票是否被禁投，所以用1填充
                        pivot_df = df.pivot_table(
                            index='trade_date', 
                            columns='stock_code', 
                            values='signal', 
                            fill_value=0,
                            aggfunc='max'  # 如果同一天同一股票有多条记录，取最大值
                        )
                        
                        logger.info(f"量化禁投数据透视表构建完成：{len(pivot_df.index)}个交易日，{len(pivot_df.columns)}只股票")
                        return pivot_df
                    else:
                        logger.warning(f"量化禁投表 {nas_table_name} 中没有禁投数据 (signal=1)")
                        return pd.DataFrame()
                else:
                    logger.warning(f"量化禁投表 {nas_table_name} 不存在")
                    return pd.DataFrame()
                    
            except Exception as e:
                logger.error(f"从NAS量化禁投表获取数据失败: {str(e)}")
                import traceback
                logger.error(f"详细错误信息: {traceback.format_exc()}")
                return pd.DataFrame()
            
        except Exception as e:
            logger.error(f"获取量化禁投池数据时出错: {str(e)}")
            import traceback
            logger.error(f"详细错误信息: {traceback.format_exc()}")
            return pd.DataFrame()

    def _ensure_table_exists(self):
        """确保禁投池表存在"""
        try:
            if not self.db_manager.check_table_exists(self.table_name):
                logger.info(f"表 '{self.table_name}' 不存在，正在创建...")
                schema_def = TableSchemaBuilder.create_forbid_table_schema()
                self.db_manager.create_table(self.table_name, schema_def)
                logger.info(f"表 '{self.table_name}' 创建成功")
            else:
                logger.info(f"表 '{self.table_name}' 已存在")
        except Exception as e:
            logger.error(f"确保表存在时出错: {str(e)}")
            raise

    def run(self, start_date: str, end_date: str = None, overlap_days: int = 3) -> bool:
        """
        执行禁投池生成任务
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD 或 YYYYMMDD)
            end_date: 结束日期，默认为今天 (YYYY-MM-DD 或 YYYYMMDD)
            overlap_days: 重叠天数，用于更新模式
            
        Returns:
            bool: 执行是否成功
        """
        try:
            # 日期格式标准化
            if '-' in start_date:
                start_date = start_date.replace('-', '')
            if end_date and '-' in end_date:
                end_date = end_date.replace('-', '')
            elif not end_date:
                end_date = datetime.now().strftime('%Y%m%d')

            logger.info(f"开始生成禁投池数据：{start_date} 至 {end_date}")
            
            # 确保表存在
            self._ensure_table_exists()
            
            # 获取交易日历（暂不限制股票池，保留函数以便未来启用）
            trading_dates = self._get_trading_dates(start_date, end_date, lookback_days=max(self.ipo_days, 200))
            # stock_pool = self._get_stock_pool(start_date, end_date)
            logger.info("跳过股票池筛选：当前阶段不限制股票集合（包含历史退市股票/停牌/无交易但有因子记录的股票）")
            
            if not trading_dates:
                logger.error("未获取到交易日历")
                return False
            
            # if not stock_pool:
            #     logger.error("未获取到股票池")
            #     return False
            
            # 筛选实际处理的交易日期
            target_dates = [date for date in trading_dates if start_date <= date <= end_date]
            
            if not target_dates:
                logger.warning(f"在指定日期范围内未找到交易日：{start_date} 至 {end_date}")
                return True
                
            logger.info(f"需要处理的交易日共{len(target_dates)}个（未限制股票池）")
            
            # 获取量化部门禁投池数据
            quant_forbid_df = self._get_quantitative_forbid_pool()
            
            # 准备结果数据
            all_results = []
            
            # 逐日处理
            for trade_date in tqdm(target_dates, desc="处理禁投池"):
                # 获取各类禁投股票
                new_stocks = self._filter_new_stocks(trade_date, trading_dates)
                st_stocks = self._filter_st_stocks(trade_date, trading_dates)
                # suspended_stocks = self._filter_suspended_stocks(trade_date)  # 暂时不使用停牌股票筛选逻辑
                logger.debug(f"{trade_date}: 暂时跳过停牌股票筛选逻辑")
                limit_stocks = self._filter_limit_stocks(trade_date)
                delisted_stocks = self._filter_delisted_stocks(trade_date)  # 新增：退市股票
                
                # 合并自有筛选条件的禁投股票（不包含停牌股票）
                self_forbid_stocks = list(set(new_stocks + st_stocks + delisted_stocks))
                all_forbid_stocks = list(set(new_stocks + st_stocks + delisted_stocks + limit_stocks))
                
                # 只处理被禁投的股票，减少数据量
                # 合并所有禁投股票（自有 + 量化）
                all_forbid_stocks_for_date = set(self_forbid_stocks)
                
                # 添加量化部门的禁投股票
                if not quant_forbid_df.empty:
                    try:
                        check_date = datetime.strptime(trade_date, '%Y%m%d').date()
                        if check_date in quant_forbid_df.index:
                            # 获取该日期所有被禁投的股票（signal=1）
                            quant_forbid_stocks = quant_forbid_df.loc[check_date]
                            quant_forbid_stocks = quant_forbid_stocks[quant_forbid_stocks == 1].index.tolist()
                            all_forbid_stocks_for_date.update(quant_forbid_stocks)
                    except (ValueError, KeyError) as e:
                        # 如果日期转换失败或键不存在，跳过
                        pass
                
                # 只保存被禁投的股票记录（signal=1），大幅减少数据量
                for stock_code in all_forbid_stocks_for_date:
                    # 不再依赖股票池过滤；保留历史/退市/停牌但仍在因子库中的股票
                    all_results.append({
                        'trade_date': pd.to_datetime(trade_date),
                        'stock_code': stock_code,
                        'signal': 1,  # 只保存禁投股票，signal固定为1
                        'insert_time': datetime.utcnow()
                    })
                
                # 统计信息
                if len(all_forbid_stocks_for_date) > 0:
                    logger.debug(f"{trade_date}: 找到{len(all_forbid_stocks_for_date)}只禁投股票")
            
            # 保存到数据库
            if all_results:
                result_df = pd.DataFrame(all_results)
                logger.info(f"准备保存{len(result_df)}条禁投池记录到表 '{self.table_name}'")
                
                success = self.db_manager.save_dataframe(
                    df=result_df,
                    table_name=self.table_name,
                    mode='update',  # 使用upsert模式
                    index=False,
                    pk_fields=['trade_date', 'stock_code'],
                    batch_size=10000,
                    use_parallel=True
                )
                
                if success:
                    logger.info(f"禁投池数据保存成功，共{len(result_df)}条记录")
                    return True
                else:
                    logger.error("禁投池数据保存失败")
                    return False
            else:
                logger.warning("没有生成禁投池数据")
                return True
                
        except Exception as e:
            logger.error(f"执行禁投池生成任务时出错: {str(e)}", exc_info=True)
            return False
