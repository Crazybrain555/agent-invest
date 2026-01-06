import os
import yaml
from typing import Dict, Any, Optional, Union
from pathlib import Path
import logging
from datetime import datetime
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

class ConfigLoader:
    """配置加载器，用于加载和管理YAML配置文件"""
    
    def __init__(self, config_dir: Union[str, Path] = "configs"):
        """
        初始化配置加载器
        
        Args:
            config_dir: 配置文件根目录
        """
        self.config_dir = Path(config_dir)
        self._config_cache = {}
        self._config_timestamps = {}
        self._setup_logging()
        
    def _setup_logging(self):
        """设置日志"""
        self.logger = logging.getLogger(__name__)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
            
    def load_config(self, config_file: str, use_cache: bool = True) -> Dict[str, Any]:
        """加载配置文件
        
        Args:
            config_file: 配置文件名称
            use_cache: 是否使用缓存
            
        Returns:
            Dict: 配置信息
        """
        # 检查缓存
        if use_cache and config_file in self._config_cache:
            return self._config_cache[config_file]
            
        # 构建完整路径
        config_path = os.path.join(self.config_dir, config_file)
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                
            # 如果是主配置文件，处理导入
            if config_file == 'field_mapping.yaml' and 'imports' in config:
                merged_config = {}
                # 加载全局配置
                if 'global' in config:
                    merged_config['global'] = config['global']
                    
                # 加载所有导入的配置文件
                for import_file in config['imports']:
                    import_path = os.path.join(self.config_dir, import_file)
                    try:
                        with open(import_path, 'r', encoding='utf-8') as f:
                            import_config = yaml.safe_load(f)
                            merged_config.update(import_config)
                    except Exception as e:
                        logger.error(f"Error loading imported config {import_file}: {str(e)}")
                        raise
                        
                config = merged_config
            
            # 处理环境变量
            config = self._process_env_vars(config)
                
            # 缓存配置
            if use_cache:
                self._config_cache[config_file] = config
                
            return config
            
        except Exception as e:
            logger.error(f"Error loading config file {config_file}: {str(e)}")
            raise
    
    def _process_env_vars(self, config: Any) -> Any:
        """处理配置中的环境变量
        
        Args:
            config: 配置值（可以是字典、列表或字符串）
            
        Returns:
            处理后的配置值
        """
        if isinstance(config, dict):
            return {k: self._process_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._process_env_vars(item) for item in config]
        elif isinstance(config, str) and config.startswith('${') and config.endswith('}'):
            env_var = config[2:-1]
            return os.getenv(env_var, config)
        return config
    
    def _merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        合并基础配置和覆盖配置
        
        Args:
            base: 基础配置
            override: 覆盖配置
            
        Returns:
            合并后的配置
        """
        merged = base.copy()
        
        for key, value in override.items():
            if key == 'extends':
                continue
                
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = self._merge_configs(merged[key], value)
            else:
                merged[key] = value
                
        return merged
    
    def _validate_config(self, config: Dict[str, Any]) -> None:
        """
        验证配置的有效性
        
        Args:
            config: 要验证的配置字典
        """
        if not isinstance(config, dict):
            raise ValueError("配置必须是字典类型")
            
        # 这里可以添加更多的验证规则
        required_fields = ['version']  # 示例：要求配置中包含version字段
        for field in required_fields:
            if field not in config:
                raise ValueError(f"配置缺少必需字段: {field}")
    
    def get_model_config(self, model_type: str, model_name: str) -> Dict[str, Any]:
        """
        获取模型配置
        
        Args:
            model_type: 模型类型（如LSTM、GRU等）
            model_name: 模型名称
            
        Returns:
            模型配置字典
        """
        config_path = f"models/{model_type.lower()}/{model_name}.yaml"
        return self.load_config(config_path)
    
    def get_strategy_config(self, strategy_type: str, strategy_name: str) -> Dict[str, Any]:
        """
        获取策略配置
        
        Args:
            strategy_type: 策略类型
            strategy_name: 策略名称
            
        Returns:
            策略配置字典
        """
        config_path = f"strategies/{strategy_name}.yaml"
        return self.load_config(config_path)
    
    def clear_cache(self):
        """清除配置缓存"""
        self._config_cache.clear()
        self._config_timestamps.clear()
        
    def reload_config(self, config_path: str) -> Dict[str, Any]:
        """
        重新加载配置文件（不使用缓存）
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            配置字典
        """
        return self.load_config(config_path, use_cache=False)
        
    def get_cached_configs(self) -> Dict[str, datetime]:
        """
        获取所有缓存的配置及其时间戳
        
        Returns:
            配置路径到时间戳的映射
        """
        return self._config_timestamps.copy()
    
    def get_field_mapping(self, data_type: Optional[str] = None) -> Dict[str, Any]:
        """获取字段映射配置
        
        Args:
            data_type: 数据类型，如 'market_data', 'financial_data' 等
            
        Returns:
            Dict: 字段映射配置
        """
        config = self.load_config('field_mapping.yaml')
        
        if data_type:
            if data_type not in config:
                raise ValueError(f"Unknown data type: {data_type}")
            return config[data_type]
            
        return config

    def get_db_config(self, db_type: str) -> Dict[str, Any]:
        """获取数据库配置
        
        Args:
            db_type: 数据库类型，如 'wind', 'gogoal', 'test_tdsql', 'prod'
            
        Returns:
            Dict: 数据库配置信息
        """
        config_file = f'db/{db_type}_db.yaml'
        config = self.load_config(config_file)
        
        # 验证数据库配置
        required_fields = ['connection_string']
        for field in required_fields:
            if field not in config:
                raise ValueError(f"数据库配置缺少必需字段: {field}")
                
        # 处理连接字符串中的环境变量
        if 'connection_string' in config:
            config['connection_string'] = self._process_env_vars(config['connection_string'])
            
        return config

    def get_all_db_configs(self) -> Dict[str, Dict[str, Any]]:
        """获取所有数据库配置
        
        Returns:
            Dict: 所有数据库配置信息
        """
        db_types = ['wind', 'gogoal', 'test_tdsql', 'prod']
        return {db_type: self.get_db_config(db_type) for db_type in db_types} 