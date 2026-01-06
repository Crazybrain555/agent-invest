"""
Test database operations manager for handling data operations in the test database.
This module provides functionality for saving, updating, and deleting data in the test database.

🚀 Performance Optimizations (2024):
- SQLAlchemy 2.0 style session management
- Optimized connection pool configuration  
- Enhanced COPY operations with psycopg3 support
- PyArrow integration for faster data processing
"""

import pandas as pd
import numpy as np
from typing import Optional, Union, List, Dict, Any
from sqlalchemy import Table, MetaData, Column, Integer, String, Numeric, Date, text, inspect, Float
from sqlalchemy.orm import Session
from src.utils.db_connection import db_config, Base
import logging
from tqdm import tqdm
import time
import concurrent.futures
import io
import psycopg2
from sqlalchemy.dialects.postgresql import insert
import hashlib
import random
import threading

# 🚀 尝试导入psycopg3，如果不可用则降级到psycopg2
try:
    import psycopg
    PSYCOPG3_AVAILABLE = True
    PSYCOPG_MODULE = psycopg
except ImportError:
    import psycopg2 as psycopg
    PSYCOPG3_AVAILABLE = False
    PSYCOPG_MODULE = psycopg

# 🚀 尝试导入PyArrow，用于加速pandas操作
try:
    import pyarrow as pa
    import pyarrow.csv
    PYARROW_AVAILABLE = True
except ImportError:
    PYARROW_AVAILABLE = False


