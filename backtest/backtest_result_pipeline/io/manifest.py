"""
产物清单 manifest.json

负责：
- 记录本次运行的所有产物路径
- 记录策略名称映射
- 记录运行配置快照
"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .atomic_write import atomic_write_json


class ManifestBuilder:
    """产物清单构建器"""
    
    def __init__(self, run_id: str, run_dir: Path, factor_base_dir: Optional[Path] = None):
        self.run_id = run_id
        self.run_dir = run_dir
        self.factor_base_dir = factor_base_dir
        self.created_at = datetime.now().isoformat()
        
        # 产物路径（相对于 run_dir）
        self.config_files: Dict[str, str] = {}
        self.factor_files: List[str] = []
        self.nav_csv_files: Dict[str, Dict[str, str]] = {}  # pool_code -> strategy_name -> relative_path
        self.nav_png_files: Dict[str, Dict[str, str]] = {}
        self.signals_csv_files: Dict[str, Dict[str, str]] = {}
        self.tables_files: List[str] = []
        self.log_files: List[str] = []
        
        # 策略名称映射
        self.strategy_name_mapping: Dict[str, str] = {}  # original -> safe
        
        # 运行配置摘要
        self.run_config_summary: Dict[str, Any] = {}
    
    def add_config_file(self, name: str, path: Union[str, Path]):
        """添加配置文件"""
        self.config_files[name] = self._to_relative(path)
    
    def add_factor_file(self, path: Union[str, Path]):
        """添加因子文件"""
        self.factor_files.append(self._to_relative(path, base_dir=self.factor_base_dir))
    
    def add_nav_csv(self, pool_code: str, strategy_name: str, path: Union[str, Path]):
        """添加 NAV CSV 文件"""
        self.nav_csv_files.setdefault(pool_code, {})[strategy_name] = self._to_relative(path)
    
    def add_nav_png(self, pool_code: str, strategy_name: str, path: Union[str, Path]):
        """添加 NAV PNG 文件"""
        self.nav_png_files.setdefault(pool_code, {})[strategy_name] = self._to_relative(path)
    
    def add_signals_csv(self, pool_code: str, strategy_name: str, path: Union[str, Path]):
        """添加 signals CSV 文件"""
        self.signals_csv_files.setdefault(pool_code, {})[strategy_name] = self._to_relative(path)
    
    def add_tables_file(self, path: Union[str, Path]):
        """添加表格文件"""
        self.tables_files.append(self._to_relative(path))
    
    def add_log_file(self, path: Union[str, Path]):
        """添加日志文件"""
        self.log_files.append(self._to_relative(path))
    
    def set_strategy_name_mapping(self, mapping: Dict[str, str]):
        """设置策略名称映射"""
        self.strategy_name_mapping = mapping.copy()
    
    def set_run_config_summary(self, summary: Dict[str, Any]):
        """设置运行配置摘要"""
        self.run_config_summary = summary.copy()
    
    def _to_relative(self, path: Union[str, Path], base_dir: Optional[Path] = None) -> str:
        """转换为相对路径"""
        path = Path(path)
        for base in [base_dir, self.run_dir]:
            if base is None:
                continue
            try:
                return str(path.relative_to(base))
            except ValueError:
                continue
        # 不是 run_dir/base_dir 的子路径，返回绝对路径
        return str(path)
    
    def build(self) -> Dict[str, Any]:
        """构建清单字典"""
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "created_at": self.created_at,
            "config_files": self.config_files,
            "factor_files": self.factor_files,
            "nav_csv_files": self.nav_csv_files,
            "nav_png_files": self.nav_png_files,
            "signals_csv_files": self.signals_csv_files,
            "tables_files": self.tables_files,
            "log_files": self.log_files,
            "strategy_name_mapping": self.strategy_name_mapping,
            "run_config_summary": self.run_config_summary
        }
    
    def save(self, no_overwrite: bool = True) -> Path:
        """保存清单到 manifest.json"""
        manifest_path = self.run_dir / "manifest.json"
        return atomic_write_json(self.build(), manifest_path, no_overwrite=no_overwrite)


def load_manifest(manifest_path: Union[str, Path]) -> Dict[str, Any]:
    """加载清单"""
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)
