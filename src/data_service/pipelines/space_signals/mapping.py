# -*- coding: utf-8 -*-
"""
因子映射加载模块

支持二级分类的 factor_mapping.yaml 文件加载和查询
"""

from __future__ import annotations
import os
import yaml
from typing import Dict, Tuple, Optional


class FactorMapping:
    """
    加载 factor_mapping.yaml（支持二级分类），并提供信号→(一级,二级) 的反查。
    
    兼容格式：
    - 新格式：一级 -> 二级 -> [signals]
    - 旧格式：一级 -> [signals]
    - 特殊格式：other -> [signals]（无二级分类）
    """
    
    def __init__(self, path: str):
        """
        初始化因子映射
        
        Args:
            path: factor_mapping.yaml 文件路径
        """
        self.path = path
        self._raw: Dict = {}
        self._signal2cat: Dict[str, Tuple[str, Optional[str]]] = {}
        self._load()

    def _load(self):
        """加载 YAML 配置文件并构建反查表（支持大小写不敏感匹配）"""
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"mapping not found: {self.path}")
        
        with open(self.path, 'r', encoding='utf-8') as f:
            self._raw = yaml.safe_load(f) or {}

        # 构建反查表：signal_name (lowercase) -> (level1, level2)
        # 🔥 使用小写作为键，实现大小写不敏感匹配
        self._signal2cat.clear()
        
        for l1, sub in (self._raw or {}).items():
            # 特殊处理 'other' 类别（扁平结构，无二级分类）
            if l1 == 'other' and isinstance(sub, list):
                for s in sub:
                    self._signal2cat[str(s).lower()] = ('other', None)
                continue

            # 二级结构：一级 -> {二级: [signals]}
            if isinstance(sub, dict):
                for l2, sigs in sub.items():
                    if sigs:  # 确保信号列表不为空
                        for s in sigs:
                            self._signal2cat[str(s).lower()] = (str(l1), str(l2))
            
            # 兼容旧版：一级 -> [signals]（无二级分类）
            elif isinstance(sub, list):
                for s in sub:
                    self._signal2cat[str(s).lower()] = (str(l1), None)

    def reload(self):
        """重新加载配置文件"""
        self._load()

    def category_of(self, signal_name: str) -> Optional[Tuple[str, Optional[str]]]:
        """
        查询信号的分类（大小写不敏感）
        
        Args:
            signal_name: 信号名称（任意大小写）
            
        Returns:
            (一级分类, 二级分类) 或 None（未映射）
            - 若属于 other: ('other', None)
            - 若为二级分类: (level1, level2)
            - 若仅一级分类: (level1, None)
            - 若未映射: None
        """
        # 🔥 转换为小写进行查询，实现大小写不敏感匹配
        return self._signal2cat.get(signal_name.lower())

    def categories(self):
        """
        返回所有一级分类列表
        
        Returns:
            一级分类名称列表
        """
        return list(self._raw.keys())

    @property
    def raw(self) -> Dict:
        """
        返回原始配置字典的副本
        
        Returns:
            配置字典
        """
        return self._raw.copy()
    
    def get_all_signals(self) -> set:
        """
        获取所有已映射的信号名称
        
        Returns:
            信号名称集合
        """
        return set(self._signal2cat.keys())
    
    def get_signals_by_category(self, level1: str, level2: Optional[str] = None) -> list:
        """
        根据分类获取信号列表
        
        Args:
            level1: 一级分类名称
            level2: 二级分类名称（可选）
            
        Returns:
            信号名称列表
        """
        if level1 not in self._raw:
            return []
        
        if level2 is None:
            # 返回一级分类下所有信号
            sub = self._raw[level1]
            if isinstance(sub, list):
                return sub
            elif isinstance(sub, dict):
                result = []
                for signals in sub.values():
                    if signals:
                        result.extend(signals)
                return result
            return []
        else:
            # 返回二级分类下的信号
            sub = self._raw.get(level1, {})
            if isinstance(sub, dict):
                return sub.get(level2, [])
            return []
    
    def statistics(self) -> Dict[str, int]:
        """
        获取映射统计信息
        
        Returns:
            包含统计信息的字典：
            - total_categories: 一级分类总数
            - total_subcategories: 二级分类总数
            - total_signals: 已映射信号总数
        """
        total_categories = len(self._raw)
        total_subcategories = 0
        total_signals = len(self._signal2cat)
        
        # 统计二级分类数量
        for l1, sub in self._raw.items():
            if isinstance(sub, dict):
                total_subcategories += len(sub)
        
        return {
            'total_categories': total_categories,
            'total_subcategories': total_subcategories,
            'total_signals': total_signals
        }