class TestDBManager:
    """Test database operations manager with 2024 performance optimizations"""
    
    # 🚀 性能优化：单次UPSERT的最大行数阈值
    DEFAULT_UPSERT_BATCH_ROWS = 10_000_000  # 提升到1000万行为一批，提高大数据集处理效率
    
    def __init__(self):
        """Initialize the test database manager"""
        self.logger = logging.getLogger(__name__)
        self.engine = db_config.get_test_engine()
        # 缓存已创建的索引，避免重复检查和创建
        self._index_cache = set()
        
        # 🚀 记录可用的性能优化特性
        self.logger.info(f"Performance features - psycopg3: {PSYCOPG3_AVAILABLE}, PyArrow: {PYARROW_AVAILABLE}")
        
    def check_table_exists(self, table_name: str, schema: Optional[str] = None) -> bool:
        """
        Check if a table exists in the database
        
        Args:
            table_name: Name of the table to check
            schema: Database schema name
            
        Returns:
            bool: True if table exists, False otherwise
        """
        inspector = inspect(self.engine)
        return table_name in inspector.get_table_names(schema=schema)
            
    def execute_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[tuple]:
        """
        Execute a SQL query and return the results
        
        Args:
            query: SQL query to execute
            params: Parameters for the query
            
        Returns:
            List[tuple]: Query results as a list of tuples
        """
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(query), params or {})
                return result.fetchall()
        except Exception as e:
            self.logger.error(f"Error executing query: {str(e)}")
            return []
            
    def save_dataframe(
        self,
        df: pd.DataFrame,
        table_name: str,
        mode: str = 'append',
        if_exists: str = 'append',
        index: bool = False,
        schema: Optional[str] = None,
        batch_size: int = 10000,
        use_parallel: bool = False,
        max_workers: int = 4,
        pk_fields: Optional[list] = None,
        upsert_batch_rows: Optional[int] = None
    ) -> bool:
        """
        Save DataFrame to test database
        
        Args:
            df: DataFrame to save
            table_name: Name of the target table
            mode: Operation mode ('append', 'replace', 'update')
            if_exists: How to behave if table exists ('fail', 'replace', 'append')
            index: Whether to include DataFrame index
            schema: Database schema name
            batch_size: Number of records to save in each batch
            use_parallel: Whether to use parallel processing for batch saving
            max_workers: Maximum number of parallel workers to use
            pk_fields: List of columns to use as primary key for deduplication (optional)
            upsert_batch_rows: Maximum rows per UPSERT batch (optional, defaults to 2M rows)
            
        Returns:
            bool: True if operation successful, False otherwise
        """
        try:
            total_rows = len(df)
            start_time = time.time()
            self.logger.info(f"开始保存数据到表 {table_name}，共 {total_rows} 行数据")
            
            if total_rows == 0:
                self.logger.warning("DataFrame为空，无需保存")
                return True
            
            # --------- 优化后的 update：临时表 + UPSERT ----------
            if mode == 'update':
                # 默认使用 trade_date 和 stock_code 作为去重键
                conflict_cols = pk_fields if pk_fields else ["trade_date", "stock_code"]
                self.logger.info(f"使用冲突列进行去重: {conflict_cols}")
                
                # 确保表存在，不存在则创建
                if not self.check_table_exists(table_name, schema):
                    self.logger.info(f"表 {table_name} 不存在，自动创建表结构")
                    df.head(0).to_sql(
                        name=table_name,
                        con=self.engine,
                        if_exists='replace',
                        index=False,
                        schema=schema
                    )
                
                # 🚀 关键修复：无论表是否已存在，都要确保唯一索引存在
                # 这解决了表已存在但缺少唯一索引导致UPSERT失败的问题
                # 🚀 使用带数据的索引确保方法，传递数据用于优化去重
                self._ensure_unique_index_with_data(table_name, schema, conflict_cols, df)
                
                # 将数据按冲突列是否有空值拆分
                conflict_cols_na = df[conflict_cols].isna().any(axis=1)
                df_complete = df[~conflict_cols_na]
                df_incomplete = df[conflict_cols_na]
                
                # 处理冲突列完整的数据 - 使用 UPSERT
                if not df_complete.empty:
                    self.logger.info(f"处理 {len(df_complete)} 行冲突列完整的数据")
                    
                    # 如果数据量很大，分批处理UPSERT以避免单次事务过大
                    batch_rows = upsert_batch_rows or self.DEFAULT_UPSERT_BATCH_ROWS
                    if len(df_complete) > batch_rows:
                        self.logger.info(f"数据量大于 {batch_rows} 行，将分批进行UPSERT")
                        self._upsert_in_batches(
                            df=df_complete,
                            table_name=table_name,
                            schema=schema,
                            pk_fields=conflict_cols,
                            batch_rows=batch_rows,
                            copy_batch_size=batch_size,
                        )
                    else:
                        self._upsert_with_temp(
                            df=df_complete,
                            table_name=table_name,
                            schema=schema,
                            pk_fields=conflict_cols,
                            batch_size=batch_size,
                            skip_index_check=False,  # 🚀 确保执行索引检查和去重
                        )
                
                # 处理含 NaN 的数据 - 直接追加
                if not df_incomplete.empty:
                    self.logger.info(f"处理 {len(df_incomplete)} 行含 NaN 冲突列的数据（直接追加）")
                    df_incomplete.to_sql(
                        name=table_name,
                        con=self.engine,
                        if_exists="append",
                        index=False,
                        schema=schema,
                        method="multi"
                    )
                
                elapsed = time.time() - start_time
                self.logger.info(
                    f"成功保存数据到表 {table_name}，用时 {elapsed:.2f}s，"
                    f"平均 {len(df)/elapsed:.1f} 行/秒"
                )
                return True   # update 完事直接返回
            
            # 删除后插入（append模式）
            mode_to_use = 'append' if mode == 'update' else mode
            if mode_to_use == 'append' or mode_to_use == 'replace':
                if mode_to_use == 'replace' and self.check_table_exists(table_name, schema):
                    self.logger.info(f"删除现有表 {table_name}")
                    self.delete_table(table_name, schema)
                    
                # 调整最佳批处理大小 - 对于较小的数据集不需要分批
                if batch_size > total_rows:
                    batch_size = total_rows
                else:
                    # 根据数据量自动调整批处理大小
                    if total_rows > 1000000:  # 超过100万行
                        batch_size = 50000
                    elif total_rows > 100000:  # 超过10万行
                        batch_size = 20000
                    # 否则使用默认值
                
                # 优化数值型列，减少内存占用
                for col in df.select_dtypes(include=['float']).columns:
                    df[col] = df[col].astype('float32')
                    
                # 检查是否需要批量处理
                if total_rows > batch_size:
                    # 批量处理
                    num_batches = int(np.ceil(total_rows / batch_size))
                    self.logger.info(f"将数据分为 {num_batches} 批处理，每批 {batch_size} 行")
                    
                    # 尝试检测数据库类型并选择最优的批量插入方法
                    is_postgresql = 'postgresql' in str(self.engine.url).lower()
                    
                    if is_postgresql and total_rows > 50000:
                        # 对于PostgreSQL，使用更高效的COPY命令
                        self._save_with_copy(df, table_name, mode, schema, batch_size)
                    elif use_parallel and num_batches > 4:
                        # 使用并行处理批量插入
                        self._save_parallel(df, table_name, mode, index, if_exists, schema, batch_size, max_workers, num_batches)
                    else:
                        # 顺序批量处理
                        for i in tqdm(range(num_batches), desc="保存数据进度", unit="批"):
                            batch_start = i * batch_size
                            batch_end = min((i + 1) * batch_size, total_rows)
                            batch_df = df.iloc[batch_start:batch_end]
                            
                            # 保存批次数据
                            if i == 0 and mode == 'replace':
                                # 第一批替换或创建表
                                current_if_exists = 'replace'
                            else:
                                # 后续批次追加数据
                                current_if_exists = 'append'
                            
                            batch_df.to_sql(
                                name=table_name,
                                con=self.engine,
                                if_exists=current_if_exists,
                                index=index,
                                schema=schema,
                                method='multi'  # 使用更快的多行插入
                            )
                else:
                    # 数据量较小，直接保存
                    df.to_sql(
                        name=table_name,
                        con=self.engine,
                        if_exists=if_exists,
                        index=index,
                        schema=schema,
                        method='multi'  # 使用更快的多行插入
                    )
                    
                elapsed_time = time.time() - start_time
                self.logger.info(f"成功{mode}数据到表 {table_name}，用时 {elapsed_time:.2f} 秒，"
                              f"平均速度: {total_rows/elapsed_time:.1f}行/秒")
                
           
                
            return True
                
        except Exception as e:
            self.logger.error(f"Error saving data to table {table_name}: {str(e)}")
            return False

    def _save_with_copy(self, df, table_name, mode, schema, batch_size):
        """
        使用PostgreSQL的COPY命令高效保存数据
        
        这是对大数据集最快的插入方法，可以比常规INSERT快5-10倍
        """
        conn_string = str(self.engine.url)
        total_rows = len(df)
        
        # 获取当前连接信息
        if 'postgresql' not in conn_string.lower():
            raise ValueError("COPY命令只适用于PostgreSQL数据库")
            
        try:
            # 建立直接的psycopg2连接以使用COPY
            conn_info = self.engine.url
            conn_params = {
                'host': conn_info.host,
                'port': conn_info.port or 5432,
                'database': conn_info.database,
                'user': conn_info.username,
                'password': conn_info.password
            }
            
            conn = psycopg2.connect(**conn_params)
            conn.autocommit = False
            cursor = conn.cursor()
            
            # 第一次执行可能需要先DROP表
            if mode == 'replace':
                if schema:
                    cursor.execute(f"DROP TABLE IF EXISTS {schema}.{table_name}")
                else:
                    cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                conn.commit()
            
            # 创建表（使用to_sql一次性创建，但不插入数据）
            df.head(0).to_sql(
                name=table_name,
                con=self.engine,
                if_exists='append' if mode == 'append' else 'replace',
                index=False,
                schema=schema
            )
            
            # 获取列名
            columns = df.columns
            
            # 使用COPY命令批量写入
            num_batches = int(np.ceil(total_rows / batch_size))
            full_table = f"{schema}.{table_name}" if schema else table_name
            
            for i in tqdm(range(num_batches), desc="COPY数据进度", unit="批"):
                batch_start = i * batch_size
                batch_end = min((i + 1) * batch_size, total_rows)
                batch = df.iloc[batch_start:batch_end]
                
                # 将DataFrame转为CSV格式内存字符串
                csv_file = io.StringIO()
                batch.to_csv(csv_file, header=False, index=False, sep=',')
                csv_file.seek(0)
                
                # 开始COPY操作
                cursor.copy_expert(
                    f"COPY {full_table} ({','.join(columns)}) FROM STDIN WITH CSV",
                    csv_file
                )
                
                # 定期提交以避免事务过大
                conn.commit()
                
            # 最终提交
            conn.commit()
            cursor.close()
            conn.close()
            
        except Exception as e:
            self.logger.error(f"COPY命令失败: {str(e)}")
            raise
        
    def _save_parallel(self, df, table_name, mode, index, if_exists, schema, batch_size, max_workers, num_batches):
        """使用并行处理保存数据批次"""
        total_rows = len(df)
        processed_rows = 0
        
        # 定义每个工作线程的任务
        def process_batch(batch_idx):
            try:
                # 创建独立的数据库连接
                engine = db_config.get_test_engine()
                
                batch_start = batch_idx * batch_size
                batch_end = min((batch_idx + 1) * batch_size, total_rows)
                batch_df = df.iloc[batch_start:batch_end]
                
                # 确定使用replace还是append
                current_if_exists = 'replace' if batch_idx == 0 and mode == 'replace' else 'append'
                
                batch_df.to_sql(
                    name=table_name,
                    con=engine,
                    if_exists=current_if_exists,
                    index=index,
                    schema=schema,
                    method='multi'
                )
                
                return batch_end - batch_start
            except Exception as e:
                self.logger.error(f"并行处理批次 {batch_idx} 失败: {str(e)}")
                raise
        
        # 使用线程池并行处理批次
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_batch, i) for i in range(num_batches)]
            
            # 显示进度
            for i, future in enumerate(tqdm(concurrent.futures.as_completed(futures), 
                                        total=num_batches, 
                                        desc="并行保存数据")):
                try:
                    batch_count = future.result()
                    processed_rows += batch_count
                except Exception as e:
                    self.logger.error(f"批次处理异常: {str(e)}")

    def delete_table(self, table_name: str, schema: Optional[str] = None) -> bool:
        """
        Delete a table from the test database
        
        Args:
            table_name: Name of the table to delete
            schema: Database schema name
            
        Returns:
            bool: True if operation successful, False otherwise
        """
        try:
            if not self.check_table_exists(table_name, schema):
                self.logger.warning(f"Table {table_name} does not exist, skipping deletion")
                return True
                
            with self.engine.begin() as conn:
                full_table_name = f"{schema}.{table_name}" if schema else table_name
                conn.execute(text(f"DROP TABLE IF EXISTS {full_table_name}"))
                self.logger.info(f"Successfully deleted table {full_table_name}")
                return True
                
        except Exception as e:
            self.logger.error(f"Error deleting table {table_name}: {str(e)}")
            return False
            
    def delete_data(
        self,
        table_name: str,
        conditions: Dict[str, Any],
        schema: Optional[str] = None
    ) -> bool:
        """
        Delete data from a table based on conditions
        
        Args:
            table_name: Name of the table
            conditions: Dictionary of column-value pairs for deletion conditions
            schema: Database schema name
            
        Returns:
            bool: True if operation successful, False otherwise
        """
        try:
            if not self.check_table_exists(table_name, schema):
                self.logger.warning(f"Table {table_name} does not exist, skipping data deletion")
                return True
                
            with db_config.get_test_session() as session:
                full_table_name = f"{schema}.{table_name}" if schema else table_name
                where_clause = " AND ".join(f"{k} = :{k}" for k in conditions.keys())
                
                session.execute(
                    text(f"DELETE FROM {full_table_name} WHERE {where_clause}"),
                    conditions
                )
                session.commit()
                self.logger.info(f"Successfully deleted data from table {full_table_name}")
                return True
                
        except Exception as e:
            self.logger.error(f"Error deleting data from table {table_name}: {str(e)}")
            return False
            
    def create_table(
        self,
        table_name: str,
        columns: List[Dict[str, Any]],
        schema: Optional[str] = None
    ) -> bool:
        """
        Create a new table in the test database
        
        Args:
            table_name: Name of the table to create
            columns: List of column definitions
            schema: Database schema name
            
        Returns:
            bool: True if operation successful, False otherwise
        """
        try:
            if self.check_table_exists(table_name, schema):
                self.logger.warning(f"Table {table_name} already exists, skipping creation")
                return True
                
            metadata = MetaData()
            
            # Create table object with proper column definitions
            table_columns = []
            for col in columns:
                col_type = col.pop('type', None)
                if col_type is None:
                    raise ValueError(f"Column type is required for column {col.get('name', 'unknown')}")
                    
                table_columns.append(Column(
                    name=col.get('name'),
                    type_=col_type,
                    primary_key=col.get('primary_key', False),
                    nullable=col.get('nullable', True),
                    **{k: v for k, v in col.items() if k not in ['name', 'type', 'primary_key', 'nullable']}
                ))
            
            table = Table(
                table_name,
                metadata,
                *table_columns,
                schema=schema
            )
            
            # Create table in database
            metadata.create_all(self.engine, tables=[table])
            self.logger.info(f"Successfully created table {table_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error creating table {table_name}: {str(e)}")
            return False

    # ---------  NEW: 分批UPSERT处理器  ------------------
    def _upsert_in_batches(
        self,
        df: pd.DataFrame,
        table_name: str,
        schema: Optional[str],
        pk_fields: List[str],
        batch_rows: int,
        copy_batch_size: int = 50000,
    ):
        """
        将大数据集分批进行UPSERT操作，避免单次事务过大导致的性能问题
        
        Args:
            df: 要处理的DataFrame
            table_name: 目标表名
            schema: 数据库schema
            pk_fields: 主键字段列表
            batch_rows: 每批处理的行数
            copy_batch_size: COPY操作的批次大小
        """
        total_rows = len(df)
        num_batches = int(np.ceil(total_rows / batch_rows))
        
        self.logger.info(f"开始分批UPSERT: 总计 {total_rows} 行，分为 {num_batches} 批，每批最多 {batch_rows} 行")
        
        # 预先创建索引，使用带数据的优化去重方法，避免全局扫描
        self._ensure_unique_index_with_data(table_name, schema, pk_fields, df)
        
        for i in range(num_batches):
            start_idx = i * batch_rows
            end_idx = min((i + 1) * batch_rows, total_rows)
            batch_df = df.iloc[start_idx:end_idx].copy()
            
            self.logger.info(f"处理第 {i+1}/{num_batches} 批: 行 {start_idx+1}-{end_idx} ({len(batch_df)} 行)")
            
            # 调用优化后的_upsert_with_temp方法处理这一批数据
            self._upsert_with_temp(
                df=batch_df,
                table_name=table_name,
                schema=schema,
                pk_fields=pk_fields,
                batch_size=copy_batch_size,
                skip_index_check=True,  # 🚀 跳过索引检查，因为已经预先创建
            )
            
            # 记录进度
            progress = (i + 1) / num_batches * 100
            self.logger.info(f"分批UPSERT进度: {progress:.1f}% ({i+1}/{num_batches})")

    # ---------  临时表 + MERGE UPSERT  ------------------
    def _upsert_with_temp(
        self,
        df: pd.DataFrame,
        table_name: str,
        schema: Optional[str],
        pk_fields: List[str],
        batch_size: int = 50000,
        skip_index_check: bool = False,  # 🚀 新增参数：跳过索引检查
    ):
        """
        利用临时表和 COPY 实现高效 UPSERT
        1. 创建临时表 tmp_<table>_<ts>
        2. COPY 批量导入数据
        3. 确保目标表有唯一索引（可选跳过）
        4. INSERT … ON CONFLICT DO UPDATE 合并
        5. 删除临时表
        """
        if not pk_fields:
            raise ValueError("pk_fields 不能为空，否则无法构造 ON CONFLICT")

        # 记录开始时间
        start_time = time.time()
            
        # ---------- 建临时表 ----------
        tmp_table = self._generate_unique_temp_table_name(table_name)
        full_tmp = f"{schema}.{tmp_table}" if schema else tmp_table
        
        try:
            # 🚀 安全检查：确保临时表不存在，如果存在先清理
            self._ensure_temp_table_clean(tmp_table, schema)
            
            # 🚀 优化2: 直接创建UNLOGGED临时表，减少DDL开销
            self._create_unlogged_temp_table(df, tmp_table, schema)

            # ---------- COPY 导入 ----------
            self._copy_df_to_table(df, tmp_table, schema, batch_size)
            
            # ---------- 确保唯一索引（可选跳过） ----------
            if not skip_index_check:
                # 🚀 传递数据DataFrame给优化的去重方法
                self._ensure_unique_index_with_data(table_name, schema, pk_fields, df)

            # ---------- 🚀 优化3: 使用更高效的UPSERT SQL ----------
            full_table = f"{schema}.{table_name}" if schema else table_name
            cols = list(df.columns)
            col_list = ",".join(cols)
            pk_list = ",".join(pk_fields)
            
            # 只更新非主键字段，避免不必要的更新
            non_pk_cols = [c for c in cols if c not in pk_fields]
            if non_pk_cols:
                update_set = ",".join(f"{c}=EXCLUDED.{c}" for c in non_pk_cols)
                upsert_sql = text(f"""
                    INSERT INTO {full_table} ({col_list})
                    SELECT {col_list} FROM {full_tmp}
                    ON CONFLICT ({pk_list})
                    DO UPDATE SET {update_set}
                    WHERE ({' OR '.join(f'{full_table}.{c} IS DISTINCT FROM EXCLUDED.{c}' for c in non_pk_cols)});
                """)
            else:
                # 如果只有主键字段，使用DO NOTHING
                upsert_sql = text(f"""
                    INSERT INTO {full_table} ({col_list})
                    SELECT {col_list} FROM {full_tmp}
                    ON CONFLICT ({pk_list}) DO NOTHING;
                """)

            # 执行 UPSERT
            with self.engine.begin() as conn:
                try:
                    conn.execute(upsert_sql)  # 执行 UPSERT
                    self.logger.info(
                        f"成功 upsert 数据到表 {table_name}，用时 {time.time() - start_time:.2f}s，"
                        f"平均 {len(df)/(time.time() - start_time):.1f} 行/秒"
                    )
                except Exception as e:
                    # 增强报错信息：唯一性冲突时给出主键不一致的提示
                    if hasattr(e, 'orig') and hasattr(e.orig, 'pgcode') and e.orig.pgcode == '23505':  # UniqueViolation
                        self.logger.error(
                            f"UPSERT 操作失败: {str(e)}\n"
                            f"可能原因：表的主键和 pk_fields 不一致。\n"
                            f"请检查表结构的主键设置和 pk_fields 参数是否一致。\n"
                            f"例如：表主键为 (trade_date, stock_code)，但 pk_fields 传了 (trade_date, stock_code, field_name)。"
                        )
                    else:
                        self.logger.error(f"UPSERT 操作失败: {str(e)}")
                    raise
        finally:
            # 确保总是清理临时表，即使发生错误
            try:
                with self.engine.begin() as conn:
                    conn.execute(text(f"DROP TABLE IF EXISTS {full_tmp};"))
                    self.logger.info(f"已清理临时表 {full_tmp}")
            except Exception as e:
                self.logger.error(f"清理临时表 {full_tmp} 失败: {str(e)}")
            
        elapsed = time.time() - start_time
        self.logger.info(
            f"成功保存数据到表 {table_name}，用时 {elapsed:.2f}s，"
            f"平均 {len(df)/elapsed:.1f} 行/秒"
        )

    def _create_unlogged_temp_table(self, df: pd.DataFrame, tmp_table: str, schema: Optional[str]):
        """🚀 优化：直接创建UNLOGGED临时表，减少DDL开销"""
        full_table_name = f"{schema}.{tmp_table}" if schema else tmp_table
        
        # 构建CREATE TABLE语句 - 🚀 优化字段类型映射
        col_defs = []
        for col, dtype in df.dtypes.items():
            if pd.api.types.is_integer_dtype(dtype):
                col_type = "BIGINT"
            elif pd.api.types.is_float_dtype(dtype):
                col_type = "DOUBLE PRECISION"
            elif pd.api.types.is_datetime64_any_dtype(dtype):
                # 🚀 修复：使用TIMESTAMP保留完整时间信息，避免DATE丢失时分秒
                col_type = "TIMESTAMP"
            elif pd.api.types.is_bool_dtype(dtype):
                col_type = "BOOLEAN"
            else:
                # 🚀 优化：对于字符串类型，根据实际数据长度选择合适的类型
                if col in df.columns:
                    max_len = df[col].astype(str).str.len().max() if not df[col].empty else 0
                    if max_len <= 255:
                        col_type = "VARCHAR(255)"
                    elif max_len <= 1000:
                        col_type = "VARCHAR(1000)"
                    else:
                        col_type = "TEXT"
                else:
                    col_type = "TEXT"
            col_defs.append(f"{col} {col_type}")
        
        create_sql = f"""
        CREATE UNLOGGED TABLE {full_table_name} (
            {', '.join(col_defs)}
        );
        """
        
        with self.engine.begin() as conn:
            conn.execute(text(create_sql))
            self.logger.info(f"直接创建UNLOGGED临时表 {tmp_table}")

    def _generate_unique_temp_table_name(self, table_name: str) -> str:
        """
        生成唯一的临时表名，避免冲突
        
        Args:
            table_name: 目标表名
            
        Returns:
            str: 唯一的临时表名
        """
        # 🚀 修复临时表名冲突：增加更强的唯一性保证
        # 使用更高精度的时间戳（微秒级）+ 随机数 + 线程ID
        ts_suffix = int(time.time() * 1000000)  # 微秒时间戳
        random_suffix = random.randint(10000, 99999)  # 5位随机数
        thread_id = threading.get_ident() % 10000  # 线程ID的后4位
        
        # 用表名的 MD5 前 8 位作为哈希，确保长度可控
        hash_str = hashlib.md5(table_name.encode('utf-8')).hexdigest()[:8]
        tmp_table = f"tmp_{hash_str}_{ts_suffix}_{random_suffix}_{thread_id}"
        
        return tmp_table

    def _ensure_temp_table_clean(self, tmp_table: str, schema: Optional[str]):
        """确保临时表不存在，如果存在先清理"""
        full_tmp = f"{schema}.{tmp_table}" if schema else tmp_table
        
        try:
            with self.engine.begin() as conn:
                # 尝试删除可能存在的同名临时表
                conn.execute(text(f"DROP TABLE IF EXISTS {full_tmp};"))
                self.logger.debug(f"预清理临时表: {full_tmp}")
        except Exception as e:
            # 如果清理失败，记录警告但不中断流程
            self.logger.warning(f"预清理临时表 {full_tmp} 失败: {str(e)}")

    def _ensure_unique_index(self, table_name: str, schema: Optional[str], pk_fields: List[str]):
        """
        🚀 已弃用：此方法已被 _ensure_unique_index_with_data 替代
        
        该方法没有数据上下文，会导致全局扫描性能问题。
        请使用 _ensure_unique_index_with_data 方法替代。
        """
        import warnings
        warnings.warn(
            "_ensure_unique_index 已弃用，请使用 _ensure_unique_index_with_data 替代",
            DeprecationWarning,
            stacklevel=2
        )
        
        self.logger.warning(f"⚠️  调用了已弃用的 _ensure_unique_index 方法，请使用 _ensure_unique_index_with_data 替代")
        
        # 🚀 优化1：先检查是否已经有任何覆盖这些字段的唯一索引或约束
        if self._has_unique_constraint_on_fields(table_name, schema, pk_fields):
            self.logger.debug(f"表 {table_name} 已存在覆盖字段 {pk_fields} 的唯一索引/约束，跳过创建")
            return

        #         # 先尝试对目标表做去重，但使用优化的局部去重
        # try:
        #     self._deduplicate_table_optimized(table_name=table_name, schema=schema, pk_fields=pk_fields)
        # except Exception as e:
        #     self.logger.warning(f"去重步骤失败: {e}")
        # 🚀 修复：跳过去重，因为没有数据上下文，避免全局扫描
        # 如果需要去重，应该使用 _ensure_unique_index_with_data 方法
        self.logger.debug(f"跳过去重步骤，因为没有数据上下文（避免全局扫描）")

        # 生成索引名称
        import hashlib
        full_table_name = f"{schema}.{table_name}" if schema else table_name
        table_and_fields = f"{full_table_name}_{'_'.join(pk_fields)}"
        fields_hash = hashlib.md5(table_and_fields.encode()).hexdigest()[:8]
        table_short = table_name[:20] if len(table_name) > 20 else table_name
        index_name = f"uidx_{table_short}_{fields_hash}"
        cache_key = f"{schema}.{table_name}.{index_name}" if schema else f"{table_name}.{index_name}"
        
        # 检查缓存，避免重复创建
        if cache_key in self._index_cache:
            return
            
        cols = ", ".join(pk_fields)
        full_table = f"{schema}.{table_name}" if schema else table_name

        # 🚀 优化2：更准确的索引存在检查
        if self._index_exists(table_name, schema, index_name):
            self._index_cache.add(cache_key)
            self.logger.debug(f"索引 {index_name} 已存在")
            return
                
        # 索引不存在，创建它
        ddl = f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON {full_table} ({cols});
        """
        
        try:
            with self.engine.begin() as conn:
                conn.execute(text(ddl))
            
            # 🚀 优化3：简化验证逻辑
            if self._index_exists(table_name, schema, index_name):
                self._index_cache.add(cache_key)
                self.logger.info(f"成功创建唯一索引: {index_name}")
                return
            else:
                self.logger.warning(f"索引创建后验证失败: {index_name}")
        except Exception as e:
            self.logger.warning(f"创建索引失败: {str(e)}，尝试使用约束")

            # 如果索引创建失败，尝试创建约束
            constraint_name = f"uk_{table_short}_{fields_hash}"
            alter_ddl = f"""
            ALTER TABLE {full_table}
            ADD CONSTRAINT {constraint_name} UNIQUE ({cols});
            """
            
            try:
                with self.engine.begin() as conn:
                    conn.execute(text(alter_ddl))

                # 验证约束
                if self._constraint_exists(table_name, schema, constraint_name):
                    self._index_cache.add(cache_key)
                    self.logger.info(f"成功创建唯一约束: {constraint_name}")
                    return
            except Exception as e2:
                self.logger.error(f"创建唯一约束失败: {e2}")

            self.logger.error("无法创建唯一索引或约束，后续 UPSERT 可能失败。")

    def _has_unique_constraint_on_fields(self, table_name: str, schema: Optional[str], pk_fields: List[str]) -> bool:
        """检查表是否已有覆盖指定字段的唯一索引或约束"""
        try:
            schema_name = schema or 'public'
            
            # 首先检查表是否存在，避免后续查询错误
            if not self.check_table_exists(table_name, schema):
                self.logger.debug(f"表 {table_name} 不存在，跳过约束检查")
                return False
            
            with self.engine.connect() as conn:
                # 检查唯一索引 - 使用安全的查询方式，避免regclass转换错误
                index_sql = """
                SELECT i.indexname, array_agg(a.attname ORDER BY a.attnum) as columns
                FROM pg_indexes i
                JOIN pg_class t ON t.relname = i.tablename AND t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = i.schemaname)
                JOIN pg_index idx ON idx.indrelid = t.oid AND idx.indisunique = true
                JOIN pg_class ic ON ic.oid = idx.indexrelid AND ic.relname = i.indexname
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(idx.indkey)
                WHERE i.tablename = :table_name
                AND i.schemaname = :schema_name
                GROUP BY i.indexname
                """
                
                result = conn.execute(text(index_sql), {
                    'table_name': table_name,
                    'schema_name': schema_name
                }).fetchall()
                
                for row in result:
                    index_columns = row[1]
                    if set(pk_fields).issubset(set(index_columns)):
                        self.logger.debug(f"找到覆盖字段 {pk_fields} 的唯一索引: {row[0]}")
                        return True
                
                # 检查唯一约束 - 使用参数化查询
                constraint_sql = """
                SELECT c.conname, array_agg(a.attname ORDER BY a.attnum) as columns
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                WHERE t.relname = :table_name
                AND n.nspname = :schema_name
                AND c.contype = 'u'
                GROUP BY c.conname
                """
                
                result = conn.execute(text(constraint_sql), {
                    'table_name': table_name,
                    'schema_name': schema_name
                }).fetchall()
                
                for row in result:
                    constraint_columns = row[1]
                    if set(pk_fields).issubset(set(constraint_columns)):
                        self.logger.debug(f"找到覆盖字段 {pk_fields} 的唯一约束: {row[0]}")
                        return True
                        
            return False
        except Exception as e:
            # 根据SQLAlchemy文档，UndefinedTable错误通常表示查询的对象不存在
            # 在这种情况下，我们应该返回False而不是抛出错误
            if "does not exist" in str(e) or "UndefinedTable" in str(e):
                self.logger.debug(f"表或索引不存在，跳过约束检查: {table_name}")
                return False
            else:
                self.logger.warning(f"检查唯一约束失败: {e}")
                return False

    def _index_exists(self, table_name: str, schema: Optional[str], index_name: str) -> bool:
        """检查索引是否存在"""
        try:
            # 使用简单的pg_indexes视图查询，这是最安全的方式
            check_sql = """
            SELECT 1 FROM pg_indexes 
            WHERE tablename = :table_name 
            AND indexname = :index_name
            AND schemaname = :schema_name
            """
            
            schema_name = schema or 'public'
            
            with self.engine.connect() as conn:
                result = conn.execute(text(check_sql), {
                    'table_name': table_name,
                    'index_name': index_name,
                    'schema_name': schema_name
                }).fetchone()
                return result is not None
        except Exception as e:
            # 根据SQLAlchemy文档建议，对于不存在的对象应该优雅处理
            if "does not exist" in str(e) or "UndefinedTable" in str(e):
                self.logger.debug(f"索引查询对象不存在: {index_name}")
                return False
            else:
                self.logger.debug(f"检查索引存在性时出错: {e}")
                return False

    def _constraint_exists(self, table_name: str, schema: Optional[str], constraint_name: str) -> bool:
        """检查约束是否存在"""
        try:
            # 使用参数化查询避免SQL注入
            verify_sql = """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            JOIN pg_namespace n ON t.relnamespace = n.oid
            WHERE t.relname = :table_name
            AND c.conname = :constraint_name
            AND n.nspname = :schema_name
            """
            
            schema_name = schema or 'public'
            
            with self.engine.connect() as conn:
                result = conn.execute(text(verify_sql), {
                    'table_name': table_name,
                    'constraint_name': constraint_name,
                    'schema_name': schema_name
                }).fetchone()
                return result is not None
        except Exception as e:
            self.logger.debug(f"检查约束存在性时出错: {e}")
            return False

    # ---------  NEW: 重复数据处理 -----------
    def _deduplicate_table_optimized(self, table_name: str, schema: Optional[str], pk_fields: List[str], 
                                     data_df: Optional[pd.DataFrame] = None):
        """
        🚀 优化的去重方法：只检查和去重相关日期范围的数据，而不是全表扫描
        
        Args:
            table_name: 目标表名
            schema: schema 名，可为 None
            pk_fields: 唯一键字段列表
            data_df: 要插入的数据DataFrame，用于确定日期范围
        """
        if not pk_fields:
            return  # 无法去重

        full_table = f"{schema}.{table_name}" if schema else table_name
        cols = ", ".join(pk_fields)
        
        # 🚀 优化1：如果提供了数据，只检查相关日期范围（往前多看20天作为余量）
        date_filter = ""
        if data_df is not None and 'trade_date' in data_df.columns:
            min_date = data_df['trade_date'].min()
            max_date = data_df['trade_date'].max()
            
            # 🚀 添加20天余量：往前多看20天，确保覆盖可能的重复数据
            if pd.api.types.is_datetime64_any_dtype(data_df['trade_date']):
                min_date_with_buffer = min_date - pd.Timedelta(days=20)
                min_date_str = min_date_with_buffer.strftime('%Y-%m-%d')
                max_date_str = max_date.strftime('%Y-%m-%d')
                original_min_str = min_date.strftime('%Y-%m-%d')
            else:
                # 对于非datetime类型，尝试转换后再计算
                try:
                    min_date_dt = pd.to_datetime(min_date)
                    max_date_dt = pd.to_datetime(max_date)
                    min_date_with_buffer = min_date_dt - pd.Timedelta(days=20)
                    min_date_str = min_date_with_buffer.strftime('%Y-%m-%d')
                    max_date_str = max_date_dt.strftime('%Y-%m-%d')
                    original_min_str = min_date_dt.strftime('%Y-%m-%d')
                except:
                    # 如果转换失败，使用原始值
                    Warning("日期转换失败，使用原始值")
                    min_date_str = str(min_date)
                    max_date_str = str(max_date)
                    original_min_str = str(min_date)
            
            date_filter = f"WHERE trade_date BETWEEN '{min_date_str}' AND '{max_date_str}'"
            self.logger.info(f"检查日期范围 {min_date_str} 到 {max_date_str} 的重复数据（数据范围：{original_min_str} 到 {max_date_str}，含20天缓冲）")
        
        # 🚀 优化：使用更高效的重复检查方式
        if date_filter:
            # 对于有日期范围的情况，使用快速重复检查
            check_sql = f"""
            SELECT EXISTS(
                SELECT 1 FROM {full_table} 
                {date_filter}
                GROUP BY {cols}
                HAVING COUNT(*) > 1
                LIMIT 1
            ) as has_duplicates
            """
        else:
            Warning("全表检查，还是用原来的方式")
            # 全表检查，还是用原来的方式
            check_sql = f"""
            SELECT COUNT(*) as total_rows,
                   COUNT(DISTINCT ({cols})) as unique_rows
            FROM {full_table}
            """
        
        try:
            with self.engine.begin() as conn:
                result = conn.execute(text(check_sql)).fetchone()
                
                if date_filter:
                    # 快速检查模式
                    has_duplicates = result[0]
                    if not has_duplicates:
                        self.logger.info(f"表 {full_table} 在指定范围内无重复数据，跳过去重")
                        return
                    self.logger.info(f"表 {full_table} 在指定范围内发现重复记录，开始局部去重...")
                else:
                    # 全表检查模式
                    total_rows = result[0]
                    unique_rows = result[1]
                    
                    if total_rows == unique_rows:
                        self.logger.info(f"表 {full_table} 无重复数据，跳过去重")
                        return
                        
                    duplicate_count = total_rows - unique_rows
                    self.logger.info(f"表 {full_table} 发现 {duplicate_count} 条重复记录，开始去重...")
                
                # 🚀 优化2：对有日期范围的去重使用更高效的DELETE策略
                if date_filter:
                    # 局部去重：只删除指定日期范围内的重复数据
                    dedup_sql = f"""
                    WITH ranked AS (
                        SELECT ctid,
                               ROW_NUMBER() OVER (PARTITION BY {cols} ORDER BY ctid) AS rn
                        FROM {full_table}
                        {date_filter}
                    )
                    DELETE FROM {full_table} t
                    USING ranked r
                    WHERE t.ctid = r.ctid AND r.rn > 1
                    """
                    
                    deleted = conn.execute(text(dedup_sql)).rowcount
                    self.logger.info(f"局部去重完成：删除 {deleted} 条重复记录")
                else:
                    # 全表去重：使用原有逻辑
                    self._deduplicate_table(table_name, schema, pk_fields)
                    
        except Exception as e:
            self.logger.warning(f"优化去重失败，降级到原有方法: {e}")
            # 降级到原有的全表去重方法
            self._deduplicate_table(table_name, schema, pk_fields)

    def _deduplicate_table(self, table_name: str, schema: Optional[str], pk_fields: List[str]):
        """原有的全表去重方法，作为降级方案"""
        if not pk_fields:
            return  # 无法去重

        full_table = f"{schema}.{table_name}" if schema else table_name
        cols = ", ".join(pk_fields)
        
        # 先检查是否有重复数据
        check_sql = f"""
        SELECT COUNT(*) as total_rows,
               COUNT(DISTINCT ({cols})) as unique_rows
        FROM {full_table}
        """
        
        with self.engine.begin() as conn:
            result = conn.execute(text(check_sql)).fetchone()
            total_rows = result[0]
            unique_rows = result[1]
            
            if total_rows == unique_rows:
                self.logger.info(f"表 {full_table} 无重复数据，跳过去重")
                return
                
            duplicate_count = total_rows - unique_rows
            self.logger.info(f"表 {full_table} 发现 {duplicate_count} 条重复记录，开始去重...")
            
            # 🚀 对大表使用更高效的策略：CREATE TABLE AS SELECT
            if total_rows > 1_000_000:  # 超过100万行使用高效策略
                self.logger.info(f"大表去重：使用 CREATE TABLE AS SELECT 策略")
                
                # 1. 创建去重后的临时表
                temp_table = f"{table_name}_dedup_temp"
                full_temp_table = f"{schema}.{temp_table}" if schema else temp_table
                
                # 获取所有列名
                cols_sql = f"""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = '{table_name}' 
                {f"AND table_schema = '{schema}'" if schema else "AND table_schema = 'public'"}
                ORDER BY ordinal_position
                """
                columns = [row[0] for row in conn.execute(text(cols_sql)).fetchall()]
                all_cols = ", ".join(columns)
                
                # 2. 创建去重表
                dedup_sql = f"""
                CREATE TABLE {full_temp_table} AS
                SELECT {all_cols}
                FROM (
                    SELECT {all_cols},
                           ROW_NUMBER() OVER (PARTITION BY {cols} ORDER BY ctid) AS rn
                    FROM {full_table}
                ) ranked
                WHERE rn = 1
                """
                
                conn.execute(text(dedup_sql))
                self.logger.info(f"创建去重临时表 {full_temp_table}")
                
                # 3. 删除原表
                conn.execute(text(f"DROP TABLE {full_table}"))
                
                # 4. 重命名临时表
                conn.execute(text(f"ALTER TABLE {full_temp_table} RENAME TO {table_name}"))
                
                self.logger.info(f"已删除 {duplicate_count} 条重复记录，完成大表去重")
                
            else:
                # 小表使用原有的DELETE策略
                dedup_sql = f"""
                WITH ranked AS (
                    SELECT ctid,
                           ROW_NUMBER() OVER (PARTITION BY {cols} ORDER BY ctid) AS rn
                    FROM {full_table}
                )
                DELETE FROM {full_table} t
                USING ranked r
                WHERE t.ctid = r.ctid AND r.rn > 1;
                """
                
                deleted = conn.execute(text(dedup_sql)).rowcount
                self.logger.info(f"已删除 {deleted} 条重复记录以确保唯一性 ({cols})")

    # ---------  NEW: 复用 COPY 逻辑的小工具 -----------
    def _copy_df_to_table_enhanced(
        self,
        df: pd.DataFrame,
        table_name: str,
        schema: Optional[str],
        batch_size: int = 50000,
        show_progress: bool = True,
    ):
        """
        🚀 增强版COPY方法：支持psycopg3和PyArrow，自动选择最优性能策略
        
        Performance improvements:
        - psycopg3 native streaming (2-3x faster than psycopg2)
        - PyArrow backend for pandas (zero-copy data transfer)
        - Optimized batch sizes based on data volume
        """
        # 🚀 预处理数据，确保格式兼容性
        df_processed = df.copy()

        # 🚀 优化：只处理确实有问题的列，避免不必要的格式化开销
        for col in df_processed.columns:
            if 'date' in col.lower() and pd.api.types.is_datetime64_any_dtype(df_processed[col]):
                # 只处理datetime类型的日期列，保留时间信息
                try:
                    df_processed[col] = df_processed[col].dt.strftime('%Y-%m-%d %H:%M:%S')
                except Exception as e:
                    self.logger.debug(f"日期列 {col} 格式化失败: {e}")
            elif df_processed[col].dtype in ['float64', 'float32']:
                # 🚀 优化：只在发现科学计数法时才转换，保持数值精度
                sample_values = df_processed[col].dropna().head(100)
                if not sample_values.empty:
                    # 检查是否有科学计数法表示
                    has_scientific = any('e' in str(val).lower() for val in sample_values)
                    if has_scientific:
                        self.logger.debug(f"列 {col} 发现科学计数法，转换为固定格式")
                        df_processed[col] = df_processed[col].apply(lambda x: f"{x:.10f}" if pd.notna(x) else '')
                # 否则保持原数值类型，让PostgreSQL直接解析
            # 🚀 移除整数强制转字符串，保持数值类型性能更好

        # 🚀 关键修复：统一 trade_date 为 YYYY-MM-DD 字符串，避免 Arrow dtype 导致 COPY 解析问题
        if 'trade_date' in df_processed.columns:
            try:
                df_processed['trade_date'] = (
                    pd.to_datetime(df_processed['trade_date'], errors='coerce')
                      .dt.strftime('%Y-%m-%d')
                )
            except Exception as e:
                self.logger.debug(f"trade_date 转换失败，保持原样: {e}")
        
        # 🚀 如果可用，使用PyArrow后端处理pandas数据
        if PYARROW_AVAILABLE and len(df) > 100_000:  # 大数据集才使用PyArrow
            try:
                # 正确的PyArrow优化：转换为Arrow Table再转回pandas以获得更好的内存布局
                arrow_table = pa.Table.from_pandas(df_processed)
                df_processed = arrow_table.to_pandas(types_mapper=pd.ArrowDtype)
                self.logger.debug("Using PyArrow backend for enhanced performance")
            except Exception as e:
                self.logger.warning(f"PyArrow conversion failed, falling back to standard pandas: {e}")
                # 保持原DataFrame不变
        
        conn_info = self.engine.url
        total = len(df_processed)
        cols = df_processed.columns.tolist()
        full_table = f"{schema}.{table_name}" if schema else table_name
        
        # 🚀 动态调整批次大小
        if total > 10_000_000:  # 超过1000万行
            batch_size = min(int(batch_size * 2), 200_000)  # 增大批次但不超过20万
        elif total > 1_000_000:  # 超过100万行
            batch_size = min(int(batch_size * 1.5), 150_000)

        # 防止 batch_size 变成浮点或非正数，影响 iloc 切片
        if not isinstance(batch_size, int):
            batch_size = int(batch_size)
        if batch_size <= 0:
            batch_size = 1
        
        num_batches = int(np.ceil(total / batch_size))

        # 🚀 选择最优的连接方式
        if PSYCOPG3_AVAILABLE:
            self._copy_with_psycopg3(df_processed, full_table, cols, batch_size, num_batches, show_progress)
        else:
            self._copy_with_psycopg2(df_processed, full_table, cols, batch_size, num_batches, show_progress, conn_info)

    def _copy_with_psycopg3(self, df_processed, full_table, cols, batch_size, num_batches, show_progress):
        """使用psycopg3的原生COPY流式API"""
        conn_info = self.engine.url
        
        # psycopg3 连接字符串格式
        conn_params = {
            'host': conn_info.host,
            'port': conn_info.port or 5432,
            'dbname': conn_info.database,
            'user': conn_info.username,
            'password': conn_info.password
        }
        
        total = len(df_processed)
        
        try:
            # 🚀 psycopg3的现代连接方式
            with psycopg.connect(**conn_params) as conn:
                with conn.cursor() as cursor:
                    # 创建进度条
                    iterable = range(num_batches)
                    if show_progress:
                        iterable = tqdm(iterable, desc=f"COPY→{full_table.split('.')[-1]}", unit="批",
                                        bar_format=" {l_bar}{bar} | {n_fmt}/{total_fmt} 批 [耗时:{elapsed}]")

                    # 🚀 使用psycopg3的copy()方法，支持流式处理
                    copy_sql = f"COPY {full_table} ({','.join(cols)}) FROM STDIN WITH CSV NULL ''"
                    
                    with cursor.copy(copy_sql) as copy:
                        for i in iterable:
                            start, end = i * batch_size, min((i + 1) * batch_size, total)
                            batch_data = df_processed.iloc[start:end]
                            
                            # 🚀 将数据转换为CSV格式的字符串流
                            csv_buffer = io.StringIO()
                            batch_data.to_csv(csv_buffer, index=False, header=False, na_rep='')
                            csv_buffer.seek(0)
                            
                            # 逐行写入数据
                            for line in csv_buffer:
                                if line.strip():  # 跳过空行
                                    copy.write(line)
                    
                    conn.commit()
                    
        except Exception as e:
            self.logger.error(f"psycopg3 COPY failed: {e}, falling back to psycopg2")
            # 记录详细的数据格式信息用于调试
            if self.logger.isEnabledFor(logging.DEBUG):
                debug_info = self._debug_data_format(df_processed, sample_size=3)
                self.logger.debug(f"Data format debug info:\n{debug_info}")
            
            # 降级到psycopg2
            try:
                self._copy_with_psycopg2(df_processed, full_table, cols, batch_size, num_batches, show_progress, self.engine.url)
            except Exception as e2:
                self.logger.error(f"psycopg2 COPY also failed: {e2}, this may indicate a data format issue")
                # 记录详细的数据格式信息用于调试
                debug_info = self._debug_data_format(df_processed, sample_size=3)
                self.logger.error(f"Data format debug info:\n{debug_info}")
                raise e2

    def _copy_with_psycopg2(self, df_processed, full_table, cols, batch_size, num_batches, show_progress, conn_info):
        """使用psycopg2的传统COPY方法（降级方案）"""
        conn = psycopg2.connect(
            host=conn_info.host,
            port=conn_info.port or 5432,
            database=conn_info.database,
            user=conn_info.username,
            password=conn_info.password,
        )
        cursor = conn.cursor()
        total = len(df_processed)

        # 创建进度条迭代器
        iterable = range(num_batches)
        if show_progress:
            iterable = tqdm(iterable, desc=f"COPY→{full_table.split('.')[-1]}", unit="批",
                            mininterval=1.0,
                            bar_format=" {l_bar}{bar} | {n_fmt}/{total_fmt} 批 [耗时:{elapsed}]")

        for i in iterable:
            start, end = i * batch_size, min((i + 1) * batch_size, total)
            buf = io.StringIO()
            df_processed.iloc[start:end].to_csv(buf, index=False, header=False, na_rep='')
            buf.seek(0)
            cursor.copy_expert(
                f"COPY {full_table} ({','.join(cols)}) FROM STDIN WITH CSV NULL ''",
                buf,
            )
            conn.commit()
        cursor.close()
        conn.close()

    def _ensure_unique_index_with_data(self, table_name: str, schema: Optional[str], pk_fields: List[str], data_df: pd.DataFrame):
        """
        🚀 新方法：结合数据的索引确保，传递数据给优化的去重方法
        """
        # 🚀 优化1：先检查是否已经有任何覆盖这些字段的唯一索引或约束
        if self._has_unique_constraint_on_fields(table_name, schema, pk_fields):
            self.logger.debug(f"表 {table_name} 已存在覆盖字段 {pk_fields} 的唯一索引/约束，跳过去重和索引创建")
            return

        # 优化2：对于大数据集，先尝试创建索引，如果失败再去重
        # 这样可以避免不必要的去重操作
        if len(data_df) > 1_000_000:  # 超过100万行
            self.logger.debug(f"大数据集 ({len(data_df)} 行)，先尝试创建索引")
            if self._try_create_index_first(table_name, schema, pk_fields):
                return
        
        # 先尝试对目标表做去重，传递数据用于局部去重
        try:
            self._deduplicate_table_optimized(table_name=table_name, schema=schema, pk_fields=pk_fields, data_df=data_df)
        except Exception as e:
            self.logger.warning(f"去重步骤失败: {e}")

        # 其余逻辑与原_ensure_unique_index相同
        import hashlib
        full_table_name = f"{schema}.{table_name}" if schema else table_name
        table_and_fields = f"{full_table_name}_{'_'.join(pk_fields)}"
        fields_hash = hashlib.md5(table_and_fields.encode()).hexdigest()[:8]
        table_short = table_name[:20] if len(table_name) > 20 else table_name
        index_name = f"uidx_{table_short}_{fields_hash}"
        cache_key = f"{schema}.{table_name}.{index_name}" if schema else f"{table_name}.{index_name}"
        
        # 检查缓存，避免重复创建
        if cache_key in self._index_cache:
            return
            
        cols = ", ".join(pk_fields)
        full_table = f"{schema}.{table_name}" if schema else table_name

        # 检查索引是否存在
        if self._index_exists(table_name, schema, index_name):
            self._index_cache.add(cache_key)
            self.logger.debug(f"索引 {index_name} 已存在")
            return
                
        # 索引不存在，创建它
        ddl = f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON {full_table} ({cols});
        """
        
        try:
            with self.engine.begin() as conn:
                conn.execute(text(ddl))
            
            if self._index_exists(table_name, schema, index_name):
                self._index_cache.add(cache_key)
                self.logger.info(f"成功创建唯一索引: {index_name}")
                return
            else:
                self.logger.warning(f"索引创建后验证失败: {index_name}")
        except Exception as e:
            self.logger.warning(f"创建索引失败: {str(e)}，尝试使用约束")

            # 如果索引创建失败，尝试创建约束
            constraint_name = f"uk_{table_short}_{fields_hash}"
            alter_ddl = f"""
            ALTER TABLE {full_table}
            ADD CONSTRAINT {constraint_name} UNIQUE ({cols});
            """
            
            try:
                with self.engine.begin() as conn:
                    conn.execute(text(alter_ddl))

                if self._constraint_exists(table_name, schema, constraint_name):
                    self._index_cache.add(cache_key)
                    self.logger.info(f"成功创建唯一约束: {constraint_name}")
                    return
            except Exception as e2:
                self.logger.error(f"创建唯一约束失败: {e2}")

            self.logger.error("无法创建唯一索引或约束，后续 UPSERT 可能失败。")

    def _copy_df_to_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        schema: Optional[str],
        batch_size: int = 50000,
        show_progress: bool = True,
    ):
        """
        原有的COPY方法，保持向后兼容性
        
        内部工具：用 psycopg2 COPY 批量把 df 写到指定表
        """
        # 🚀 优先使用增强版方法
        try:
            self._copy_df_to_table_enhanced(df, table_name, schema, batch_size, show_progress)
        except Exception as e:
            self.logger.warning(f"Enhanced COPY failed, falling back to original method: {e}")
            # 降级到原有实现
            self._copy_df_to_table_legacy(df, table_name, schema, batch_size, show_progress)

    def _copy_df_to_table_legacy(
        self,
        df: pd.DataFrame,
        table_name: str,
        schema: Optional[str],
        batch_size: int = 50000,
        show_progress: bool = True,
    ):
        """原有的psycopg2 COPY实现，作为最终降级方案"""
        # 预处理数据，确保日期字段格式正确
        df_processed = df.copy()
        
        # 如果有trade_date列，确保它是日期格式
        if 'trade_date' in df_processed.columns:
            df_processed['trade_date'] = pd.to_datetime(df_processed['trade_date']).dt.date
        
        conn_info = self.engine.url
        conn = psycopg2.connect(
            host=conn_info.host,
            port=conn_info.port or 5432,
            database=conn_info.database,
            user=conn_info.username,
            password=conn_info.password,
        )
        cursor = conn.cursor()
        total  = len(df_processed)
        cols   = df_processed.columns.tolist()
        full_table = f"{schema}.{table_name}" if schema else table_name
        num_batches = int(np.ceil(total / batch_size))

        # 创建进度条迭代器
        iterable = range(num_batches)
        if show_progress:
            iterable = tqdm(iterable, desc=f"COPY→{table_name}", unit="批",
                            mininterval=1.0,  # 至少 1 s 刷新一次，避免太花
                            bar_format=" {l_bar}{bar} | {n_fmt}/{total_fmt} 批 "
                                       "[耗时:{elapsed} 预计:{remaining}]")

        for i in iterable:
            start, end = i * batch_size, min((i + 1) * batch_size, total)
            buf = io.StringIO()
            # 使用na_rep='' 确保NULL值被正确处理为空字符串，然后PostgreSQL可以正确识别为NULL
            df_processed.iloc[start:end].to_csv(buf, index=False, header=False, na_rep='')
            buf.seek(0)
            cursor.copy_expert(
                f"COPY {full_table} ({','.join(cols)}) FROM STDIN WITH CSV NULL ''",
                buf,
            )
            conn.commit()
        cursor.close()
        conn.close()

    def _debug_data_format(self, df: pd.DataFrame, sample_size: int = 5) -> str:
        """
        Debug helper: 分析DataFrame的数据格式，用于诊断COPY操作问题
        """
        debug_info = []
        debug_info.append(f"DataFrame shape: {df.shape}")
        debug_info.append(f"Column count: {len(df.columns)}")
        debug_info.append(f"Columns: {list(df.columns)}")
        
        # 数据类型分析
        debug_info.append("\nData types:")
        for col in df.columns:
            dtype = df[col].dtype
            null_count = df[col].isnull().sum()
            debug_info.append(f"  {col}: {dtype} (nulls: {null_count})")
        
        # 样本数据
        debug_info.append(f"\nFirst {sample_size} rows:")
        try:
            sample_data = df.head(sample_size)
            for i, row in sample_data.iterrows():
                row_str = " | ".join([f"{col}={repr(val)}" for col, val in row.items()])
                debug_info.append(f"  Row {i}: {row_str}")
        except Exception as e:
            debug_info.append(f"  Error sampling data: {e}")
        
        return "\n".join(debug_info)

    def _try_create_index_first(self, table_name: str, schema: Optional[str], pk_fields: List[str]) -> bool:
        """
         优化方法：先尝试创建索引，如果成功说明没有重复数据，可以跳过去重
        
        Returns:
            bool: True if index created successfully (no duplicates), False if failed (has duplicates)
        """
        import hashlib
        full_table_name = f"{schema}.{table_name}" if schema else table_name
        table_and_fields = f"{full_table_name}_{'_'.join(pk_fields)}"
        fields_hash = hashlib.md5(table_and_fields.encode()).hexdigest()[:8]
        table_short = table_name[:20] if len(table_name) > 20 else table_name
        index_name = f"uidx_{table_short}_{fields_hash}"
        
        cols = ", ".join(pk_fields)
        full_table = f"{schema}.{table_name}" if schema else table_name
        
        # 尝试创建唯一索引
        ddl = f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON {full_table} ({cols});
        """
        
        try:
            with self.engine.begin() as conn:
                conn.execute(text(ddl))
            
            # 验证索引是否创建成功
            if self._index_exists(table_name, schema, index_name):
                cache_key = f"{schema}.{table_name}.{index_name}" if schema else f"{table_name}.{index_name}"
                self._index_cache.add(cache_key)
                self.logger.info(f"✅ 直接创建唯一索引成功: {index_name}，无需去重")
                return True
            else:
                self.logger.debug(f"索引创建验证失败: {index_name}")
                return False
                
        except Exception as e:
            # 如果创建失败，通常是因为有重复数据
            if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                self.logger.debug(f"索引创建失败(有重复数据): {e}")
            else:
                self.logger.debug(f"索引创建失败(其他原因): {e}")
            return False

# Create singleton instance
test_db_manager = TestDBManager()


