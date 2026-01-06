"""
回测结果工程化产物流水线

编排 steps + report，统一落盘到 <run_dir>/...
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from backtest.backtest_result_pipeline.types import PipelineResult, RunContext
from backtest.backtest_result_pipeline.io.layout import (
    normalize_path,
    generate_run_id,
    create_run_context
)
from backtest.backtest_result_pipeline.steps.step_factor import run_step_factor
from backtest.backtest_result_pipeline.steps.step_signal import run_step_signal
from backtest.backtest_result_pipeline.steps.step_backtest import run_step_backtest
from backtest.backtest_result_pipeline.steps.step_benchmark import run_step_benchmark
from backtest.backtest_result_pipeline.steps.step_aggregate import run_step_aggregate
from backtest.backtest_result_pipeline.steps.step_export import run_step_export

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig


class BacktestResultPipeline:
    """
    回测结果工程化产物流水线
    
    职责：
    - 编排 steps（factor → signal → backtest → benchmark → aggregate → export）
    - 统一落盘到 <run_dir>/...
    - 生成 manifest.json
    """
    
    def __init__(
        self,
        cfg: "ModelBacktestConfig",
        run_id: Optional[str] = None,
        auto_suffix: bool = True,
        overwrite: bool = False
    ):
        """
        初始化 Pipeline
        
        Args:
            cfg: ModelBacktestConfig 配置对象
            run_id: 运行 ID（可选，默认自动生成）
            auto_suffix: run_id 冲突时是否自动追加后缀
            overwrite: 是否允许覆盖已存在的 run_dir
        """
        self.cfg = cfg
        self.auto_suffix = auto_suffix
        self.overwrite = overwrite
        
        # 配置日志
        self._setup_logging()
        
        # 归一化路径
        self._normalize_config_paths()
        
        # 生成 run_id
        if run_id is None:
            benchmark_code = getattr(cfg, "benchmark_code", "unknown")
            self.run_id = generate_run_id(
                benchmark_code,
                cfg.start_date,
                cfg.end_date
            )
        else:
            self.run_id = run_id
        
        self.logger = logging.getLogger(__name__)
        self.logger.info(f"Pipeline 初始化: run_id={self.run_id}")
    
    def _setup_logging(self):
        """配置日志"""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
    
    def _normalize_config_paths(self):
        """归一化配置中的路径"""
        # model_path
        if self.cfg.model_path:
            normalized = normalize_path(self.cfg.model_path)
            if normalized:
                self.cfg.model_path = str(normalized)
        
        # dataset_path
        if self.cfg.dataset_path:
            normalized = normalize_path(self.cfg.dataset_path)
            if normalized:
                self.cfg.dataset_path = str(normalized)
        
        # backtest_result_path（pipeline 会重新指向 run_dir/data/factors）
        # 这里不做处理，留给 step_factor
    
    def run(self) -> PipelineResult:
        """
        运行完整 Pipeline
        
        Returns:
            PipelineResult: 产物路径汇总
        """
        self.logger.info("=" * 60)
        self.logger.info("🚀 BacktestResultPipeline 开始运行")
        self.logger.info(f"   回测期间: {self.cfg.start_date} - {self.cfg.end_date}")
        self.logger.info(f"   模型路径: {self.cfg.model_path}")
        self.logger.info(f"   基准代码: {getattr(self.cfg, 'benchmark_code', 'N/A')}")
        self.logger.info("=" * 60)
        
        start_time = datetime.now()
        
        try:
            # ========== 1. 创建运行上下文（目录结构） ==========
            self.logger.info("\n📁 Step 0: 创建运行目录...")
            run_ctx = create_run_context(
                model_path=self.cfg.model_path,
                run_id=self.run_id,
                auto_suffix=self.auto_suffix,
                overwrite=self.overwrite
            )
            self.logger.info(f"   run_dir: {run_ctx.run_dir}")
            
            # ========== 2. 因子生成 ==========
            self.logger.info("\n📊 Step 1: 因子生成...")
            df_factor = run_step_factor(self.cfg, run_ctx)
            
            if df_factor.empty:
                raise RuntimeError("因子生成失败：df_factor 为空")
            
            # ========== 3. 信号构建 ==========
            self.logger.info("\n📈 Step 2: 信号构建...")
            alpha_expressions = run_step_signal(self.cfg)
            
            if not alpha_expressions:
                raise RuntimeError("信号构建失败：alpha_expressions 为空")
            
            # ========== 4. 回测执行（一次回测，不再按年重跑） ==========
            self.logger.info("\n🔄 Step 3: 回测执行...")
            backtest_results = run_step_backtest(self.cfg, df_factor, alpha_expressions, run_ctx)
            
            if not backtest_results:
                raise RuntimeError("回测执行失败：backtest_results 为空")
            
            # ========== 5. 基准对齐 ==========
            self.logger.info("\n📉 Step 4: 基准对齐...")
            benchmark_results = run_step_benchmark(self.cfg, backtest_results)
            
            # ========== 6. 聚合统计（总体 + 年度，从序列切片） ==========
            self.logger.info("\n📋 Step 5: 聚合统计...")
            aggregated_tables = run_step_aggregate(self.cfg, backtest_results, benchmark_results)
            
            # ========== 7. 统一导出 ==========
            self.logger.info("\n💾 Step 6: 统一导出...")
            pipeline_result = run_step_export(
                self.cfg,
                run_ctx,
                backtest_results,
                benchmark_results,
                aggregated_tables
            )
            
            # ========== 完成 ==========
            elapsed = (datetime.now() - start_time).total_seconds()
            self.logger.info("\n" + "=" * 60)
            self.logger.info("✅ BacktestResultPipeline 运行完成")
            self.logger.info(f"   耗时: {elapsed:.1f} 秒")
            self.logger.info(f"   run_dir: {pipeline_result.run_dir}")
            self.logger.info(f"   Excel: {pipeline_result.tables_excel_path}")
            self.logger.info(f"   manifest: {pipeline_result.manifest_path}")
            self.logger.info("=" * 60)
            
            return pipeline_result
        
        except Exception as e:
            self.logger.error(f"\n❌ Pipeline 运行失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
