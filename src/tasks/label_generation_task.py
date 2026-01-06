from __future__ import annotations

"""src.tasks.label_generation_task
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Optimised implementation of :class:`LabelGenerationTask`.
The original version grew organically and mixed configuration, execution
logic and persistence concerns in a single class.  This refactor
introduces a small ``@dataclass`` for immutable configuration, clearer
separation of concerns, stricter type‑checking and more defensive error
handling while remaining fully backward‑compatible with existing call
sites.

Key improvements
================
* **Config dataclass** – strongly‑typed, validated at construction time.
* **Lazy initialisation** – expensive helpers are created only when
  first used, making the class lighter to construct in batch jobs.
* **Statistics helper** – added ``get_label_statistics`` used by client
  scripts but missing in the original file.
* **Robust table‑name builder** – extracted to a standalone private
  method for clarity and reuse.
* **Database save** – retries & chunked inserts for large tables,
  controllable via config.
* **Logging** – consistent contextual information with job‑id & elapsed
  time.
"""

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any, Dict, Optional, List, Union

import pandas as pd
import time

from src.data_service.data_engineering.labels_engineering import LabelGenerator
from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.table_schema import TableSchemaBuilder

__all__ = ["LabelGenerationConfig", "LabelGenerationTask"]

logger = logging.getLogger(__name__)
if not logger.handlers:
    # Ensure library users get at least *some* output.
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s – %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@dataclass(frozen=True, slots=True)
class LabelGenerationConfig:
    """Immutable configuration for a label‑generation job."""

    # Strategy & core hyper‑parameters
    strategy: Union[str, List[str]] = "top_correlation"  # 🎯 支持单个字符串或字符串列表
    label_shift: int = 10
    corr_window: int = 240
    corr_rank_num: int = 30
    min_rank_num: int = 20
    correlation_type: str = "pearson"

    # Data‑source & adjustment options
    use_db_pct_change: bool = True
    adjuster_params: Dict[str, Any] = field(default_factory=dict)
    overlap_days: int = 0  # 添加 overlap_days 参数，默认为0

    # Persistence
    table_name: Optional[str] = None
    batch_size: int = 10_000
    save_intermediate: bool = True

    # Internal
    job_id: str = field(default_factory=lambda: datetime.utcnow().strftime("%Y%m%d%H%M%S"))

    def __post_init__(self):  # type: ignore[override]
        if self.label_shift <= 0:
            raise ValueError("label_shift must be positive")
        if self.corr_window <= 0:
            raise ValueError("corr_window must be positive")
        if self.corr_rank_num <= 0:
            raise ValueError("corr_rank_num must be positive")
        if self.min_rank_num <= 0:
            raise ValueError("min_rank_num must be positive")
        
        # 🎯 支持多策略验证
        strategies = [self.strategy] if isinstance(self.strategy, str) else self.strategy
        supported_strategies = {"top_correlation", "raw", "rank"}
        for strategy in strategies:
            if strategy not in supported_strategies:
                raise ValueError(f"Unsupported strategy '{strategy}'. Supported: {supported_strategies}")
        
        if self.overlap_days < 0:
            raise ValueError("overlap_days must be non-negative")


