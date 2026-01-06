"""
Space Data Pipeline - Signals 入库管线入口

专注处理 Space NAS 上的 signals 因子数据：
- 支持二级分类映射
- 未映射信号仅记录日志
- 高效 UPSERT 入库
- 支持全量和增量处理

使用方式：
    # 处理最近20天的所有 signals
    python run_space_data_pipeline.py --latest --range-days 20 --data-type signals
    
    # 处理指定日期范围的所有 signals
    python run_space_data_pipeline.py --latest --start-date 20240101 --end-date 20241231
    
    # 处理特定的 signals
    python run_space_data_pipeline.py --signals qop_stb qop_acc --range-days 20

注意：
    - 本脚本已重构，专注于 signals 处理
    - theme 和 forbid 数据请使用对应的专用脚本
    - 因子分类映射配置: configs/field_mappings/factor_mapping.yaml
"""

import logging
import os
import sys
import argparse
from datetime import datetime, timedelta

# Ensure the project root is in the path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.append(project_root)

# Import the refactored task module
from src.tasks.space_signals_ingest import SpaceSignalsIngest

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            f"space_pipeline_{datetime.now().strftime('%Y%m%d')}.log",
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger("space_signals_entry")


def _dateint_today() -> int:
    """获取今天的日期（YYYYMMDD 格式）"""
    return int(datetime.now().strftime('%Y%m%d'))


def _dateint_from_range_days(range_days: int) -> int:
    """
    计算回溯日期
    
    Args:
        range_days: 回溯天数
        
    Returns:
        日期（YYYYMMDD 格式）
    """
    dt = datetime.now() - timedelta(days=range_days)
    return int(dt.strftime('%Y%m%d'))


def main():
    """
    主入口函数
    
    解析命令行参数并执行对应的 signals 入库任务
    
    参数使用规则：
    - --latest: 处理所有可用的 signals
    - --signals: 处理指定的 signals 列表
    - --range-days: 回溯天数（默认20天）
    - --start-date: 指定开始日期（YYYYMMDD）
    - --end-date: 指定结束日期（YYYYMMDD，默认为今天）
    - --data-type: 数据类型（仅支持 signals）
    - --mapping-path: 因子映射文件路径
    
    返回值：
    - 0: 成功
    - 1: 失败
    """
    parser = argparse.ArgumentParser(
        description='Space Data Pipeline - Signals Only (Refactored)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 处理最近20天的所有 signals
  %(prog)s --latest --range-days 20 --data-type signals
  
  # 处理指定日期范围
  %(prog)s --latest --start-date 20240101 --end-date 20241231
  
  # 处理特定 signals
  %(prog)s --signals qop_stb qop_acc da2ev --range-days 30
        """
    )
    
    # 模式选择（互斥）
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        '--latest',
        action='store_true',
        help='处理所有可用的 signals（从 Space NAS 自动发现）'
    )
    mode_group.add_argument(
        '--signals',
        nargs='+',
        metavar='SIGNAL',
        help='处理指定的 signals 列表'
    )
    
    # 数据类型（保留向后兼容，但仅接受 signals）
    parser.add_argument(
        '--data-type',
        choices=['signals'],
        default='signals',
        help='数据类型（重构后仅支持 signals）'
    )
    
    # 日期范围参数
    parser.add_argument(
        '--range-days',
        type=int,
        default=20,
        help='回溯天数（默认: 20）'
    )
    parser.add_argument(
        '--start-date',
        type=str,
        metavar='YYYYMMDD',
        help='开始日期（YYYYMMDD 格式，会覆盖 --range-days）'
    )
    parser.add_argument(
        '--end-date',
        type=str,
        metavar='YYYYMMDD',
        help='结束日期（YYYYMMDD 格式，默认为今天）'
    )
    
    # 配置路径
    parser.add_argument(
        '--mapping-path',
        type=str,
        default='configs/field_mappings/factor_mapping.yaml',
        help='因子分类映射文件路径（默认: configs/field_mappings/factor_mapping.yaml）'
    )
    
    # 🚀 新增：数据保存模式（性能优化）
    parser.add_argument(
        '--mode',
        type=str,
        default='auto',
        choices=['auto', 'append', 'update'],
        help=('数据保存模式: '
              'auto (智能检测，空表用append，有数据用update), '
              'append (快速追加，无去重，适合初始化), '
              'update (UPSERT模式，有去重，适合日常更新)')
    )
    
    args = parser.parse_args()
    
    # ==================== 日期计算 ====================
    
    # 结束日期：使用指定值或默认为今天
    end_date = int(args.end_date) if args.end_date else _dateint_today()
    
    # 开始日期：优先使用指定值，否则从 range_days 计算
    if args.start_date:
        start_date = int(args.start_date)
        logger.info(f"Using specified start date: {start_date}")
    else:
        start_date = _dateint_from_range_days(args.range_days)
        logger.info(f"Calculated start date from range_days={args.range_days}: {start_date}")
    
    logger.info(f"Effective date range: {start_date} ~ {end_date}")
    
    # ==================== 任务执行 ====================
    
    try:
        # 初始化任务处理器
        logger.info(f"Initializing Space Signals Ingest Task...")
        logger.info(f"Factor mapping: {args.mapping_path}")
        logger.info(f"Save mode: {args.mode}")
        
        task = SpaceSignalsIngest(mapping_path=args.mapping_path)
        
        # 根据模式执行任务
        if args.latest:
            logger.info("=== Mode: Process ALL available signals ===")
            success = task.run_latest(start_date=start_date, end_date=end_date, save_mode=args.mode)
        
        elif args.signals:
            logger.info(f"=== Mode: Process {len(args.signals)} specific signals ===")
            logger.info(f"Signals: {args.signals}")
            success = task.run_specific(
                signal_names=args.signals, 
                start_date=start_date,
                end_date=end_date,
                save_mode=args.mode
            )
        else:
            # 理论上不会到这里（argparse 会保证）
            logger.error("No valid mode specified")
            return 1
        
        # ==================== 结果报告 ====================
        
        if success:
            logger.info("=" * 80)
            logger.info("Pipeline execution SUCCEEDED")
            logger.info("=" * 80)
            
            # 显示统计信息
            stats = task.get_statistics()
            logger.info(f"Mapping statistics: {stats['mapping']['total_categories']} categories, "
                       f"{stats['mapping']['total_signals']} signals")
            
            if stats['unmapped_count'] > 0:
                logger.warning(f"Found {stats['unmapped_count']} unmapped signals "
                             f"(see logs/missing_signals_*.log)")
            
            return 0
        else:
            logger.error("=" * 80)
            logger.error("Pipeline execution FAILED")
            logger.error("=" * 80)
            return 1
    
    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user (Ctrl+C)")
        return 1
            
    except Exception as e:
        logger.exception(f"Pipeline execution failed with exception: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main()) 
