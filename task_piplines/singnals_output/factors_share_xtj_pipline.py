#!/usr/bin/env python3
r"""
策略调仓文件导出脚本（10天调仓周期）

功能：
- 从给定的 start_date 到最新可用交易日生成模型预测（不做回测）
- 输出到 NAS: \\nas-sz\信息技术部\人工智能组\zyy_stk_pool\
- 文件名为: <strgyid>_<当天日期>.zyy（CSV格式内容）
- 文件内容包含表头，五列：adjust_date, strgyid, stkid, weight, datasource
- 调仓周期：10个交易日

说明：
- 利用现有 FactorGenerator 的推理流程（包含 dataset 段 + DB 补齐段）生成 df_pred
- 按10天调仓周期输出，权重根据模型预测值计算
- 默认 end_date 为当天；实际以数据可用为准
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Optional

import pandas as pd
from pandas.tseries.offsets import BDay

# 添加项目根目录到Python路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.data_service.pipelines.factor_utils import FactorGenerator
from src.data_service.pipelines.factor_utils.config_utils import (
    resolve_experiment_and_schema,
    detect_dataset_last_date,
)

# 导入完整配置类
from configs.backtest.model_backtest_config import ModelBacktestConfig


# ----------------------------
# 工具函数
# ----------------------------
def _normalize_stock_code(code: str) -> str:
    """标准化股票代码为6位数字"""
    if not isinstance(code, str):
        code = str(code)
    if "." in code:
        code = code.split(".", 1)[0]
    return code.zfill(6)


def _ensure_dir(path: str) -> None:
    """确保目录存在"""
    os.makedirs(path, exist_ok=True)


def _generate_rebalance_dates(start_date: str, end_date: str, rebalance_freq: int = 10) -> list[str]:
    """生成调仓日期列表（按交易日计算）"""
    from pandas.tseries.offsets import BDay
    
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    
    # 如果起始日期不是交易日，调整到下一个交易日
    if start_dt.weekday() >= 5:  # 周六(5)或周日(6)
        start_dt = start_dt + BDay(1)
        print(f"⚠️  起始日期{start_date}不是交易日，调整为{start_dt.strftime('%Y%m%d')}")
    
    # 生成调仓日期：从起始交易日开始，每rebalance_freq个交易日调仓一次
    rebalance_dates = []
    current_date = start_dt
    
    while current_date <= end_dt:
        rebalance_dates.append(current_date.strftime("%Y%m%d"))
        # 下一个调仓日：当前日期 + rebalance_freq个交易日
        current_date = current_date + BDay(rebalance_freq)
    
    return rebalance_dates


def _calculate_weights(df_pred: pd.DataFrame, method: str = "equal", top_n: int = None) -> pd.DataFrame:
    """根据模型预测值计算权重（默认等权重分配）"""
    df = df_pred.copy()
    
    if method == "equal":
        # 等权重分配：每个交易日的选定股票权重相等
        for date, df_date in df.groupby("trade_date"):
            if top_n and top_n > 0:
                # 选择因子值最大的前N只股票
                df_sorted = df_date.sort_values("model_pred", ascending=False)
                df_selected = df_sorted.head(top_n)
                n_stocks = len(df_selected)
                
                # 等权重分配：1/N
                weight = 1.0 / n_stocks if n_stocks > 0 else 0.0
                
                # 先将该日期所有股票权重设为0，再设置选中股票的权重
                df.loc[df["trade_date"] == date, "weight"] = 0.0
                df.loc[df_selected.index, "weight"] = weight
                
                print(f"   📊 {date}: 选择因子值前{top_n}只股票，实际选中{n_stocks}只")
            else:
                # 所有股票等权重分配
                n_stocks = len(df_date)
                weight = 1.0 / n_stocks if n_stocks > 0 else 0.0
                df.loc[df["trade_date"] == date, "weight"] = weight
    
    elif method == "rank":
        # 基于排名的权重分配（排名越高权重越大）- 暂未实现
        # TODO: 如需要基于模型预测值的加权，可在此实现
        for date, df_date in df.groupby("trade_date"):
            if top_n and top_n > 0:
                # 选择前N只股票
                df_sorted = df_date.sort_values("model_pred", ascending=False)
                df_selected = df_sorted.head(top_n)
                n_stocks = len(df_selected)
                
                # 目前仍使用等权重分配
                weight = 1.0 / n_stocks if n_stocks > 0 else 0.0
                
                # 先将该日期所有股票权重设为0，再设置选中股票的权重
                df.loc[df["trade_date"] == date, "weight"] = 0.0
                df.loc[df_selected.index, "weight"] = weight
            else:
                # 所有股票等权重分配
                df_sorted = df_date.sort_values("model_pred", ascending=False)
                n_stocks = len(df_sorted)
                weight = 1.0 / n_stocks if n_stocks > 0 else 0.0
                df.loc[df["trade_date"] == date, "weight"] = weight
    
    # 确保权重精度不超过8位小数
    df["weight"] = df["weight"].round(8)
    
    return df


def _filter_zero_weight_stocks(df: pd.DataFrame) -> pd.DataFrame:
    """
    过滤权重为0的股票
    
    备注：这是临时处理方案
    - 在数据补全阶段，有些不该出现的股票会被补全进来，权重变成0
    - 这些股票需要从最终结果中去除
    - TODO: 后续需要在数据补全阶段修复，避免不合理股票的补全
    """
    original_count = len(df)
    
    # 过滤权重为0的记录
    df_filtered = df[df["weight"] > 0].copy()
    
    filtered_count = len(df_filtered)
    removed_count = original_count - filtered_count
    
    if removed_count > 0:
        print(f"⚠️  [临时处理] 过滤权重为0的股票: {removed_count} 条记录")
        print(f"   原始记录数: {original_count}, 过滤后: {filtered_count}")
        print(f"   备注: 这些股票在数据补全阶段不应该出现，后续需要在源头修复")
    
    return df_filtered


def _export_rebalance_files(df_pred: pd.DataFrame, output_root: str, strgyid: str, 
                           rebalance_dates: list[str], weight_method: str = "equal", 
                           top_n: int = None, overwrite: bool = True) -> int:
    """按调仓日期导出策略文件，所有调仓记录保存在同一个文件中"""
    if df_pred is None or len(df_pred) == 0:
        return 0

    # 确保输出目录存在
    _ensure_dir(output_root)

    # 准备数据
    df = df_pred.copy()
    df["stkid"] = df["stock_code"].map(_normalize_stock_code)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    
    # 计算权重（根据指定方法）
    df = _calculate_weights(df, method=weight_method, top_n=top_n)

    # 收集所有调仓记录
    all_rebalance_records = []
    
    # 按调仓日期生成数据
    for adjust_date in rebalance_dates:
        # 查找该调仓日期对应的预测数据（取最接近的日期）
        available_dates = sorted(df["trade_date"].unique())
        
        # 找到小于等于调仓日期的最近日期
        target_date = None
        for date in reversed(available_dates):
            if date <= adjust_date:
                target_date = date
                break
        
        if target_date is None:
            continue
            
        df_rebalance = df[df["trade_date"] == target_date].copy()
        if df_rebalance.empty:
            continue
            
        # 构造该调仓日期的数据
        df_rebalance_out = pd.DataFrame({
            "adjust_date": adjust_date,
            "strgyid": strgyid,
            "stkid": df_rebalance["stkid"],
            "weight": df_rebalance["weight"],
            "datasource": "AI-人工智能组"
        })
        
        # 过滤权重为0的股票（临时处理方案）
        df_rebalance_out = _filter_zero_weight_stocks(df_rebalance_out)
        
        if not df_rebalance_out.empty:
            all_rebalance_records.append(df_rebalance_out)
            print(f"📅 处理调仓日期: {adjust_date}, 最终股票数: {len(df_rebalance_out)}")
        else:
            print(f"📅 处理调仓日期: {adjust_date}, 过滤后无有效股票")
    
    if not all_rebalance_records:
        print("❌ 未生成任何调仓记录")
        return 0
    
    # 合并所有调仓记录
    df_all = pd.concat(all_rebalance_records, ignore_index=True)
    df_all = df_all.sort_values(["adjust_date", "stkid"])
    
    # 文件路径（使用当天日期作为标识）
    today_str = pd.Timestamp.today().strftime("%Y%m%d")
    file_name = f"{strgyid}_{today_str}.zyy"
    file_path = os.path.join(output_root, file_name)
    
    if (not overwrite) and os.path.exists(file_path):
        print(f"⏭️ 文件已存在且未设置覆盖: {file_path}")
        return 0
        
    # 导出文件（包含表头）
    df_all.to_csv(file_path, index=False, float_format="%.8f")
    
    print(f"📁 导出策略文件: {file_path}")
    print(f"   📊 总调仓次数: {len(rebalance_dates)}")
    print(f"   📊 总记录数: {len(df_all)}")
    print(f"   📊 调仓日期范围: {min(rebalance_dates)} → {max(rebalance_dates)}")

    return 1


# 查找已导出的最后调仓日期，通过读取现有文件内容确定
def _load_factor_files(factor_files_path: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从现有因子文件目录中读取数据"""
    import glob
    
    print(f"📂 从因子文件目录读取数据: {factor_files_path}")
    
    if not os.path.exists(factor_files_path):
        raise FileNotFoundError(f"因子文件目录不存在: {factor_files_path}")
    
    # 查找所有CSV文件
    csv_files = glob.glob(os.path.join(factor_files_path, "*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"在目录 {factor_files_path} 中未找到CSV文件")
    
    print(f"📊 发现 {len(csv_files)} 个因子文件")
    
    # 解析文件名获取日期，过滤日期范围
    valid_files = []
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    
    for csv_file in csv_files:
        filename = os.path.basename(csv_file)
        # 提取日期：文件名格式如 TSVIT_PVHF_10d_v1.20240115.csv
        parts = filename.split('.')
        if len(parts) >= 2:
            try:
                date_str = parts[-2]  # 倒数第二部分应该是日期
                file_date = pd.to_datetime(date_str)
                if start_dt <= file_date <= end_dt:
                    valid_files.append((csv_file, date_str))
            except Exception:
                continue
    
    if not valid_files:
        raise ValueError(f"在指定日期范围 {start_date}-{end_date} 内未找到有效的因子文件")
    
    print(f"📅 日期范围内有效文件: {len(valid_files)} 个")
    
    # 读取并合并所有文件
    all_data = []
    for csv_file, date_str in sorted(valid_files, key=lambda x: x[1]):
        try:
            # 读取无表头的CSV文件：stock_code, model_pred
            df_day = pd.read_csv(csv_file, header=None, names=['stock_code', 'model_pred'])
            df_day['trade_date'] = date_str
            
            # 过滤model_pred为0的记录（提前过滤）
            original_count = len(df_day)
            df_day = df_day[df_day['model_pred'] != 0.0].copy()
            filtered_count = len(df_day)
            
            if original_count > filtered_count:
                print(f"   📁 {date_str}: {original_count} → {filtered_count} 条记录 (过滤了{original_count-filtered_count}个0值)")
            else:
                print(f"   📁 {date_str}: {filtered_count} 条记录")
                
            all_data.append(df_day)
            
        except Exception as e:
            print(f"⚠️  读取文件失败: {csv_file}, 错误: {e}")
            continue
    
    if not all_data:
        raise ValueError("所有因子文件都读取失败")
    
    # 合并所有数据
    df_all = pd.concat(all_data, ignore_index=True)
    
    # 确保trade_date格式一致（转换为datetime）
    df_all['trade_date'] = pd.to_datetime(df_all['trade_date'])
    
    print(f"✅ 成功读取因子数据: {len(df_all):,} 条记录")
    
    return df_all


def _find_last_rebalance_date(output_root: str, strgyid: str) -> Optional[str]:
    """查找已导出文件中的最后调仓日期"""
    import glob
    
    if not os.path.exists(output_root):
        return None
    
    # 查找所有匹配的策略文件：{strgyid}_*.zyy
    pattern = os.path.join(output_root, f"{strgyid}_*.zyy")
    matching_files = glob.glob(pattern)
    
    if not matching_files:
        return None
    
    # 从所有匹配文件中找到最新的调仓日期
    latest_date = None
    
    for file_path in matching_files:
        try:
            # 读取文件，获取最新调仓日期
            df_existing = pd.read_csv(file_path)
            if "adjust_date" in df_existing.columns:
                max_date = str(df_existing["adjust_date"].max())
                # 验证日期格式
                try:
                    _ = datetime.strptime(max_date, "%Y%m%d")
                    if (latest_date is None) or (max_date > latest_date):
                        latest_date = max_date
                except Exception:
                    continue
        except Exception:
            continue
    
    return latest_date


# ----------------------------
# 主流程
# ----------------------------
def run_export(start_date: str,
               strgyid: str,
               output_root: str = r"\\nas-sz\信息技术部\人工智能组\zyy_stk_pool",
               data_source: str = "model",
               model_path: Optional[str] = None,
               factor_files_path: Optional[str] = None,
               dataset_path: Optional[str] = None,
               end_date: Optional[str] = None,
               rebalance_freq: int = 10,
               weight_method: str = "equal",
               top_n: int = None,
               overwrite: bool = True,
               resume: bool = True) -> None:
    # 1) 计算实际起始日期（断点续跑）
    effective_start = start_date
    if resume:
        last_rebalance = _find_last_rebalance_date(output_root, strgyid)
        if last_rebalance is not None:
            # 下一个调仓日期
            from pandas.tseries.offsets import BDay
            next_rebalance = (pd.to_datetime(last_rebalance) + BDay(rebalance_freq)).strftime("%Y%m%d")
            if next_rebalance > effective_start:
                print(f"🔁 检测到已有导出截至调仓日期 {last_rebalance}，从 {next_rebalance} 开始增量导出")
                effective_start = next_rebalance

    # 2) 计算实际结束日期（为空则自动探测最新可用日期）
    def _auto_detect_end_date() -> str:
        if data_source == "factor_files":
            # 对于因子文件模式，使用昨天作为默认结束日期
            try:
                yesterday = (pd.Timestamp.today() - BDay(1)).strftime("%Y%m%d")
            except Exception:
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            return yesterday
        else:
            # 原有的模型模式逻辑
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

    # 3) 获取预测数据
    print(f"📊 数据源: {data_source}")
    
    if data_source == "factor_files":
        # 直接从因子文件读取数据
        if not factor_files_path:
            raise ValueError("使用factor_files模式时，必须指定factor_files_path参数")
        df_pred = _load_factor_files(factor_files_path, effective_start, effective_end)
    else:
        # 模型推理模式
        if not model_path:
            raise ValueError("使用model模式时，必须指定model_path参数")
            
        print(f"🤖 使用模型推理生成因子数据")
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

        # 仅做推理，拿到 df_pred: ['trade_date','stock_code','model_pred']
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

    # 5) 生成调仓日期并导出
    # 注意：导出过程中会自动过滤权重为0的股票（临时处理方案）
    # 这些0权重股票是数据补全阶段不合理引入的，过滤后保留原dataloader生成的有效股票
    print(f"⚖️  权重分配方法: {weight_method} ({'等权重分配' if weight_method == 'equal' else '基于排名加权'})")
    if top_n:
        print(f"📊 股票选择策略: 因子值最大前{top_n}只股票")
    else:
        print(f"📊 股票选择策略: 使用所有有效股票")
    
    rebalance_dates = _generate_rebalance_dates(effective_start, effective_end, rebalance_freq)
    if not rebalance_dates:
        print("❌ 未生成任何调仓日期，退出")
        return
        
    print(f"📅 调仓日期列表: {rebalance_dates}")
    
    written = _export_rebalance_files(df_pred, output_root, strgyid, rebalance_dates, weight_method, top_n, overwrite=overwrite)
    print(f"✅ 导出完成：{written} 个调仓周期文件，目录: {output_root}")


def parse_args():
    parser = argparse.ArgumentParser(description="导出策略调仓文件（10天调仓周期）")
    # 默认从 20210101 开始；结束日期默认自动探测到最近可用交易日
    parser.add_argument("--start_date", default="20210101", help="开始日期 YYYYMMDD")
    parser.add_argument("--end_date",   default=None, help="结束日期 YYYYMMDD，默认自动探测最近交易日")

    parser.add_argument(
        "--data_source",
        default="model",
        choices=["model", "factor_files"],
        help="数据源选择：model=模型推理生成, factor_files=直接读取现有因子文件(更快)"
    )
    parser.add_argument(
        "--factor_files_path",
        default=r"\\space\iqshare\AI_share\AI_signals\TSVIT_PVHF_10d_v1",
        help="现有因子文件路径（当data_source=factor_files时使用）"
    )
    parser.add_argument(
        "--model_path",
        default=r'outputs\TSViT_MODEL\use_symmetric_h64_l6_lr4e-05_wd4e-01_attn_pv_v5_pv_v5_pvhflow_solid300_20250916_192818',
        help="训练好的模型目录（当data_source=model时使用）"
    )
    parser.add_argument(
        "--strgyid",
        default="111188",
        help="策略ID，用于文件命名（默认111188）"
    )
    parser.add_argument(
        "--rebalance_freq", 
        type=int, 
        default=10, 
        help="调仓频率（交易日数，默认10天）"
    )
    parser.add_argument(
        "--weight_method",
        default="equal",
        choices=["equal", "rank"],
        help="权重分配方法：equal=等权重(默认), rank=基于排名加权"
    )
    parser.add_argument(
        "--top_n",
        type=int,
        default=50,
        help="选择因子值最大的前N只股票（默认50，设为0或不设置表示使用所有股票）"
    )

    parser.add_argument("--output_root", default=r"\\nas-sz\信息技术部\人工智能组\zyy_stk_pool", help="输出根目录（NAS路径）")
    parser.add_argument("--dataset_path", default=None, help="数据集目录，可不传（从实验配置自动解析）")
    parser.add_argument("--no_overwrite", action="store_true", help="存在文件时不覆盖")
    parser.add_argument("--no_resume", action="store_true", help="禁用断点续跑（总是从 start_date 开始）")
    return parser.parse_args()


def main():
    args = parse_args()
    
    # 处理top_n参数：0表示使用所有股票
    top_n = args.top_n if args.top_n > 0 else None
    
    run_export(
        start_date=args.start_date,
        strgyid=args.strgyid,
        output_root=args.output_root,
        data_source=args.data_source,
        model_path=args.model_path,
        factor_files_path=args.factor_files_path,
        dataset_path=args.dataset_path,
        end_date=args.end_date,
        rebalance_freq=args.rebalance_freq,
        weight_method=args.weight_method,
        top_n=top_n,
        overwrite=not args.no_overwrite,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()


