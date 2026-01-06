"""
目录布局管理

负责：
- run_id 生成与冲突处理
- run_dir 子目录结构创建
- 路径归一化（WSL/Windows 兼容）
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from backtest.backtest_result_pipeline.types import RunContext


def normalize_path(path_str: Optional[str], project_root: Optional[Path] = None) -> Optional[Path]:
    """
    归一化路径（WSL/Windows 兼容）
    
    复用 FactorGenerator._normalize_path 的同口径逻辑
    """
    if path_str is None:
        return None

    raw = str(path_str)

    # Translate Windows drive (e.g., F:\foo) to /mnt/f/foo when running on Linux/WSL
    if os.name != "nt" and len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        drive = raw[0].lower()
        remainder = raw[2:].lstrip("\\/")
        raw = f"/mnt/{drive}/{remainder}"

    # Unify separators so "outputs\TSViT_MODEL" works
    raw = raw.replace("\\", "/")

    path = Path(raw)
    if not path.is_absolute():
        if project_root is not None:
            path = project_root / path
        else:
            # 默认使用当前文件往上 3 级作为项目根目录
            # layout.py 位于 backtest/backtest_result_pipeline/io/layout.py
            # parents[0]=io, [1]=backtest_result_pipeline, [2]=backtest, [3]=AIQuantLab
            default_root = Path(__file__).resolve().parents[3]
            path = default_root / path

    return path.expanduser()


def generate_run_id(
    benchmark_code: str,
    start_date: str,
    end_date: str,
    timestamp: Optional[datetime] = None
) -> str:
    """
    生成 run_id
    
    格式：YYYYMMDD_HHMMSS_<benchmark>_<start>_<end>
    例如：20251215_223000_000852SH_20210101_20241231
    """
    if timestamp is None:
        timestamp = datetime.now()
    
    ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
    
    # 清理 benchmark_code（移除点号等）
    safe_benchmark = benchmark_code.replace(".", "")
    
    return f"{ts_str}_{safe_benchmark}_{start_date}_{end_date}"


def resolve_run_dir(
    base_path: Path,
    run_id: str,
    auto_suffix: bool = True,
    overwrite: bool = False
) -> Path:
    """
    解析 run_dir，处理冲突
    
    Args:
        base_path: bt_results 根目录
        run_id: 运行 ID
        auto_suffix: 是否自动追加后缀避免冲突
        overwrite: 是否允许覆盖
    
    Returns:
        最终的 run_dir 路径
    
    Raises:
        FileExistsError: 当 overwrite=False 且 auto_suffix=False 时，目录已存在
    """
    run_dir = base_path / run_id
    
    if not run_dir.exists():
        return run_dir
    
    if overwrite:
        return run_dir
    
    if auto_suffix:
        # 自动追加后缀 _001, _002, ... _999
        for i in range(1, 1000):
            candidate = base_path / f"{run_id}_{i:03d}"
            if not candidate.exists():
                return candidate
        raise FileExistsError(f"无法找到可用的 run_id 后缀（已尝试 001-999）: {run_id}")
    else:
        raise FileExistsError(
            f"run_dir 已存在且 overwrite=False: {run_dir}\n"
            f"使用 --overwrite 参数覆盖，或删除该目录后重试。"
        )


def create_run_context(
    model_path: str,
    run_id: str,
    auto_suffix: bool = True,
    overwrite: bool = False,
    bt_results_subdir: str = "bt_results"
) -> RunContext:
    """
    创建运行上下文（含目录结构）
    
    Args:
        model_path: 模型目录
        run_id: 运行 ID
        auto_suffix: 是否自动追加后缀避免冲突
        overwrite: 是否允许覆盖
        bt_results_subdir: bt_results 子目录名
    
    Returns:
        RunContext 实例
    """
    # 归一化 model_path
    model_path_normalized = normalize_path(model_path)
    if model_path_normalized is None:
        raise ValueError("model_path 不能为空")
    
    # bt_results 根目录
    base_path = model_path_normalized / bt_results_subdir
    
    # 解析 run_dir
    run_dir = resolve_run_dir(base_path, run_id, auto_suffix=auto_suffix, overwrite=overwrite)
    
    # 创建目录结构
    config_dir = run_dir / "config"
    data_dir = run_dir / "data"
    factors_dir = data_dir / "factors"
    nav_dir = data_dir / "nav"
    signals_dir = data_dir / "signals"
    tables_dir = run_dir / "tables"
    plots_dir = run_dir / "plots"
    logs_dir = run_dir / "logs"
    
    # 创建所有目录
    for d in [config_dir, factors_dir, nav_dir, signals_dir, tables_dir, plots_dir, logs_dir]:
        d.mkdir(parents=True, exist_ok=True)
    
    return RunContext(
        run_id=run_dir.name,  # 使用实际的目录名（可能带后缀）
        run_dir=run_dir,
        config_dir=config_dir,
        data_dir=data_dir,
        factors_dir=factors_dir,
        nav_dir=nav_dir,
        signals_dir=signals_dir,
        tables_dir=tables_dir,
        plots_dir=plots_dir,
        logs_dir=logs_dir
    )
