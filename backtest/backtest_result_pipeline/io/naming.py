"""
命名规范

负责：
- safe_strategy_name 生成（白名单/截断/去重）
- 文件命名规范
"""

import hashlib
import re
from typing import Dict, List, Set


def to_safe_name(
    original_name: str,
    max_length: int = 64,
    whitelist_pattern: str = r"[^a-zA-Z0-9_-]"
) -> str:
    """
    将原始名称转换为安全的文件名
    
    规则：
    - 白名单：[a-zA-Z0-9_-]，其余字符替换为 _
    - 长度截断：默认 64
    - 去除首尾 _ 和重复 _
    """
    # 替换非白名单字符
    safe = re.sub(whitelist_pattern, "_", original_name)
    
    # 去除重复 _
    safe = re.sub(r"_+", "_", safe)
    
    # 去除首尾 _
    safe = safe.strip("_")
    
    # 截断
    if len(safe) > max_length:
        safe = safe[:max_length].rstrip("_")
    
    # 如果为空，使用默认名称
    if not safe:
        safe = "unnamed_strategy"
    
    return safe


def deduplicate_names(
    names: List[str],
    existing: Set[str] = None
) -> Dict[str, str]:
    """
    去重策略名称（追加短 hash 后缀）
    
    Args:
        names: 原始名称列表
        existing: 已存在的名称集合
    
    Returns:
        原始名称 -> 安全名称的映射
    """
    if existing is None:
        existing = set()
    
    result = {}
    used_names = set(existing)
    
    for original in names:
        safe = to_safe_name(original)
        
        if safe not in used_names:
            result[original] = safe
            used_names.add(safe)
        else:
            # 追加短 hash 后缀
            hash_suffix = hashlib.md5(original.encode()).hexdigest()[:8]
            # 截断 safe 以留出 hash 空间
            max_base = 64 - 9  # 8 for hash + 1 for _
            safe_base = safe[:max_base].rstrip("_")
            safe_with_hash = f"{safe_base}_{hash_suffix}"
            
            # 确保唯一
            counter = 0
            candidate = safe_with_hash
            while candidate in used_names:
                counter += 1
                candidate = f"{safe_base}_{hash_suffix}_{counter}"
            
            result[original] = candidate
            used_names.add(candidate)
    
    return result


def get_nav_csv_filename(benchmark_code: str, safe_strategy_name: str) -> str:
    """生成 NAV CSV 文件名"""
    safe_benchmark = benchmark_code.replace(".", "")
    return f"nav_with_benchmark_{safe_benchmark}_{safe_strategy_name}.csv"


def get_nav_png_filename(benchmark_code: str, safe_strategy_name: str) -> str:
    """生成 NAV PNG 文件名"""
    safe_benchmark = benchmark_code.replace(".", "")
    return f"nav_with_benchmark_{safe_benchmark}_{safe_strategy_name}.png"


def get_signals_csv_filename(safe_strategy_name: str) -> str:
    """生成 signals CSV 文件名"""
    return f"signals_{safe_strategy_name}.csv"


def get_excel_filename(run_id: str) -> str:
    """生成 Excel 文件名"""
    return f"模型回测结果_{run_id}.xlsx"


def get_detailed_log_filename(safe_strategy_name: str) -> str:
    """生成详细交易日志文件名"""
    return f"detailed_trading_log_{safe_strategy_name}.csv"
