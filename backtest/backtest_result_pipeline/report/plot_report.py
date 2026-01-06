"""
图表报告生成

生成 NAV 曲线图（策略/基准/超额净值）- 投行/量化报告风格
"""

import logging
from pathlib import Path
from typing import Union

import pandas as pd
import numpy as np

from backtest.backtest_result_pipeline.types import BenchmarkNavResult
from backtest.backtest_result_pipeline.io.atomic_write import atomic_write_figure

logger = logging.getLogger(__name__)


def plot_nav_curve(
    nav_result: BenchmarkNavResult,
    output_path: Union[str, Path],
    no_overwrite: bool = True,
    figsize: tuple = (14, 8),
    dpi: int = 300,
    style: str = "professional",  # professional, simple, dark
    layout: str = "tearsheet"     # tearsheet(推荐), single_right, single_overlay(旧版兼容)
) -> Path:
    """
    绘制 NAV 曲线图（投行/量化报告风格 - 适合PPT展示）
    
    Args:
        nav_result: 基准对齐结果
        output_path: 输出路径
        no_overwrite: 是否禁止覆盖
        figsize: 图片尺寸（默认14x8，适合PPT 16:9）
        dpi: 分辨率（默认300，高清输出）
        style: 图表风格 (professional/simple/dark)
        layout: 布局方式
            - tearsheet: 主图+底部超额面板+右侧信息栏（推荐，不遮挡）
            - single_right: 单图+右侧信息栏（简洁版）
            - single_overlay: 单图+图内overlay（旧版兼容，不推荐）
    
    Returns:
        实际写入的路径
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib 未安装，跳过图表生成")
        return None
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    nav_df = nav_result.nav_df.copy()
    nav_df["trade_date"] = pd.to_datetime(nav_df["trade_date"])
    nav_df = nav_df.sort_values("trade_date")
    excess_col = "excess_nav_diff" if "excess_nav_diff" in nav_df.columns else "excess_nav"
    nav_df = nav_df.dropna(subset=["trade_date", "strategy_nav", "benchmark_nav", excess_col])

    if nav_df.empty:
        logger.warning("nav_df 为空，跳过图表生成")
        return None

    start_date = nav_df["trade_date"].iloc[0]
    end_date = nav_df["trade_date"].iloc[-1]
    date_range_days = int((end_date - start_date).days)
    
    # ============ 投行/量化报告配色方案 ============
    if style == "dark":
        bg_color = "#111827"
        text_color = "#E5E7EB"
        grid_color = "#374151"
        strategy_color = "#38BDF8"   # 青蓝
        benchmark_color = "#F59E0B"  # 橙
        excess_color = "#34D399"     # 绿
        neg_color = "#F87171"        # 红
        fill_alpha = 0.10
        panel_bg = "#0B1220"
    else:  # professional / simple（白底更接近机构报告）
        bg_color = "#FFFFFF"
        text_color = "#111827"
        grid_color = "#E5E7EB"
        strategy_color = "#1D4ED8"   # 专业蓝
        benchmark_color = "#DC2626"  # 专业红
        excess_color = "#059669"     # 专业绿
        neg_color = benchmark_color
        fill_alpha = 0.08
        panel_bg = "#FFFFFF"
    
    # Matplotlib 局部样式
    rc = {
        "axes.labelcolor": text_color,
        "xtick.color": text_color,
        "ytick.color": text_color,
        "text.color": text_color,
        "font.size": 11,
        "font.family": "sans-serif",
    }
    
    # fill_between 对 datetime 在部分环境会出类型问题，统一转成 matplotlib date float
    x_num = mdates.date2num(nav_df["trade_date"].to_numpy())
    
    # ============ 计算关键指标（补充Max Drawdown等）============
    def _safe_get(name: str, fallback):
        return getattr(nav_result, name, fallback)

    strat_total = float(_safe_get("strategy_total_return", nav_df["strategy_nav"].iloc[-1] - 1.0))
    bench_total = float(_safe_get("benchmark_total_return", nav_df["benchmark_nav"].iloc[-1] - 1.0))
    if excess_col == "excess_nav_diff":
        excess_total_fallback = nav_df[excess_col].iloc[-1]
    else:
        excess_total_fallback = nav_df[excess_col].iloc[-1] - 1.0
    excess_total = float(_safe_get("excess_total_return", excess_total_fallback))

    years = max(date_range_days / 365.25, 1e-9)
    nav_end = float(nav_df["strategy_nav"].iloc[-1])
    ann_ret = (nav_end ** (1 / years) - 1) if nav_end > 0 else float("nan")

    # 计算最大回撤
    running_max = nav_df["strategy_nav"].cummax()
    dd = nav_df["strategy_nav"] / running_max - 1.0
    max_dd = float(dd.min())

    # 计算夏普比率
    daily = nav_df["strategy_nav"].pct_change().dropna()
    ann_vol = float(daily.std(ddof=0) * np.sqrt(252)) if len(daily) > 1 else float("nan")
    sharpe = float((daily.mean() * 252) / ann_vol) if (ann_vol > 0 and len(daily) > 1) else float("nan")

    metrics_lines = [
        f"Total Return      {strat_total:>7.2%}",
        f"Annualized Return {ann_ret:>7.2%}",
        f"Max Drawdown      {max_dd:>7.2%}",
        f"Sharpe (rf=0)     {sharpe:>7.2f}" if np.isfinite(sharpe) else "Sharpe (rf=0)         N/A",
        "",
        f"Benchmark Return  {bench_total:>7.2%}",
        f"Excess Return     {excess_total:>7.2%}",
    ]
    metrics_text = "\n".join(metrics_lines)

    # 日期轴格式化
    if date_range_days > 730:
        locator = mdates.YearLocator()
        formatter = mdates.DateFormatter("%Y")
    elif date_range_days > 365:
        locator = mdates.MonthLocator(interval=6)
        formatter = mdates.DateFormatter("%Y-%m")
    else:
        locator = mdates.MonthLocator(interval=3)
        formatter = mdates.DateFormatter("%Y-%m")

    # x 轴留一点 padding，避免边界点被裁掉
    pad_days = max(5, int(date_range_days * 0.01)) if date_range_days else 5
    x0 = start_date - pd.Timedelta(days=pad_days)
    x1 = end_date + pd.Timedelta(days=pad_days)

    
    with plt.rc_context(rc):
        # ============ 布局选择 ============
        if layout == "tearsheet":
            # 主图+底部超额面板+右侧信息栏（推荐）
            fig = plt.figure(figsize=figsize, facecolor=bg_color)
            gs = fig.add_gridspec(
                nrows=2, ncols=2,
                height_ratios=[3.2, 1.2],
                width_ratios=[5.6, 1.7],
                left=0.06, right=0.98, top=0.86, bottom=0.10,
                wspace=0.05, hspace=0.10,
            )
            ax_main = fig.add_subplot(gs[0, 0], facecolor=bg_color)
            ax_ex = fig.add_subplot(gs[1, 0], sharex=ax_main, facecolor=bg_color)
            ax_info = fig.add_subplot(gs[:, 1], facecolor=bg_color)
            ax_info.axis("off")
        elif layout == "single_right":
            # 单图+右侧信息栏（简洁版）
            fig = plt.figure(figsize=figsize, facecolor=bg_color)
            gs = fig.add_gridspec(
                nrows=1, ncols=2,
                width_ratios=[5.6, 1.7],
                left=0.06, right=0.98, top=0.86, bottom=0.12,
                wspace=0.05,
            )
            ax_main = fig.add_subplot(gs[0, 0], facecolor=bg_color)
            ax_ex = None
            ax_info = fig.add_subplot(gs[0, 1], facecolor=bg_color)
            ax_info.axis("off")
        else:
            # single_overlay：旧版兼容，不推荐
            fig, ax_main = plt.subplots(figsize=figsize, facecolor=bg_color)
            ax_main.set_facecolor(bg_color)
            ax_ex = None
            ax_info = None

        # ============ 统一轴风格（投研风：去掉y轴线，只保留水平网格）============
        def _style_axis(ax):
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["left"].set_visible(False)
            ax.spines["bottom"].set_color(grid_color)
            ax.tick_params(axis="both", which="both", length=0)
            ax.grid(True, axis="y", color=grid_color, linestyle="-", linewidth=0.8, alpha=0.9)
            ax.grid(False, axis="x")

        _style_axis(ax_main)
        if ax_ex is not None:
            _style_axis(ax_ex)

        # ============ 主图：策略 vs 基准 ============
        if style != "simple":
            ax_main.fill_between(
                x_num,
                nav_df["strategy_nav"].to_numpy(float),
                nav_df["benchmark_nav"].to_numpy(float),
                where=(nav_df["strategy_nav"] >= nav_df["benchmark_nav"]).to_numpy(bool),
                color=excess_color, alpha=fill_alpha, linewidth=0, zorder=1,
            )
            ax_main.fill_between(
                x_num,
                nav_df["strategy_nav"].to_numpy(float),
                nav_df["benchmark_nav"].to_numpy(float),
                where=(nav_df["strategy_nav"] < nav_df["benchmark_nav"]).to_numpy(bool),
                color=neg_color, alpha=fill_alpha, linewidth=0, zorder=1,
            )

        h_strat, = ax_main.plot(
            nav_df["trade_date"], nav_df["strategy_nav"],
            color=strategy_color, linewidth=2.8, label="Strategy", zorder=3, alpha=0.98
        )
        h_bench, = ax_main.plot(
            nav_df["trade_date"], nav_df["benchmark_nav"],
            color=benchmark_color, linewidth=2.0,
            label=f"Benchmark ({getattr(nav_result, 'benchmark_code', '')})",
            zorder=2, alpha=0.95
        )
        ax_main.axhline(1.0, color=grid_color, linewidth=1.0, zorder=0, alpha=0.9)
        
        # 标注最新值
        ax_main.scatter(
            end_date, float(nav_df["strategy_nav"].iloc[-1]),
            s=35, color=strategy_color, zorder=4,
            edgecolors=bg_color, linewidths=1.0
        )

        ax_main.set_xlim(x0, x1)
        ax_main.set_ylabel("NAV (Start = 1.0)", fontsize=12, fontweight="600")
        ax_main.set_xlabel("")

        # ============ 超额面板：单独一条小图（更清晰）============
        h_excess = None
        if ax_ex is not None:
            ex_series = nav_df[excess_col].astype(float)
            if excess_col == "excess_nav_diff":
                baseline = 0.0
                ex_label = "Excess NAV (Diff)"
            else:
                ex0 = float(ex_series.iloc[0])
                baseline = 1.0 if 0.5 <= ex0 <= 1.5 else 0.0
                ex_label = "Excess (Strategy / Benchmark)" if baseline == 1.0 else "Excess"

            h_excess, = ax_ex.plot(
                nav_df["trade_date"], ex_series,
                color=excess_color, linewidth=1.8, linestyle="--",
                label=ex_label,
                zorder=3, alpha=0.95
            )
            ax_ex.axhline(baseline, color=grid_color, linewidth=1.0, zorder=0, alpha=0.9)

            if style != "simple":
                ax_ex.fill_between(
                    x_num, ex_series.to_numpy(float), baseline,
                    where=(ex_series >= baseline).to_numpy(bool),
                    color=excess_color, alpha=fill_alpha, linewidth=0, zorder=1
                )
                ax_ex.fill_between(
                    x_num, ex_series.to_numpy(float), baseline,
                    where=(ex_series < baseline).to_numpy(bool),
                    color=neg_color, alpha=fill_alpha, linewidth=0, zorder=1
                )

            ax_ex.set_xlim(x0, x1)
            ax_ex.set_ylabel(ex_label, fontsize=11, fontweight="600")
            ax_ex.set_xlabel("Date", fontsize=11, fontweight="600")
            plt.setp(ax_main.get_xticklabels(), visible=False)

        # ============ 日期轴 ============
        target_ax = ax_ex if ax_ex is not None else ax_main
        target_ax.xaxis.set_major_locator(locator)
        target_ax.xaxis.set_major_formatter(formatter)

        # ============ 标题区（figure 上方，不占绘图区）============
        title_text = "Portfolio Performance"
        subtitle_text = (
            f"{getattr(nav_result, 'strategy_name', '')}\n"
            f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )
        fig.text(0.06, 0.94, title_text, fontsize=18, fontweight="bold",
                 ha="left", va="top", color=text_color)
        fig.text(0.06, 0.895, subtitle_text, fontsize=11,
                 ha="left", va="top", color=text_color, alpha=0.85)

        # ============ 右侧信息栏：Legend + Metrics（永不遮挡曲线）============
        props = dict(
            boxstyle="round,pad=0.6",
            facecolor=panel_bg if style != "dark" else "#0B1220",
            edgecolor=grid_color,
            linewidth=1.5,
            alpha=0.98,
        )

        if ax_info is not None:
            handles = [h_strat, h_bench] + ([h_excess] if h_excess is not None else [])
            labels = [h.get_label() for h in handles]
            leg = ax_info.legend(handles, labels, loc="upper left",
                                 frameon=False, fontsize=11, handlelength=3)
            for t in leg.get_texts():
                t.set_color(text_color)

            ax_info.text(0.0, 0.72, "Key Metrics",
                         fontsize=12, fontweight="bold",
                         ha="left", va="top", color=text_color)
            ax_info.text(0.0, 0.68, metrics_text,
                         fontsize=10, family="monospace",
                         ha="left", va="top",
                         bbox=props, color=text_color)
        else:
            # overlay（旧版兼容，不推荐）
            ax_main.text(0.98, 0.97, metrics_text, transform=ax_main.transAxes,
                         fontsize=11, ha="right", va="top",
                         bbox=props, color=text_color, family="monospace")

        # ============ 可选水印 ============
        if style == "professional":
            ax_main.text(0.01, 0.02, "AIQuantLab",
                         transform=ax_main.transAxes,
                         fontsize=9, alpha=0.25, style="italic",
                         color=text_color)

        # ============ 保存 ============
        result_path = atomic_write_figure(fig, output_path, no_overwrite=no_overwrite, dpi=dpi)
        plt.close(fig)

    logger.info(f"   NAV 曲线图已生成（{style}风格，layout={layout}，{dpi}dpi）: {result_path}")
    return result_path
