"""
原子写入

负责：
- 先写到 *.tmp，再 rename 到最终文件名
- no-overwrite 模式下发现同名文件的处理
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Union

import pandas as pd


def atomic_write_df(
    df: pd.DataFrame,
    target_path: Union[str, Path],
    write_func: Optional[Callable] = None,
    no_overwrite: bool = True,
    **kwargs
) -> Path:
    """
    原子写入 DataFrame
    
    Args:
        df: 要写入的 DataFrame
        target_path: 目标路径
        write_func: 自定义写入函数，默认为 df.to_csv
        no_overwrite: 是否禁止覆盖
        **kwargs: 传递给写入函数的参数
    
    Returns:
        实际写入的路径
    
    Raises:
        FileExistsError: 当 no_overwrite=True 且文件已存在
    """
    target_path = Path(target_path)
    
    # 检查是否存在
    if no_overwrite and target_path.exists():
        raise FileExistsError(f"文件已存在且 no_overwrite=True: {target_path}")
    
    # 确保目录存在
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 写入临时文件
    suffix = target_path.suffix
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".tmp{suffix}", dir=target_path.parent)
    os.close(tmp_fd)
    tmp_path = Path(tmp_path)
    
    try:
        if write_func is not None:
            write_func(df, tmp_path, **kwargs)
        else:
            # 默认 CSV 写入
            df.to_csv(tmp_path, index=False, encoding="utf-8-sig", **kwargs)
        
        # 原子 rename
        if no_overwrite and target_path.exists():
            # 再次检查（防止竞态）
            tmp_path.unlink()
            raise FileExistsError(f"文件已存在且 no_overwrite=True: {target_path}")
        
        shutil.move(str(tmp_path), str(target_path))
        return target_path
    
    except Exception:
        # 清理临时文件
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def atomic_write_json(
    data: dict,
    target_path: Union[str, Path],
    no_overwrite: bool = True,
    indent: int = 2
) -> Path:
    """
    原子写入 JSON
    
    Args:
        data: 要写入的字典
        target_path: 目标路径
        no_overwrite: 是否禁止覆盖
        indent: JSON 缩进
    
    Returns:
        实际写入的路径
    """
    import json
    
    target_path = Path(target_path)
    
    if no_overwrite and target_path.exists():
        raise FileExistsError(f"文件已存在且 no_overwrite=True: {target_path}")
    
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp.json", dir=target_path.parent)
    os.close(tmp_fd)
    tmp_path = Path(tmp_path)
    
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent, default=str)
        
        if no_overwrite and target_path.exists():
            tmp_path.unlink()
            raise FileExistsError(f"文件已存在且 no_overwrite=True: {target_path}")
        
        shutil.move(str(tmp_path), str(target_path))
        return target_path
    
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def atomic_write_figure(
    fig,
    target_path: Union[str, Path],
    no_overwrite: bool = True,
    dpi: int = 150,
    **kwargs
) -> Path:
    """
    原子写入 matplotlib figure
    
    Args:
        fig: matplotlib figure 对象
        target_path: 目标路径
        no_overwrite: 是否禁止覆盖
        dpi: 图片分辨率
        **kwargs: 传递给 savefig 的参数
    
    Returns:
        实际写入的路径
    """
    target_path = Path(target_path)
    
    if no_overwrite and target_path.exists():
        raise FileExistsError(f"文件已存在且 no_overwrite=True: {target_path}")
    
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    suffix = target_path.suffix
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".tmp{suffix}", dir=target_path.parent)
    os.close(tmp_fd)
    tmp_path = Path(tmp_path)
    
    try:
        fig.savefig(tmp_path, dpi=dpi, **kwargs)
        
        if no_overwrite and target_path.exists():
            tmp_path.unlink()
            raise FileExistsError(f"文件已存在且 no_overwrite=True: {target_path}")
        
        shutil.move(str(tmp_path), str(target_path))
        return target_path
    
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
