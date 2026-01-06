#!/usr/bin/env python3
r"""
因子日度CSV导出脚本

功能：
- 从给定的 start_date 到最新可用交易日生成模型预测（不做回测）
- 输出到 NAS: \\space\iqshare\AI_share\AI_signals\<factor_name>\
- 文件名为: <factor_name>.<YYYYMMDD>.csv
- 文件内容无表头，两列：stock_code, model_pred（示例："000001,0.009423479"）

说明：
- 利用现有 FactorGenerator 的推理流程（包含 dataset 段 + DB 补齐段）生成 df_pred
- 默认 end_date 为当天；实际以数据可用为准
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Optional

import pandas as pd

from src.data_service.pipelines.factor_utils import FactorGenerator
from src.data_service.pipelines.factor_utils.config_utils import (
    resolve_experiment_and_schema,
    detect_dataset_last_date,
)
from pandas.tseries.offsets import BDay

# 导入完整配置类
from configs.backtest.model_backtest_config import ModelBacktestConfig


# ----------------------------
# 工具函数
# ----------------------------
def _normalize_stock_code(code: str) -> str:
    if not isinstance(code, str):
        code = str(code)
    if "." in code:
        code = code.split(".", 1)[0]
    return code.zfill(6)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _export_daily_csvs(df_pred: pd.DataFrame, output_root: str, factor_name: str, overwrite: bool = True) -> int:
    if df_pred is None or len(df_pred) == 0:
        return 0

    # 目标目录：<output_root>/<factor_name>/
    factor_dir = os.path.join(output_root, factor_name)
    _ensure_dir(factor_dir)

    # 规范化代码；转为日期键
    df = df_pred.copy()
    df["stock_code"] = df["stock_code"].map(_normalize_stock_code)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")

    # 按天导出
    written = 0
    for trade_date, df_day in df.groupby("trade_date", sort=True):
        df_out = df_day[["stock_code", "model_pred"]].sort_values("stock_code")
        file_name = f"{factor_name}.{trade_date}.csv"
        file_path = os.path.join(factor_dir, file_name)

        if (not overwrite) and os.path.exists(file_path):
            continue

        df_out.to_csv(file_path, index=False, header=False, float_format="%.9g")
        written += 1

    return written


# 查找已导出的最后日期（YYYYMMDD），无则返回 None
def _find_last_exported_date(output_root: str, factor_name: str) -> Optional[str]:
    factor_dir = os.path.join(output_root, factor_name)
    if not os.path.isdir(factor_dir):
        return None

    pattern = re.compile(rf"^{re.escape(factor_name)}\.(\d{{8}})\.csv$")
    last_date: Optional[str] = None
    try:
        for fname in os.listdir(factor_dir):
            m = pattern.match(fname)
            if not m:
                continue
            date_str = m.group(1)
            try:
                _ = datetime.strptime(date_str, "%Y%m%d")
            except Exception:
                continue
            if (last_date is None) or (date_str > last_date):
                last_date = date_str
    except FileNotFoundError:
        return None

    return last_date


# ----------------------------
# 主流程
# ----------------------------
def run_export(start_date: str,
               model_path: str,
               factor_name: str,
               output_root: str = r"\\space\iqshare\AI_share\AI_signals",
               dataset_path: Optional[str] = None,
               end_date: Optional[str] = None,
               overwrite: bool = True,
               resume: bool = True) -> None:
    # 1) 计算实际起始日期（断点续跑）
    effective_start = start_date
    if resume:
        last_exported = _find_last_exported_date(output_root, factor_name)
        if last_exported is not None:
            next_day = (datetime.strptime(last_exported, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
            if next_day > effective_start:
                print(f"🔁 检测到已有导出截至 {last_exported}，从 {next_day} 开始增量导出")
                effective_start = next_day

    # 2) 计算实际结束日期（为空则自动探测最新可用日期）
    def _auto_detect_end_date() -> str:
        try:
            resolved = resolve_experiment_and_schema(model_path, fallback_dataset_path=dataset_path)
            ds_last = detect_dataset_last_date(resolved.dataset_path)
        except Exception:
            resolved = None
            ds_last = None

        db_last = None
        # 可选：尝试从DB获取最新日期（使用第一个特征表）
        try:
            if resolved and resolved.features_tables:
                from src.data_service.pipelines.factor_utils.db_fetcher import get_available_date_range
                for tbl in resolved.features_tables:
                    try:
                        _, max_date = get_available_date_range(tbl)
                        if (db_last is None) or (max_date > db_last):
                            db_last = max_date
                    except Exception:
                        continue
        except Exception:
            pass

        # 优先目标：昨天（或上一个工作日），以便尽可能拉到最新
        try:
            yesterday = (pd.Timestamp.today() - BDay(1)).strftime("%Y%m%d")
        except Exception:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        # 如果DB能返回更晚日期，取更晚者；否则至少用昨天
        cand = [yesterday]
        if db_last:
            cand.append(db_last)
        # ds_last 仅用于日志参考，不作为上限
        chosen = max(cand)
        return chosen

    effective_end = end_date if end_date else _auto_detect_end_date()
    if effective_end < effective_start:
        effective_end = effective_start

    # 3) 构造配置并加载模型
    cfg = ModelBacktestConfig()
    cfg.model_path = model_path
    cfg.dataset_path = dataset_path
    cfg.start_date = effective_start
    cfg.end_date = effective_end
    cfg.enable_factor_save = False
    cfg.factor_save_formats = ["csv"]
    cfg.factor_target_format = "backtest"

    fg = FactorGenerator(cfg)
    fg._load_model()

    # 4) 仅做推理，拿到 df_pred: ['trade_date','stock_code','model_pred']
    df_pred = fg._generate_model_predictions()

    if df_pred is None or len(df_pred) == 0:
        print("❌ 未生成任何预测结果，退出")
        return

    # 过滤日期区间
    df_pred["trade_date"] = pd.to_datetime(df_pred["trade_date"])  # 统一为TS
    start_ts = pd.to_datetime(effective_start)
    end_ts = pd.to_datetime(effective_end)
    df_pred = df_pred[(df_pred["trade_date"] >= start_ts) & (df_pred["trade_date"] <= end_ts)].copy()

    if df_pred.empty:
        print("❌ 过滤后无数据，退出")
        return

    min_d = df_pred["trade_date"].min().strftime("%Y-%m-%d")
    max_d = df_pred["trade_date"].max().strftime("%Y-%m-%d")
    print(f"📊 预测数据范围: {min_d} → {max_d}，记录数: {len(df_pred):,}")
    if max_d.replace('-', '') < effective_end:
        print(f"⚠️ 注意：预测结果最晚日期({max_d}) 早于目标结束日期({effective_end})，可能因为DB暂无最新数据或补齐失败")

    # 5) 按天写CSV
    written = _export_daily_csvs(df_pred, output_root, factor_name, overwrite=overwrite)
    out_dir = os.path.join(output_root, factor_name)
    print(f"✅ 导出完成：{written} 个文件，目录: {out_dir}")


def parse_args():
    parser = argparse.ArgumentParser(description="导出因子为按日CSV（无表头）")
    # 默认从 20210101 开始；结束日期默认自动探测到最近可用交易日
    parser.add_argument("--start_date", default="20210101", help="开始日期 YYYYMMDD")
    parser.add_argument("--end_date",   default=None, help="结束日期 YYYYMMDD，默认自动探测最近交易日")

    parser.add_argument(
        "--model_path",
        default=r'outputs\TSViT_MODEL\use_symmetric_h64_l6_lr4e-05_wd4e-01_attn_pv_v5_pv_v5_pvhflow_solid300_20250916_192818',
        help="训练好的模型目录"
    )
    parser.add_argument(
        "--factor_name",
        default="TSVIT_PVHF_10d_v1",
        help="导出因子名（用于文件名/目录），默认突出 量价 高频：TSVIT_PVHF_10d"
    )

    parser.add_argument("--output_root", default=r"\\space\iqshare\AI_share\AI_signals", help="输出根目录（NAS路径）")
    parser.add_argument("--dataset_path", default=None, help="数据集目录，可不传（从实验配置自动解析）")
    parser.add_argument("--no_overwrite", action="store_true", help="存在文件时不覆盖")
    parser.add_argument("--no_resume", action="store_true", help="禁用断点续跑（总是从 start_date 开始）")
    return parser.parse_args()


def main():
    args = parse_args()
    run_export(
        start_date=args.start_date,
        model_path=args.model_path,
        factor_name=args.factor_name,
        output_root=args.output_root,
        dataset_path=args.dataset_path,
        end_date=args.end_date,
        overwrite=not args.no_overwrite,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()


