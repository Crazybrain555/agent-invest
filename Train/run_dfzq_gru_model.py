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
    parser.add_argument('--dataset', type=str, default='data/Dataset/pv_v5_pv_v4_price&trade_pt10818',
                       help="数据集路径，默认为'data/Dataset/pv_v1'")
    parser.add_argument('--epochs', type=int, default=500,
                       help="训练轮数，默认为200")
    parser.add_argument('--batch_size', type=int, default=256*8,
    # parser.add_argument('--batch_size', type=int, default=1,
                       help="批次大小，默认为256")
    parser.add_argument('--workers', type=int, default=2,
                       help="数据加载工作进程数，默认为1（推荐，避免多进程缓存冲突）")
    parser.add_argument('--no-amp', action='store_true',
                       help="禁用混合精度训练")
    parser.add_argument('--prefetch-factor', type=int, default=4,
                       help="DataLoader 每个 worker 预取批次数，默认为4")
    parser.add_argument('--cpu', action='store_true',
                       help="强制使用CPU训练")
    parser.add_argument('--shuffle', action='store_true',
                       help="是否打乱数据")
    parser.add_argument('--weight_decay', type=float, default=2.5e-2,  #调整为1 e-3 晚上
                       help="权重衰减系数，默认为0.01")
    parser.add_argument('--lr', type=float, default=2e-5 ,
                       help="学习率，默认为config中的默认值")
    parser.add_argument('--no-fixed-indices', action='store_true',
                       help="禁用固定索引，使用随机顺序（可能导致数据不一致）")
    
    # 🚀 新增特征选择参数
    parser.add_argument('--selected-factors', type=str, nargs='*', default=None,
                       help="选择的特征列表，用空格分隔。例如：--selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
    parser.add_argument('--list-factors', action='store_true',
                       help="列出数据集中所有可用的特征名称")
    
    # 🚀 新增chunk_size参数
    parser.add_argument('--chunk-size', type=int, default=131072,
                       help="DuckDB数据流读取块大小，默认为65536。可选: 16384, 32768, 65536, 131072")
    

    
    # DuckDB performance tuning - 针对64GB内存优化
    parser.add_argument('--duck-threads', type=int, default=8, help="DuckDB worker threads.")
    parser.add_argument('--duck-memory', type=str, default='16GB', help="DuckDB memory limit.")
    parser.add_argument('--duck-cache', type=str, default='4GB', help="DuckDB object cache size.")
    

    
    args = parser.parse_args()

    # 🚀 新增：处理特征列表查询
    if args.list_factors:
        try:
            # 临时创建数据集实例来获取可用特征
            import json
            from pathlib import Path
            schema_path = Path(args.dataset) / "meta" / "schema.json"
            if schema_path.exists():
                with schema_path.open("r", encoding="utf-8") as fp:
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
    cfg.batch_size = args.batch_size
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
    cfg.chunk_size = args.chunk_size
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
        "test": ("20220101", "20241231"),
    }
    cfg.use_custom_splits = True
    

    cfg.selected_factors = [
        "adj_close_mar_w1",
        "adj_open_mar_w1",
        "adj_high_mar_w1",
        "adj_low_mar_w1",
        "vwap_mar_w1",
        "vwap_mar_w30",
        "TRADES_COUNT_mar_w30",
        "large_buy_value_mar_w30",
        "large_sell_value_mar_w30",
        "med_buy_value_mar_w30",
        "med_sell_value_mar_w30",
        # "small_buy_value_mar_w30",
        # "small_sell_value_mar_w30",
        "inst_buy_value_mar_w30",
        "inst_sell_value_mar_w30",
        "large_net_inflow_mar_w30",
        "amount_mar_w30",
        "turnover_rate_w0"
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
    logger.info(f"  训练参数: {cfg.max_epochs}轮, 批次{cfg.batch_size}, 学习率{cfg.lr}, 权重衰减{cfg.weight_decay}")
    logger.info(f"  数据加载: {cfg.num_workers}进程, 块大小{cfg.chunk_size}, 预取{cfg.prefetch_factor}")
    logger.info(f"  DuckDB配置: {cfg.duck_threads}线程, {cfg.duck_memory}内存, {cfg.duck_cache}缓存")
    logger.info(f"  其他设置: 混合精度{cfg.use_amp}, 固定索引{cfg.use_fixed_indices}, 打乱{cfg.shuffle}")
    if cfg.date_ranges:
        logger.info(f"  自定义日期范围: {cfg.date_ranges}")
    if cfg.selected_factors:
        logger.info(f"  🎯 特征选择: {cfg.selected_factors} (共{len(cfg.selected_factors)}个特征)")
    else:
        logger.info(f"  📊 使用全部特征")
    
    # 执行训练
    logger.info("开始训练")
    try:
        run_training(cfg)
        logger.info("训练完成")
        logger.info(f"输出文件保存在模型自动生成的目录中")
    except Exception as e:
        logger.exception(f"训练过程中发生错误: {e}")
        sys.exit(1)
    
    logger.info("训练过程结束")

if __name__ == "__main__":
    main() 