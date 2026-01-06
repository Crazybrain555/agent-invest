"""
Space Signals 入库任务

专注处理 signals 数据：
1. 从 \\\\space\\signal 读取因子数据
2. 根据二级映射路由到目标表
3. 未映射的信号仅记录日志，不落表
4. 数据标准化和清洗
5. 高效入库（UPSERT）
"""

from __future__ import annotations
import os
import logging
from datetime import datetime
from typing import List, Optional, Set
from tqdm import tqdm

import pandas as pd

from src.data_service.data_loading.get_data_from_shared_disk import Get_data_from_sshared_disk
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.space_connection import SpaceConnection
from src.utils.config_loader import ConfigLoader

from src.data_service.pipelines.space_signals.mapping import FactorMapping
from src.data_service.pipelines.space_signals.table_utils import (
    generate_table_name,
    ensure_factor_table,
    get_schema_from_config
)

logger = logging.getLogger(__name__)

# 系统文件/目录，需要跳过
SYSTEM_SKIP = {'nohup.out', 'System Volume Information', 'prepare', 'high_VolVar'}


class SpaceSignalsIngest:
    """
    Space Signals 入库任务处理器
    
    功能：
    - 从 Space NAS 读取 signals 数据
    - 根据二级分类映射路由到不同的目标表
    - 未映射的 signals 记录到日志文件
    - 数据清洗和标准化
    - 高效批量入库（支持 UPSERT）
    """
    
    def __init__(self, mapping_path: str = 'configs/field_mappings/factor_mapping.yaml'):
        """
        初始化任务处理器
        
        Args:
            mapping_path: 因子分类映射文件路径
        """
        # 加载配置
        self.cfg_loader = ConfigLoader(config_dir='configs')
        self.space_cfg = self.cfg_loader.load_config("space_disk/space_config.yaml")
        
        # Space 路径配置
        self.signal_path = (self.space_cfg.get('paths') or {}).get(
            'signal_path', r'\\space\signal'
        )
        
        # 数据库 schema
        self.schema = get_schema_from_config()
        logger.info(f"Using database schema: {self.schema or 'default'}")
        
        # 初始化组件
        self.mapping = FactorMapping(mapping_path)
        self.loader = Get_data_from_sshared_disk()
        self.db = TestDBManager()
        
        # 未映射信号集合（用于日志记录）
        self.missing_signals: Set[str] = set()
        
        # 确保日志目录存在
        os.makedirs("logs", exist_ok=True)
        
        # 记录映射统计信息
        stats = self.mapping.statistics()
        logger.info(f"Loaded factor mapping: {stats['total_categories']} categories, "
                   f"{stats['total_signals']} signals")
    
    # ==================== 公共 API ====================
    
    def run_latest(self, start_date: int, end_date: int, save_mode: str = 'auto') -> bool:
        """
        处理所有可用的最新 signals
        
        Args:
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            save_mode: 保存模式 ('auto', 'append', 'update')
        
        Returns:
            True 如果有成功处理的信号，False 仅当全部失败
        """
        logger.info(f"=== Processing ALL signals: {start_date} ~ {end_date} ===")
        logger.info(f"🚀 Save mode: {save_mode}")
        
        # 获取所有可用 signals
        signals = self._list_all_signals()
        if not signals:
            logger.warning("No signals found to process")
            return True
        
        logger.info(f"Found {len(signals)} signals to process")
        
        # 处理所有 signals
        return self._process_list(signals, start_date, end_date, save_mode=save_mode)
    
    def run_specific(
        self, 
        signal_names: List[str], 
        start_date: int, 
        end_date: int,
        save_mode: str = 'auto'
    ) -> bool:
        """
        处理指定的 signals
        
        Args:
            signal_names: 信号名称列表
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            save_mode: 保存模式 ('auto', 'append', 'update')
        
        Returns:
            True 如果有成功处理的信号，False 仅当全部失败
        """
        logger.info(f"=== Processing {len(signal_names)} specific signals: "
                   f"{start_date} ~ {end_date} ===")
        logger.info(f"Signal names: {signal_names}")
        logger.info(f"🚀 Save mode: {save_mode}")
        
        return self._process_list(signal_names, start_date, end_date, save_mode=save_mode)
    
    # ==================== 内部方法 ====================
    
    def _list_all_signals(self) -> List[str]:
        """
        列出 Space NAS 上所有可用的 signals
        
        Returns:
            信号名称列表
        """
        conn = SpaceConnection(self.signal_path)
        
        if not conn.test_connection(self.signal_path):
            logger.error(f"Signal path not accessible: {self.signal_path}")
            return []
        
        # 列出所有目录
        items = conn.list_directories(self.signal_path)
        
        # 过滤系统文件
        valid_signals = [x for x in items if x not in SYSTEM_SKIP and not x.startswith('.')]
        
        logger.info(f"Found {len(valid_signals)} valid signals "
                   f"(filtered from {len(items)} items) in {self.signal_path}")
        
        return sorted(valid_signals)
    
    def _process_list(
        self, 
        signals: List[str], 
        start_date: int, 
        end_date: int,
        save_mode: str = 'auto'
    ) -> bool:
        """
        批量处理信号列表
        
        Args:
            signals: 信号名称列表
            start_date: 开始日期
            end_date: 结束日期
            save_mode: 保存模式 ('auto', 'append', 'update')
        
        Returns:
            True 如果有成功处理的信号，False 仅当全部失败
        """
        success_count = 0
        skip_count = 0
        fail_count = 0
        failed_signals = []  # 🚀 记录失败的信号
        
        # 使用进度条处理
        for signal_name in tqdm(signals, desc="Processing signals", unit="signal"):
            try:
                result = self._process_one(signal_name, start_date, end_date, save_mode=save_mode)
                
                if result == 'success':
                    success_count += 1
                elif result == 'skip':
                    skip_count += 1
                else:  # 'fail'
                    fail_count += 1
                    failed_signals.append(signal_name)  # 🚀 记录失败的信号
                    
            except Exception as e:
                logger.exception(f"Exception processing signal '{signal_name}': {e}")
                fail_count += 1
                failed_signals.append(signal_name)  # 🚀 记录失败的信号
        
        # 记录汇总
        logger.info(f"Processing completed: {success_count} success, "
                   f"{skip_count} skipped, {fail_count} failed")
        
        # 🚀 显示失败的信号列表（警告级别，不影响返回值）
        if failed_signals:
            logger.warning(f"Failed signals ({len(failed_signals)}): {failed_signals}")
            logger.warning(f"Failure reason: likely no data or data format issues")
        
        # 写入未映射日志
        self._flush_missing_log(start_date, end_date)
        
        # 🚀 只要有成功的就返回 True（容忍个别失败）
        return success_count > 0 or fail_count == 0
    
    def _process_one(
        self, 
        signal_name: str, 
        start_date: int, 
        end_date: int,
        save_mode: str = 'auto'
    ) -> str:
        """
        处理单个信号
        
        Args:
            signal_name: 信号名称
            start_date: 开始日期
            end_date: 结束日期
            save_mode: 保存模式 ('auto', 'append', 'update')
        
        Returns:
            'success': 成功入库
            'skip': 跳过（未映射或无数据）
            'fail': 失败
        """
        # 1. 检查映射
        cat = self.mapping.category_of(signal_name)
        if not cat:
            self.missing_signals.add(signal_name)
            logger.debug(f"[skip-unmapped] {signal_name}")
            return 'skip'
        
        level1, level2 = cat
        table_name = generate_table_name(level1, level2)
        
        logger.debug(f"Processing {signal_name} -> {self.schema}.{table_name} "
                    f"[{level1}/{level2 or 'N/A'}]")
        
        # 2. 读取数据
        try:
            df = self.loader.combine_data_from_disk(
                signal_name, 
                start_date=start_date, 
                end_date=end_date
            )
        except Exception as e:
            logger.error(f"[fail-read] {signal_name}: {e}")
            return 'fail'
        
        if df is None or df.empty:
            logger.debug(f"[skip-empty] {signal_name} ({start_date}~{end_date})")
            return 'skip'
        
        # 3. 数据标准化
        df_clean = self._standardize_data(df, signal_name)
        
        if df_clean.empty:
            logger.debug(f"[skip-clean-empty] {signal_name} after validation")
            return 'skip'
        
        # 4. 确保表存在
        if not ensure_factor_table(self.db, table_name, schema=self.schema):
            logger.error(f"[fail-table] Failed to ensure table: {self.schema}.{table_name}")
            return 'fail'
        
        # 🚀 5. 智能模式选择
        actual_mode = self._determine_save_mode(
            save_mode, table_name, self.schema
        )
        
        # 6. 入库
        try:
            success = self.db.save_dataframe(
                df=df_clean,
                table_name=table_name,
                schema=self.schema,
                mode=actual_mode,
                index=False,
                pk_fields=['trade_date', 'stock_code', 'factor_name'],
                batch_size=100000,
                use_parallel=True
            )
            
            if success:
                logger.info(f"[ok] {signal_name} -> {self.schema}.{table_name} "
                           f"({len(df_clean)} rows, mode={actual_mode})")
                return 'success'
            else:
                logger.error(f"[fail-save] {signal_name} -> {self.schema}.{table_name}")
                return 'fail'
                
        except Exception as e:
            logger.error(f"[fail-save] {signal_name}: {e}", exc_info=True)
            return 'fail'
    
    def _standardize_data(self, df: pd.DataFrame, signal_name: str) -> pd.DataFrame:
        """
        标准化数据格式
        
        处理步骤：
        1. 列名规范化
        2. 日期格式转换
        3. 股票代码标准化（6位补零）
        4. 数值清洗
        5. 去重
        
        Args:
            df: 原始数据
            signal_name: 信号名称
        
        Returns:
            标准化后的 DataFrame，列为 [trade_date, stock_code, factor_name, factor_value]
        """
        try:
            # 1. 识别列名（原始格式: tdate, stk_code, signal_name）
            cols = list(df.columns)
            value_col = [c for c in cols if c not in ['tdate', 'stk_code']][0]
            
            # 2. 重命名列
            df = df.rename(columns={
                'tdate': 'trade_date',
                'stk_code': 'stock_code',
                value_col: 'factor_value'
            })
            
            # 3. 添加 factor_name 列
            df['factor_name'] = signal_name
            
            # 4. 日期格式转换
            df['trade_date'] = pd.to_datetime(
                df['trade_date'], 
                format='%Y%m%d', 
                errors='coerce'
            )
            
            # 5. 股票代码标准化（转为6位字符串）
            def format_stock_code(x):
                if pd.isna(x):
                    return None
                try:
                    return str(int(float(x))).zfill(6)
                except (ValueError, TypeError):
                    return None
            
            df['stock_code'] = df['stock_code'].apply(format_stock_code)
            
            # 6. 数值清洗
            df['factor_value'] = pd.to_numeric(df['factor_value'], errors='coerce')
            
            # 7. 删除关键字段为空的记录
            df = df.dropna(subset=['trade_date', 'stock_code', 'factor_value', 'factor_name'])
            
            # 8. 选择所需列
            df = df[['trade_date', 'stock_code', 'factor_name', 'factor_value']]
            
            # 9. 去重（保留最后一条）
            df = df.drop_duplicates(
                subset=['trade_date', 'stock_code', 'factor_name'], 
                keep='last'
            )
            
            # 10. 重置索引
            df = df.reset_index(drop=True)
            
            logger.debug(f"Standardized {signal_name}: {len(df)} valid rows")
            
            return df
            
        except Exception as e:
            logger.error(f"Error standardizing data for {signal_name}: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _determine_save_mode(
        self, 
        user_mode: str, 
        table_name: str, 
        schema: Optional[str]
    ) -> str:
        """
        🚀 智能模式选择：根据用户指定和表状态决定使用哪种保存模式
        
        逻辑：
        - user_mode='append': 强制使用 append（快速，无去重）
        - user_mode='update': 强制使用 update（UPSERT，有去重）
        - user_mode='auto': 智能检测
            - 如果表不存在或为空 → 使用 append（3-5x速度提升）
            - 如果表有数据 → 使用 update（确保数据一致性）
        
        Args:
            user_mode: 用户指定模式 ('auto', 'append', 'update')
            table_name: 表名
            schema: 数据库schema
        
        Returns:
            实际使用的模式 ('append' 或 'update')
        """
        # 用户强制指定
        if user_mode in ('append', 'update'):
            logger.debug(f"Using user-specified mode: {user_mode}")
            return user_mode
        
        # auto 模式：智能检测
        try:
            # 检查表是否存在
            table_exists = self.db.check_table_exists(table_name, schema)
            
            if not table_exists:
                logger.debug(f"Table {table_name} does not exist → using 'append'")
                return 'append'
            
            # 检查表是否为空
            full_table = f"{schema}.{table_name}" if schema else table_name
            count_sql = f"SELECT COUNT(*) FROM {full_table}"
            
            from sqlalchemy import text
            with self.db.engine.connect() as conn:
                result = conn.execute(text(count_sql))
                row_count = result.scalar()
            
            if row_count == 0:
                logger.debug(f"Table {table_name} is empty ({row_count} rows) → using 'append'")
                return 'append'
            else:
                logger.debug(f"Table {table_name} has data ({row_count} rows) → using 'update'")
                return 'update'
                
        except Exception as e:
            logger.warning(f"Error detecting table state, falling back to 'update': {e}")
            return 'update'  # 出错时保守选择 update
    
    def _flush_missing_log(self, start_date: int, end_date: int):
        """
        将未映射的 signals 写入日志文件
        
        日志文件格式: logs/missing_signals_YYYYMMDD.log
        
        Args:
            start_date: 处理的开始日期
            end_date: 处理的结束日期
        """
        if not self.missing_signals:
            logger.info("All signals are mapped, no missing signals to log")
            return
        
        # 生成日志文件名（按天）
        today = datetime.now().strftime("%Y%m%d")
        log_path = os.path.join("logs", f"missing_signals_{today}.log")
        
        # 写入日志
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                # 写入时间戳和处理范围
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n{'='*80}\n")
                f.write(f"Timestamp: {timestamp}\n")
                f.write(f"Date Range: {start_date} ~ {end_date}\n")
                f.write(f"Unmapped Signals Count: {len(self.missing_signals)}\n")
                f.write(f"{'='*80}\n")
                
                # 写入未映射的信号列表（排序）
                for signal in sorted(self.missing_signals):
                    f.write(f"{signal}\n")
            
            logger.warning(f"Unmapped signals logged to: {log_path} "
                         f"(count={len(self.missing_signals)})")
            
            # 同时在控制台显示前10个未映射的信号
            if len(self.missing_signals) <= 10:
                logger.warning(f"Unmapped signals: {sorted(self.missing_signals)}")
            else:
                preview = list(sorted(self.missing_signals))[:10]
                logger.warning(f"Unmapped signals (first 10): {preview} ...")
                
        except Exception as e:
            logger.error(f"Failed to write missing signals log: {e}", exc_info=True)
    
    def get_statistics(self) -> dict:
        """
        获取处理统计信息
        
        Returns:
            统计信息字典
        """
        return {
            'mapping': self.mapping.statistics(),
            'unmapped_signals': list(sorted(self.missing_signals)),
            'unmapped_count': len(self.missing_signals)
        }

