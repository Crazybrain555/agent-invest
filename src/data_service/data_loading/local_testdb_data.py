import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional, Union, Tuple, Any
from sqlalchemy import text
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
import os
import sys
import re
import time
import random

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, project_root)

from src.utils.db_connection import db_config
from src.utils.config_loader import ConfigLoader
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

class LocalTestDBDataProvider:
    """本地测试数据库数据提供者"""
    
    def __init__(self):
        """初始化本地测试数据库数据提供者"""
        self.config_loader = ConfigLoader()
        # 加载表配置（从db/local_db_configs.yaml文件）
        self.table_config = self.config_loader.load_config('db/local_db_configs.yaml')['tables']
        # 获取测试数据库引擎
        self.engine = db_config.get_test_engine()
        
    def _get_table_config(self, table: str) -> Dict:
        """获取表配置信息
        
        Args:
            table: 表名
            
        Returns:
            Dict: 表配置信息
            
        Raises:
            ValueError: 当表不存在时
        """
        if table not in self.table_config:
            raise ValueError(f"Unknown table: {table}")
        return self.table_config[table]
    
    def _get_engine(self):
        """获取数据库引擎
        
        Returns:
            SQLAlchemy engine: 测试数据库引擎
        """
        return self.engine
    
    def _read_sql_with_retry(
        self,
        sql: Union[str, Any],
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 6,
        base_delay: float = 2.0,
        backoff: float = 1.8,
        jitter: float = 0.25,
    ) -> pd.DataFrame:
        """带指数退避与抖动的安全 SQL 读取。
        
        - 自动处理偶发断连（如 server closed the connection unexpectedly）
        - 失败时主动 dispose 连接池并重建引擎
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            try:
                engine = self._get_engine()
                if params is not None:
                    return pd.read_sql(sql, engine, params=params)
                return pd.read_sql(sql, engine)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                message = str(exc)
                is_transient = (
                    isinstance(exc, sa_exc.OperationalError)
                    or isinstance(exc, sa_exc.InterfaceError)
                    or "server closed the connection unexpectedly" in message
                    or "connection already closed" in message
                    or "could not connect to server" in message
                    or "timeout" in message.lower()
                    or "reset by peer" in message.lower()
                )
                if attempt < max_retries and is_transient:
                    # 主动重置连接池并重建引擎
                    try:
                        try:
                            self.engine.dispose()
                        except Exception:  # noqa: BLE001
                            pass
                        # 尝试重建引擎
                        self.engine = db_config.get_test_engine()
                    except Exception:  # noqa: BLE001
                        # 忽略重建失败，走后续重试
                        pass
                    delay = base_delay * (backoff ** (attempt - 1))
                    delay *= 1.0 + (random.random() * 2.0 - 1.0) * jitter
                    logger.warning(
                        f"DB transient error on attempt {attempt}/{max_retries}: {message}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    continue
                # 不可重试或已用尽重试
                logger.error(
                    f"DB read failed (attempt {attempt}/{max_retries}). Error: {message}"
                )
                break
        # 全部失败后抛出最后一次异常
        assert last_exc is not None
        raise last_exc
    
    def _transform_stock_codes(self, stock_codes: List[str]) -> List[str]:
        """转换股票代码格式（移除后缀）
        
        Args:
            stock_codes: 原始股票代码列表
            
        Returns:
            List[str]: 标准化后的股票代码列表
        """
        if not stock_codes:
            return []
            
        # 加载配置
        config = self.config_loader.load_config('db/table_config.yaml')
        code_format_rules = config.get('code_format_rules', {})
        
        # 获取移除后缀规则
        remove_suffix_rule = code_format_rules['output_format']['remove_all_suffix']
        
        # 应用规则
        transformed_codes = []
        for code in stock_codes:
            for suffix in remove_suffix_rule.get('suffixes', []):
                if code.endswith(suffix):
                    code = code[:-len(suffix)]
                    break
            transformed_codes.append(code)
            
        return transformed_codes
    
    def _build_query(self, 
                     table: str, 
                     start_date: Optional[str] = None, 
                     end_date: Optional[str] = None,
                     stock_codes: Optional[List[str]] = None,
                     fields: Optional[List[str]] = None,
                     format: str = 'wide',
                     column_filters: Optional[Dict[str, List]] = None) -> str:
        """根据表类型构建SQL查询
        
        Args:
            table: 表名
            start_date: 开始日期
            end_date: 结束日期
            stock_codes: 股票代码列表
            fields: 字段列表
            format: 输出格式
            column_filters: 列筛选条件
            
        Returns:
            str: SQL查询字符串
        """
        # 获取表配置
        cfg = self._get_table_config(table)
        table_type = cfg.get('table_type')
        
        # 准备日期和代码条件
        where_clauses = []
        
        # 日期条件（如果表有日期字段）
        date_field = cfg.get('date_field')
        if date_field and start_date and end_date:
            where_clauses.append(f"{date_field} BETWEEN '{start_date}' AND '{end_date}'")
        
        # 股票代码条件（如果表有股票代码字段）
        code_field = cfg.get('code_field')
        if code_field and stock_codes:
            transformed_codes = self._transform_stock_codes(stock_codes)
            if transformed_codes:
                codes_str = ', '.join([f"'{code}'" for code in transformed_codes])
                where_clauses.append(f"{code_field} IN ({codes_str})")
        
        # 列筛选条件
        if column_filters:
            for col, vals in column_filters.items():
                if not vals:
                    continue
                formatted_vals = []
                for v in vals:
                    if date_field and col == date_field:
                        if isinstance(v, (pd.Timestamp, datetime, date, np.datetime64)):
                            v = pd.Timestamp(v).strftime("%Y%m%d")
                        formatted_vals.append(f"'{v}'")
                        continue
                    # 如果是数字类型（int/float），不加引号；否则加引号
                    if isinstance(v, (int, float)):
                        formatted_vals.append(str(v))
                    else:
                        # 如果字符串看起来是数字，也不加引号
                        try:
                            float(v)
                            formatted_vals.append(str(v))
                        except (ValueError, TypeError):
                            formatted_vals.append(f"'{v}'")
                values_str = ', '.join(formatted_vals)
                where_clauses.append(f"{col} IN ({values_str})")
        
        # 构造WHERE子句
        where_clause = ""
        if where_clauses:
            where_clause = "WHERE " + " AND ".join(where_clauses)
        
        # 根据表类型构建查询
        if table_type == 'wide':
            return self._build_wide_query(table, where_clause, fields, format)
        elif table_type == 'long':
            return self._build_long_query(table, where_clause, fields, cfg)
        elif table_type == 'stat':
            return self._build_stat_query(table, fields)
        elif table_type == 'flag':
            return self._build_flag_query(table, where_clause)
        else:
            raise ValueError(f"Unsupported table type: {table_type}")
    
    def _build_wide_query(self, table: str, where_clause: str, fields: Optional[List[str]], format: str) -> str:
        """构建宽表查询
        
        Args:
            table: 表名
            where_clause: WHERE子句
            fields: 字段列表
            format: 输出格式
            
        Returns:
            str: SQL查询字符串
        """
        # 选择特定字段或所有字段
        select_clause = "*"
        if fields:
            select_clause = "trade_date, stock_code, " + ", ".join(fields)
        
        # 构建查询
        query = f"""
        SELECT {select_clause}
        FROM {table}
        {where_clause}
        ORDER BY trade_date, stock_code
        """
        
        return query
    
    def _build_long_query(self, table: str, where_clause: str, fields: Optional[List[str]], cfg: Dict) -> str:
        """构建长表查询
        
        Args:
            table: 表名
            where_clause: WHERE子句
            fields: 字段列表
            cfg: 表配置
            
        Returns:
            str: SQL查询字符串
        """
        # 获取表的字段名设置
        date_field = cfg.get('date_field', 'trade_date')
        code_field = cfg.get('code_field', 'stock_code')
        field_name_field = cfg.get('field_name_field', 'field_name')
        value_field = cfg.get('value_field', 'value')
        
        # 如果有字段过滤，添加到WHERE子句（排除系统字段）
        field_filter = ""
        if fields:
            # 排除系统字段，只对实际的因子/标签字段进行过滤
            system_fields = {date_field, code_field, field_name_field, value_field, "lag", "label_shift"}
            actual_factor_fields = [f for f in fields if f not in system_fields]
            
            if actual_factor_fields:
                field_list = ', '.join([f"'{field}'" for field in actual_factor_fields])
                field_filter = f" AND {field_name_field} IN ({field_list})"
        
        # 构建基础SELECT字段列表
        select_fields = [
            f"{date_field} as trade_date",
            f"{code_field} as stock_code", 
            f"{field_name_field} as field_name",
            f"{value_field} as value"
        ]
        
        # 根据表配置添加额外字段
        extra_fields = cfg.get('extra_fields', [])
        if extra_fields:
            select_fields.extend(extra_fields)
        
        # 对于特定的标签表，添加label_shift字段（如果存在）
        if 'label' in table.lower() and 'label_shift' not in [f.split()[-1] for f in select_fields]:
            # 先检查表是否有这个字段，避免硬编码
            try:
                # 可以在这里添加字段存在性检查，但为了简化，我们通过配置来控制
                if cfg.get('has_label_shift', False):
                    select_fields.append('label_shift')
            except:
                pass  # 如果字段不存在，跳过
        
        # 构建基础查询
        select_clause = ", ".join(select_fields)
        query = f"""
        SELECT {select_clause}
        FROM {table}
        {where_clause}{field_filter}
        ORDER BY trade_date, stock_code, field_name
        """
        
        return query
    
    def _build_stat_query(self, table: str, fields: Optional[List[str]]) -> str:
        """构建统计表查询
        
        Args:
            table: 表名
            fields: 特征名称列表
            
        Returns:
            str: SQL查询字符串
        """
        # 如果有特征名过滤
        where_clause = ""
        if fields:
            fields_str = ', '.join([f"'{field}'" for field in fields])
            where_clause = f"WHERE feature_name IN ({fields_str})"
        
        # 构建查询
        query = f"""
        SELECT *
        FROM {table}
        {where_clause}
        """
        
        return query
    
    def _build_flag_query(self, table: str, where_clause: str) -> str:
        """构建标志表查询
        
        Args:
            table: 表名
            where_clause: WHERE子句
            
        Returns:
            str: SQL查询字符串
        """
        # 构建查询
        query = f"""
        SELECT *
        FROM {table}
        {where_clause}
        ORDER BY trade_date, stock_code
        """
        
        return query
    
    def _wide_to_long(self, df: pd.DataFrame, id_vars: List[str], value_vars: List[str]) -> pd.DataFrame:
        """宽表转长表
        
        Args:
            df: 宽表数据
            id_vars: 保持不变的ID列
            value_vars: 需要转换的值列
            
        Returns:
            pd.DataFrame: 长表数据
        """
        # 使用pandas的melt函数进行转换
        long_df = pd.melt(
            df,
            id_vars=id_vars,
            value_vars=value_vars,
            var_name='field_name',
            value_name='value'
        )
        return long_df
    
    def _standardize_output_codes(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化输出的股票代码格式
        
        Args:
            df: 包含股票代码的数据框
            
        Returns:
            pd.DataFrame: 标准化股票代码后的数据框
        """
        # 如果没有股票代码列，直接返回
        if 'stock_code' not in df.columns:
            return df
            
        # 加载配置
        config = self.config_loader.load_config('db/table_config.yaml')
        code_format_rules = config.get('code_format_rules', {})
        
        # 获取默认输出格式
        default_output_format = config.get('default_output_format')
        if not default_output_format:
            return df
        
        # 获取规则
        rule = code_format_rules['output_format'][default_output_format]
        
        # 应用规则
        logger.info(f"Standardizing stock codes using output_format.{default_output_format}")
        
        # 创建正则表达式模式匹配所有后缀
        suffixes = rule.get('suffixes', [])
        if suffixes:
            suffix_pattern = '|'.join(map(re.escape, suffixes))
            df['stock_code'] = df['stock_code'].astype(str).str.replace(f'({suffix_pattern})$', '', regex=True)
        
        return df
    
    def fetch_data(self,
                  table: str,
                  start_date: Optional[str] = None,
                  end_date: Optional[str] = None,
                  stock_codes: Optional[List[str]] = None,
                  fields: Optional[List[str]] = None,
                  format: str = 'wide',
                  column_filters: Optional[Dict[str, List]] = None) -> pd.DataFrame:
        """获取本地测试数据库数据
        
        Args:
            table: 表名 (必填)
            start_date: 开始日期 (YYYYMMDD 格式)
            end_date: 结束日期 (YYYYMMDD 格式)
            stock_codes: 股票代码列表 (任何格式，会自动标准化)
            fields: 字段列表 (若为空，则获取所有字段)
            format: 输出格式，'wide'或'long' (仅对宽表和长表有效)
            column_filters: 列筛选条件，形如 {"factor_name": ["adj_open"], "z_windows": [0,20]}
            
        Returns:
            pd.DataFrame: 标准化格式的数据
        """
        try:
            # 获取表配置
            cfg = self._get_table_config(table)
            table_type = cfg.get('table_type')
            
            # 日期格式转换
            start_date_str = None
            end_date_str = None
            if start_date:
                start_date_str = start_date if len(start_date) == 8 else start_date.replace('-', '')
            if end_date:
                end_date_str = end_date if len(end_date) == 8 else end_date.replace('-', '')
            
            # 构建查询
            query = self._build_query(
                table=table,
                start_date=start_date_str,
                end_date=end_date_str,
                stock_codes=stock_codes,
                fields=fields,
                format=format,
                column_filters=column_filters
            )
            
            logger.debug(f"Generated SQL query: {query}")
            
            # 执行查询
            df = self._read_sql_with_retry(query)
            
            # 处理结果
            if df.empty:
                logger.warning(f"Query returned no data for table {table}")
                return df
            
            # 如果是宽表，并且要求长表格式，执行转换
            if table_type == 'wide' and format == 'long' and 'stock_code' in df.columns and 'trade_date' in df.columns:
                # 确定ID列和值列
                id_vars = ['trade_date', 'stock_code']
                # 排除ID列、模型版本、插入时间、临时标志等非数据列
                exclude_cols = id_vars + ['model_version', 'insert_time', 'is_temporary']
                value_vars = [col for col in df.columns if col not in exclude_cols]
                
                # 执行转换
                df = self._wide_to_long(df, id_vars, value_vars)
            
            # 日期列转换为datetime
            if 'trade_date' in df.columns:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
            
            # 标准化股票代码格式
            df = self._standardize_output_codes(df)
            
            # 根据表类型进行特殊处理
            if table_type == 'flag':
                # 对于禁投池表，转换signal为布尔值
                if 'signal' in df.columns:
                    df['signal'] = df['signal'].astype(bool)
            
            return df
            
        except Exception as e:
            logger.error(f"Error fetching data from table {table}: {str(e)}")
            raise

    def list_fields(self, table: str) -> list:
        """
        返回指定表的所有字段名，便于查阅和前端下拉。
        Args:
            table: 表名（如 'ai_is.intermediate_training_factors_market_normalize_lag30_countday1'）
        Returns:
            List[str]: 字段名列表
        Raises:
            ValueError: 如果表不存在或无法查询
        """
        try:
            # 只查一行，获取字段名
            sql = f"SELECT * FROM {table} LIMIT 0"
            df = self._read_sql_with_retry(sql)
            return list(df.columns)
        except Exception as e:
            logger.error(f"Error listing fields for table {table}: {str(e)}")
            raise