class LabelGenerationTask:
    """High‑level task wrapper used by batch jobs and notebooks."""

    def __init__(self, market_data_provider: MarketDataProvider, *, config: LabelGenerationConfig):
        self._mdp = market_data_provider
        self._cfg = config

        # 🎯 统一成列表处理
        self._strategies = [self._cfg.strategy] if isinstance(self._cfg.strategy, str) else list(self._cfg.strategy)

        # 自动合并 correlation_type 到 adjuster_params
        if "correlation_type" not in self._cfg.adjuster_params:
            self._cfg.adjuster_params["correlation_type"] = self._cfg.correlation_type

        self._label_generator: Optional[LabelGenerator] = None
        self._db_manager: Optional[TestDBManager] = None

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def execute(self, *, start_date: str, end_date: str, save_mode: str = "update", pk_fields: List[str] = None) -> pd.DataFrame:
        """Generate labels and (optionally) persist them.

        Args:
            start_date: 开始日期，格式为YYYY-MM-DD
            end_date: 结束日期，格式为YYYY-MM-DD
            save_mode: 保存模式，'update'或'append'
            pk_fields: 主键字段列表，用于去重

        Raises
        ------
        RuntimeError
            If generation succeeds but persistence fails and
            ``save_intermediate`` is ``True``.
        """
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        if start_dt > end_dt:
            raise ValueError("start_date must be <= end_date")

        # 应用 overlap_days 参数，往前查询几天以确保数据连续性
        if self._cfg.overlap_days > 0:
            adjusted_start_dt = start_dt - pd.Timedelta(days=self._cfg.overlap_days)
            adjusted_start_date = adjusted_start_dt.strftime('%Y-%m-%d')
            logger.info(
                "[%s] 应用 overlap_days=%d，调整查询起始日期从 %s 到 %s",
                self._cfg.job_id,
                self._cfg.overlap_days,
                start_date,
                adjusted_start_date,
            )
            start_date = adjusted_start_date

        logger.info(
            "[%s] Running LabelGenerationTask strategies=%s period=%s→%s",
            self._cfg.job_id,
            self._strategies,
            start_date,
            end_date,
        )
        t0 = datetime.now()

        # ------------------------------------------------------------------
        # 1) Generate labels for all strategies
        # ------------------------------------------------------------------
        lg = self._get_label_generator()
        
        # 🎯 支持多策略执行
        all_labels_df = pd.DataFrame()
        shared_label_raw = None  # 共享的 label_raw，避免重复计算
        
        for strategy in self._strategies:
            logger.info(f"[{self._cfg.job_id}] Executing strategy: {strategy}")
            
            if strategy == "top_correlation":
                labels_df = lg.generate_top_correlation_labels(
                start_date=start_date,
                end_date=end_date,
                label_shift=self._cfg.label_shift,
                corr_window=self._cfg.corr_window,
                corr_rank_num=self._cfg.corr_rank_num,
                min_rank_num=self._cfg.min_rank_num,
                use_db_pct_change=self._cfg.use_db_pct_change,
                correlation_type=self._cfg.correlation_type,
                adjuster_params=self._cfg.adjuster_params,
            )
            elif strategy == "rank":
                labels_df = lg.generate_rank_labels(
                    start_date=start_date,
                    end_date=end_date,
                    label_shift=self._cfg.label_shift,
                    ascending=self._cfg.adjuster_params.get('ascending', True),
                    adjuster_params=self._cfg.adjuster_params,
                )
            elif strategy == "raw":
                labels_df = lg.generate_labels(
                strategy="raw",
                start_date=start_date,
                end_date=end_date,
                label_shift=self._cfg.label_shift,
            )
            else:
                raise ValueError(f"Unsupported strategy: {strategy}")
            
            if labels_df.empty:
                logger.warning(f"[{self._cfg.job_id}] Strategy '{strategy}' generated 0 labels")
                continue
            
            # 🎯 优化：提取并共享 label_raw，避免重复
            if shared_label_raw is None:
                # 第一个策略：保存完整结果
                shared_label_raw = labels_df[labels_df['field_name'] == 'label_raw'].copy()
                all_labels_df = labels_df.copy()
            else:
                # 后续策略：只添加非 label_raw 的字段
                strategy_specific = labels_df[labels_df['field_name'] != 'label_raw'].copy()
                all_labels_df = pd.concat([all_labels_df, strategy_specific], ignore_index=True)
            
            logger.info(f"[{self._cfg.job_id}] Strategy '{strategy}' completed, generated {len(labels_df)} records")

        # 如果应用了 overlap_days，只保留原始日期范围内的数据
        if self._cfg.overlap_days > 0 and not all_labels_df.empty:
            original_start_dt = pd.to_datetime(start_date) + pd.Timedelta(days=self._cfg.overlap_days)
            all_labels_df = all_labels_df[all_labels_df['trade_date'] >= original_start_dt]
            logger.info(
                "[%s] 过滤后的数据范围: %s 到 %s，共 %d 条记录",
                self._cfg.job_id,
                original_start_dt.strftime('%Y-%m-%d'),
                end_date,
                len(all_labels_df),
            )

        # ------------------------------------------------------------------
        # 去重：同一个 (trade_date, stock_code, field_name, label_shift) 只能保留一行
        # 多策略 concat 后，'label_raw' 等公共 field_name 可能重复。
        # 若数值完全一致，保留任意即可；若不一致，默认保留最后生成的策略结果。
        # 这一步可以彻底避免数据库 UPSERT 出现
        #     ON CONFLICT DO UPDATE command cannot affect row a second time
        # 的错误。
        # ------------------------------------------------------------------
        if not all_labels_df.empty:
            pk_cols = ["trade_date", "stock_code", "field_name", "label_shift"]
            before_cnt = len(all_labels_df)
            # 确保 trade_date 类型一致，便于正确识别重复行
            all_labels_df["trade_date"] = pd.to_datetime(all_labels_df["trade_date"])  # type: ignore[arg-type]
            all_labels_df = (
                all_labels_df
                .sort_values(pk_cols)  # 可根据需要调整排序逻辑
                .drop_duplicates(subset=pk_cols, keep="last")
            )
            after_cnt = len(all_labels_df)
            dup_cnt = before_cnt - after_cnt
            if dup_cnt > 0:
                logger.info(
                    "[%s] 去重完成：删除了 %d 条完全重复的主键行，保留 %d 条。",
                    self._cfg.job_id,
                    dup_cnt,
                    after_cnt,
                )

        if all_labels_df.empty:
            logger.warning("[%s] Generated 0 labels across all strategies – nothing to persist.", self._cfg.job_id)
            return all_labels_df

        # ------------------------------------------------------------------
        # 2) Persist
        # ------------------------------------------------------------------
        if self._cfg.save_intermediate:
            if pk_fields is None:
                pk_fields = ["trade_date", "stock_code", "field_name", "label_shift"]
            ok = self._save_to_database(all_labels_df, save_mode=save_mode, pk_fields=pk_fields)
            if not ok:
                raise RuntimeError("Database save failed – see logs above")

        elapsed = (datetime.now() - t0).total_seconds()
        logger.info("[%s] Finished – %d rows in %.1fs", self._cfg.job_id, len(all_labels_df), elapsed)
        return all_labels_df

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    @staticmethod
    def get_label_statistics(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:  # noqa: D401
        """Return per‑field basic statistics (mean, std, min, max)."""
        stats: Dict[str, Dict[str, float]] = {}
        if df.empty:
            return stats
        for field in df["field_name"].unique():
            subset = df.loc[df["field_name"] == field, "value"].astype(float)
            stats[field] = {
                "count": int(subset.count()),
                "mean": subset.mean(),
                "std": subset.std(),
                "min": subset.min(),
                "max": subset.max(),
            }
        return stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_label_generator(self) -> LabelGenerator:
        if self._label_generator is None:
            self._label_generator = LabelGenerator(self._mdp)
        return self._label_generator

    def _get_db_manager(self) -> TestDBManager:
        if self._db_manager is None:
            self._db_manager = TestDBManager()
        return self._db_manager

    def _make_table_name(self) -> str:
        """🎯 支持多策略的表名生成"""
        if self._cfg.table_name is not None:
            return self._cfg.table_name
        
        # 默认表名统一为 training_label_v1
        return "training_label_v1"

    def _save_to_database(self, df: pd.DataFrame, save_mode: str = "update", pk_fields: List[str] = None) -> bool:
        """Persist the dataframe with basic retry logic."""
        table_name = self._make_table_name()
        db = self._get_db_manager()

        # Ensure schema consistency – convert dtypes *before* writing.
        cleaned = df.copy()
        cleaned["trade_date"] = pd.to_datetime(cleaned["trade_date"])  # type: ignore[arg-type]
        cleaned["value"] = pd.to_numeric(cleaned["value"], errors="coerce")
        cleaned["label_shift"] = cleaned["label_shift"].astype("Int64")

        # 检查表是否存在，不存在则使用 TableSchemaBuilder 创建表结构
        if not db.check_table_exists(table_name):
            logger.info(f"表 {table_name} 不存在，使用 TableSchemaBuilder 创建表结构")
            
            # 使用 TableSchemaBuilder 创建表结构，确保主键设置与 pk_fields 一致
            columns = TableSchemaBuilder.create_factor_table_schema(
                table_name=table_name,
                df=cleaned,
                lag=30,  # 默认值，标签表通常不需要用到
                days_count=1,  # 默认值，标签表通常不需要用到
                numeric_type="float",
                numeric_precision=(38, 32),
                pk_fields=pk_fields  # 确保与后续 UPSERT 操作的 pk_fields 一致
            )
            
            # 创建表
            db.create_table(table_name, columns)
            logger.info(f"已创建表 {table_name}，主键设置为: {pk_fields}")

        attempts = 3
        for i in range(1, attempts + 1):
            try:
                ok = db.save_dataframe(
                    df=cleaned,
                    table_name=table_name,
                    mode=save_mode,
                    index=False,
                    batch_size=self._cfg.batch_size,
                    use_parallel=True,
                    pk_fields=pk_fields,
                )
                if ok:
                    logger.info("[%s] Saved to %s (attempt %d)", self._cfg.job_id, table_name, i)
                    return True
            except Exception:  # pragma: no cover – logged by TestDBManager
                logger.exception("[%s] DB save attempt %d/%d failed", self._cfg.job_id, i, attempts)
            if i < attempts:
                logger.info("[%s] Retrying in 2s…", self._cfg.job_id)
                time.sleep(2)
        return False
