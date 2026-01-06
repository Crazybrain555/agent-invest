#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TSViT 模型训练执行脚本 - 简洁版本
"""

import argparse
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.train.Transformer.TSVIT.config import TSViTConfig
from src.train.Transformer.TSVIT.train_tsvit import run_training
from src.dataloader.DuckwideDataloader import (
    get_duckwide_train_valid_test_loaders,
)
from src.data_service.pipelines.Dataset_builder.maintenance import DatasetMaintenance



def get_dataloaders(config: TSViTConfig):
    """Build train/valid/test loaders using the Duckwide pipeline."""
    default_pv6_root = Path(r"F:\AIQuantLab\data\Dataset\pv_v6")
    raw_dataset_path = getattr(config, "dataset_path", None)
    if raw_dataset_path:
        dataset_path = Path(raw_dataset_path).expanduser()
        if "pv_v5" in dataset_path.as_posix():
            dataset_path = default_pv6_root
        if not dataset_path.is_absolute():
            dataset_path = Path.cwd() / dataset_path
    else:
        dataset_path = default_pv6_root
    dataset_path = dataset_path.resolve()
    config.dataset_path = str(dataset_path)

    date_ranges = getattr(config, "date_ranges", {}) or {}
    if isinstance(date_ranges, dict) and date_ranges:
        starts = []
        ends = []
        for rng in date_ranges.values():
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                starts.append(str(rng[0]))
                ends.append(str(rng[1]))
        if starts and ends:
            target_start = min(starts)
            target_end = max(ends)
        else:
            target_start = None
            target_end = None
    else:
        target_start = None
        target_end = None

    auto_extend = getattr(config, "auto_extend_dataset", True)
    extend_threads = getattr(config, "dataset_extend_threads", None)
    if auto_extend and target_start and target_end:
        maint = DatasetMaintenance(dataset_path)
        report = maint.ensure_coverage(
            target_start,
            target_end,
            auto_extend=True,
            threads=extend_threads,
        )
        if report.extended:
            logging.getLogger("run_tsvit").info(
                "[Maintenance] Dataset extended to %s", report.dataset_end or target_end
            )
        elif not report.is_satisfied:
            logging.getLogger("run_tsvit").warning(
                "[Maintenance] Dataset still missing ranges for %s - %s: %s",
                target_start,
                target_end,
                report.missing_ranges,
            )

    seq_len = getattr(config, "seq_len", None) or 300
    config.seq_len = seq_len

    # 统一使用 chunk_size 参数（控制每个batch的样本数）
    chunk_size = (
        getattr(config, "chunk_size", None)
        or getattr(config, "batch_size", None)
        or 8192
    )
    config.chunk_size = chunk_size

    dataset_impl = getattr(config, "dataset_impl", "ring")
    if not hasattr(config, "grad_accum_steps"):
        config.grad_accum_steps = 1
    config.dataset_impl = dataset_impl

    days_per_fetch = getattr(config, "days_per_fetch", None)
    if days_per_fetch is None:
        days_per_fetch = 10
        config.days_per_fetch = days_per_fetch
    part_pad = getattr(config, "part_pad", "auto")
    config.part_pad = part_pad

    default_num_workers = getattr(config, "num_workers", None)
    if default_num_workers is None:
        default_num_workers = 0 if str(dataset_impl).lower() in ("ring", "stream", "streaming") else 0
        config.num_workers = default_num_workers

    dl_cfg = {
        "dataset_path": str(dataset_path),
        "seq_len": seq_len,
        "num_workers": getattr(config, "num_workers", default_num_workers),
        "shuffle": config.shuffle,
        "seed": config.seed,
        "chunk_size": chunk_size,
        "days_per_fetch": days_per_fetch,
        "part_pad": part_pad,
        "prefetch_factor": getattr(config, "prefetch_factor", None),
        "duck_threads": getattr(config, "duck_threads", 8),
        "duck_memory": getattr(config, "duck_memory", "8GB"),
        "duck_cache": getattr(config, "duck_cache", "8GB"),
        "duck_max_temp": getattr(config, "duck_max_temp", None),
        "duck_materialize": getattr(config, "duck_materialize", True),
        "duck_persist_conn": getattr(config, "duck_persist_conn", True),
        "persist_connection": getattr(config, "duck_persist_conn", True),
        "duck_temp_dir": str(dataset_path / "duck_tmp"),
        "pin_memory": getattr(config, "pin_memory", True),
        "persistent_workers": getattr(config, "persistent_workers", True),
        "use_custom_splits": getattr(config, "use_custom_splits", True),
        "date_ranges": getattr(
            config,
            "date_ranges",
            {
                "train": ("20080101", "20181231"),
                "valid": ("20190101", "20211231"),
                "test": ("20220101", "20250731"),
            },
        ),
        "selected_factors": getattr(config, "selected_factors", None),
        "days_per_fetch": getattr(config, "days_per_fetch", 10),
        "part_pad": getattr(config, "part_pad", "auto"),
        "require_label_for_train": True,
        "batch_by": getattr(config, "batch_by", None) or "chunk",
        "dataset_impl": dataset_impl,
    }

    t0 = time.time()
    date_grouping_enabled = ((getattr(config, "batch_by", None) or "chunk") == "date")
    keep_meta_train = date_grouping_enabled
    keep_meta_eval = False

    train_loader, valid_loader, test_loader = get_duckwide_train_valid_test_loaders(
        dl_cfg,
        keep_meta_train=keep_meta_train,
        keep_meta_eval=keep_meta_eval,
    )

    logger = logging.getLogger("run_tsvit")
    logger.info(f"Using dataset: {config.dataset_path}")
    if date_grouping_enabled:
        logger.info("Date-group batching enabled (each batch is a single trading day).")
    else:
        logger.info("Sample-group batching enabled (default).")
    logger.info(f"Data loaders ready in {time.time()-t0:.2f}s (includes DuckDB/index init).")

    return train_loader, valid_loader, test_loader

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="TSViT 模型训练脚本")
    
    # 基本参数
    parser.add_argument('--config', type=str, default='configs/models/transformer/tsvit.yaml',
                       help="配置文件路径 (默认: configs/models/transformer/tsvit.yaml)")
    parser.add_argument('--output', type=str, default=None,
                       help="输出目录(默认自动生成，形如 outputs/TSViT_MODEL_...)")
    parser.add_argument('--exp-tag', type=str, default='use_symmetric_hfrequenciy',
                       help="实验标注，会添加到路径前缀 (默认: use_symmetric, 与实验配置一致)")
    parser.add_argument('--dataset', type=str, default='data/Dataset/pv_v7',
                       help="数据集路径(默认: YAML data.dataset_path)")
    
    # 训练参数覆盖
    parser.add_argument('--epochs', type=int, default=250,
                       help="训练轮数 (默认: 250, 与实验配置一致)")
    # 说明：TSViT流水线中实际每步的批大小由 Dataset 的 chunk_size 决定；
    # 该参数仅作为回退值，当未提供 --chunk-size 且 YAML 未设置 data.chunk_size 时才生效。
    parser.add_argument('--batch-size', type=int, default=4096,
                       help="回退批次大小（默认: 4096, 与实验配置一致）")
    parser.add_argument('--chunk-size', type=int, default=4096,
                       help="实际每步样本数（默认: 4096, 与实验配置一致）")
    parser.add_argument('--lr', type=float, default=4e-5,
                       help="学习率 (默认: 4e-5, 与实验配置一致)")
    parser.add_argument('--weight-decay', type=float, default=1.0,
                       help="权重衰减 (默认: 1.0, 与实验配置一致)")
    
    # 模型参数覆盖
    parser.add_argument('--pos-encoding', type=str, default=None,
                       choices=['none', 'abs', 'rope', 'rpb', 'rope_rpb', 'sinus'],
                       help="位置编码类型 (默认: YAML model.pos_encoding)")
    parser.add_argument('--num-layers', type=int, default=None,
                       help="Transformer层数 (默认: YAML model.num_layers)")
    parser.add_argument('--nheads', type=int, default=None,
                       help="注意力头数 (默认: YAML model.nheads)")
    parser.add_argument('--hidden-size', type=int, default=None,
                       help="隐藏维度 (默认: YAML model.hidden_size)")
    parser.add_argument('--head-type', type=str, default=None,
                       choices=['query', 'pool', 'cls', 'baseline'],
                       help="头部类型 (默认: YAML model.head_type)")

    # 数据/加载与特征/分割参数 (参考 run_dfzq_gru.py)
    parser.add_argument('--selected-factors', type=str, nargs='*', default=None,
                       help="选择的特征列表 (默认: YAML data.selected_factors 或全部)")
    parser.add_argument('--list-factors', action='store_true',
                       help="列出数据集中所有可用的特征名称")
    parser.add_argument('--use-custom-splits', action='store_true',
                       help="使用自定义日期分割 (默认: YAML data.use_custom_splits)")
    parser.add_argument('--num-workers', type=int, default=None,
                       help="DataLoader worker 数，覆盖 YAML data.num_workers")
    parser.add_argument('--prefetch-factor', type=int, default=None,
                       help="DataLoader 预取批次数，覆盖 YAML data.prefetch_factor")
    parser.add_argument('--duck-threads', type=int, default=None,
                       help="DuckDB worker 线程数，覆盖 YAML data.duck_threads")
    parser.add_argument('--duck-memory', type=str, default=None,
                       help="DuckDB 内存限制（如 32GB），覆盖 YAML data.duck_memory")
    parser.add_argument('--duck-cache', type=str, default=None,
                       help="DuckDB 对象缓存大小（如 8GB），覆盖 YAML data.duck_cache")
    parser.add_argument('--days-per-fetch', type=int, default=None,
                       help="Streaming 数据集一次批量读取的交易日数量 (默认: 10)")
    parser.add_argument('--part-pad', type=str, default=None, choices=['auto', 'padded', 'unpadded'],
                       help="宽表分区目录是否补零 (auto/padded/unpadded，默认: auto)")
    # batch分组方式参数
    parser.add_argument('--batch-by', type=str, choices=['date', 'chunk'], default='date',
                       help="batch分组方式：'date'=按日期分组（单日batch，适合逐日横截面IC计算），'chunk'=按样本数分组（默认）")
    
    # 数据加载优化参数（新增）
    parser.add_argument('--cpu-queue-size', type=int, default=None,
                       help="CPU预取队列大小（默认: 2），控制TwoStagePrefetcher的缓冲深度")
    parser.add_argument('--io-half', action='store_true',
                       help="启用I/O半精度传输（H2D时使用fp16，可减半带宽）")
    parser.add_argument('--opt-log-freq', type=int, default=None,
                       help="优化器统计日志频率（默认: 500），降低以减少CPU开销")
    parser.add_argument('--step-log-freq', type=int, default=None,
                       help="训练步日志频率（默认: 100），降低以减少CPU开销")

    # 回测参数 (参考 run_dfzq_gru.py)
    parser.add_argument('--no-backtest', action='store_true',
                       help="禁用训练结束后的自动回测 (默认: 启用)")
    parser.add_argument('--backtest-start', type=str, default="20210101",
                       help="回测开始日期 (默认: 20210101)")
    parser.add_argument('--backtest-end', type=str, default="20250731",
                       help="回测结束日期 (默认: 20250731)")
    
    # 其他选项
    parser.add_argument('--cpu', action='store_true',
                       help="强制使用CPU")
    parser.add_argument('--no-amp', action='store_true',
                       help="禁用混合精度")
    
    args = parser.parse_args()
    
    # 加载配置
    config = TSViTConfig.from_yaml(args.config)
    
    # 命令行参数覆盖
    if args.output:
        config.output_root = args.output
    if args.dataset:
        config.dataset_path = args.dataset
    if args.epochs:
        config.epochs = args.epochs
    if args.batch_size:
        config.batch_size = args.batch_size
    # 优先使用 --chunk-size 覆盖 YAML 的 data.chunk_size
    if getattr(args, 'chunk_size', None):
        config.chunk_size = args.chunk_size
    if args.lr:
        config.lr = args.lr
    if args.weight_decay:
        config.weight_decay = args.weight_decay
    if args.pos_encoding:
        config.pos_encoding = args.pos_encoding
    if getattr(args, 'num_layers', None):
        config.num_layers = args.num_layers
    if args.nheads:
        config.nheads = args.nheads
    if args.hidden_size:
        config.hidden_size = args.hidden_size
    if args.head_type:
        config.head_type = args.head_type
    # 覆盖数据加载与DuckDB相关参数
    if getattr(args, 'num_workers', None) is not None:
        config.num_workers = args.num_workers
    if getattr(args, 'prefetch_factor', None) is not None:
        config.prefetch_factor = args.prefetch_factor
    if getattr(args, 'duck_threads', None) is not None:
        config.duck_threads = args.duck_threads
    if getattr(args, 'duck_memory', None) is not None:
        config.duck_memory = args.duck_memory
    if getattr(args, 'duck_cache', None) is not None:
        config.duck_cache = args.duck_cache
    if getattr(args, 'days_per_fetch', None) is not None:
        config.days_per_fetch = args.days_per_fetch
    if getattr(args, 'part_pad', None) is not None:
        config.part_pad = args.part_pad
    # batch分组方式参数
    if args.batch_by:
        config.batch_by = args.batch_by
    
    # 数据加载优化参数
    if getattr(args, 'cpu_queue_size', None) is not None:
        config.cpu_queue_size = args.cpu_queue_size
    if getattr(args, 'io_half', False):
        config.io_half = True
    if getattr(args, 'opt_log_freq', None) is not None:
        config.opt_log_freq = args.opt_log_freq
    if getattr(args, 'step_log_freq', None) is not None:
        config.step_log_freq = args.step_log_freq
    
    if args.cpu:
        config.force_cpu = True
    if args.no_amp:
        config.use_amp = False
    if args.exp_tag:
        config.exp_tag = args.exp_tag
    
    # 处理 --list-factors
    if args.list_factors:
        try:
            import json
            schema_path = Path(config.dataset_path) / "meta" / "schema.json"
            if schema_path.exists():
                with schema_path.open("r", encoding="utf-8-sig") as fp:
                    schema_json = json.load(fp)
                expanded_factor_names = schema_json.get("expanded_factor_names", [])
                print("📊 数据集中可用的特征：")
                for i, factor in enumerate(expanded_factor_names, 1):
                    print(f"  {i:2d}. {factor}")
                print(f"\n总计：{len(expanded_factor_names)}个特征")
                print("\n💡 使用示例：")
                print("  python run_tsvit.py --selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
            else:
                print(f"❌ 未找到数据集schema文件: {schema_path}")
        except Exception as e:
            print(f"❌ 获取特征列表失败: {e}")
        return

    # 参考 run_dfzq_gru.py：自定义日期范围与特征
    if args.use_custom_splits or config.use_custom_splits:
        config.use_custom_splits = True
        config.date_ranges = {
            "train": ("20080101", "20181231"),
            "valid": ("20190101", "20211231"),
            "test": ("20220101", "20250731"),
        }
    if args.selected_factors:
        config.selected_factors = args.selected_factors
    elif config.selected_factors is None:
        # 根据 factor_windows.py 定义的可用因子列表
        # 通过注释/取消注释来控制使用哪些因子
        config.selected_factors = [
            
            # ========== ① 价格相关因子 ==========
            "adj_close_mar_w1",
            "adj_open_mar_w1",
            "adj_high_mar_w1",
            "adj_low_mar_w1",
            "vwap_mar_w1",
            # "vwap_mar_w30",
            # "vwap_mar_w60",
            # "vwap_mar_w90",
            
            # ========== ② 估值/规模因子（factor_windows.py中已注释） ==========
            # "pe_ttm_w1",
            # "pb_ratio_w1",
            # "dividend_yield_12m_w252",
            # "float_market_cap_w1",
            
            # ========== ③ 流动性/资金流因子（factor_windows.py中已注释） ==========
            # "TRADES_COUNT_mar_w30",
            # "large_buy_value_mar_w30",
            # "large_sell_value_mar_w30",
            # "med_buy_value_mar_w30",
            # "med_sell_value_mar_w30",
            # "small_buy_value_mar_w30",
            # "small_sell_value_mar_w30",
            # "inst_buy_value_mar_w30",
            # "inst_sell_value_mar_w30",
            # "large_net_inflow_mar_w30",
            # "amount_mar_w30",  # ❌ 已删除：factor_windows.py中被注释
            # "large_buy_rate_w0",
            # "large_sell_rate_w0",
            # "initiative_sell_rate_w0",
            
            # ========== ④ 技术/情绪因子 ==========
            # "turnover_rate_w0",
            # "swing_w0",
            
            # ========== ⑤ 高频/微观结构因子 ==========
            "high_PVcor_w0",
            "MinuVol_Call_w0",
            # "MinuVol_rate_w0",
            "amresid_amount_w0",
            "apm_w0",
            # "high_RetKurt_w0",
            # "high_RetSkew_w0",
            # "high_RetVar_w0",
            # "high_VolSkew_w0",
            # "high_beta_w0",
            # "high_dev_w0",
            # "high_hprice_w0",
            "high_pvi_w0",
            "high_vol_close_w0",
            "high_vol_open_w0",
            "high_vr_w0",
            "price2vol_w0",
            "residpos_amount_pm_w0",
            
            # ========== ⑥ 状态哑变量 ==========
            # "up_down_limit_status_w0",
            
            # ========== ⑦ Space Signals - Growth (成长-盈利增长和质量, 32个) ==========
            # "nde2p_w0",              # 季度净利润同比变化占总市值比（含快报预告）
            # "ne2e_q_w0",             # 季度净利润同比增长率（含快报预告）
            # "npegl_w0",              # 同pegl（未来2年预期增长率/PE，含快报预告）
            # "sup_con_np_yg_w0",      # 定期报告（含预告快报）相对分析师预测数据的增长
            # "de2p_w0",               # 季度净利润同比变化/总市值
            # "nqdroe_w0",             # 季度ROE同比变化（含快报预告）
            # "nfes1_w0",              # 净利润同比变化/总资产（含快报预告）
            # "opg_q_w0",              # 季度营业利润同比增长率
            # "qdroe_w0",              # 季度ROE同比变化
            # "sue_w0",                # 历史季度盈利惊喜（净利润同比变化的Z值）
            # "fes1_w0",               # 每股季度净利润同比变化占每股总资产比例
            # "kfes1_w0",              # 每股季度扣非净利润同比变化占每股总资产比例
            # "kqdroa_w0",             # 扣非季度ROA同比变化
            # "kqdroe_w0",             # 扣非季度ROE同比变化
            # "npegs_w0",              # 同pegs（未来1年预期增长率/PE，含快报预告）
            # "qdroa_w0",              # 季度ROA同比变化
            # "rrqop_qcpacf_w0",       # 营业利润对资本支出的残差回归（特质利润）
            # "rrqop_qncfoa_w0",       # 营业利润对经营现金流的残差回归（特质利润）
            # "de2e_w0",               # TTM净利润同比增长率
            # "npg_q_w0",              # 季度净利润同比增长率（分母最小1000万）
            # "npg_ttm_w0",            # TTM净利润同比增长率（分母最小1000万）
            # "npg3_ttm_w0",           # TTM净利润相比3年前的增长率
            # "qop_stb_w0",            # 8期季度营业利润同比增长率均值/标准差
            # "qop_acc_w0",            # 8期季度营业利润对时间回归的二次项系数
            # "qop_dsd_w0",            # 一阶差分（8期季度营业利润同比增长率均值/标准差）
            # "qe_stb_w0",             # 8期季度净利润同比增长率均值/标准差
            # "roeg_ttm_w0",           # TTM ROE同比变化
            # "epsg_ttm_w0",           # TTM EPS同比变化
            # "rqnp_rqcpbe_w0",        # 季度净利润同比增长率-季度职工薪酬同比增长率
            # "rqop_rqcpbe_w0",        # 季度营业利润同比增长率-季度职工薪酬同比增长率
            # "rqop_rqlgae_w0",        # 季度营业利润同比增长率-季度管理费用同比增长率
            # "rqop_rcpg_w0",          # 季度营业利润同比增长率-季度采购商品同比增长率
            
            # ========== ⑧ Space Signals - Analyst (分析师-覆盖度和评级, 11个) ==========
            # "cvg_og1_w0",            # 1个月覆盖度（机构数）
            # "cvg_og2_w0",            # 2个月覆盖度（机构数）
            # "cvg_og3_w0",            # 3个月覆盖度（机构数）
            # "cvg1_w0",               # 1个月覆盖度（同一机构不同日期算多次）
            # "cvg2_w0",               # 2个月覆盖度（同一机构不同日期算多次）
            # "cvg3_w0",               # 3个月覆盖度（同一机构不同日期算多次）
            # "eps_variability2_w0",   # n个月分析师分歧度（至少3家覆盖）
            # "drec_w0",               # 最新一致预期评级变化（本月-上月）
            # "recud_180_30_w0",       # 评级方向加权（180工作日，30半衰期）
            # "rec_w0",                # 最新一致预期评级（90天内3家加权）
            # "rec2_w0",               # 最新一致预期评级（90天内1家加权）
            
            # ========== ⑨ Space Signals - Analyst (分析师-盈利预测修正, 10个) ==========
            # "darev_120_60_w0",       # 盈利预测同比变化/总市值（120天，60半衰期）
            # "darev_40_10_w0",        # 盈利预测同比变化/总市值（40天，10半衰期）
            # "darev_60_20_w0",        # 盈利预测同比变化/总市值（60天，20半衰期）
            # "grev3_w0",              # 盈利预测变动（相比3个月前）
            # "rev12_w0",              # 过去20天盈利预测相比前一天加权变化值/总市值
            # "revudsratio_w0",        # EPS扩散度（上调数-下调数）/（上调数+下调数）
            # "revud_120_60_w0",       # 盈利预测方向变化（120天，60半衰期）
            # "revuds_120_60_w0",      # 盈利预测方向变化（120天，60半衰期）
            # "revuds_40_10_w0",       # 盈利预测方向变化（40天，10半衰期）
            # "revuds_60_20_w0",       # 盈利预测方向变化（60天，20半衰期）
            
            # ========== ⑩ Space Signals - Sentiment (情绪-波动率, 37个) ==========
            # "highlow12m_w0",         # 12个月最高价/最低价
            # "highlow1m_w0",          # 1个月最高价/最低价
            # "highlow24m_w0",         # 24个月最高价/最低价
            # "highlow3m_w0",          # 3个月最高价/最低价
            # "highlow6m_w0",          # 6个月最高价/最低价
            # "i_volatility_w0",       # 残差波动率
            # "ivo_w0",                # 残差波动率
            # "ivo_ff12m_w0",          # 过去12个月残差波动率（22交易日）
            # "ivo_ff1m_w0",           # 过去1个月残差波动率（22交易日）
            # "ivo_ff24m_w0",          # 过去24个月残差波动率（22交易日）
            # "ivo_ff3m_w0",           # 过去3个月残差波动率（22交易日）
            # "ivo_ff6m_w0",           # 过去6个月残差波动率（22交易日）
            # "ivr_w0",                # 过去1个月残差波动率（22交易日）
            # "ivr_ff12m_w0",          # 过去12个月R方（22交易日）
            # "ivr_ff1m_w0",           # 过去1个月R方（22交易日）
            # "ivr_ff24m_w0",          # 过去24个月R方（22交易日）
            # "ivr_ff3m_w0",           # 过去3个月R方（22交易日）
            # "ivr_ff6m_w0",           # 过去6个月R方（22交易日）
            # "volatility_actual12m_w0",  # 过去12个月真实波幅均值
            # "volatility_actual1m_w0",   # 过去1个月真实波幅均值
            # "volatility_actual24m_w0",  # 过去24个月真实波幅均值
            # "volatility_actual3m_w0",   # 过去3个月真实波幅均值
            # "volatility_actual6m_w0",   # 过去6个月真实波幅均值
            # "volatility12m_w0",      # 过去12个月波动率（22交易日）
            # "volatility1m_w0",       # 过去1个月波动率（22交易日）
            # "volatility24m_w0",      # 过去24个月波动率（22交易日）
            # "volatility3m_w0",       # 过去3个月波动率（22交易日）
            # "volatility6m_w0",       # 过去6个月波动率（22交易日）
            # "volume_volatility12m_w0",  # 成交量波动率12个月
            # "volume_volatility1m_w0",   # 成交量波动率1个月
            # "volume_volatility24m_w0",  # 成交量波动率24个月
            # "volume_volatility3m_w0",   # 成交量波动率3个月
            # "volume_volatility6m_w0",   # 成交量波动率6个月
            # "maxret33_w0",           # 最近3个月最大3日收益率均值
            # "maxret66_w0",           # 最近6个月最大6日收益率均值
            # "maxret11_w0",           # 最近1个月日收益率最大值
            # "beta3m_w0",             # 个股收益率相对中证全指过去3个月回归系数
            
            # ========== ⑪ Space Signals - Sentiment (情绪-动量, 23个) ==========
            # "cto6m_w0",              # 隔日动量（剔除最近1月后5个月）
            # "cthl6m_w0",             # 高低开动量（剔除最近1月后5个月）
            # "cthl9m_w0",             # 高低开动量（剔除最近1月后8个月）
            # "cto12m_w0",             # 隔日动量（剔除最近1月后11个月）
            # "cto9m_w0",              # 隔日动量（剔除最近1月后8个月）
            # "drmom12m_w0",           # 收益率排名动量（剔除最近1月后11个月）
            # "drmom9m_w0",            # 收益率排名动量（剔除最近1月后8个月）
            # "cthl12m_w0",            # 高低开动量（剔除最近1月后11个月）
            # "cto24m_w0",             # 隔日动量（剔除最近1月后23个月）
            # "drmom6m_w0",            # 收益率排名动量（剔除最近1月后5个月）
            # "cthl24m_w0",            # 高低开动量（剔除最近1月后23个月）
            # "drmom24m_w0",           # 收益率排名动量（剔除最近1月后23个月）
            # "momvol7m_w0",           # 过去7个月波动率调整后动量
            # "momentum_t12_w0",       # 过去1年价格对时间回归的t值（剔除最近1月）
            # "momentum_w0",           # 风险模型动量因子
            # "momvol10m_w0",          # 过去10个月波动率调整后动量
            # "demom8_w0",             # 过去8个月对数收益率和（剔除最近1月，头尾减半）
            # "demom7_w0",             # 过去7个月对数收益率和（剔除最近1月，头尾减半）
            # "demom9_w0",             # 过去9个月对数收益率和（剔除最近1月，头尾减半）
            # "momvol12m_w0",          # 过去12个月波动率调整后动量
            # "demom10_w0",            # 过去10个月对数收益率和（剔除最近1月，头尾减半）
            # "m52w_w0",               # 当前价格/过去52周最高价格
            # "momvol24m_w0",          # 过去24个月波动率调整后动量
            
            # ========== ⑫ Space Signals - Sentiment (情绪-价格和收益率, 21个) ==========
            # "mad120_w0",             # 当前价/20日均价-1
            # "mad560_w0",             # 5日均价/60日均价
            # "pairs_w0",              # 个股对行业协整关系（均值回复）
            # "pairs_l_w0",            # 个股对行业协整关系（长期）
            # "ret12m_w0",             # 过去12个月涨跌幅
            # "ret12m_1m_w0",          # 过去12个月涨跌幅（剔除最近1月）
            # "ret1m_w0",              # 过去1个月涨跌幅
            # "ret24m_w0",             # 过去24个月涨跌幅
            # "ret24m_1m_w0",          # 过去24个月涨跌幅（剔除最近1月）
            # "ret3m_w0",              # 过去3个月涨跌幅
            # "ret3m_1m_w0",           # 过去3个月涨跌幅（剔除最近1月）
            # "ret6m_w0",              # 过去6个月涨跌幅
            # "ret6m_1m_w0",           # 过去6个月涨跌幅（剔除最近1月）
            # "revs_w0",               # 价格反转（最近40日回报时间加权）
            # "revsTA_w0",             # 单笔成交金额反转（高低单笔金额差异）
            # "revsTV_w0",             # 单笔成交量反转（高低单笔量差异）
            # "spb_w0",                # 类似pairs（收益率回归相关性分组）
            # "tprtn1_w0",             # 一致预期潜在收益率（3家以上）
            # "tprtn2_w0",             # 一致预期潜在收益率（1家以上）
            # "tskew_w0",              # 最近1年收益率偏态
            
            # ========== ⑬ Space Signals - Sentiment (情绪-价值反转, 21个) ==========
            # "mom1_con_pb_roll_w0",   # 最新/1月前一致预期滚动BP-1
            # "mom1_con_ps_roll_w0",   # 最新/1月前一致预期滚动SP-1
            # "mom3_con_pb_roll_w0",   # 最新/3月前一致预期滚动BP-1
            # "nre2p_w0",              # 过去1年相对nEP
            # "mom1_con_pe_roll_w0",   # 最新/1月前一致预期滚动EP-1
            # "mom3_con_ps_roll_w0",   # 最新/3月前一致预期滚动SP-1
            # "rfy12p_w0",             # fy12p当前值相对最近1年Z值
            # "mom3_con_pe_roll_w0",   # 最新/3月前一致预期滚动EP-1
            # "rcon_pe_roll_w0",       # 一致预期滚动PE当前值相对最近1年Z值
            # "rcon_pb_roll_w0",       # 一致预期滚动PB当前值相对最近1年Z值
            # "rb2p_w0",               # 历史相对BP
            # "rcon_ps_roll_w0",       # 一致预期滚动PS当前值相对最近1年Z值
            # "re2p_w0",               # E2P当前值相对最近1年Z值
            # "rpegl_w0",              # pegl当前值相对最近1年Z值
            # "nrpegl_w0",             # 同rpegl
            # "rcon_peg_roll_w0",      # 一致预期滚动PEG当前值相对最近1年Z值
            # "rpegs_w0",              # pegs当前值相对最近1年Z值
            # "nrpegs_w0",             # 同rpegs
            # "rcon_eps_roll_w0",      # 一致预期滚动EPS当前值相对最近1年Z值
            # "nrb2p_w0",              # 过去1年相对nBP
            # "mom3_con_eps_roll_w0",  # Con_EPS环比增长率（本月/前一月-1）
            
            # ========== ⑭ Space Signals - Sentiment (情绪-流动性, 28个) ==========
            # "astd12m_w0",            # 12个月成交额/收益率标准差
            # "astd1m_w0",             # 1个月成交额/收益率标准差
            # "astd24m_w0",            # 24个月成交额/收益率标准差
            # "astd3m_w0",             # 3个月成交额/收益率标准差
            # "astd6m_w0",             # 6个月成交额/收益率标准差
            # "davol_w0",              # 最近20日换手率/最近60日换手率（取负）
            # "davol_floatshr_w0",     # 流通股本davol
            # "davol_freeshr_w0",      # 自由流通股本davol
            # "illiq_w0",              # 非流动性因子（单位成交金额的涨跌幅）
            # "liquidty_w0",           # 0.35·STOM+0.35·STOQ+0.30·STOA
            # "lowprice_w0",           # 1/昨日收盘价
            # "lsf_w0",                # 20日非流动性abs(收益率/成交数量)
            # "mad_amount_20240_w0",   # 过去20日/过去240日日均成交额
            # "mad_amount_2060_w0",    # 过去20日/过去60日日均成交额
            # "mad_volume_20240_w0",   # 过去20日/过去240日日均成交量
            # "mad_volume_2060_w0",    # 过去20日/过去60日日均成交量
            # "turnover_amount12m_w0", # 最近12个月买卖循环率（取负）
            # "turnover_amount1m_w0",  # 最近1个月买卖循环率（取负）
            # "turnover_amount24m_w0", # 最近24个月买卖循环率（取负）
            # "turnover_amount3m_w0",  # 最近3个月买卖循环率（取负）
            # "turnover_amount6m_w0",  # 最近6个月买卖循环率（取负）
            # "turnover12m_w0",        # 过去12个月换手率
            # "turnover1m_w0",         # 过去1个月换手率
            # "turnover24m_w0",        # 过去24个月换手率
            # "turnover3m_w0",         # 过去3个月换手率
            # "turnover6m_w0",         # 过去6个月换手率
            # "vol_w0",                # 最近40天加权换手率
            # "vol_freeshr_w0",        # 自由流通股本换手率

        ]

    # 打印配置
    logger = logging.getLogger("run_tsvit")
    logger.info("TSViT 训练配置:")
    logger.info(f"  模型: {config.T}x{config.D} -> {config.hidden_size}, {config.num_layers}层, {config.nheads}头")
    logger.info(f"  位置编码: {config.pos_encoding}")
    logger.info(f"  训练: {config.epochs}轮, lr={config.lr}, wd={config.weight_decay}")
    eff_chunk = getattr(config, 'chunk_size', None) or getattr(config, 'batch_size', None)
    logger.info(f"  批次: batch_size(回退)={config.batch_size}, chunk_size(实际)={eff_chunk}, AMP={config.use_amp}")
    logger.info("  说明: 实际每步样本数由 chunk_size 决定；若未设置 chunk_size，则回退使用 batch_size")
    
    # 数据加载优化配置
    cpu_queue = getattr(config, 'cpu_queue_size', 4)
    io_half_flag = getattr(config, 'io_half', False)
    opt_log = getattr(config, 'opt_log_freq', 500)
    step_log = getattr(config, 'step_log_freq', 100)
    logger.info(f"  数据优化: TwoStagePrefetcher(queue={cpu_queue}), IO-Half={io_half_flag}")
    logger.info(f"  日志频率: opt_log={opt_log}, step_log={step_log} (降低以减少CPU开销)")
    
    logger.info(f"  输出: {config.output_root}")
    
    # 获取数据加载器
    train_loader, valid_loader, test_loader = get_dataloaders(config)
    
    # 开始训练
    try:
        output_dir = run_training(config, train_loader, valid_loader, test_loader)
        print(f"\n🎉 训练完成！输出目录: {output_dir}")
        print(f"📊 TensorBoard: tensorboard --logdir {Path(output_dir)/'/logs'}")

        # ========= 自动回测（参考 run_dfzq_gru.py） =========
        auto_backtest = not args.no_backtest
        if auto_backtest:
            logger.info("\n" + "="*60)
            logger.info("🚀 训练完成，开始自动回测...")
            logger.info("="*60)
            try:
                from backtest_model import ModelBacktestConfig, ModelBacktester
                actual_output_dir = output_dir
                bt_cfg = ModelBacktestConfig()
                bt_cfg.model_path = actual_output_dir
                bt_cfg.dataset_path = config.dataset_path
                bt_cfg.start_date = args.backtest_start
                bt_cfg.end_date = args.backtest_end
                bt_cfg.rebalance_frequency = "10D"
                bt_cfg.max_stocks = 100
                bt_cfg.trade_cost_rate = 20 / 10000
                bt_cfg.save_excel = True
                bt_cfg.print_results = True
                bt_cfg.enable_factor_save = True
                bt_cfg.factor_target_format = "backtest"
                bt_cfg.factor_save_formats = ["csv"]
                logger.info("📊 回测配置:")
                logger.info(f"  模型路径: {bt_cfg.model_path}")
                logger.info(f"  数据集路径: {bt_cfg.dataset_path}")
                logger.info(f"  回测时间: {bt_cfg.start_date} - {bt_cfg.end_date}")
                logger.info(f"  调仓频率: {bt_cfg.rebalance_frequency}")
                logger.info(f"  最大持股: {bt_cfg.max_stocks}")
                print(f"\n🚀 开始回测 {bt_cfg.start_date}-{bt_cfg.end_date}...")
                backtester = ModelBacktester(bt_cfg)
                backtester.run_full_backtest()
            except Exception as e:
                logger.error(f"❌ 自动回测失败: {e}")
                logger.exception("回测详细错误信息:")
                print("训练已完成，可以稍后手动运行回测")

    except Exception as e:
        logger.error(f"训练失败: {e}")
        raise


if __name__ == "__main__":
    main()
