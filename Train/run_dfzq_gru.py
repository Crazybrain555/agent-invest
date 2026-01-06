#!/usr/bin/env python
# run_dfzq_gru_train.py - 完整DFZQ-GRU模型训练执行脚本
"""
用于执行完整的东方证券GRU模型训练。
使用标准配置进行完整训练，而不是小规模测试。
"""

import os
import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime

from src.train.Neural_networks.RNN.DFZQ_GRU.train_dfzq_gru import run_training
from src.train.Neural_networks.RNN.DFZQ_GRU.config import TrainingConfig

def main():
    # 参数解析
    parser = argparse.ArgumentParser(description="DFZQ GRU模型训练脚本")
    parser.add_argument('--output', type=str, default=None, 
                       help="输出目录，默认为'outputs/DFZQ_GRU_MODEL_[日期时间]'")
    parser.add_argument('--dataset', type=str, default='data/Dataset/pv_v5_pv_v5_pvhflow_solid30',   #data\Dataset\pv_v5_pv_v5_pvhflow_solid30_test1
                       help="数据集路径，默认为'data/Dataset/pv_v1'")
    parser.add_argument('--epochs', type=int, default=300,
                       help="训练轮数，默认为200")
    parser.add_argument('--batch_size', type=int, default=256*8,
                       help="🚀 每GPU批次大小(实际控制chunk_size)，默认2048。推荐: 2048, 4096, 8192")
    parser.add_argument('--workers', type=int, default=1,
                       help="数据加载工作进程数，默认为0（单进程模式，减少内存消耗）")
    parser.add_argument('--no-amp', action='store_true',
                       help="禁用混合精度训练")
    parser.add_argument('--prefetch-factor', type=int, default=4,
                       help="DataLoader 每个 worker 预取批次数，默认为4")
    parser.add_argument('--cpu', action='store_true',
                       help="强制使用CPU训练")
    parser.add_argument('--shuffle', action='store_true',
                       help="是否打乱数据")
    parser.add_argument('--weight_decay', type=float, default=5e-2,  #调整为1 e-3 晚上
                       help="权重衰减系数，默认为0.01")
    parser.add_argument('--lr', type=float, default=5e-5 ,
                       help="学习率，默认为config中的默认值")
    parser.add_argument('--no-fixed-indices', action='store_true',
                       help="禁用固定索引，使用随机顺序（可能导致数据不一致）")
    
    # 🚀 新增回测集成参数
    parser.add_argument('--no-backtest', action='store_true',
                       help="禁用训练结束后的自动回测（默认启用）")
    parser.add_argument('--backtest-start', type=str, default="20210101",
                       help="回测开始日期，默认20210101")
    parser.add_argument('--backtest-end', type=str, default="20250731",
                       help="回测结束日期，默认20250731")
    
    # 🚀 新增特征选择参数
    parser.add_argument('--selected-factors', type=str, nargs='*', default=None,
                       help="选择的特征列表，用空格分隔。例如：--selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
    parser.add_argument('--list-factors', action='store_true',
                       help="列出数据集中所有可用的特征名称")
    
    # DuckDB performance tuning 
    parser.add_argument('--duck-threads', type=int, default=16, help="DuckDB worker threads.")
    parser.add_argument('--duck-memory', type=str, default='32GB', help="DuckDB memory limit.")
    parser.add_argument('--duck-cache', type=str, default='8GB', help="DuckDB object cache size.")
    
    args = parser.parse_args()

    # 🚀 新增：处理特征列表查询
    if args.list_factors:
        try:
            # 临时创建数据集实例来获取可用特征
            import json
            from pathlib import Path
            schema_path = Path(args.dataset) / "meta" / "schema.json"
            if schema_path.exists():
                with schema_path.open("r", encoding="utf-8-sig") as fp:
                    schema_json = json.load(fp)
                expanded_factor_names = schema_json.get("expanded_factor_names", [])
                print("📊 数据集中可用的特征：")
                for i, factor in enumerate(expanded_factor_names, 1):
                    print(f"  {i:2d}. {factor}")
                print(f"\n总计：{len(expanded_factor_names)}个特征")
                print("\n💡 使用示例：")
                print("  python run_dfzq_gru.py --selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
                print("  python run_dfzq_gru.py --selected-factors adj_close_mar_w1 adj_open_mar_w1 adj_high_mar_w1 adj_low_mar_w1")
            else:
                print(f"❌ 未找到数据集schema文件: {schema_path}")
        except Exception as e:
            print(f"❌ 获取特征列表失败: {e}")
        return

    # 创建并配置训练参数
    cfg = TrainingConfig()
    
    # 配置输出目录 - 修复双重目录问题
    if args.output is None:
        # 让experiment_utils处理完整的目录名生成，这里只设置基础路径
        cfg.output_root = "outputs/DFZQ_GRU_MODEL"
    else:
        cfg.output_root = args.output
    

    
    # 日志配置
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger = logging.getLogger("run_dfzq_gru_train")
    
    # 基本配置
    cfg.dataset_path = args.dataset
    cfg.max_epochs = args.epochs
    # 🚀 关键映射：CLI的batch_size实际控制chunk_size
    cfg.chunk_size = args.batch_size    # ← CLI --batch_size 映射到实际的数据块大小
    cfg.batch_size = None               # ← DataLoader不再使用batch_size (批级yield)
    cfg.num_workers = args.workers
    cfg.shuffle = args.shuffle
    cfg.force_cpu = args.cpu
    # cfg.use_amp = not args.no_amp
    cfg.use_amp = not args.no_amp
    
    # 配置参数
    cfg.weight_decay = args.weight_decay
    if args.lr is not None:
        cfg.lr = args.lr
    cfg.use_fixed_indices = not args.no_fixed_indices

    # 注意：chunk_size现在由args.batch_size控制，不再使用args.chunk_size
    cfg.duck_threads = args.duck_threads
    cfg.duck_memory = args.duck_memory
    cfg.duck_cache = args.duck_cache
    cfg.prefetch_factor = args.prefetch_factor
    
    # 🚀 特征选择配置
    if args.selected_factors:
        cfg.selected_factors = args.selected_factors
        logger.info(f"🎯 特征选择模式：选择了{len(args.selected_factors)}个特征")
    else:
        cfg.selected_factors = None
        logger.info(f"📊 使用全部特征")
    
    # 🚀 自定义日期范围配置 - 直接在这里设置
    cfg.date_ranges = {
        "train": ("20080101", "20181231"),
        "valid": ("20190101", "20211231"),  # 🔧 修复：现在应该可以正确使用2019-2020年数据
        "test": ("20220101", "20250731"),
    }
    cfg.use_custom_splits = True
    

    cfg.selected_factors = [
        
    "adj_close_mar_w1",
    # "adj_open_mar_w1",
    # "adj_high_mar_w1",
    # "adj_low_mar_w1",
    "vwap_mar_w1",
    "vwap_mar_w30",
    "amount_mar_w30",
    
        
    # ③ 流动性 / 资金流 单
    
    # "TRADES_COUNT_mar_w30",
    # "large_buy_value_mar_w30",
    # "large_sell_value_mar_w30",
    # "med_buy_value_mar_w30",
    # "med_sell_value_mar_w30",
    # # "small_buy_value_mar_w30",
    # # "small_sell_value_mar_w30",
    # "inst_buy_value_mar_w30",
    # "inst_sell_value_mar_w30",
    # "large_net_inflow_mar_w30",
    
    
    "large_buy_rate_w0",
    "large_sell_rate_w0",
    "initiative_sell_rate_w0",
    # "turnover_rate_w0",
    # "swing_w0",
    # ## "High_VolKurt_w0",
    "High_PVcor_w0",
    "MinuVol_call_w0",
    ## "MinuVol_coc_w0",
    ## "MinuVol_diff_w0",
    ## "MinuVol_intra_w0",
    ## "MinuVol_rate_w0",
    "amresid_amount_w0",
    "apm_w0",
    # ## "high_RetDV_w0",
    # # "high_RetKurt_w0",
    # # "high_RetSkew_w0",
    # # "high_RetVar_w0",
    # # "high_VolSkew_w0",
    # # "high_beta_w0",
    # # "high_dev_w0",
    # # "high_hprice_w0",
    "high_pvi_w0",
    "high_vol_close_w0",
    "high_vol_open_w0",
    "high_vr_w0",
    "price2vol_w0",
    # ## "r2_amount_pm_w0",
    "residpos_amount_pm_w0",
    "up_down_limit_status_w0"
    ]
    
    
    # 更新输出目录以包含日期范围信息
    if args.output is None:
        
        valid_range = cfg.date_ranges["valid"]
        train_range = cfg.date_ranges["train"]
        cfg.output_root = f"outputs/DFZQ_GRU_MODEL_vd_{valid_range[0]}_{valid_range[1]}_t_{train_range[0]}_{train_range[1]}"
    
    # 打印配置参数
    logger.info(f"训练配置:")
    logger.info(f"  输出目录: {cfg.output_root}")
    logger.info(f"  数据集路径: {cfg.dataset_path}")
    logger.info(f"  基础输入维度: {cfg.base_input_size} {'(自动检测)' if cfg.base_input_size is None else '(手动设置)'}")
    logger.info(f"  训练参数: {cfg.max_epochs}轮, 学习率{cfg.lr}, 权重衰减{cfg.weight_decay}")
    logger.info(f"  数据加载: {cfg.num_workers}进程, 批次大小{cfg.chunk_size} (每GPU批), 预取{cfg.prefetch_factor}")
    logger.info(f"  DuckDB配置: {cfg.duck_threads}线程, {cfg.duck_memory}内存, {cfg.duck_cache}缓存")
    logger.info(f"  其他设置: 混合精度{cfg.use_amp}, 固定索引{cfg.use_fixed_indices}, 打乱{cfg.shuffle}")

    if cfg.date_ranges:
        logger.info(f"  自定义日期范围: {cfg.date_ranges}")
    if cfg.selected_factors:
        logger.info(f"  🎯 特征选择: {cfg.selected_factors} (共{len(cfg.selected_factors)}个特征)")
    else:
        logger.info(f"  📊 使用全部特征")
    
    # 🚀 回测集成配置信息  
    auto_backtest = not args.no_backtest
    if auto_backtest:
        logger.info(f"  🚀 自动回测: 启用 ({args.backtest_start} - {args.backtest_end})")
    else:
        logger.info(f"  💡 自动回测: 已禁用 (--no-backtest)")
    
    # 执行训练
    logger.info("开始训练")
    try:
        training_output_dir = run_training(cfg)
        logger.info("训练完成")
        logger.info(f"输出文件保存在模型自动生成的目录中")
        
        # ========= 🚀 新增：自动回测集成 =========
        if auto_backtest:
            logger.info("\n" + "="*60)
            logger.info("🚀 训练完成，开始自动回测...")
            logger.info("="*60)
            
            try:
                # 导入回测相关类
                from backtest_model import ModelBacktestConfig, ModelBacktester
                
                # 🔧 修复：直接从训练过程获取实际使用的输出目录
                # 而不是通过get_experiment_summary重新生成（会产生新的时间戳）
                actual_output_dir = None
                
                # 方法1: 尝试从全局变量或训练函数返回值获取
                # 方法2: 扫描outputs目录找最新的匹配目录
                import os
                from pathlib import Path
                
                # 优先使用训练函数返回的实际输出目录
                if training_output_dir and Path(training_output_dir).exists():
                    actual_output_dir = str(training_output_dir)
                    logger.info(f"🎯 使用训练返回的输出目录: {actual_output_dir}")
                else:
                    # 构建模式匹配最新的输出目录（跨平台安全）
                    base_root = Path(cfg.output_root)
                    base_dir = base_root.parent
                    prefix = base_root.name + "_"
                    
                    if not base_dir.exists():
                        logger.error(f"❌ 输出根目录不存在: {base_dir}")
                        raise FileNotFoundError(f"输出根目录不存在: {base_dir}")
                    
                    matching_paths = sorted(
                        [p for p in base_dir.glob(prefix + "*") if p.is_dir()],
                        key=lambda p: p.stat().st_mtime
                    )
                    
                    if matching_paths:
                        actual_output_dir = str(matching_paths[-1])
                        logger.info(f"🎯 自动检测到最新训练目录: {actual_output_dir}")
                    else:
                        pattern = str(base_dir / (prefix + "*"))
                        logger.error(f"❌ 未找到匹配的训练目录，模式: {pattern}")
                        raise FileNotFoundError(f"未找到训练输出目录，模式: {pattern}")
                
                # 创建回测配置
                bt_cfg = ModelBacktestConfig()
                bt_cfg.model_path = actual_output_dir        # 刚才训练保存的目录
                bt_cfg.dataset_path = cfg.dataset_path       # 使用相同的数据集
                
                # 设置回测时间范围
                bt_cfg.start_date = args.backtest_start
                bt_cfg.end_date = args.backtest_end
                
                # 可以根据需要调整其他回测参数
                bt_cfg.rebalance_frequency = "10D"
                bt_cfg.max_stocks = 100  # 与 backtest_model 默认一致
                bt_cfg.trade_cost_rate = 20 / 10000  # 换手费率设置为 0.002 (20bp)
                # 与 backtest_model 的输出配置保持一致
                bt_cfg.save_excel = True
                bt_cfg.print_results = True
                bt_cfg.enable_factor_save = True
                bt_cfg.factor_target_format = "backtest"
                bt_cfg.factor_save_formats = ["csv"]
                
                logger.info(f"📊 回测配置:")
                logger.info(f"  模型路径: {bt_cfg.model_path}")
                logger.info(f"  数据集路径: {bt_cfg.dataset_path}")
                logger.info(f"  回测时间: {bt_cfg.start_date} - {bt_cfg.end_date}")
                logger.info(f"  调仓频率: {bt_cfg.rebalance_frequency}")
                logger.info(f"  最大持股: {bt_cfg.max_stocks}")
                
                print(f"\n🚀 开始回测 {bt_cfg.start_date}-{bt_cfg.end_date}...")
                
                # 执行回测
                backtester = ModelBacktester(bt_cfg)
                backtest_results = backtester.run_full_backtest()
                
                if backtest_results is not None:
                    logger.info("✅ 自动回测完成！")
                    logger.info(f"📊 回测结果保存至: {bt_cfg.backtest_result_path}")
                    print(f"\n✅ 回测全部完成！")
                    print(f"📊 结果保存至: {bt_cfg.backtest_result_path}")
                    print(f"📈 因子文件保存至: {bt_cfg.backtest_result_path}/factors/")
                else:
                    logger.warning("❌ 回测未能正常完成")
                    print("❌ 回测未能正常完成")
                    
            except ImportError as e:
                logger.error(f"❌ 导入回测模块失败: {e}")
                logger.error("请确保 backtest_model.py 在当前目录下")
                print(f"❌ 导入回测模块失败: {e}")
            except Exception as e:
                logger.error(f"❌ 自动回测失败: {e}")
                logger.exception("回测详细错误信息:")
                print(f"❌ 自动回测失败: {e}")
                print("训练已完成，可以稍后手动运行回测")
        else:
            logger.info("💡 如需跳过回测，请添加 --no-backtest 参数")
            
    except Exception as e:
        logger.exception(f"训练过程中发生错误: {e}")
        sys.exit(1)
    
    logger.info("训练过程结束")

if __name__ == "__main__":
    main() 
