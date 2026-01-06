import logging
from datetime import datetime, timedelta
import pandas as pd
from typing import Optional, List, Dict, Any, Tuple, Union
from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Boolean
from tqdm import tqdm
from dateutil.relativedelta import relativedelta
from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.preprocessing.methods.normalizer import DataNormalizer
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.logger import setup_logger
from src.utils.table_schema import TableSchemaBuilder
from src.data_service.preprocessing.methods.norm_config import (
    Z_WINDOW_MAP_FACTOR_ENGINEERING, DEFAULT_Z_WINDOWS,
    get_field_category_factor_engineering, FieldCategoryFactorEng
)

logger = setup_logger(__name__)

class MarketPriceNormDataTask:
    """市场数据因子工程任务类 - 基于窗口的因子工程处理"""
    
    def __init__(self, 
                 start_date: str = "2002-01-01",
                 end_date: str = None,
                 processing_mode: str = "factor_engineering",  # "factor_engineering" 或 "academic"
                 field_window_config: Optional[Dict[str, List[int]]] = None,
                 fields: Optional[List[str]] = None,
                 days_count: int = 1,
                 table_name: str = "inter_factors_processed",
                 numeric_type: str = 'numeric',
                 numeric_precision: Optional[Tuple[int, int]] = (15, 6),
                 use_parallel: bool = True,
                 batch_size: int = 20000,
                 field_batch_size: int = 5,
                 lookback_periods: int = 60,
                 stock_code_prefixes: Optional[List[Union[int, str]]] = None,
                 enable_trading_day_alignment: bool = True):
        """
        初始化市场数据因子工程任务
        
        Args:
            start_date: 数据开始日期 (YYYY-MM-DD)
            end_date: 数据结束日期，默认为当天 (YYYY-MM-DD)
            processing_mode: 处理模式，"factor_engineering"使用因子工程，"academic"使用原有逻辑
            field_window_config: 字段窗口配置，默认使用因子工程配置
            fields: 需要获取的字段列表，如果为None则使用配置中的字段
            days_count: 时间粒度（天数）
            table_name: 输出表名
            numeric_type: 数值类型，'float'或'numeric'，默认为'numeric'
            numeric_precision: 当numeric_type为'numeric'时，指定精度和标度(precision, scale)
            use_parallel: 是否使用并行处理保存数据
            batch_size: 批处理大小
            field_batch_size: 字段批处理大小
            lookback_periods: 查找历史数据的回看期数（交易日）
            stock_code_prefixes: 股票代码前缀筛选，默认[0, 3, 6]表示筛选0、3、6开头的股票
            enable_trading_day_alignment: 是否启用交易日对齐功能，确保所有股票的所有因子都有完整的交易日数据
        """
        # 转换日期格式为 YYYYMMDD
        self.start_date = datetime.strptime(start_date, '%Y-%m-%d').strftime('%Y%m%d')
        self.end_date = (end_date and datetime.strptime(end_date, '%Y-%m-%d').strftime('%Y%m%d')) or datetime.now().strftime('%Y%m%d')
        
        # 处理模式配置
        self.processing_mode = processing_mode
        
        # 字段窗口配置
        if processing_mode == "factor_engineering":
            if field_window_config is None:
                self.field_window_config = Z_WINDOW_MAP_FACTOR_ENGINEERING.copy()
            else:
                self.field_window_config = field_window_config
        else:
            # academic模式使用原有配置逻辑
            if field_window_config is None:
                from src.data_service.preprocessing.methods.norm_config import Z_WINDOW_MAP_DEFAULT
                self.field_window_config = Z_WINDOW_MAP_DEFAULT.copy()
            else:
                self.field_window_config = field_window_config
        
        # 股票代码前缀筛选 - 默认筛选主板(0)、创业板(3)、科创板(6)
        if stock_code_prefixes is None:
            self.stock_code_prefixes = [0, 3, 6]
        else:
            self.stock_code_prefixes = stock_code_prefixes
        
        # 转换为字符串格式，确保数据类型一致
        if self.stock_code_prefixes:
            self.stock_code_prefixes = [str(prefix) for prefix in self.stock_code_prefixes]
            logger.info(f"股票代码筛选前缀: {self.stock_code_prefixes}")
        else:
            logger.info("不进行股票代码筛选")
        
        # 自动从字段配置构造字段列表
        if fields is None:
            # 从字段窗口配置中获取所有字段
            all_fields = set(self.field_window_config.keys())
            
            # 如果是因子工程模式，确保包含adj_close用于价格字段处理
            if processing_mode == "factor_engineering":
                # 检查是否有价格字段，如果有则确保包含adj_close
                price_fields = {f for f in all_fields 
                              if get_field_category_factor_engineering(f) == FieldCategoryFactorEng.PRICE}
                if price_fields and 'adj_close' not in all_fields:
                    all_fields.add('adj_close')
                    # 为adj_close添加默认窗口配置
                    if 'adj_close' not in self.field_window_config:
                        self.field_window_config['adj_close'] = [0, 20, 60]
            
            # 按字母顺序排序，便于日志查看
            self.fields = sorted(list(all_fields))
            
            logger.info(f"自动构造字段列表，共 {len(self.fields)} 个字段")
            logger.debug(f"字段列表: {self.fields}")
        else:
            self.fields = fields
            logger.info(f"使用自定义字段列表，共 {len(self.fields)} 个字段")
        
        # 其他配置
        self.days_count = days_count
        self.table_name = table_name
        self.numeric_type = numeric_type
        self.numeric_precision = numeric_precision
        self.use_parallel = use_parallel
        self.enable_trading_day_alignment = enable_trading_day_alignment
        
        # 动态计算实际需要的回看期数
        self.configured_lookback_periods = lookback_periods  # 保存用户配置的值
        self.max_window_size = self._calculate_max_window_size()
        # 确保回看期数至少等于最大窗口大小，并增加一些缓冲区
        self.effective_lookback_periods = max(lookback_periods, self.max_window_size + 30)
        
        if self.effective_lookback_periods > lookback_periods:
            logger.warning(f"字段配置中的最大窗口为 {self.max_window_size} 天，大于配置的 lookback_periods={lookback_periods}")
            logger.info(f"自动调整有效回看期数为 {self.effective_lookback_periods} 天（包含30天缓冲区）")
        
        # 批处理配置 - 🚀 性能优化
        self.batch_size = batch_size
        self.field_batch_size = field_batch_size
        self.upsert_batch_rows = 10_000_000  # 增加UPSERT批次大小到1000万行
        self.copy_batch_size = 200_000       # 增加COPY批次大小到20万行
        
        # 初始化组件
        self.provider = MarketDataProvider()
        self.normalizer = DataNormalizer(
            lookback_periods=self.effective_lookback_periods,  # 使用调整后的回看期数
            provider=self.provider
        )
        self.db_manager = TestDBManager()
        
        logger.info(f"初始化任务完成，处理模式: {self.processing_mode}，目标表: {self.table_name}")
        logger.info(f"有效回看期数: {self.effective_lookback_periods} 天（最大窗口: {self.max_window_size} 天）")
        logger.info(f"交易日对齐功能: {'启用' if self.enable_trading_day_alignment else '禁用'}")
        
    def _calculate_max_window_size(self) -> int:
        """
        计算字段配置中的最大窗口大小
        
        Returns:
            int: 最大窗口大小（天数）
        """
        max_window = 0
        
        for field_name, windows in self.field_window_config.items():
            if windows:  # 确保窗口列表不为空
                field_max = max(windows)
                if field_max > max_window:
                    max_window = field_max
                    logger.debug(f"字段 {field_name} 最大窗口: {field_max} 天")
        
        logger.info(f"计算得出的最大窗口大小: {max_window} 天")
        return max_window
        
    def _get_trading_days_before(self, date_str: str, periods: int) -> str:
        """
        获取指定日期之前N个交易日的日期
        
        Args:
            date_str: 基准日期 (YYYYMMDD格式)
            periods: 向前查找的交易日数量
            
        Returns:
            str: N个交易日之前的日期 (YYYYMMDD格式)
        """
        try:
            # 获取交易日历
            end_date = datetime.strptime(date_str, '%Y%m%d')
            # 向前推算足够的自然日（通常交易日约占70%）
            start_estimate = end_date - timedelta(days=int(periods * 1.5) + 30)
            
            # 获取交易日历数据
            trading_calendar = self.provider.fetch_data(
                fields=['adj_close'],  # 只需要一个字段来获取交易日
                start_date=start_estimate.strftime('%Y%m%d'),
                end_date=date_str,
                feature_lag=None,
                days_counted=1,
                format='long',
                stock_code_prefixes=self.stock_code_prefixes
            )
            
            if trading_calendar is None or trading_calendar.empty:
                # 如果无法获取交易日历，使用估算方法
                logger.warning(f"无法获取交易日历，使用估算方法：向前推{int(periods * 1.4)}个自然日")
                estimated_date = end_date - timedelta(days=int(periods * 1.4))
                return estimated_date.strftime('%Y%m%d')
            
            # 获取唯一的交易日期并排序
            trading_dates = sorted(trading_calendar['trade_date'].dt.strftime('%Y%m%d').unique())
            
            # 找到基准日期在交易日历中的位置
            if date_str in trading_dates:
                target_index = trading_dates.index(date_str)
            else:
                # 如果基准日期不是交易日，找到最近的前一个交易日
                target_index = len([d for d in trading_dates if d < date_str]) - 1
            
            # 计算目标日期的索引
            target_date_index = max(0, target_index - periods)
            
            return trading_dates[target_date_index]
            
        except Exception as e:
            logger.error(f"获取交易日失败: {str(e)}")
            # 降级处理：使用估算方法
            estimated_date = datetime.strptime(date_str, '%Y%m%d') - timedelta(days=int(periods * 1.4))
            return estimated_date.strftime('%Y%m%d')

    def _get_trading_calendar(self, start_date: str, end_date: str) -> List[str]:
        """
        获取指定日期范围内的完整交易日历
        
        Args:
            start_date: 开始日期 (YYYYMMDD格式)
            end_date: 结束日期 (YYYYMMDD格式)
            
        Returns:
            List[str]: 交易日期列表 (YYYYMMDD格式)
        """
        try:
            # 获取交易日历数据
            trading_calendar = self.provider.fetch_data(
                fields=['adj_close'],  # 只需要一个字段来获取交易日
                start_date=start_date,
                end_date=end_date,
                feature_lag=None,
                days_counted=1,
                format='long',
                stock_code_prefixes=self.stock_code_prefixes
            )
            
            if trading_calendar is None or trading_calendar.empty:
                logger.warning(f"无法获取交易日历 ({start_date} - {end_date})，返回空列表")
                return []
            
            # 获取唯一的交易日期并排序
            trading_dates = sorted(trading_calendar['trade_date'].dt.strftime('%Y%m%d').unique())
            logger.debug(f"获取交易日历: {len(trading_dates)} 个交易日")
            
            return trading_dates
            
        except Exception as e:
            logger.error(f"获取交易日历失败: {str(e)}")
            return []

    def _align_trading_days(self, df: pd.DataFrame, start_date: str, end_date: str, stock_batch_size: int = 200) -> pd.DataFrame:
        """
        对齐交易日数据，确保所有股票的所有字段都有完整的交易日数据
        缺失的数据填充为NaN
        
        使用分批处理避免内存爆炸：
        - 按股票分批处理，避免创建巨大的稀疏矩阵
        - 智能跳过完整股票，只处理有缺失的股票
        
        Args:
            df: 原始数据 [trade_date, stock_code, field_name, value]
            start_date: 目标开始日期 (YYYYMMDD格式)
            end_date: 目标结束日期 (YYYYMMDD格式)
            stock_batch_size: 每批处理的股票数量，控制内存使用
            
        Returns:
            pd.DataFrame: 对齐后的数据
        """
        if df.empty:
            return df
        
        # 获取交易日历
        trading_days = self._get_trading_calendar(start_date, end_date)
        if not trading_days:
            logger.warning("无法获取交易日历，跳过交易日对齐")
            return df
        
        # 转换为datetime格式
        trading_days_dt = pd.to_datetime(trading_days, format='%Y%m%d')
        
        # 提取数据中的唯一值
        stock_codes = df['stock_code'].unique()
        field_names = df['field_name'].unique()
        
        total_expected_rows = len(trading_days_dt) * len(stock_codes) * len(field_names)
        
        # 内存安全检查：如果预期行数过大，减少批处理大小或直接跳过
        if total_expected_rows > 50_000_000:  # 超过5000万行
            logger.warning(f"预期数据量过大 ({total_expected_rows:,} 行)，为避免内存问题，跳过交易日对齐")
            return df
        elif total_expected_rows > 10_000_000:  # 超过1000万行，减少批处理大小
            stock_batch_size = min(stock_batch_size, 50)
            logger.info(f"数据量较大 ({total_expected_rows:,} 行)，使用较小的批处理大小: {stock_batch_size}")
        
        logger.info(f"开始交易日对齐: {len(trading_days_dt)} 个交易日 × {len(stock_codes)} 只股票 × {len(field_names)} 个字段")
        
        # 预处理：检查哪些股票需要对齐（智能跳过完整股票）
        incomplete_stocks = self._find_incomplete_stocks(df, trading_days_dt, field_names)
        
        if not incomplete_stocks:
            logger.info("所有股票数据都已完整，无需对齐")
            return df
        
        logger.info(f"发现 {len(incomplete_stocks)} 只股票需要对齐，总计 {len(stock_codes)} 只")
        
        # 分离完整股票和不完整股票的数据
        complete_data = df[~df['stock_code'].isin(incomplete_stocks)].copy()
        incomplete_data = df[df['stock_code'].isin(incomplete_stocks)].copy()
        
        # 分批处理不完整的股票
        aligned_batches = [complete_data] if not complete_data.empty else []
        
        # 按批次处理股票
        stock_batches = [incomplete_stocks[i:i + stock_batch_size] 
                        for i in range(0, len(incomplete_stocks), stock_batch_size)]
        
        logger.info(f"将 {len(incomplete_stocks)} 只不完整股票分成 {len(stock_batches)} 批处理")
        
        for batch_idx, stock_batch in enumerate(stock_batches, 1):
            try:
                # 获取当前批次的数据
                batch_data = incomplete_data[incomplete_data['stock_code'].isin(stock_batch)].copy()
                
                if batch_data.empty:
                    continue
                
                # 对当前批次进行对齐
                aligned_batch = self._align_stock_batch(batch_data, trading_days_dt, field_names)
                
                if not aligned_batch.empty:
                    aligned_batches.append(aligned_batch)
                    
                logger.debug(f"批次 {batch_idx}/{len(stock_batches)} 对齐完成: {len(stock_batch)} 只股票")
                
            except Exception as e:
                logger.warning(f"批次 {batch_idx} 对齐失败: {str(e)}，跳过该批次")
                # 添加原始数据以避免数据丢失
                batch_data = incomplete_data[incomplete_data['stock_code'].isin(stock_batch)].copy()
                if not batch_data.empty:
                    aligned_batches.append(batch_data)
        
        if not aligned_batches:
            logger.warning("所有批次对齐失败，返回原始数据")
            return df
        
        # 合并所有批次
        final_result = pd.concat(aligned_batches, ignore_index=True)
        
        original_rows = len(df)
        aligned_rows = len(final_result)
        filled_rows = aligned_rows - original_rows
        
        logger.info(f"交易日对齐完成: 原始 {original_rows} 行 → 对齐后 {aligned_rows} 行 (填充 {filled_rows} 行NaN)")
        
        return final_result

    def _find_incomplete_stocks(self, df: pd.DataFrame, trading_days_dt: pd.DatetimeIndex, field_names: List[str]) -> List[str]:
        """
        快速找出数据不完整的股票（缺少某些交易日或字段的股票）
        
        Args:
            df: 原始数据
            trading_days_dt: 交易日列表
            field_names: 字段名列表
            
        Returns:
            List[str]: 不完整股票代码列表
        """
        try:
            # 计算每只股票应有的数据行数
            expected_rows_per_stock = len(trading_days_dt) * len(field_names)
            
            # 统计每只股票实际的数据行数
            df_copy = df.copy()
            df_copy['trade_date'] = pd.to_datetime(df_copy['trade_date'])
            
            actual_counts = df_copy.groupby('stock_code').size()
            
            # 找出行数不足的股票
            incomplete_stocks = actual_counts[actual_counts < expected_rows_per_stock].index.tolist()
            
            return incomplete_stocks
            
        except Exception as e:
            logger.warning(f"检查股票完整性失败: {str(e)}，假设所有股票都需要对齐")
            return df['stock_code'].unique().tolist()

    def _align_stock_batch(self, batch_data: pd.DataFrame, trading_days_dt: pd.DatetimeIndex, field_names: List[str]) -> pd.DataFrame:
        """
        对一批股票进行交易日对齐
        
        Args:
            batch_data: 当前批次的股票数据
            trading_days_dt: 交易日列表
            field_names: 字段名列表
            
        Returns:
            pd.DataFrame: 对齐后的数据
        """
        if batch_data.empty:
            return batch_data
        
        stock_codes = batch_data['stock_code'].unique()
        
        # 创建当前批次的完整索引
        batch_index = pd.MultiIndex.from_product(
            [trading_days_dt, stock_codes, field_names],
            names=['trade_date', 'stock_code', 'field_name']
        )
        
        # 准备数据进行reindex
        batch_copy = batch_data.copy()
        batch_copy['trade_date'] = pd.to_datetime(batch_copy['trade_date'])
        
        # 检查并处理重复索引
        if batch_copy.duplicated(subset=['trade_date', 'stock_code', 'field_name']).any():
            logger.warning("发现重复索引，进行去重处理（保留最后一个值）")
            batch_copy = batch_copy.drop_duplicates(
                subset=['trade_date', 'stock_code', 'field_name'], 
                keep='last'
            )
        
        batch_indexed = batch_copy.set_index(['trade_date', 'stock_code', 'field_name'])
        
        # 重新索引，缺失值自动填充为NaN
        batch_aligned = batch_indexed.reindex(batch_index)
        
        # 重置索引
        batch_aligned = batch_aligned.reset_index()
        
        return batch_aligned

    def check_data_status(self) -> Dict[str, Any]:
        """检查数据状态"""
        try:
            table_exists = self.db_manager.check_table_exists(self.table_name)
            if not table_exists:
                return {
                    'needs_initialization': True,
                    'latest_date': None,
                    'table_exists': False,
                    'has_data': False
                }
            
            latest_date = self._get_latest_date_from_db()
            return {
                'needs_initialization': False,
                'latest_date': latest_date,
                'table_exists': True,
                'has_data': latest_date is not None
            }
            
        except Exception as e:
            logger.error(f"检查数据状态失败: {str(e)}")
            return {
                'needs_initialization': True,
                'latest_date': None,
                'table_exists': False,
                'has_data': False
            }
    
    def execute(self, overlap_days: int = 90, force_update: bool = False) -> bool:
        """
        执行数据处理任务
        
        Args:
            overlap_days: 重叠天数，用于更新时确保数据连续性，默认90天
            force_update: 是否强制更新，即使数据看起来是最新的
            
        Returns:
            bool: 任务是否成功执行
        """
        try:
            # 检查数据状态
            status = self.check_data_status()
            
            if status['table_exists'] and status['has_data']:
                # 检测是否有新增字段
                existing_fields = self._get_existing_factor_names()
                missing_fields = [f for f in self.fields if f not in existing_fields]
                if missing_fields:
                    logger.info(f"检测到 {len(missing_fields)} 个新增字段: {missing_fields}，将执行历史回溯填充。")
                    original_fields = self.fields
                    try:
                        self.fields = missing_fields
                        ok_new_fields = self._execute_initialization()
                        if not ok_new_fields:
                            return False
                    finally:
                        self.fields = original_fields
                
                logger.info(f"表 {self.table_name} 已存在且有数据，执行常规更新操作")
                return self._execute_update(status['latest_date'], overlap_days, force_update)
            else:
                logger.info(f"表 {self.table_name} 不存在或无数据，执行初始化操作")
                return self._execute_initialization()
                
        except Exception as e:
            logger.error(f"执行任务失败: {str(e)}", exc_info=True)
            return False
    
    def _execute_initialization(self) -> bool:
        """执行数据初始化"""
        try:
            logger.info(f"开始初始化数据，目标日期范围: {self.start_date} 到 {self.end_date}")
            logger.info(f"使用 {self.processing_mode} 处理模式，lookback_periods={self.effective_lookback_periods}个交易日")
            
            start_dt = datetime.strptime(self.start_date, '%Y%m%d')
            end_dt = datetime.strptime(self.end_date, '%Y%m%d')
            
            # 检查目标表是否存在
            table_created = self.db_manager.check_table_exists(self.table_name)
            if table_created:
                logger.info(f"表 {self.table_name} 已存在，将追加/更新数据")
            
            current_start_dt = start_dt
            
            # 使用年度chunk处理数据
            total_chunks = end_dt.year - start_dt.year + 1
            logger.info(f"按年度分 {total_chunks} 个chunk处理数据...")
            
            for chunk_index in tqdm(range(total_chunks), desc="Processing Years"):
                chunk_start_dt = current_start_dt
                chunk_end_dt = min(chunk_start_dt + relativedelta(years=1) - timedelta(days=1), end_dt)
                
                # 每个chunk开始时清空缓存
                self.normalizer.clear_cache()
                logger.debug(f"已清空Normalizer缓存 (chunk {chunk_index + 1}/{total_chunks})")
                
                # 获取数据时需要往前推lookback_periods个交易日
                data_start_date = self._get_trading_days_before(
                    chunk_start_dt.strftime('%Y%m%d'), 
                    self.effective_lookback_periods
                )
                
                logger.info(f"处理chunk: {chunk_start_dt.strftime('%Y%m%d')} - {chunk_end_dt.strftime('%Y%m%d')}")
                logger.info(f"数据获取范围: {data_start_date} - {chunk_end_dt.strftime('%Y%m%d')} (包含{self.effective_lookback_periods}个交易日的历史数据)")
                
                # 按字段分批处理
                field_batches = [self.fields[i:i + self.field_batch_size] 
                               for i in range(0, len(self.fields), self.field_batch_size)]
                
                logger.info(f"将 {len(self.fields)} 个字段分成 {len(field_batches)} 批处理")
                
                for batch_idx, field_batch in enumerate(tqdm(field_batches, desc="Field Batches", leave=False), 1):
                    logger.info(f"处理字段批次 {batch_idx}/{len(field_batches)}: {field_batch}")
                    
                    # 获取数据 - 包含历史数据用于计算
                    batch_data = self.provider.fetch_data(
                        fields=field_batch,
                        start_date=data_start_date,  # 从更早的日期开始获取
                        end_date=chunk_end_dt.strftime('%Y%m%d'),
                        feature_lag=None,  # 不生成lag特征
                        days_counted=self.days_count,
                        format='long',
                        stock_code_prefixes=self.stock_code_prefixes
                    )
                    
                    if batch_data is None or batch_data.empty:
                        logger.warning(f"字段批次 {batch_idx} 无数据: {field_batch}")
                        continue
                    
                    logger.info(f"字段批次 {batch_idx} 获取数据: {len(batch_data)} 行 (包含历史数据)")
                    
                    # 交易日对齐：确保所有股票的所有字段都有完整的交易日数据
                    if self.enable_trading_day_alignment:
                        try:
                            batch_data = self._align_trading_days(
                                batch_data, 
                                data_start_date, 
                                chunk_end_dt.strftime('%Y%m%d')
                            )
                            logger.info(f"字段批次 {batch_idx} 交易日对齐后: {len(batch_data)} 行")
                        except Exception as e:
                            logger.warning(f"字段批次 {batch_idx} 交易日对齐失败: {str(e)}，继续处理原数据")
                    else:
                        logger.debug(f"字段批次 {batch_idx} 跳过交易日对齐（已禁用）")
                    
                    # 使用因子工程或学术方法进行处理
                    if self.processing_mode == "factor_engineering":
                        processed_batch = self.normalizer.normalize_data_factor_engineering(
                            df=batch_data,
                            field_window_config=self.field_window_config
                        )
                    else:
                        # 使用原有的academic方法
                        processed_batch = self.normalizer.normalize_data(
                            df=batch_data,
                            fields=field_batch,
                            method='academic',
                            data_format='long',
                            z_windows=self.field_window_config
                        )
                    
                    del batch_data
                    
                    if processed_batch.empty:
                        logger.warning(f"字段批次 {batch_idx} 处理后无数据")
                        del processed_batch
                        continue
                    
                    # 过滤数据，只保留目标时间范围内的数据（去掉用于计算的历史数据）
                    target_start_dt = chunk_start_dt
                    processed_batch = processed_batch[
                        processed_batch['trade_date'] >= pd.to_datetime(target_start_dt)
                    ]
                    
                    if processed_batch.empty:
                        logger.warning(f"字段批次 {batch_idx} 过滤后无数据")
                        del processed_batch
                        continue
                    
                    logger.info(f"字段批次 {batch_idx} 过滤后保留数据: {len(processed_batch)} 行")
                    
                    # 转换为数据库格式（因子工程模式已经是正确格式）
                    if self.processing_mode == "factor_engineering":
                        final_batch = processed_batch
                    else:
                        final_batch = self._convert_to_factor_table_format(processed_batch)
                    
                    del processed_batch
                    
                    if final_batch.empty:
                        logger.warning(f"字段批次 {batch_idx} 转换后无数据")
                        del final_batch
                        continue
                    
                    # 保存数据
                    if not self._save_to_database(final_batch, mode='update'):
                        logger.error(f"字段批次 {batch_idx} 保存失败: {field_batch}")
                        return False
                    
                    del final_batch
                    logger.info(f"字段批次 {batch_idx} 处理完成并已写入数据库")
                
                # 移动到下一个chunk
                current_start_dt = chunk_end_dt + timedelta(days=1)
            
            logger.info("数据初始化完成")
            return True
            
        except Exception as e:
            logger.error(f"数据初始化失败: {str(e)}", exc_info=True)
            return False

    def _convert_to_factor_table_format(self, normalized_data: pd.DataFrame) -> pd.DataFrame:
        """将归一化后的数据转换为因子表格式（用于向后兼容）"""
        if normalized_data.empty:
            logger.warning("输入数据为空，返回空DataFrame")
            return pd.DataFrame()
        
        result_data = normalized_data.copy()
        
        # 如果已经是因子格式，直接返回
        if 'factor_name' in result_data.columns and 'factor_value' in result_data.columns:
            return result_data
        
        # 转换为因子格式
        result_data = result_data.rename(columns={
            'field_name': 'factor_name',
            'value': 'factor_value'
        })
        
        # 确保必要的列存在
        if 'z_windows' not in result_data.columns:
            result_data['z_windows'] = 0
        
        # 确保列的顺序和类型正确
        expected_columns = ['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']
        result_data = result_data[expected_columns]
        
        # 数据类型转换
        result_data['trade_date'] = pd.to_datetime(result_data['trade_date'])
        result_data['z_windows'] = result_data['z_windows'].astype(int)
        result_data['factor_value'] = pd.to_numeric(result_data['factor_value'], errors='coerce')
        
        return result_data
    
    def _get_latest_date_from_db(self) -> Optional[str]:
        """从数据库中获取最新数据日期"""
        try:
            query = f"""
            SELECT MAX(trade_date) as latest_date
            FROM {self.table_name}
            """
            result = pd.read_sql(query, self.db_manager.engine)
            
            if result['latest_date'].iloc[0] is not None:
                return result['latest_date'].iloc[0].strftime('%Y-%m-%d')
            return None
        except Exception as e:
            logger.error(f"获取数据库最新日期失败: {str(e)}")
            return None
    
    def _get_overlap_start_date(self, latest_date: str, overlap_days: int = 90) -> str:
        """计算重叠开始日期（基于交易日）"""
        latest_date_dt = datetime.strptime(latest_date, '%Y-%m-%d')
        
        # 确保重叠天数至少等于最大窗口大小，以避免因子计算时缺少历史数据
        effective_overlap_days = max(overlap_days, self.max_window_size + 10)
        
        if effective_overlap_days > overlap_days:
            logger.info(f"重叠天数 {overlap_days} 小于最大窗口 {self.max_window_size}，自动调整为 {effective_overlap_days} 天")
        
        # 使用交易日计算重叠开始日期
        overlap_start_date = self._get_trading_days_before(
            latest_date_dt.strftime('%Y%m%d'), 
            effective_overlap_days
        )
        return overlap_start_date
    
    def _execute_update(self, latest_date: str, overlap_days: int = 90, force_update: bool = False) -> bool:
        """执行数据更新"""
        try:
            if latest_date is None:
                logger.info("没有找到现有数据，将执行完整初始化")
                return self._execute_initialization()
                
            # 计算重叠开始日期（基于交易日）
            overlap_start_date = self._get_overlap_start_date(latest_date, overlap_days)
            
            # 进一步向前调整开始日期，确保有足够的历史数据用于计算
            data_start_date = self._get_trading_days_before(overlap_start_date, self.effective_lookback_periods)
            
            latest_date_ymd = datetime.strptime(latest_date, '%Y-%m-%d').strftime('%Y%m%d')
            today = datetime.now().strftime('%Y%m%d')
            
            if latest_date_ymd >= today and not force_update:
                logger.info("数据已是最新，无需更新。如需重新处理最新数据，请指定 force_update=True。")
                return True
                
            logger.info(f"开始更新数据")
            logger.info(f"数据获取范围: {data_start_date} 到 {today} (包含{self.effective_lookback_periods}个交易日的历史数据)")
            logger.info(f"将替换 {overlap_start_date} 之后的数据")
            
            # 更新操作开始时清空缓存
            self.normalizer.clear_cache()
            logger.debug("已清空Normalizer缓存 (update mode)")
            
            # 按字段分批处理
            field_batches = [self.fields[i:i + self.field_batch_size] 
                           for i in range(0, len(self.fields), self.field_batch_size)]
            
            logger.info(f"更新操作：将 {len(self.fields)} 个字段分成 {len(field_batches)} 批处理")
            
            for batch_idx, field_batch in enumerate(tqdm(field_batches, desc="Update Field Batches", leave=False), 1):
                logger.info(f"更新字段批次 {batch_idx}/{len(field_batches)}: {field_batch}")
                
                # 获取更新数据 - 包含历史数据
                batch_data = self.provider.fetch_data(
                    fields=field_batch,
                    start_date=data_start_date,  # 从更早的日期开始获取
                    end_date=self.end_date,
                    feature_lag=None,  # 不生成lag特征
                    days_counted=self.days_count,
                    format='long',
                    stock_code_prefixes=self.stock_code_prefixes
                )
                
                if batch_data is None or batch_data.empty:
                    logger.warning(f"更新字段批次 {batch_idx} 无数据: {field_batch}")
                    continue
                
                logger.info(f"更新字段批次 {batch_idx} 获取数据: {len(batch_data)} 行 (包含历史数据)")
                
                # 交易日对齐：确保所有股票的所有字段都有完整的交易日数据
                if self.enable_trading_day_alignment:
                    try:
                        batch_data = self._align_trading_days(
                            batch_data, 
                            data_start_date, 
                            self.end_date
                        )
                        logger.info(f"更新字段批次 {batch_idx} 交易日对齐后: {len(batch_data)} 行")
                    except Exception as e:
                        logger.warning(f"更新字段批次 {batch_idx} 交易日对齐失败: {str(e)}，继续处理原数据")
                else:
                    logger.debug(f"更新字段批次 {batch_idx} 跳过交易日对齐（已禁用）")
                
                # 处理数据
                if self.processing_mode == "factor_engineering":
                    processed_batch = self.normalizer.normalize_data_factor_engineering(
                        df=batch_data,
                        field_window_config=self.field_window_config
                    )
                else:
                    processed_batch = self.normalizer.normalize_data(
                        df=batch_data,
                        fields=field_batch,
                        method='academic',
                        data_format='long',
                        z_windows=self.field_window_config
                    )
                
                del batch_data
                
                if processed_batch.empty:
                    logger.warning(f"更新字段批次 {batch_idx} 处理后无数据")
                    del processed_batch
                    continue
                
                # 过滤数据，只保留需要更新的时间范围
                filter_start_dt = datetime.strptime(overlap_start_date, '%Y%m%d')
                processed_batch = processed_batch[
                    processed_batch['trade_date'] >= pd.to_datetime(filter_start_dt)
                ]
                
                if processed_batch.empty:
                    logger.warning(f"更新字段批次 {batch_idx} 过滤后无数据")
                    del processed_batch
                    continue
                
                logger.info(f"更新字段批次 {batch_idx} 过滤后保留数据: {len(processed_batch)} 行")
                
                # 转换格式
                if self.processing_mode == "factor_engineering":
                    final_batch = processed_batch
                else:
                    final_batch = self._convert_to_factor_table_format(processed_batch)
                
                del processed_batch
                
                if final_batch.empty:
                    logger.warning(f"更新字段批次 {batch_idx} 转换后无数据")
                    del final_batch
                    continue
                
                # 保存数据（使用UPSERT模式）
                if not self._save_to_database(final_batch, mode='update'):
                    logger.error(f"更新字段批次 {batch_idx} 保存失败: {field_batch}")
                    return False
                
                del final_batch
                logger.info(f"更新字段批次 {batch_idx} 处理完成并已写入数据库")
            
            logger.info("所有字段批次更新完成")
            return True
            
        except Exception as e:
            logger.error(f"数据更新失败: {str(e)}", exc_info=True)
            return False
    
    def _save_to_database(self, data: pd.DataFrame, mode: str = 'append') -> bool:
        """保存数据到测试数据库"""
        try:
            total_rows = len(data)
            if total_rows == 0:
                logger.warning("DataFrame is empty, nothing to save.")
                return True

            logger.info(f"正在保存数据到数据库 ({self.table_name}), 总行数: {total_rows}, mode: {mode}...")
            
            # 🚀 优化批处理大小配置
            copy_batch_size = self.copy_batch_size
            if total_rows > 50_000_000:  # 超过5000万行
                copy_batch_size = 500_000
            elif total_rows > 10_000_000:  # 超过1000万行
                copy_batch_size = 300_000
            elif total_rows > 1_000_000:   # 超过100万行
                copy_batch_size = 200_000
            elif total_rows < 100_000:     # 少于10万行
                copy_batch_size = 50_000
            
            # 🚀 更积极的并行处理策略
            use_parallel = self.use_parallel and total_rows > 100_000  # 降低并行处理门槛
            max_workers = 6 if total_rows > 5_000_000 else 4  # 大数据集使用更多线程
            
            # 根据数据是否包含z_windows字段来确定主键
            if mode == 'update':
                pk_fields = ["trade_date", "stock_code", "factor_name", "z_windows"]
            else:
                pk_fields = None
            
            success = self.db_manager.save_dataframe(
                df=data,
                table_name=self.table_name,
                mode=mode,
                index=False,
                batch_size=copy_batch_size,
                use_parallel=use_parallel,
                max_workers=max_workers,  # 使用优化后的线程数
                pk_fields=pk_fields,
                upsert_batch_rows=self.upsert_batch_rows
            )
            
            if success:
                logger.info(f"成功保存数据 (mode={mode})，记录数: {total_rows}")
            else:
                logger.error(f"保存数据失败 (mode={mode})")
                
            return success
            
        except Exception as e:
            logger.error(f"保存数据到数据库失败 (mode={mode}): {str(e)}", exc_info=True)
            return False

    def _get_existing_factor_names(self) -> set:
        """查询数据库中已存在的 factor_name 集合。如果表不存在或查询失败，则返回空集合。"""
        try:
            if not self.db_manager.check_table_exists(self.table_name):
                return set()
            query = f"SELECT DISTINCT factor_name FROM {self.table_name}"
            df = pd.read_sql(query, self.db_manager.engine)
            return set(df['factor_name'].dropna().unique())
        except Exception as e:
            logger.warning(f"获取已存在的 factor_name 列表失败: {e}")
            return set()