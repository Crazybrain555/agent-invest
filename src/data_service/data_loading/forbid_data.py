import os
import re
import logging
import pandas as pd
from typing import List, Optional
from src.utils.nas_connection import NASConnection
from src.utils.config_loader import ConfigLoader

# Configure logging for this module
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# 默认NAS路径，可被构造函数参数覆盖
DEFAULT_NAS_PATH = r'\\space\forbid'

class ForbidDataLoader:
    """
    Loads forbid pool data from CSV files stored on NAS.
    Uses NASConnection for file access and configuration for parsing details.
    """

    def __init__(self, nas_path=None):
        """
        Initializes the DataLoader by loading configuration and NAS connection.
        
        Args:
            nas_path (str, optional): 完整的NAS路径。如果提供，将直接使用此路径而不依赖配置文件中的base_path。
        """
        # 使用传入的NAS路径或默认路径
        self.nas_path = nas_path or DEFAULT_NAS_PATH
        logger.info(f"Using NAS path: {self.nas_path}")
        
        self.config_loader = ConfigLoader(config_dir='configs')
        self.cfg = self.config_loader.load_config("nas_disk/nas_config.yaml")
        self.loader_cfg = self.cfg.get('loader', {})

        # 初始化NAS连接，传入完整路径
        self.nas = NASConnection(nas_path=self.nas_path)

        # 编译从文件名提取日期的正则表达式
        self.date_pattern_str = self.loader_cfg.get('date_pattern')
        if not self.date_pattern_str:
            raise ValueError("Loader 'date_pattern' not found in configuration.")
        try:
            self.DATE_PATTERN = re.compile(self.date_pattern_str)
        except re.error as e:
            logger.error(f"Invalid regex pattern for date_pattern '{self.date_pattern_str}': {e}")
            raise ValueError(f"Invalid regex for date_pattern: {e}") from e

        # 获取其他配置
        self.filename_format_string = self.loader_cfg.get('filename_format_string')
        if not self.filename_format_string:
            raise ValueError("Loader 'filename_format_string' not found in configuration.")
        self.columns = self.loader_cfg.get('columns', ["stock_code", "signal"])
        self.encoding = self.loader_cfg.get('encoding', 'utf-8')

        logger.info(f"ForbidDataLoader initialized. Using NAS path: {self.nas_path}, Expecting columns: {self.columns}, Encoding: {self.encoding}")

    def _parse_date(self, filename: str) -> Optional[str]:
        """
        Extracts the date string (YYYYMMDD) from a filename using the configured regex.

        Args:
            filename: The name of the file.

        Returns:
            The date string (YYYYMMDD) if matched, otherwise None.
        """
        match = self.DATE_PATTERN.match(filename)
        if match:
            return match.group(1) # Assumes the first capture group is the date
        logger.debug(f"Filename '{filename}' did not match date pattern '{self.date_pattern_str}'")
        return None

    def list_available_dates(self) -> List[str]:
        """
        Lists all available dates by finding matching files in the NAS base path.

        Returns:
            A sorted list of date strings (YYYYMMDD).
        """
        logger.info(f"Listing available dates from NAS path: {self.nas.base_path}")
        try:
            all_files = self.nas.list_files() # Lists files in the base_path defined in NASConnection
        except Exception as e:
            logger.error(f"Failed to list files from NAS: {e}", exc_info=True)
            return []

        dates = []
        for f in all_files:
            date_str = self._parse_date(f)
            if date_str:
                dates.append(date_str)

        logger.info(f"Found {len(dates)} potential data files corresponding to dates.")
        return sorted(dates)

    def load_dates(self, dates: List[str]) -> pd.DataFrame:
        """
        Loads data for the specified list of dates.

        Args:
            dates: A list of date strings (YYYYMMDD) to load.

        Returns:
            A pandas DataFrame containing the concatenated data for the requested dates,
            with columns 'trade_date' (datetime) and the columns defined in config.
        """
        dfs = []
        if not dates:
            logger.warning("No dates provided to load.")
            # Return empty DataFrame with expected columns
            return pd.DataFrame(columns=['trade_date'] + self.columns)

        logger.info(f"Attempting to load data for {len(dates)} dates: {dates[:5]}...{dates[-5:] if len(dates) > 5 else ''}")

        for date_str in dates:
            # 构建文件名
            try:
                filename = self.filename_format_string.format(date=date_str)
            except KeyError as e:
                logger.error(f"Invalid filename_format_string '{self.filename_format_string}'. Missing placeholder: {e}")
                continue # 格式错误则跳过

            file_path_relative = filename # 相对于NASConnection.base_path的路径

            try:
                logger.debug(f"Loading data for date: {date_str} from file: {filename}")
                # 使用NASConnection读取文件
                buff = self.nas.read_file_to_buffer(file_path_relative)

                # 用pandas读取缓冲区
                df = pd.read_csv(
                    buff,
                    header=None, # 假设没有表头
                    names=self.columns, # 使用配置的列名
                    encoding=self.encoding,
                    sep=',', # 假设逗号分隔
                    skipinitialspace=True
                )

                # 验证列数
                if len(df.columns) != len(self.columns):
                    logger.warning(f"Mismatch columns for {filename}. Expected {len(self.columns)} ({self.columns}), got {len(df.columns)} ({list(df.columns)}). Skipping file.")
                    continue

                # 插入trade_date列
                df.insert(0, "trade_date", pd.to_datetime(date_str, format='%Y%m%d'))

                # 格式化stock_code列
                stock_code_col = self.columns[0] # 假设第一列是stock_code
                if stock_code_col in df.columns:
                    # 先转字符串，处理可能的NaN或非数字数据
                    df[stock_code_col] = df[stock_code_col].astype(str)
                    # 移除可能的.0后缀
                    df[stock_code_col] = df[stock_code_col].str.replace(r'\.0$', '', regex=True)
                    # 左侧补零确保6位数
                    df[stock_code_col] = df[stock_code_col].str.zfill(6)
                else:
                    logger.warning(f"Configured stock code column '{stock_code_col}' not found in file {filename}. Skipping stock code formatting.")

                # 转换signal列类型
                signal_col = self.columns[1] # 假设第二列是signal
                if signal_col in df.columns:
                    try:
                        # 转为数值，错误强制为NaN，填充0，然后转为int
                        df[signal_col] = pd.to_numeric(df[signal_col], errors='coerce').fillna(0).astype(int)
                    except Exception as e:
                        logger.warning(f"Could not reliably convert '{signal_col}' column to numeric/int for {filename}: {e}")

                dfs.append(df)
                logger.debug(f"Successfully loaded and processed {len(df)} rows for {date_str}")

            except FileNotFoundError:
                logger.error(f"File not found via NASConnection for date {date_str}: {filename}")
                # 文件不存在则跳过
            except Exception as e:
                # 记录其他错误
                logger.error(f"Failed to load or process data for date {date_str} from file {filename}: {e}", exc_info=True)
                # 继续处理其他日期

        if not dfs:
            logger.warning("No dataframes were successfully loaded for the requested dates.")
            return pd.DataFrame(columns=['trade_date'] + self.columns)

        # 合并所有加载的数据框
        try:
            out_df = pd.concat(dfs, ignore_index=True)
            logger.info(f"Successfully concatenated data. Total rows: {len(out_df)} for {len(dates)} requested dates.")
        except Exception as e:
            logger.error(f"Failed to concatenate loaded DataFrames: {e}", exc_info=True)
            return pd.DataFrame(columns=['trade_date'] + self.columns)

        # 确保最终数据框的列顺序正确
        final_columns = ['trade_date'] + self.columns
        # 过滤掉可能错误添加的列
        out_df = out_df[[col for col in final_columns if col in out_df.columns]]

        return out_df
