import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Union, Tuple
from sqlalchemy import text
import os
import sys
import re

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, project_root)

from src.utils.db_connection import db_config
from src.utils.config_loader import ConfigLoader
from src.utils.logger import setup_logger



logger = setup_logger(__name__)

class MarketDataProvider:
    """市场数据提供者"""
    
    def __init__(self, sections: Optional[List[str]] = None):
        """初始化市场数据提供者

        Args:
            sections: 要加载的字段映射分组，如 ['market_data'], ['index_data'] 或组合。
                      默认 None 等价于 ['market_data']，保持向后兼容。
        """
        self.config_loader = ConfigLoader()
        all_cfg = self.config_loader.load_config('field_mapping.yaml')

        if sections is None:
            sections = ['market_data']

        field_mapping: Dict[str, Dict] = {}
        for sec in sections:
            if sec in all_cfg:
                field_mapping.update(all_cfg[sec])

        self.field_mapping = field_mapping
        self.table_config = self.config_loader.load_config('db/table_config.yaml')['tables']
        self.engines = {
            'wind': db_config.get_wind_engine(),
            'gogoal': db_config.get_gogoal_engine()
        }
        
    def _get_field_info(self, field: str) -> Dict:
        """获取字段信息
        
        Args:
            field: 字段名
            
        Returns:
            Dict: 字段信息
            
        Raises:
            ValueError: 当字段不存在时
        """
        # 处理带有lag后缀的字段名
        base_field = field.split('_lag_')[0]
        if base_field not in self.field_mapping:
            raise ValueError(f"Unknown field: {base_field}")
            
        field_info = self.field_mapping[base_field].copy()
        
        # 如果是带lag的字段，添加lag信息
        if '_lag_' in field:
            lag = int(field.split('_lag_')[1])
            field_info['lag'] = lag
            
        return field_info
        
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
        
    def _get_engine(self, data_source: str):
        """获取数据源对应的数据库引擎
        
        Args:
            data_source: 数据源名称
            
        Returns:
            SQLAlchemy engine: 数据库引擎
            
        Raises:
            ValueError: 当数据源不存在时
        """
        if data_source not in self.engines:
            raise ValueError(f"Unknown data source: {data_source}")
        return self.engines[data_source]
        
    def _get_trading_dates(self, start_date: str, end_date: str, lookback_days: int = 0) -> List[str]:
        """获取交易日历
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            lookback_days: 向前查找的天数
            
        Returns:
            List[str]: 交易日列表
        """
        # 将开始日期往前推lookback_days天
        start_date_dt = datetime.strptime(start_date, '%Y%m%d')
        lookback_start_date = (start_date_dt - timedelta(days=lookback_days*1.5+7)).strftime('%Y%m%d')
        
        # 获取交易日历表配置
        table_config = self._get_table_config('wind_quant.dbo.AShareCalendar')
        date_field = table_config['date_field']
        
        # 修改查询，使用正确的交易所代码列名
        query = f"""
        SELECT {date_field}
        FROM wind_quant.dbo.AShareCalendar
        WHERE S_INFO_EXCHMARKET='SSE'
        AND {date_field} BETWEEN '{lookback_start_date}' AND '{end_date}'
        ORDER BY {date_field}
        """
        
        try:
            engine = self._get_engine('wind')
            df = pd.read_sql(query, engine)
            return df[date_field].tolist()
        except Exception as e:
            logger.error(f"Error fetching trading dates: {str(e)}")
            raise
        
    def _transform_stock_code(self, code: str, table_config: Dict) -> str:
        """转换股票代码格式以匹配数据库要求
        
        Args:
            code: 原始股票代码 (可能是用户输入的格式，如 '000001.SZ')
            table_config: 表配置信息
            
        Returns:
            str: 转换后的、数据库可接受的股票代码
        """
        # 获取转换规则序列
        transform_sequence = table_config.get('code_transform_sequence', [])
        if not transform_sequence:
            return code
        
        # 获取全局转换规则
        code_format_rules = self.config_loader.load_config('db/table_config.yaml').get('code_format_rules', {})
        
        # 记录原始代码，用于日志
        original_code = code
        
        # 按顺序应用转换规则
        for rule_path in transform_sequence:
            # 解析规则路径（例如：'db_format.remove_suffix_wind'）
            rule_parts = rule_path.split('.')
            if len(rule_parts) != 2:
                logger.warning(f"Invalid rule path format: {rule_path}")
                continue
            
            rule_group, rule_name = rule_parts
            
            if rule_group not in code_format_rules:
                logger.warning(f"Unknown code format rule group: {rule_group}")
                continue
            
            if rule_name not in code_format_rules[rule_group]:
                logger.warning(f"Unknown code format rule: {rule_name} in group {rule_group}")
                continue
            
            rule = code_format_rules[rule_group][rule_name]
            code = self._apply_rule(code, rule)
        
        if original_code != code:
            logger.debug(f"Transformed stock code from '{original_code}' to '{code}' for database {table_config.get('database_type', 'unknown')}")
        
        return code
        
    @staticmethod
    def _extract_base_field_and_lag(col_name: str) -> Tuple[str, Optional[int]]:
        """
        从列名中提取基础字段名和滞后值
        Args:
            col_name: 列名，如 'adj_close_lag_0', 'volume_lag_1' 等
        Returns:
            Tuple[str, Optional[int]]: (基础字段名, 滞后值)
        """
        import re
        # 匹配模式：xxx_lag_N 或 xxx
        pattern = r'(.+?)(?:_lag_(\d+))?$'
        match = re.match(pattern, col_name)
        if match:
            base_field = match.group(1)
            lag = int(match.group(2)) if match.group(2) is not None else None
            return base_field, lag
        return col_name, None

    def _apply_rule(self, code: str, rule: Dict) -> str:
        """应用单个转换规则
        
        Args:
            code: 股票代码
            rule: 转换规则配置
            
        Returns:
            str: 转换后的股票代码
        """
        rule_type = rule.get('type')
        
        if rule_type == 'remove_suffix':
            # 移除后缀规则
            suffixes = rule.get('suffixes', [])
            for suffix in suffixes:
                if code.endswith(suffix):
                    code = code[:-len(suffix)]
                    break  # 一旦匹配到一个后缀就停止
                    
        elif rule_type == 'add_suffix':
            # 添加后缀规则
            rules = rule.get('rules', [])
            for rule_config in rules:
                pattern = rule_config.get('pattern')
                suffix = rule_config.get('suffix')
                if pattern and suffix:
                    if re.match(pattern, code):
                        code = f"{code}{suffix}"
                        break
        
        elif rule_type == 'pad_zeros':
            # 零填充规则，确保代码长度
            length = rule.get('length', 6)
            # 确保code只包含数字
            code_digits = ''.join(filter(str.isdigit, code))
            code = code_digits.zfill(length)
        
        return code

    def _build_query(self, fields: List[str], start_date: str, end_date: str, 
                     stock_codes: Optional[List[str]] = None,
                     days_counted: int = 1,
                     feature_lag: Optional[int] = None,
                     format: str = 'wide',
                     stock_code_prefixes: Optional[List[str]] = None) -> List[Tuple[str, str]]:
        """构建SQL查询"""
        # 计算需要向前查找的天数
        lookback_days = 0
        if feature_lag:
            lookback_days = max(lookback_days, feature_lag * 2+5)
        if days_counted > 1:
            lookback_days = max(lookback_days, days_counted*2+5)
            
        # 如果需要向前查找数据
        if lookback_days > 0:
            # 获取交易日历，向前查找lookback_days天
            trading_dates = self._get_trading_dates(start_date, end_date, lookback_days=lookback_days)
            if not trading_dates:
                raise ValueError(f"No trading dates found between {start_date} and {end_date}")
                
            # 找到第一个大于等于start_date的交易日
            valid_start_date = next((date for date in trading_dates if date >= start_date), None)
            if valid_start_date is None:
                raise ValueError(f"No valid trading date found after {start_date}")
                
            # 找到这个交易日的位置
            start_date_idx = trading_dates.index(valid_start_date)
            if start_date_idx < lookback_days:
                raise ValueError(f"Not enough trading days before {valid_start_date} for lookback {lookback_days}")
                
            # 获取调整后的开始日期（往前推lookback_days个交易日）
            adjusted_start_date = trading_dates[start_date_idx - lookback_days]
            start_date = adjusted_start_date
            
        # 按数据源分组字段
        source_fields = {}
        for field in fields:
            field_info = self._get_field_info(field)
            source = field_info['data_source']
            table = field_info['table']
            value_name = field_info['value_name']
            
            if source not in source_fields:
                source_fields[source] = {}
            if table not in source_fields[source]:
                source_fields[source][table] = []
            source_fields[source][table].append((field, value_name))
            
        # 构建查询
        queries = []
        for source, tables in source_fields.items():
            for table, field_mappings in tables.items():
                # 获取表配置
                table_config = self._get_table_config(table)
                date_field = table_config['date_field']
                code_field = table_config['code_field']
                
                # 转换股票代码格式
                if stock_codes:
                    transformed_codes = [self._transform_stock_code(code, table_config) for code in stock_codes]
                    stock_codes_str = ', '.join(f"'{code}'" for code in transformed_codes)
                else:
                    stock_codes_str = None
                
                # 构建字段列表，考虑days_counted和vwap特殊处理
                field_list = []
                adj_factor_value = None
                
                # 先找到adj_factor的value_name，用于vwap计算
                for field, value_name in field_mappings:
                    if field == 'adj_factor':
                        adj_factor_value = value_name
                        break
                
                # 如果没有找到adj_factor但有vwap字段，需要自动获取adj_factor信息
                has_vwap = any(field == 'vwap' for field, _ in field_mappings)
                if has_vwap and adj_factor_value is None:
                    try:
                        # 获取adj_factor的字段信息
                        adj_factor_info = self._get_field_info('adj_factor')
                        if adj_factor_info['table'] == table:  # 确保在同一张表中
                            adj_factor_value = adj_factor_info['value_name']
                    except:
                        pass  # 如果获取失败，保持adj_factor_value为None
                
                for field, value_name in field_mappings:
                    # 特殊处理vwap字段：直接在SQL中乘以adj_factor
                    if field == 'vwap' and adj_factor_value:
                        if days_counted > 1:
                            field_list.append(f"""
                            SUM({value_name} * {adj_factor_value}) OVER (
                                PARTITION BY {code_field} 
                                ORDER BY {date_field} 
                                ROWS BETWEEN {days_counted-1} PRECEDING AND CURRENT ROW
                            ) as {field}
                            """)
                        else:
                            field_list.append(f"({value_name} * {adj_factor_value}) as {field}")
                    elif days_counted > 1:
                        # 使用窗口函数计算累积值
                        field_list.append(f"""
                        SUM({value_name}) OVER (
                            PARTITION BY {code_field} 
                            ORDER BY {date_field} 
                            ROWS BETWEEN {days_counted-1} PRECEDING AND CURRENT ROW
                        ) as {field}
                        """)
                    else:
                        field_list.append(f"{value_name} as {field}")

                # 构建基础查询
                if format == 'wide':
                    # 宽表格式
                    base_query = f"""
                    SELECT 
                        {code_field} as stock_code,
                        {date_field} as trade_date,
                        {', '.join(field_list)}
                    FROM {table}
                    WHERE {date_field} BETWEEN '{start_date}' AND '{end_date}'
                    """
                    
                    # 添加股票代码过滤
                    if stock_codes_str:
                        base_query += f" AND {code_field} IN ({stock_codes_str})"
                    
                    # 添加股票代码前缀筛选
                    if stock_code_prefixes:
                        prefix_conditions = []
                        for prefix in stock_code_prefixes:
                            prefix_conditions.append(f"{code_field} LIKE '{prefix}%'")
                        if prefix_conditions:
                            base_query += f" AND ({' OR '.join(prefix_conditions)})"
                    
                    # 添加排序
                    base_query += f" ORDER BY {code_field}, {date_field}"
                    
                    queries.append((source, base_query))
                else:
                    # 长表格式，使用WITH + CROSS APPLY
                    # 1) a CTE that calculates any windowed values
                    # 2) CROSS APPLY in the main SELECT to pivot columns

                    # Build the list of columns for the CTE
                    cte_fields = []
                    field_labels = []
                    adj_factor_value = None
                    
                    # 先找到adj_factor的value_name，用于vwap计算
                    for field, value_name in field_mappings:
                        if field == 'adj_factor':
                            adj_factor_value = value_name
                            break
                    
                    # 如果没有找到adj_factor但有vwap字段，需要自动获取adj_factor信息
                    has_vwap = any(field == 'vwap' for field, _ in field_mappings)
                    if has_vwap and adj_factor_value is None:
                        try:
                            # 获取adj_factor的字段信息
                            adj_factor_info = self._get_field_info('adj_factor')
                            if adj_factor_info['table'] == table:  # 确保在同一张表中
                                adj_factor_value = adj_factor_info['value_name']
                        except:
                            pass  # 如果获取失败，保持adj_factor_value为None
                    
                    for field, value_name in field_mappings:
                        base_field_name = field.split('_lag_')[0]  # just the base field
                        
                        # 特殊处理vwap字段：直接在SQL中乘以adj_factor
                        if field == 'vwap' and adj_factor_value:
                            if days_counted > 1:
                                cte_fields.append(f""" 
                                    SUM({value_name} * {adj_factor_value}) OVER (
                                        PARTITION BY {code_field} 
                                        ORDER BY {date_field} 
                                        ROWS BETWEEN {days_counted - 1} PRECEDING AND CURRENT ROW
                                    ) AS {field} 
                                """)
                            else:
                                cte_fields.append(f"({value_name} * {adj_factor_value}) AS {field}")
                        elif days_counted > 1:
                            cte_fields.append(f""" 
                                SUM({value_name}) OVER (
                                    PARTITION BY {code_field} 
                                    ORDER BY {date_field} 
                                    ROWS BETWEEN {days_counted - 1} PRECEDING AND CURRENT ROW
                                ) AS {field} 
                            """)
                        else:
                            cte_fields.append(f"{value_name} AS {field}")

                        # 只在没有lag后缀的字段添加到field_labels
                        if '_lag_' not in field:
                            field_labels.append((base_field_name, field))

                    # Build the CTE + CROSS APPLY query
                    t = [f"{code_field} LIKE '{prefix}%'" for prefix in stock_code_prefixes] if stock_code_prefixes else []
                    base_query = f"""
                    WITH base_data AS (
                        SELECT
                            {code_field} AS stock_code,
                            {date_field} AS trade_date,
                            {', '.join(cte_fields)}
                        FROM {table}
                        WHERE {date_field} BETWEEN '{start_date}' AND '{end_date}'
                        {f"AND {code_field} IN ({stock_codes_str})" if stock_codes_str else ""}
                        {f"AND ({' OR '.join(t)})" if t else ""}
                    )
                    SELECT
                        stock_code,
                        trade_date,
                        pivoted.field_name,
                        pivoted.field_value AS value
                    FROM base_data
                    CROSS APPLY (
                        VALUES 
                            {', '.join(f"('{bf}', {alias})" for (bf, alias) in field_labels)}
                    ) AS pivoted(field_name, field_value)
                    ORDER BY trade_date, stock_code, pivoted.field_name
                    """

                    queries.append((source, base_query))
                
        return queries
            
    def fetch_data(self, 
                   fields: List[str],
                   start_date: str,
                   end_date: str,
                   stock_codes: Optional[List[str]] = None,
                   feature_lag: Optional[int] = None,
                   days_counted: int = 1,
                   format: str = 'wide',
                   stock_code_prefixes: Optional[List[Union[int, str]]] = None) -> pd.DataFrame:
        """获取市场数据
        
        Args:
            fields: 字段列表
            start_date: 开始日期 (YYYYMMDD 格式)
            end_date: 结束日期 (YYYYMMDD 格式)
            stock_codes: 股票代码列表 (任何格式，会自动转换为数据库格式)
            feature_lag: 滞后特征数量，如30表示生成lag0到lag29的特征
            days_counted: 累积天数，如2表示当天值和前一天值的和
            format: 输出格式，'wide'或'long'
            stock_code_prefixes: 股票代码前缀筛选，如[0, 3, 6]表示筛选0、3、6开头的股票，None表示不筛选
            
        Returns:
            pd.DataFrame: 标准化格式的市场数据
        """
        # 验证字段
        for field in fields:
            self._get_field_info(field)

            
        # 处理股票代码前缀筛选
        if stock_code_prefixes is not None and len(stock_code_prefixes) > 0:
            # 转换为字符串格式，确保数据类型一致
            prefix_strings = [str(prefix) for prefix in stock_code_prefixes]
            logger.info(f"应用股票代码前缀筛选: {prefix_strings}")
        else:
            prefix_strings = None
            
        # 构建查询
        queries = self._build_query(fields, start_date, end_date, stock_codes, days_counted, feature_lag, format, prefix_strings)
        
        # 加载配置（移到循环外）
        config = self.config_loader.load_config('db/table_config.yaml')
        remove_suffix_rule = config['code_format_rules']['output_format']['remove_all_suffix']
        
        # 按数据源分组字段，用于详细警告
        source_field_mapping = {}
        for field in fields:
            field_info = self._get_field_info(field)
            source = field_info['data_source']
            if source not in source_field_mapping:
                source_field_mapping[source] = []
            source_field_mapping[source].append(field)
        
        # 执行查询并合并结果
        dfs = []
        for source, query in queries:
            try:
                engine = self._get_engine(source)
                df = pd.read_sql(query, engine)
                
                # 使用向量化操作替代 apply
                if 'stock_code' in df.columns:
                    # 创建一个正则表达式模式来匹配所有后缀
                    suffix_pattern = '|'.join(map(re.escape, remove_suffix_rule['suffixes']))
                    df['stock_code'] = df['stock_code'].str.replace(f'({suffix_pattern})$', '', regex=True)
                
                dfs.append(df)
            except Exception as e:
                logger.error(f"Error fetching data from {source}: {str(e)}")
                raise
                
        # 过滤空的DataFrame并记录详细警报
        original_count = len(dfs)
        non_empty_dfs = []
        for i, df in enumerate(dfs):
            if df.empty:
                source_name = queries[i][0] if i < len(queries) else "unknown_source"
                # 获取该数据源对应的字段列表
                source_fields = source_field_mapping.get(source_name, ['未知字段'])
                logger.warning(f"数据警报: {start_date}-{end_date}时间段内，数据源{source_name}的字段{source_fields}返回空数据集。"
                             f"可能原因：1)该时期字段数据缺失 2)股票代码筛选过严 3)时间范围无交易日")
            else:
                non_empty_dfs.append(df)
        dfs = non_empty_dfs
                
        # 如果所有数据源都返回空数据，返回空DataFrame
        if not dfs:
            logger.warning(f"所有数据源在{start_date}-{end_date}时间段内都返回空数据集")
            if format == 'wide':
                return pd.DataFrame(columns=['trade_date', 'stock_code'] + fields)
            else:
                return pd.DataFrame(columns=['trade_date', 'stock_code', 'field_name', 'value', 'lag'])
                
        # 合并所有数据源的结果
        if len(dfs) > 1:
            # 获取交易日历，考虑lookback_days
            lookback_days = 0
            if feature_lag:
                lookback_days = max(lookback_days, feature_lag * 2+5)  # 为滞后特征预留足够的交易日
            if days_counted > 1:
                lookback_days = max(lookback_days, days_counted*2 - 1)  # 为累积天数预留足够的交易日
                
            trading_dates = self._get_trading_dates(start_date, end_date, lookback_days=lookback_days)
            trading_dates = pd.to_datetime(trading_dates)
            
            # 确保所有数据框都有相同的日期和股票代码列，并按交易日历对齐
            for i in range(len(dfs)):
                dfs[i]['trade_date'] = pd.to_datetime(dfs[i]['trade_date'])
                # 只保留交易日历中的日期
                dfs[i] = dfs[i][dfs[i]['trade_date'].isin(trading_dates)]
                # 股票代码已在上面标准化，不需要再次转换
            
            if format == 'wide':
                # 宽表格式：使用merge合并数据
                df = dfs[0]
                for other_df in dfs[1:]:
                    df = pd.merge(
                        df,
                        other_df,
                        on=['trade_date', 'stock_code'],
                        how='outer'  # 使用inner join确保数据对齐，outter也可以
                    )
                # 按日期和股票代码排序
                df = df.sort_values(['trade_date', 'stock_code'])
            else:
                # 长表格式：使用concat合并数据
                df = pd.concat(dfs, ignore_index=True)
                # 按日期、股票代码和字段名排序
                df = df.sort_values(['trade_date', 'stock_code', 'field_name'])
        else:
            df = dfs[0]
            # 单个数据源也需要对齐交易日历，同样考虑lookback_days
            lookback_days = 0
            if feature_lag:
                lookback_days = max(lookback_days, feature_lag * 2+5)
            if days_counted > 1:
                lookback_days = max(lookback_days, days_counted*2 - 1)
                
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            trading_dates = self._get_trading_dates(start_date, end_date, lookback_days=lookback_days)
            trading_dates = pd.to_datetime(trading_dates)
            df = df[df['trade_date'].isin(trading_dates)]
            
        # 生成滞后特征
        if feature_lag:
            if format == 'wide':
                # 宽表格式：一次性创建所有滞后特征
                lag_features = {}
                for field in fields:
                    field_info = self._get_field_info(field)
                    if field_info.get('is_lag', False):
                        # 生成0到feature_lag-1的lag特征
                        for lag in range(feature_lag):
                            lag_name = f"{field}_lag_{lag}"
                            lag_features[lag_name] = df.groupby('stock_code')[field].shift(lag)
                
                # 一次性添加所有滞后特征列
                if lag_features:
                    df = pd.concat([df, pd.DataFrame(lag_features, index=df.index)], axis=1)
            else:
                # 长表格式：对每个field_name分组计算滞后值
                all_lag_dfs = []
                for field in fields:
                    field_info = self._get_field_info(field)
                    if field_info.get('is_lag', False):
                        # 提取该字段在长表里的数据
                        field_df = df[df['field_name'] == field].copy()
                        # 逐个lag生成新行
                        for lag in range(feature_lag):
                            lag_df = field_df.copy()
                            lag_df['value'] = lag_df.groupby('stock_code')['value'].shift(lag)
                            lag_df['lag'] = lag
                            all_lag_dfs.append(lag_df)
                
                # 合并所有带lag的数据
                df = pd.concat(all_lag_dfs, ignore_index=True)
                
                # 排序
                df = df.sort_values(['trade_date', 'stock_code', 'field_name', 'lag'])
                
        # 过滤数据，只保留从start_date开始的数据
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        start_date_dt = pd.to_datetime(start_date)
        df = df[df['trade_date'] >= start_date_dt]
        
        # vwap字段已在SQL查询层面处理（乘以adj_factor），无需后处理
        
        # 输出格式已在前面统一标准化，这里不需要重复处理
        # df = self._standardize_output_codes(df)  # 注释掉避免重复处理
        
        # When creating lagged features and returning wide format
        if feature_lag is not None and format == 'wide':
            # After creating all the lag features (assuming you have both original and lag columns)
            
            # Option 2: Remove the original feature columns but keep the lag columns
            # (including lag_0 which contains the same data as the original)
            columns_to_remove = []
            for field in fields:
                # Only remove the column if there's a corresponding lag_0 column
                if f"{field}_lag_0" in df.columns and field in df.columns:
                    columns_to_remove.append(field)
            
            # Remove only if the lag columns exist (to avoid removing all data)
            if columns_to_remove and any(col.endswith('_lag_0') for col in df.columns):
                df = df.drop(columns=columns_to_remove)
        
        return df

    def _standardize_output_codes(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化输出的股票代码格式
        
        Args:
            df: 包含股票代码的数据框
            
        Returns:
            pd.DataFrame: 标准化股票代码后的数据框
        """
        # 加载配置
        config = self.config_loader.load_config('db/table_config.yaml')
        code_format_rules = config.get('code_format_rules', {})
        
        # 获取默认输出格式
        default_output_format = config.get('default_output_format')
        if not default_output_format:
            return df  # 如果没有指定默认输出格式，不做处理
        
        # 获取对应的规则组和规则名
        rule_group, rule_name = None, None
        for group_name, group_rules in code_format_rules.items():
            if default_output_format in group_rules:
                rule_group, rule_name = group_name, default_output_format
                break
        
        if not rule_group or not rule_name:
            logger.warning(f"Could not find output format rule: {default_output_format}")
            return df
        
        # 获取规则
        rule = code_format_rules[rule_group][rule_name]
        
        # 应用规则
        logger.info(f"Standardizing stock codes using {rule_group}.{rule_name}")
        # 使用.loc来避免SettingWithCopyWarning
        df.loc[:, 'stock_code'] = df['stock_code'].apply(lambda x: self._apply_rule(x, rule))
        
        return df
