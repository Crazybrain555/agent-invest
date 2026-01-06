"""
Logging configuration for the quant framework.
"""
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Dict, Any

class Logger:
    """日志工具类"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化日志器
        
        Args:
            config: 日志配置字典
        """
        self.config = config
        self.logger = self._setup_logger()
    
    def _setup_logger(self) -> logging.Logger:
        """设置日志器
        
        Returns:
            配置好的日志器实例
        """
        # 创建日志器
        logger = logging.getLogger('AIQuantInvestment')
        logger.setLevel(self.config['logging']['level'])
        
        # 创建日志目录
        log_dir = self.config['logging']['file_path']
        os.makedirs(log_dir, exist_ok=True)
        
        # 创建文件处理器
        log_file = os.path.join(
            log_dir, 
            f'{self.config["logging"]["name"]}_{datetime.now().strftime("%Y%m%d")}.log'
        )
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=self.config['logging']['max_file_size'],
            backupCount=self.config['logging']['backup_count']
        )
        file_handler.setLevel(self.config['logging']['level'])
        
        # 创建控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.config['logging']['level'])
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # 添加处理器
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
        return logger
    
    def info(self, message: str) -> None:
        """记录信息日志
        
        Args:
            message: 日志消息
        """
        self.logger.info(message)
    
    def warning(self, message: str) -> None:
        """记录警告日志
        
        Args:
            message: 日志消息
        """
        self.logger.warning(message)
    
    def error(self, message: str) -> None:
        """记录错误日志
        
        Args:
            message: 日志消息
        """
        self.logger.error(message)
    
    def debug(self, message: str) -> None:
        """记录调试日志
        
        Args:
            message: 日志消息
        """
        self.logger.debug(message)
    
    def critical(self, message: str) -> None:
        """记录严重错误日志
        
        Args:
            message: 日志消息
        """
        self.logger.critical(message)
    
    def exception(self, message: str) -> None:
        """记录异常日志
        
        Args:
            message: 日志消息
        """
        self.logger.exception(message)

def setup_logger(name: str, log_dir: str = 'logs') -> logging.Logger:
    """
    Set up a logger with both file and console handlers.
    
    Args:
        name: Logger name
        log_dir: Directory to store log files
        
    Returns:
        logging.Logger: Configured logger instance
    """
    # Create logs directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)
    
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # File handler (with rotation)
    log_file = os.path.join(
        log_dir, 
        f'{name}_{datetime.now().strftime("%Y%m%d")}.log'
    )
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger 