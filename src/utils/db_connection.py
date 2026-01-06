"""
Database configuration settings for different databases and environments.
This module provides connection settings and utilities for database operations using SQLAlchemy.
"""

import pandas as pd
from datetime import datetime, date
from sqlalchemy import create_engine, Column, Integer, String, Numeric, Date, text, Table
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from sqlalchemy.pool import QueuePool
from typing import Dict, Any, Optional
from src.utils.config_loader import ConfigLoader
import logging

class Database_connection:
    def __init__(self):
        """初始化数据库连接管理器"""
        self.config_loader = ConfigLoader(config_dir='configs')
        self._setup_logging()
        self._load_configs()
        self._initialize_engines()
        self._initialize_sessions()

    def _setup_logging(self):
        """设置日志"""
        self.logger = logging.getLogger(__name__)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def _load_configs(self):
        """加载所有数据库配置"""
        try:
            # 获取所有数据库配置
            db_configs = self.config_loader.get_all_db_configs()
            
            # 设置各个数据库的配置
            self.wind_db = db_configs['wind']
            self.gogoal_db = db_configs['gogoal']
            self.test_tdsql = db_configs['test_tdsql']
            self.prod_db = db_configs['prod']
            
            # 验证数据库配置
            self._validate_db_configs()
            
        except Exception as e:
            self.logger.error(f"加载数据库配置失败: {str(e)}")
            raise

    def _validate_db_configs(self):
        """验证数据库配置的有效性"""
        for db_name, config in [
            ('wind_db', self.wind_db),
            ('gogoal_db', self.gogoal_db),
            ('test_tdsql', self.test_tdsql),
            ('prod_db', self.prod_db)
        ]:
            if not isinstance(config, dict):
                raise ValueError(f"{db_name} 配置必须是字典类型")
            
            if 'connection_string' not in config:
                raise ValueError(f"{db_name} 配置缺少连接字符串")
            
            if not config['connection_string']:
                raise ValueError(f"{db_name} 连接字符串不能为空")

    def _initialize_engines(self):
        """初始化SQLAlchemy引擎"""
        try:
            # 🚀 优化的引擎参数（SQLAlchemy 2.0 style）
            def create_db_engine(config):
                engine_params = {
                    'poolclass': QueuePool,
                    # 🚀 性能优化配置
                    'pool_size': config.get('pool_size', 10),  # 增加连接池大小
                    'max_overflow': config.get('max_overflow', 20),  # 增加溢出连接数
                    'pool_timeout': config.get('pool_timeout', 30),  # 连接超时时间
                    'pool_recycle': config.get('pool_recycle', 3600),  # 连接回收时间
                    'pool_pre_ping': True,  # 🚀 启用连接预检查，避免"server closed connection"错误
                }
                
                # 🚀 尝试添加psycopg3支持
                connection_string = config['connection_string']
                if 'postgresql+psycopg2://' in connection_string:
                    try:
                        import psycopg
                        # 尝试使用psycopg3
                        psycopg3_string = connection_string.replace('postgresql+psycopg2://', 'postgresql+psycopg://')
                        self.logger.info(f"Attempting to use psycopg3 for enhanced performance")
                        return create_engine(psycopg3_string, **engine_params)
                    except ImportError:
                        self.logger.info("psycopg3 not available, using psycopg2")
                        # 降级到psycopg2
                        pass
                
                return create_engine(connection_string, **engine_params)

            self.wind_engine = create_db_engine(self.wind_db)
            self.gogoal_engine = create_db_engine(self.gogoal_db)
            self.test_engine = create_db_engine(self.test_tdsql)
            self.prod_engine = create_db_engine(self.prod_db)
            
            self.logger.info("数据库引擎初始化成功（已启用性能优化）")
            
        except Exception as e:
            self.logger.error(f"初始化数据库引擎失败: {str(e)}")
            raise

    def _initialize_sessions(self):
        """初始化SQLAlchemy会话工厂"""
        try:
            self.WindSession = sessionmaker(bind=self.wind_engine)
            self.GogoalSession = sessionmaker(bind=self.gogoal_engine)
            self.TestSession = sessionmaker(bind=self.test_engine)
            self.ProdSession = sessionmaker(bind=self.prod_engine)
            self.logger.info("数据库会话工厂初始化成功")
        except Exception as e:
            self.logger.error(f"初始化数据库会话工厂失败: {str(e)}")
            raise

    def get_wind_session(self) -> Session:
        """获取Wind数据库会话"""
        return self.WindSession()

    def get_gogoal_session(self) -> Session:
        """获取Gogoal数据库会话"""
        return self.GogoalSession()

    def get_test_session(self) -> Session:
        """获取测试数据库会话"""
        return self.TestSession()

    def get_prod_session(self) -> Session:
        """获取生产数据库会话"""
        return self.ProdSession()

    def get_wind_engine(self):
        """获取Wind数据库引擎"""
        return self.wind_engine

    def get_gogoal_engine(self):
        """获取Gogoal数据库引擎"""
        return self.gogoal_engine

    def get_test_engine(self):
        """获取测试数据库引擎"""
        return self.test_engine

    def get_prod_engine(self):
        """获取生产数据库引擎"""
        return self.prod_engine

    def reload_configs(self):
        """重新加载所有数据库配置"""
        self.config_loader.clear_cache()
        self._load_configs()
        self._initialize_engines()
        self._initialize_sessions()

# 创建单例实例
db_config = Database_connection()

# ALERT_TABLE模型定义
Base = declarative_base()

class ALERT_TABLE(Base):
    """报表测试表"""
    __tablename__ = 'ALERT_SIGNALS_TOTAL_DF_WINDOW'

    index_number = Column(Integer)
    alert_date = Column(String(255), primary_key=True)
    close = Column(Numeric(25, 5))
    peakfactor = Column(Numeric(25, 5))
    peakfadjust = Column(Numeric(25, 5))
    assets_id = Column(String(255), primary_key=True)
    window = Column(Integer, primary_key=True)
    chinese_name = Column(String(255))
    cumulative_days_of_rise = Column(Integer)
    cumulative_pct_of_rise = Column(Numeric(25, 5))
    cumulative_rising_3week_pct = Column(Numeric(25, 5))
    cumulative_rising_2week_pct = Column(Numeric(25, 5))
    cumulative_rising_days_in10day = Column(Numeric(25, 5))
    createtime = Column(Date())
    updatetime = Column(Date())
    memo = Column(String(255))

def windcode_to_id(code):
    """转换Wind代码为ID格式"""
    return code.split('.')[0]
