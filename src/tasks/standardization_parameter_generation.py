import logging
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple, Union
from sqlalchemy import String, Float, Integer
from src.data_service.preprocessing.methods.standardizer import DataStandardizer
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.data_service.preprocessing.methods.norm_config import STATUS_FIELDS_FACTOR_ENG
from src.utils.logger import setup_logger
from tqdm import tqdm
import gc
import os

logger = setup_logger(__name__)

class StandardParamsGenerator:
    """标准化参数生成器 - V2版本，支持长表处理"""
    
    def __init__(self, 
                 start_date: str = "2002-01-01",
                 end_date: str = "2012-12-31",
                 source_table: Union[str, List[str]] = "inter_train_factors_mkt_processed_v1",
                 data_format: str = 'long',
                 mad_multiplier: float = 7.0,
                 batch_size: int = 50,
                 min_samples: int = 1000,
                 save_format: str = 'database',
                 skip_if_exists: bool = False,
                 use_optimized_processing: bool = True,
                 table_name_prefix: str = None,
                 factors_per_batch: int = 10):
        """
        初始化标准化参数生成器 - V3版本，支持多表合并处理
        
        Args:
            start_date: 数据开始日期 (YYYY-MM-DD)
            end_date: 数据结束日期 (YYYY-MM-DD)
            source_table: 源数据表名或表名列表，支持单表或多表，默认为长表 "inter_train_factors_mkt_processed_v1"
            data_format: 数据格式，'long'（长表）或'wide'（宽表），默认为'long'
            mad_multiplier: MAD乘数，用于异常值检测
            batch_size: 每批处理的特征组合数量
            min_samples: 每个特征组合的最小样本数，少于此数量将设为NaN
            save_format: 保存格式，'database'或'csv'，默认为'database'
            skip_if_exists: 如果标准化参数表已存在，是否跳过处理，默认为False
            use_optimized_processing: 是否使用优化的按窗口分组处理策略，默认为True
            table_name_prefix: 自定义表名前缀，如果为None则自动生成，默认None
            factors_per_batch: 大窗口内因子分批处理时每批的因子数量，避免内存溢出，默认10
        """
        self.start_date = start_date
        self.end_date = end_date
        
        # 🚀 V3新增：支持多表输入，统一转换为列表格式
        if isinstance(source_table, str):
            self.source_tables = [source_table]
            self.is_multi_table = False
        else:
            self.source_tables = list(source_table)
            self.is_multi_table = True
            
        # 保持兼容性：source_table_name指向第一个表（主要用于日志显示）
        self.source_table_name = self.source_tables[0]
        
        self.data_format = data_format
        self.mad_multiplier = mad_multiplier
        self.batch_size = batch_size
        self.min_samples = min_samples
        self.save_format = save_format
        self.skip_if_exists = skip_if_exists
        self.use_optimized_processing = use_optimized_processing
        self.factors_per_batch = factors_per_batch
        
        # 🚀 V3新增：改进的表命名规则
        mad_str = str(self.mad_multiplier).replace('.', 'p')  # 将小数点替换为'p'，如7.0 -> 7p0
        
        # 确定表名前缀
        if table_name_prefix is not None:
            # 使用自定义前缀
            prefix = table_name_prefix
        elif self.is_multi_table:
            # 多表默认前缀
            prefix = "multi_table"
        else:
            # 单表使用表名作为前缀（兼容旧版本）
            prefix = source_table
            
        self.params_table_name = f"{prefix}_std_{start_date[:4]}_{end_date[:4]}_mad{mad_str}"
        # 示例：multi_table_std_2010_2018_mad8p0 或 custom_prefix_std_2010_2018_mad8p0

        # 初始化数据库管理器和标准化器
        self.db_manager = TestDBManager()
        self.standardizer = DataStandardizer()
        
        # 🚀 新增：用于存储实际数据的最早时间
        self.actual_start_date = None
        
        # 日志记录配置信息
        if self.is_multi_table:
            logger.info(f"初始化标准化参数生成器（V3版本，多表合并）：{len(self.source_tables)}个源表")
            logger.info(f"源表列表: {self.source_tables}")
        else:
            logger.info(f"初始化标准化参数生成器（V3版本，单表）：{self.source_tables[0]}")
        logger.info(f"参数表名: {self.params_table_name}")
    
    def execute(self) -> bool:
        """执行标准化参数生成任务 - V2版本，支持长表处理"""
        try:
            logger.info(f"开始生成标准化参数，使用{self.start_date}至{self.end_date}的长表数据")
            logger.info(f"源数据表: {self.source_table_name}")
            logger.info(f"MAD乘数: {self.mad_multiplier}")
            logger.info(f"最小样本数: {self.min_samples}")
            logger.info(f"排除状态类指标: {list(STATUS_FIELDS_FACTOR_ENG)} （这些指标不进行标准化参数计算）")
            
            # 🚀 改进的数据库索引建议 - 支持多表处理
            self._provide_optimized_index_suggestions()
            
            # 0. 如果参数表已存在且设置了skip_if_exists，则跳过处理
            if self.save_format == 'database' and self.skip_if_exists:
                if self.db_manager.check_table_exists(self.params_table_name):
                    logger.info(f"标准化参数表 {self.params_table_name} 已存在，且设置了skip_if_exists=True，跳过处理")
                    return True
            elif self.save_format == 'csv' and self.skip_if_exists:
                mad_str = str(self.mad_multiplier).replace('.', 'p')  # 将小数点替换为'p'
                output_path = f"data/{self.params_table_name}.csv"
                if os.path.exists(output_path):
                    logger.info(f"标准化参数文件 {output_path} 已存在，且设置了skip_if_exists=True，跳过处理")
                    return True
            
            # 1. 检查源数据表是否存在
            if not self.db_manager.check_table_exists(self.source_table_name):
                logger.error(f"源数据表 {self.source_table_name} 不存在，请先运行市场数据因子工程任务")
                return False
            
            # 🚀 新增：获取源表中实际数据的最早时间
            logger.info("正在获取源表中实际数据的最早时间...")
            try:
                earliest_date_query = f"""
                SELECT MIN(trade_date) as earliest_date
                FROM {self.source_table_name}
                WHERE trade_date IS NOT NULL
                """
                earliest_result = self.db_manager.execute_query(earliest_date_query)
                if earliest_result and earliest_result[0][0]:
                    self.actual_start_date = earliest_result[0][0]
                    if isinstance(self.actual_start_date, str):
                        # 如果是字符串，保持原样
                        pass
                    else:
                        # 如果是datetime对象，转换为字符串
                        self.actual_start_date = self.actual_start_date.strftime('%Y-%m-%d') if hasattr(self.actual_start_date, 'strftime') else str(self.actual_start_date)
                    logger.info(f"源表 {self.source_table_name} 的最早数据时间: {self.actual_start_date}")
                else:
                    logger.warning(f"无法获取源表 {self.source_table_name} 的最早数据时间，将使用配置的start_date")
                    self.actual_start_date = self.start_date
            except Exception as e:
                logger.warning(f"获取源表最早数据时间失败: {e}，将使用配置的start_date")
                self.actual_start_date = self.start_date
            
            # 2. 获取所有特征组合
            feature_combinations = self._get_feature_combinations()
            if not feature_combinations:
                logger.error("未能获取特征组合信息")
                return False
                
            logger.info(f"成功获取特征组合信息，共{len(feature_combinations)}个特征组合")
            
            # 3. 根据配置选择处理策略
            all_standard_params = []
            insufficient_data_count = 0
            
            if self.use_optimized_processing:
                # 🚀 新策略：按表优先处理，每张表独立处理所有窗口
                logger.info("使用按表优先的处理策略，更稳定的内存控制")
                
                # 按表分组特征组合
                table_groups = {}
                for factor_name, z_window in feature_combinations:
                    combination_key = (factor_name, z_window)
                    if combination_key in self.factor_table_mapping:
                        source_table = self.factor_table_mapping[combination_key]
                        if source_table not in table_groups:
                            table_groups[source_table] = []
                        table_groups[source_table].append((factor_name, z_window))
                
                logger.info(f"按表分组完成，共 {len(table_groups)} 个源表需要处理")
                for table_name, combinations in table_groups.items():
                    logger.info(f"  表 {table_name}: {len(combinations)} 个特征组合")
                
                # 按表批次处理
                table_list = list(table_groups.items())
                total_tables = len(table_list)
                
                for i, (source_table, table_combinations) in enumerate(tqdm(table_list, desc="处理源表")):
                    logger.info(f"处理源表 {i+1}/{total_tables}: {source_table}，包含 {len(table_combinations)} 个特征组合")
                    
                    # 使用新的按表处理方法
                    table_results = self._process_table_batch_optimized(source_table, table_combinations)
                    all_standard_params.extend(table_results)
                    
                    # 统计数据量不足的组合
                    for result in table_results:
                        if pd.isna(result['upper'].iloc[0]):
                            insufficient_data_count += 1
                    
                    # 清理内存
                    gc.collect()
                    logger.info(f"源表 {source_table} 处理完成，已释放内存")
                    
            else:
                # 原有策略：逐个处理特征组合
                logger.info("使用传统的逐个特征组合处理策略")
                total_batches = (len(feature_combinations) + self.batch_size - 1) // self.batch_size
                
                for i in tqdm(range(0, len(feature_combinations), self.batch_size), desc="处理特征组合批次", total=total_batches):
                    batch_combinations = feature_combinations[i:i+self.batch_size]
                    logger.info(f"处理特征组合批次 {i//self.batch_size + 1}/{total_batches}: {len(batch_combinations)} 个组合")
                    
                    # 处理每个特征组合
                    for factor_name, z_window in batch_combinations:
                        # 获取该特征组合的数据
                        result = self._fetch_feature_data(factor_name, z_window)
                        
                        if result is None:
                            logger.warning(f"特征组合 ({factor_name}, {z_window}) 没有数据，跳过")
                            continue
                        
                        feature_data, factor_start_date = result
                        
                        if feature_data.empty:
                            logger.warning(f"特征组合 ({factor_name}, {z_window}) 没有数据，跳过")
                            continue
                        
                        # 检查数据量是否足够
                        if len(feature_data) < self.min_samples:
                            logger.warning(f"特征组合 ({factor_name}, {z_window}) 数据量不足: {len(feature_data)} < {self.min_samples}")
                            insufficient_data_count += 1
                            
                            # 添加NaN结果
                            param_row = pd.DataFrame({
                                'feature_name': [factor_name],
                                'window': [z_window],
                                'upper': [np.nan],
                                'lower': [np.nan],
                                'mean': [np.nan],
                                'std': [np.nan],
                                'sample_count': [len(feature_data)],
                                'start_date': [factor_start_date]  # 🚀 修复：使用因子特定的最早时间
                            })
                            all_standard_params.append(param_row)
                            continue
                        
                        # 计算统计参数
                        try:
                            stats = self._calculate_mad_statistics(feature_data)
                            
                            # 添加结果
                            param_row = pd.DataFrame({
                                'feature_name': [factor_name],
                                'window': [z_window],
                                'upper': [stats['upper']],
                                'lower': [stats['lower']],
                                'mean': [stats['mean']],
                                'std': [stats['std']],
                                'sample_count': [len(feature_data)],
                                'start_date': [factor_start_date]  # 🚀 修复：使用因子特定的最早时间
                            })
                            all_standard_params.append(param_row)
                            
                        except Exception as e:
                            logger.error(f"计算特征组合 ({factor_name}, {z_window}) 的统计参数失败: {str(e)}")
                            continue
                    
                    # 清理内存
                    gc.collect()
                    
                    logger.info(f"批次 {i//self.batch_size + 1} 处理完成，已释放内存")
            
            # 4. 合并所有批次的结果
            if not all_standard_params:
                logger.error("所有批次处理都失败，无法生成标准化参数")
                return False
                
            combined_params = pd.concat(all_standard_params, axis=0, ignore_index=True)
            
            # 🚀 确保所有行都包含start_date字段
            if 'start_date' not in combined_params.columns:
                combined_params['start_date'] = self.actual_start_date
                
            logger.info(f"成功合并所有批次结果，共{len(combined_params)}个特征组合")
            logger.info(f"其中 {insufficient_data_count} 个组合数据量不足，已设为NaN")
            
            # 优化数据类型以节约内存
            logger.info("优化数据类型以节约内存...")
            combined_params['feature_name'] = combined_params['feature_name'].astype('category')
            combined_params['window'] = combined_params['window'].astype('int32')
            combined_params['sample_count'] = combined_params['sample_count'].astype('int32')
            combined_params['start_date'] = combined_params['start_date'].astype('category')  # 🚀 新增字段类型优化
            # 数值列保持 float64 以确保精度
            for col in ['upper', 'lower', 'mean', 'std']:
                combined_params[col] = combined_params[col].astype('float64')
            
            # 强制垃圾回收
            del all_standard_params
            gc.collect()
            logger.info("内存优化完成")
            
            # 5. 根据save_format参数决定保存方式
            if self.save_format == 'database':
                # 保存到数据库
                success = self._save_to_database(combined_params)
                
                if success:
                    logger.info(f"标准化参数已保存到数据库表 {self.params_table_name}")
                else:
                    logger.error(f"保存标准化参数到数据库失败")
                    return False
            else:
                # 保存到CSV文件
                output_path = f"data/{self.params_table_name}.csv"
                success = self._save_to_csv(combined_params, output_path)
                
                if success:
                    logger.info(f"标准化参数已保存到CSV文件 {output_path}")
                else:
                    logger.error(f"保存标准化参数到CSV文件失败")
                    return False
                
            return True
            
        except Exception as e:
            logger.error(f"生成标准化参数失败: {str(e)}", exc_info=True)
            return False
    
    def _get_feature_combinations(self) -> List[Tuple[str, int]]:
        """获取源数据表中的特征组合 (factor_name, z_windows)，排除状态类指标
        🚀 V3版本：支持多表合并，检测字段冲突，支持无z_windows字段的表"""
        try:
            # 构建状态类字段的排除条件
            status_fields_str = "', '".join(STATUS_FIELDS_FACTOR_ENG)
            logger.info(f"排除状态类字段: {list(STATUS_FIELDS_FACTOR_ENG)}")
            
            all_combinations = []
            table_sources = {}  # 记录每个(factor_name, z_windows)组合的来源表
            conflicts = []  # 记录冲突信息
            
            # 🚀 V3新增：遍历所有源表
            for table_idx, table_name in enumerate(self.source_tables):
                logger.info(f"处理表 {table_idx + 1}/{len(self.source_tables)}: {table_name}")
                
                # 检查表是否存在
                if not self.db_manager.check_table_exists(table_name):
                    logger.error(f"源数据表 {table_name} 不存在，跳过")
                    continue
                
                # 🚀 阶段4：检查表是否有z_windows字段
                has_z_windows = self._check_table_has_z_windows(table_name)
                
                if has_z_windows:
                    # 表有z_windows字段，正常查询
                    query = f"""
                    SELECT DISTINCT factor_name, z_windows
                    FROM {table_name}
                    WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                    AND factor_name NOT IN ('{status_fields_str}')
                    ORDER BY factor_name, z_windows
                    """
                else:
                    # 🚀 阶段4：表没有z_windows字段，设置默认值0
                    logger.info(f"表 {table_name} 没有z_windows字段，将所有因子的window设置为0")
                    query = f"""
                    SELECT DISTINCT factor_name, 0 as z_windows
                    FROM {table_name}
                    WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                    AND factor_name NOT IN ('{status_fields_str}')
                    ORDER BY factor_name
                    """
                
                # 执行查询
                result = self.db_manager.execute_query(query)
                if not result:
                    logger.warning(f"表 {table_name} 无法获取特征组合信息，跳过")
                    continue
                
                # 处理查询结果，检测冲突
                table_combinations = [(row[0], row[1]) for row in result]
                logger.info(f"表 {table_name} 获取到 {len(table_combinations)} 个特征组合")
                
                # 🚀 阶段3：检测冲突
                for factor_name, z_window in table_combinations:
                    combination_key = (factor_name, z_window)
                    
                    if combination_key in table_sources:
                        # 发现冲突
                        existing_table = table_sources[combination_key]
                        conflicts.append({
                            'factor_name': factor_name,
                            'z_windows': z_window,
                            'existing_table': existing_table,
                            'conflicting_table': table_name
                        })
                        logger.warning(f"发现字段冲突: ({factor_name}, {z_window}) "
                                     f"在表 {existing_table} 和 {table_name} 中都存在，"
                                     f"将使用表 {existing_table} 的数据（优先级更高）")
                    else:
                        # 新的组合，添加到结果中
                        all_combinations.append(combination_key)
                        table_sources[combination_key] = table_name
            
            # 🚀 阶段3：输出冲突统计
            if conflicts:
                logger.warning(f"检测到 {len(conflicts)} 个字段冲突，已按表优先级处理")
                logger.info("冲突处理策略：优先使用列表中前面的表的数据")
                
                # 按因子分组输出冲突详情
                conflict_by_factor = {}
                for conflict in conflicts:
                    factor = conflict['factor_name']
                    if factor not in conflict_by_factor:
                        conflict_by_factor[factor] = []
                    conflict_by_factor[factor].append(conflict)
                
                for factor, factor_conflicts in conflict_by_factor.items():
                    logger.debug(f"因子 {factor} 的冲突详情: {factor_conflicts}")
            else:
                logger.info("未检测到字段冲突")
            
            # 🚀 记录数据来源信息
            self.factor_table_mapping = table_sources
            
            logger.info(f"多表合并完成，共获取 {len(all_combinations)} 个唯一特征组合")
            logger.info(f"涉及表数: {len(set(table_sources.values()))}")
            
            return all_combinations
            
        except Exception as e:
            logger.error(f"获取特征组合信息失败: {str(e)}", exc_info=True)
            return []
    
    def _check_table_has_z_windows(self, table_name: str) -> bool:
        """检查表是否有z_windows字段"""
        try:
            query = f"""
            SELECT column_name 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = '{table_name}'
            AND column_name = 'z_windows'
            """
            result = self.db_manager.execute_query(query)
            return len(result) > 0
        except Exception as e:
            logger.warning(f"检查表 {table_name} 的z_windows字段失败: {str(e)}")
            return False  # 发生错误时假设没有该字段
            
    def _get_feature_columns(self) -> List[str]:
        """获取源数据表中的特征列 - 兼容旧版本，已弃用"""
        logger.warning("_get_feature_columns() 方法已弃用，请使用 _get_feature_combinations() 替代")
        try:
            # 构建SQL查询
            query = f"""
            SELECT column_name 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = '{self.source_table_name}'
            """
            
            # 执行查询
            result = self.db_manager.execute_query(query)
            if not result:
                logger.error(f"无法获取表 {self.source_table_name} 的列信息")
                return []
                
            # 提取列名并过滤掉非特征列
            exclude_columns = ['trade_date', 'stock_code', 'id', 'model_version', 'insert_time', 'is_temporary']
            feature_columns = [col[0] for col in result if col[0] not in exclude_columns]
            
            return feature_columns
            
        except Exception as e:
            logger.error(f"获取特征列信息失败: {str(e)}")
            return []
    
    def _fetch_feature_data(self, factor_name: str, z_window: int) -> Optional[Tuple[pd.Series, str]]:
        """从数据库中获取特定特征组合的数据，排除状态类字段，返回数据和最早日期
        🚀 V3版本：根据factor_table_mapping从正确的源表获取数据"""
        try:
            # 检查是否为状态类字段
            if factor_name in STATUS_FIELDS_FACTOR_ENG:
                logger.debug(f"跳过状态类字段: {factor_name}")
                return None
            
            # 🚀 V3新增：查找该因子的源表
            combination_key = (factor_name, z_window)
            if combination_key not in self.factor_table_mapping:
                logger.warning(f"找不到因子 {factor_name} (窗口{z_window}) 的源表信息")
                return None
            
            source_table = self.factor_table_mapping[combination_key]
            logger.debug(f"从表 {source_table} 获取特征组合 ({factor_name}, {z_window}) 的数据")
            
            # 🚀 检查该表是否有z_windows字段
            has_z_windows = self._check_table_has_z_windows(source_table)
            
            if has_z_windows:
                # 表有z_windows字段，正常查询
                query = f"""
                SELECT factor_value, trade_date
                FROM {source_table}
                WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                AND factor_name = '{factor_name}'
                AND z_windows = {z_window}
                AND factor_value IS NOT NULL
                ORDER BY trade_date
                """
            else:
                # 🚀 表没有z_windows字段
                if z_window != 0:
                    logger.warning(f"表 {source_table} 没有z_windows字段，但请求窗口为{z_window}，应该为0")
                    return None
                query = f"""
                SELECT factor_value, trade_date
                FROM {source_table}
                WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                AND factor_name = '{factor_name}'
                AND factor_value IS NOT NULL
                ORDER BY trade_date
                """
            
            # 执行查询
            data = pd.read_sql(query, self.db_manager.engine)
            
            if data.empty:
                logger.warning(f"特征组合 ({factor_name}, {z_window}) 在表 {source_table} 中没有数据")
                return None
            
            # 计算最早日期
            earliest_date = data['trade_date'].min()
            if pd.isna(earliest_date):
                factor_start_date = self.actual_start_date
            else:
                if hasattr(earliest_date, 'strftime'):
                    factor_start_date = earliest_date.strftime('%Y-%m-%d')
                else:
                    factor_start_date = str(earliest_date)
                
            return data['factor_value'], factor_start_date
            
        except Exception as e:
            logger.error(f"获取特征数据失败: {str(e)}", exc_info=True)
            return None
            
    def _calculate_mad_statistics(self, data: pd.Series) -> Dict[str, float]:
        """计算基于MAD的统计参数"""
        try:
            # 1. 计算中位数和MAD
            median_val = data.median()
            mad_val = (data - median_val).abs().median()  # median absolute deviation
            
            # 2. 计算上下界
            upper_bound = median_val + self.mad_multiplier * mad_val
            lower_bound = median_val - self.mad_multiplier * mad_val
            
            # 3. 裁剪异常值
            clipped_data = data.clip(lower=lower_bound, upper=upper_bound)
            
            # 4. 计算均值和标准差
            mean_val = clipped_data.mean()
            std_val = clipped_data.std()
            
            return {
                'upper': upper_bound,
                'lower': lower_bound,
                'mean': mean_val,
                'std': std_val
            }
            
        except Exception as e:
            logger.error(f"计算MAD统计参数失败: {str(e)}")
            raise
            
    def _fetch_window_data_optimized(self, z_window: int, factor_names: List[str]) -> Optional[pd.DataFrame]:
        """优化的数据获取：从多个源表查询获取同一窗口的多个特征数据，自动排除状态类字段
        🚀 V3版本：支持从多表获取数据，根据factor_table_mapping确定数据来源"""
        try:
            # 过滤掉状态类字段
            filtered_factor_names = [name for name in factor_names if name not in STATUS_FIELDS_FACTOR_ENG]
            
            if not filtered_factor_names:
                logger.warning(f"窗口 {z_window} 的所有特征都是状态类字段，跳过")
                return None
            
            if len(filtered_factor_names) != len(factor_names):
                excluded_count = len(factor_names) - len(filtered_factor_names)
                logger.debug(f"窗口 {z_window} 排除了 {excluded_count} 个状态类字段")
            
            # 🚀 V3新增：按源表分组因子，分别查询
            table_factor_groups = {}
            for factor_name in filtered_factor_names:
                combination_key = (factor_name, z_window)
                if combination_key in self.factor_table_mapping:
                    source_table = self.factor_table_mapping[combination_key]
                    if source_table not in table_factor_groups:
                        table_factor_groups[source_table] = []
                    table_factor_groups[source_table].append(factor_name)
                else:
                    logger.warning(f"找不到因子 {factor_name} (窗口{z_window}) 的源表信息，跳过")
            
            if not table_factor_groups:
                logger.warning(f"窗口 {z_window} 的所有因子都没有找到源表信息")
                return None
            
            # 合并所有表的查询结果
            all_data = []
            
            for source_table, table_factor_names in table_factor_groups.items():
                factor_names_str = "', '".join(table_factor_names)
                
                # 🚀 检查该表是否有z_windows字段
                has_z_windows = self._check_table_has_z_windows(source_table)
                
                if has_z_windows:
                    # 表有z_windows字段，正常查询
                    query = f"""
                    SELECT factor_name, factor_value, trade_date
                    FROM {source_table}
                    WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                    AND z_windows = {z_window}
                    AND factor_name IN ('{factor_names_str}')
                    AND factor_value IS NOT NULL
                    ORDER BY factor_name, trade_date
                    """
                else:
                    # 🚀 表没有z_windows字段，只有window=0的因子才会查询这种表
                    if z_window != 0:
                        logger.warning(f"表 {source_table} 没有z_windows字段，但请求窗口为{z_window}，跳过")
                        continue
                    query = f"""
                    SELECT factor_name, factor_value, trade_date
                    FROM {source_table}
                    WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                    AND factor_name IN ('{factor_names_str}')
                    AND factor_value IS NOT NULL
                    ORDER BY factor_name, trade_date
                    """
                
                logger.debug(f"从表 {source_table} 获取 {len(table_factor_names)} 个因子数据（窗口{z_window}）")
                
                # 执行查询
                try:
                    table_data = pd.read_sql(query, self.db_manager.engine)
                    
                    if not table_data.empty:
                        all_data.append(table_data)
                        logger.debug(f"表 {source_table} 窗口 {z_window} 查询到 {len(table_data)} 行数据")
                    else:
                        logger.debug(f"表 {source_table} 窗口 {z_window} 没有数据")
                        
                except Exception as e:
                    logger.error(f"查询表 {source_table} 窗口 {z_window} 数据时出错: {e}")
                    # 如果是内存相关错误，记录更详细信息
                    if "memory" in str(e).lower() or "allocate" in str(e).lower():
                        logger.error(f"内存不足！表 {source_table} 窗口 {z_window} 包含 {len(table_factor_names)} 个因子，"
                                   f"请减小 factors_per_batch 参数（当前值：{getattr(self, 'factors_per_batch', 10)}）")
                    continue
            
            # 合并所有数据
            if all_data:
                combined_data = pd.concat(all_data, ignore_index=True)
                logger.debug(f"窗口 {z_window} 从 {len(table_factor_groups)} 个表合并得到 {len(combined_data)} 行数据")
                return combined_data
            else:
                logger.warning(f"窗口 {z_window} 的特征组合没有数据")
                return None
                
        except Exception as e:
            logger.error(f"批量获取特征数据失败: {str(e)}", exc_info=True)
            return None
    
    def _fetch_single_table_window_data(self, source_table: str, z_window: int, factor_names: List[str]) -> Optional[pd.DataFrame]:
        """简化版：从单个源表获取指定窗口的多个特征数据，避免复杂的多表逻辑"""
        try:
            # 过滤掉状态类字段
            filtered_factor_names = [name for name in factor_names if name not in STATUS_FIELDS_FACTOR_ENG]
            
            if not filtered_factor_names:
                logger.warning(f"表 {source_table} 窗口 {z_window} 的所有特征都是状态类字段，跳过")
                return None
            
            if len(filtered_factor_names) != len(factor_names):
                excluded_count = len(factor_names) - len(filtered_factor_names)
                logger.debug(f"表 {source_table} 窗口 {z_window} 排除了 {excluded_count} 个状态类字段")
            
            # 构建因子名称字符串
            factor_names_str = "', '".join(filtered_factor_names)
            
            # 🚀 检查该表是否有z_windows字段
            has_z_windows = self._check_table_has_z_windows(source_table)
            
            if has_z_windows:
                # 表有z_windows字段，正常查询
                query = f"""
                SELECT factor_name, factor_value, trade_date
                FROM {source_table}
                WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                AND z_windows = {z_window}
                AND factor_name IN ('{factor_names_str}')
                AND factor_value IS NOT NULL
                ORDER BY factor_name, trade_date
                """
            else:
                # 🚀 表没有z_windows字段，只有window=0的因子才会查询这种表
                if z_window != 0:
                    logger.warning(f"表 {source_table} 没有z_windows字段，但请求窗口为{z_window}，跳过")
                    return None
                query = f"""
                SELECT factor_name, factor_value, trade_date
                FROM {source_table}
                WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
                AND factor_name IN ('{factor_names_str}')
                AND factor_value IS NOT NULL
                ORDER BY factor_name, trade_date
                """
            
            logger.debug(f"从表 {source_table} 获取 {len(filtered_factor_names)} 个因子数据（窗口{z_window}）")
            
            # 执行查询，包含内存错误处理
            try:
                table_data = pd.read_sql(query, self.db_manager.engine)
                
                if not table_data.empty:
                    logger.debug(f"表 {source_table} 窗口 {z_window} 查询到 {len(table_data)} 行数据")
                    return table_data
                else:
                    logger.debug(f"表 {source_table} 窗口 {z_window} 没有数据")
                    return None
                    
            except Exception as e:
                logger.error(f"查询表 {source_table} 窗口 {z_window} 数据时出错: {e}")
                # 如果是内存相关错误，记录更详细信息
                if "memory" in str(e).lower() or "allocate" in str(e).lower():
                    logger.error(f"内存不足！表 {source_table} 窗口 {z_window} 包含 {len(filtered_factor_names)} 个因子，"
                               f"请减小 factors_per_batch 参数（当前值：{self.factors_per_batch}）")
                return None
                
        except Exception as e:
            logger.error(f"从表 {source_table} 获取特征数据失败: {str(e)}", exc_info=True)
            return None
            
    def _process_table_batch_optimized(self, source_table: str, table_combinations: List[Tuple[str, int]]) -> List[pd.DataFrame]:
        """按表优先的批处理：处理单个源表的所有特征组合，支持因子分批避免内存溢出"""
        all_results = []
        
        # 按窗口分组该表的特征组合，便于批量查询
        window_groups = {}
        for factor_name, z_window in table_combinations:
            if z_window not in window_groups:
                window_groups[z_window] = []
            window_groups[z_window].append(factor_name)
        
        logger.info(f"表 {source_table} 包含 {len(window_groups)} 个不同窗口: {sorted(window_groups.keys())}")
        
        # 逐个处理该表的每个窗口
        for z_window, factor_names in tqdm(window_groups.items(), desc=f"处理{source_table}的窗口", leave=False):
            logger.info(f"  处理表 {source_table} 窗口 {z_window}，包含 {len(factor_names)} 个因子")
            
            # 🚀 使用因子分批避免内存溢出
            max_factors_per_batch = self.factors_per_batch
            
            if len(factor_names) <= max_factors_per_batch:
                # 因子数量不多，直接处理
                window_data = self._fetch_single_table_window_data(source_table, z_window, factor_names)
                
                if window_data is None or window_data.empty:
                    logger.warning(f"表 {source_table} 窗口 {z_window} 没有数据，跳过")
                    continue
                
                # 复用现有的向量化处理方法
                window_results = self._process_window_batch_vectorized(z_window, window_data)
                
                if window_results:
                    window_df = pd.DataFrame(window_results)
                    all_results.append(window_df)
            else:
                # 因子数量太多，分批处理避免内存溢出
                logger.info(f"    窗口 {z_window} 因子数量较多({len(factor_names)}个)，将分为 {(len(factor_names) + max_factors_per_batch - 1) // max_factors_per_batch} 批处理")
                
                for i in range(0, len(factor_names), max_factors_per_batch):
                    batch_factor_names = factor_names[i:i + max_factors_per_batch]
                    batch_num = i // max_factors_per_batch + 1
                    total_batches = (len(factor_names) + max_factors_per_batch - 1) // max_factors_per_batch
                    
                    logger.info(f"    处理表{source_table}窗口{z_window}子批次 {batch_num}/{total_batches}：{len(batch_factor_names)}个因子")
                    
                    # 获取该批次的数据
                    batch_data = self._fetch_single_table_window_data(source_table, z_window, batch_factor_names)
                    
                    if batch_data is None or batch_data.empty:
                        logger.warning(f"表 {source_table} 窗口 {z_window} 子批次 {batch_num} 没有数据，跳过")
                        continue
                    
                    # 复用现有的向量化处理方法
                    batch_results = self._process_window_batch_vectorized(z_window, batch_data)
                    
                    if batch_results:
                        batch_df = pd.DataFrame(batch_results)
                        all_results.append(batch_df)
                    
                    # 强制清理内存
                    del batch_data
                    gc.collect()
        
        logger.info(f"表 {source_table} 所有窗口处理完成，共生成 {len(all_results)} 个结果批次")
        return all_results
    
    def _process_window_batch_optimized(self, window_groups: Dict[int, List[str]]) -> List[pd.DataFrame]:
        """优化的窗口批处理：按窗口分组减少数据库查询次数，支持大窗口的因子分批处理"""
        all_results = []
        
        # 使用 tqdm 显示窗口级进度
        for z_window, factor_names in tqdm(window_groups.items(), desc="处理窗口", disable=False):
            logger.info(f"处理窗口 {z_window}，包含 {len(factor_names)} 个特征")
            
            # 🚀 新增：大窗口分批处理，避免内存溢出
            max_factors_per_batch = self.factors_per_batch  # 每批处理的因子数量，可配置
            
            if len(factor_names) <= max_factors_per_batch:
                # 因子数量不多，直接处理
                window_data = self._fetch_window_data_optimized(z_window, factor_names)
                
                if window_data is None or window_data.empty:
                    logger.warning(f"窗口 {z_window} 没有数据，跳过")
                    continue
                
                # 向量化处理同一窗口的所有特征
                window_results = self._process_window_batch_vectorized(z_window, window_data)
                
                if window_results:
                    # 转换为DataFrame
                    window_df = pd.DataFrame(window_results)
                    all_results.append(window_df)
            else:
                # 因子数量太多，分批处理避免内存溢出
                logger.info(f"窗口 {z_window} 因子数量较多({len(factor_names)}个)，将分为 {(len(factor_names) + max_factors_per_batch - 1) // max_factors_per_batch} 批处理")
                
                for i in range(0, len(factor_names), max_factors_per_batch):
                    batch_factor_names = factor_names[i:i + max_factors_per_batch]
                    batch_num = i // max_factors_per_batch + 1
                    total_batches = (len(factor_names) + max_factors_per_batch - 1) // max_factors_per_batch
                    
                    logger.info(f"  处理窗口{z_window}子批次 {batch_num}/{total_batches}：{len(batch_factor_names)}个因子")
                    
                    # 获取该批次的数据
                    batch_data = self._fetch_window_data_optimized(z_window, batch_factor_names)
                    
                    if batch_data is None or batch_data.empty:
                        logger.warning(f"窗口 {z_window} 子批次 {batch_num} 没有数据，跳过")
                        continue
                    
                    # 向量化处理该批次的特征
                    batch_results = self._process_window_batch_vectorized(z_window, batch_data)
                    
                    if batch_results:
                        # 转换为DataFrame并添加到结果
                        batch_df = pd.DataFrame(batch_results)
                        all_results.append(batch_df)
                    
                    # 强制清理内存
                    del batch_data
                    gc.collect()
                    
                logger.info(f"窗口 {z_window} 所有子批次处理完成")
        
        return all_results

    def _process_window_batch_vectorized(self, z_window: int, window_data: pd.DataFrame) -> List[Dict[str, Any]]:
        """向量化的窗口批处理：一次性处理同一窗口的所有特征，为每个因子计算最早出现时间"""
        if window_data.empty:
            return []
        
        results = []
        
        # 按 factor_name 分组处理
        for factor_name, group in window_data.groupby('factor_name'):
            factor_values = group['factor_value'].dropna()
            sample_count = len(factor_values)
            
            # 🚀 修复：计算该因子的最早出现时间
            factor_earliest_date = group['trade_date'].min()
            # 转换为字符串格式
            if pd.isna(factor_earliest_date):
                factor_start_date = self.actual_start_date
            else:
                if hasattr(factor_earliest_date, 'strftime'):
                    factor_start_date = factor_earliest_date.strftime('%Y-%m-%d')
                else:
                    factor_start_date = str(factor_earliest_date)
            
            if sample_count < self.min_samples:
                # 数据量不足，设置为NaN
                results.append({
                    'feature_name': factor_name,
                    'window': z_window,
                    'upper': np.nan,
                    'lower': np.nan,
                    'mean': np.nan,
                    'std': np.nan,
                    'sample_count': sample_count,
                    'start_date': factor_start_date  # 🚀 修复：使用因子特定的最早时间
                })
            else:
                # 向量化计算统计参数
                # 计算中位数和MAD
                median_val = np.nanmedian(factor_values)
                mad_val = np.nanmedian(np.abs(factor_values - median_val))
                
                # 计算上下界
                upper_bound = median_val + self.mad_multiplier * mad_val
                lower_bound = median_val - self.mad_multiplier * mad_val
                
                # 裁剪异常值
                clipped_data = np.clip(factor_values, lower_bound, upper_bound)
                
                # 计算均值和标准差
                mean_val = np.nanmean(clipped_data)
                std_val = np.nanstd(clipped_data)
                
                results.append({
                    'feature_name': factor_name,
                    'window': z_window,
                    'upper': upper_bound,
                    'lower': lower_bound,
                    'mean': mean_val,
                    'std': std_val,
                    'sample_count': sample_count,
                    'start_date': factor_start_date  # 🚀 修复：使用因子特定的最早时间
                })
        
        return results
            
    def _fetch_normalized_data(self, feature_columns: List[str]) -> Optional[pd.DataFrame]:
        """从数据库中获取指定日期范围和特征的归一化数据 - 兼容旧版本，已弃用"""
        logger.warning("_fetch_normalized_data() 方法已弃用，请使用 _fetch_feature_data() 替代")
        try:
            # 构建SQL查询，只选择指定的特征列
            columns_str = ", ".join(feature_columns)
            
            query = f"""
            SELECT {columns_str}
            FROM {self.source_table_name}
            WHERE trade_date BETWEEN '{self.start_date}' AND '{self.end_date}'
            ORDER BY trade_date
            """
            
            logger.info(f"从数据库获取{self.start_date}至{self.end_date}的归一化数据，选择特征列: {len(feature_columns)}个")
            
            # 执行查询
            data = pd.read_sql(query, self.db_manager.engine)
            
            return data
            
        except Exception as e:
            logger.error(f"获取归一化数据失败: {str(e)}")
            return None
            
    def _save_to_database(self, standard_params: pd.DataFrame) -> bool:
        """保存标准化参数到数据库 - V2版本，支持窗口字段"""
        try:
            logger.info(f"正在保存标准化参数到数据库表 {self.params_table_name}...")
            
            # 检查表是否存在
            if self.db_manager.check_table_exists(self.params_table_name):
                logger.info(f"标准化参数表 {self.params_table_name} 已存在，将删除并重新创建")
                if not self.db_manager.delete_table(self.params_table_name):
                    logger.error(f"删除现有标准化参数表 {self.params_table_name} 失败")
                    return False
            
            # 创建新表 - V2版本的列定义，包含窗口字段和start_date字段
            columns = [
                {'name': 'feature_name', 'type': String(100), 'primary_key': True},
                {'name': 'window', 'type': Integer, 'primary_key': True},
                {'name': 'upper', 'type': Float, 'nullable': True},
                {'name': 'lower', 'type': Float, 'nullable': True},
                {'name': 'mean', 'type': Float, 'nullable': True},
                {'name': 'std', 'type': Float, 'nullable': True},
                {'name': 'sample_count', 'type': Integer, 'nullable': True},
                {'name': 'start_date', 'type': String(10), 'nullable': True}  # 🚀 新增start_date字段
            ]
            
            if not self.db_manager.create_table(self.params_table_name, columns):
                logger.error(f"创建标准化参数表 {self.params_table_name} 失败")
                return False
            
            # 数据类型转换和验证
            numeric_columns = ['upper', 'lower', 'mean', 'std']
            
            # 1. 将numpy的NaN转换为None，但保留在DataFrame中用于数据库保存
            for col in numeric_columns:
                # 对于包含NaN的数值列，pandas会自动处理
                standard_params[col] = standard_params[col].astype('float64')
            
            # 2. 确保字符串和整数类型正确
            standard_params['feature_name'] = standard_params['feature_name'].astype(str)
            standard_params['window'] = standard_params['window'].astype(int)
            standard_params['sample_count'] = standard_params['sample_count'].astype(int)
            standard_params['start_date'] = standard_params['start_date'].astype(str)  # 🚀 确保start_date为字符串类型
            
            # 使用TestDBManager的save_dataframe方法保存数据
            success = self.db_manager.save_dataframe(
                df=standard_params,
                table_name=self.params_table_name,
                mode='append',
                index=False,
                batch_size=1000  # 每批保存1000行
            )
            
            if success:
                logger.info(f"成功保存标准化参数到数据库表 {self.params_table_name}")
                # 记录统计信息
                total_count = len(standard_params)
                nan_count = standard_params[numeric_columns].isna().any(axis=1).sum()
                valid_count = total_count - nan_count
                logger.info(f"保存统计: 总计 {total_count} 个特征组合，有效 {valid_count} 个，数据不足 {nan_count} 个")
                logger.info(f"数据最早时间: {self.actual_start_date}")  # 🚀 记录实际最早时间
            else:
                logger.error(f"保存标准化参数到数据库失败")
                
            return success
            
        except Exception as e:
            logger.error(f"保存标准化参数到数据库失败: {str(e)}")
            return False

    def _save_to_csv(self, standard_params: pd.DataFrame, output_path: str) -> bool:
        """保存标准化参数到CSV文件 - V2版本，支持窗口字段"""
        try:
            logger.info(f"正在保存标准化参数到CSV文件 {output_path}...")
            
            # 确保目录存在
            os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
            
            # 将DataFrame保存为CSV文件
            standard_params.to_csv(output_path, index=False)
            
            # 记录统计信息
            total_count = len(standard_params)
            numeric_columns = ['upper', 'lower', 'mean', 'std']
            nan_count = standard_params[numeric_columns].isna().any(axis=1).sum()
            valid_count = total_count - nan_count
            
            logger.info(f"成功保存标准化参数到CSV文件 {output_path}")
            logger.info(f"保存统计: 总计 {total_count} 个特征组合，有效 {valid_count} 个，数据不足 {nan_count} 个")
            logger.info(f"数据最早时间: {self.actual_start_date}")  # 🚀 记录实际最早时间
            return True
            
        except Exception as e:
            logger.error(f"保存标准化参数到CSV文件失败: {str(e)}")
            return False

    def _provide_optimized_index_suggestions(self):
        """提供优化的数据库索引建议，支持多表处理"""
        logger.info("性能优化建议：为提升查询性能，请确保源表有适当的复合索引")
        logger.info("索引建议基于实际查询模式分析，可显著提升标准化参数生成速度")
        
        for table_idx, table_name in enumerate(self.source_tables, 1):
            if not self.db_manager.check_table_exists(table_name):
                logger.warning(f"表 {table_name} 不存在，跳过索引建议")
                continue
                
            # 检查表是否有z_windows字段
            has_z_windows = self._check_table_has_z_windows(table_name)
            
            if has_z_windows:
                # 有z_windows字段的表：建议三字段复合索引
                index_name = f"idx_factor_processing_{table_name.replace('.', '_')}"
                logger.info(f"表 {table_idx}/{len(self.source_tables)} - {table_name} (有z_windows字段):")
                logger.info(f"  CREATE INDEX {index_name} ON {table_name} (z_windows, factor_name, trade_date);")
                logger.info("  └─ 用途: 优化按窗口+因子+时间范围的查询")
            else:
                # 没有z_windows字段的表：建议二字段复合索引
                index_name = f"idx_factor_processing_{table_name.replace('.', '_')}"  
                logger.info(f"表 {table_idx}/{len(self.source_tables)} - {table_name} (无z_windows字段):")
                logger.info(f"  CREATE INDEX {index_name} ON {table_name} (factor_name, trade_date);")
                logger.info("  └─ 用途: 优化按因子+时间范围的查询")
        
        logger.info("索引创建建议:")
        logger.info("  1. 在数据库维护窗口执行索引创建，避免影响生产查询")
        logger.info("  2. 大表创建索引可能耗时较长，可考虑使用CONCURRENTLY选项")
        logger.info("  3. 创建前可用 EXPLAIN ANALYZE 验证查询计划改进效果")