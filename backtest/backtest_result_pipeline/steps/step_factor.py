"""
Step: 因子生成

职责：
- 使用共享因子缓存 <model_path>/bt_results/factors
- 缺失日期范围时进行增量补齐并写回缓存
- 返回回测所需 df_factor（不落 per-run factors 文件）
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from backtest.backtest_result_pipeline.types import RunContext
from backtest.backtest_result_pipeline.io.factor_cache import FactorCacheManager

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig

logger = logging.getLogger(__name__)


def _factor_to_pred(df_factor: pd.DataFrame) -> pd.DataFrame:
    """将回测格式 df_factor 转为缓存用 df_pred。"""
    required = {"trade_date", "stock_code", "value"}
    if not required.issubset(df_factor.columns):
        raise ValueError(f"df_factor missing columns: {required - set(df_factor.columns)}")

    df_pred = df_factor.copy()
    df_pred["trade_date"] = pd.to_datetime(df_pred["trade_date"], errors="coerce")
    df_pred = df_pred.dropna(subset=["trade_date"])

    codes = df_pred["stock_code"].astype(str).str.upper().str.strip()
    codes = codes.str.replace(r"\.(SZ|SH|BJ)$", "", regex=True)
    codes = codes.str.split(".").str[0]
    codes = codes.replace({"": pd.NA, "NAN": pd.NA})
    mask = codes.notna()
    codes.loc[mask] = codes.loc[mask].str.zfill(6)

    df_pred["stock_code"] = codes
    df_pred["trade_date"] = df_pred["trade_date"].dt.strftime("%Y-%m-%d")
    df_pred = df_pred.rename(columns={"value": "model_pred"})
    return df_pred[["trade_date", "stock_code", "model_pred"]].dropna(subset=["trade_date", "stock_code"])


def _run_factor_generator_for_range(cfg: "ModelBacktestConfig", start_date: str, end_date: str) -> pd.DataFrame:
    """在指定区间内运行 FactorGenerator（不写文件），返回 df_factor。"""
    from src.data_service.pipelines.factor_utils import FactorGenerator

    original_start = cfg.start_date
    original_end = cfg.end_date
    original_save = getattr(cfg, "enable_factor_save", True)

    cfg.start_date = start_date
    cfg.end_date = end_date
    cfg.enable_factor_save = False

    try:
        generator = FactorGenerator(cfg)
        return generator.run()
    finally:
        cfg.start_date = original_start
        cfg.end_date = original_end
        cfg.enable_factor_save = original_save


def run_step_factor(
    cfg: "ModelBacktestConfig",
    run_ctx: RunContext
) -> pd.DataFrame:
    """
    生成因子数据
    
    Args:
        cfg: ModelBacktestConfig 配置对象
        run_ctx: 运行上下文
    
    Returns:
        df_factor: 因子 DataFrame
    
    Side effects:
        - 使用共享缓存目录 <model_path>/bt_results/factors
        - 缺失区间会触发增量补齐并写回缓存
    """
    logger.info("Step Factor: 生成因子数据...")

    cache_dir = Path(cfg.model_path) / "bt_results" / "factors"
    cache = FactorCacheManager(cache_dir)

    missing_ranges = cache.compute_missing_ranges(cfg.start_date, cfg.end_date)
    if missing_ranges:
        logger.info(f"   缓存缺失区间: {missing_ranges}")
        for start_date, end_date in missing_ranges:
            logger.info(f"   补齐区间: {start_date} -> {end_date}")
            df_factor_new = _run_factor_generator_for_range(cfg, start_date, end_date)
            if df_factor_new.empty:
                raise RuntimeError(f"因子补齐失败：区间 {start_date}-{end_date} 为空")
            df_pred_new = _factor_to_pred(df_factor_new)
            cache.merge_write_pred(df_pred_new)
    else:
        logger.info("   共享缓存覆盖回测区间，跳过因子推理")

    df_pred = cache.load_pred(cfg.start_date, cfg.end_date)
    if df_pred.empty:
        raise RuntimeError("因子生成失败：共享缓存为空且补齐无结果")

    from src.data_service.pipelines.factor_utils import convert

    df_factor = convert(df_pred, target=getattr(cfg, "factor_target_format", "backtest"), cfg=cfg)

    logger.info(f"   因子准备完成: {len(df_factor)} 条记录")
    if not df_factor.empty:
        logger.info(f"   日期范围: {df_factor['trade_date'].min()} - {df_factor['trade_date'].max()}")
        logger.info(f"   股票数量: {df_factor['stock_code'].nunique()}")

    return df_factor
