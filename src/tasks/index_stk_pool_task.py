import re
import time
from datetime import datetime, date
from typing import List, Optional, Dict

import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from src.tasks.base import BaseTask
from src.utils.table_schema import TableSchemaBuilder
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.db_connection import db_config
from src.utils.config_loader import ConfigLoader


class IndexStockPoolTask(BaseTask):
    """
    Build and update index stock pool data from Wind AIndexMembers.
    """

    def __init__(
        self,
        table_name: str = "stk_pool_of_index",
        pool_codes: Optional[List[str]] = None,
        overlap_days: int = 20,
        init_start_date: str = "20050104",
        calendar_market: str = "SSE",
        write_batch_rows: int = 200_000,
    ):
        super().__init__("index_stk_pool_task")
        self.table_name = table_name
        self.pool_codes = self._normalize_pool_codes(pool_codes or [])
        self.overlap_days = overlap_days
        self.init_start_date = self._normalize_date_str(init_start_date)
        self.calendar_market = calendar_market
        self.write_batch_rows = write_batch_rows

        self.config_loader = ConfigLoader(config_dir="configs")
        self.wind_engine = db_config.get_wind_engine()
        self.db = TestDBManager()

        self._suffix_pattern = self._build_suffix_pattern()
        self._ensure_table_exists()

    @staticmethod
    def _normalize_date_str(date_value: Optional[object]) -> Optional[str]:
        if date_value is None:
            return None
        if isinstance(date_value, (datetime, date)):
            return date_value.strftime("%Y%m%d")
        date_str = str(date_value).strip()
        if "-" in date_str:
            date_str = date_str.replace("-", "")
        return date_str

    @staticmethod
    def _normalize_pool_codes(pool_codes: List[str]) -> List[str]:
        normalized = []
        for code in pool_codes:
            if code is None:
                continue
            for part in str(code).split(","):
                part = part.strip().upper()
                if part:
                    normalized.append(part)
        # Deduplicate while preserving order
        seen = set()
        unique_codes = []
        for code in normalized:
            if code not in seen:
                seen.add(code)
                unique_codes.append(code)
        return unique_codes

    def _build_suffix_pattern(self) -> Optional[str]:
        config = self.config_loader.load_config("db/table_config.yaml")
        remove_suffix_rule = config["code_format_rules"]["output_format"]["remove_all_suffix"]
        suffixes = remove_suffix_rule.get("suffixes", [])
        if not suffixes:
            return None
        return "|".join(map(re.escape, suffixes))

    def _normalize_stock_codes(self, series: pd.Series) -> pd.Series:
        codes = series.fillna("").astype(str).str.upper().str.strip()
        if self._suffix_pattern:
            codes = codes.str.replace(f"({self._suffix_pattern})$", "", regex=True)
        codes = codes.replace({"": pd.NA, "NAN": pd.NA})
        mask = codes.notna()
        codes.loc[mask] = codes.loc[mask].str.zfill(6)
        return codes

    def _ensure_table_exists(self) -> None:
        if not self.db.check_table_exists(self.table_name):
            self.logger.info(f"Table '{self.table_name}' not found. Creating...")
            schema_def = TableSchemaBuilder.create_stk_pool_table_schema()
            self.db.create_table(self.table_name, schema_def)
            self.logger.info(f"Table '{self.table_name}' created.")

    def _get_latest_trading_date(self, end_date: Optional[str] = None) -> Optional[str]:
        end_date_str = self._normalize_date_str(end_date) or datetime.now().strftime("%Y%m%d")
        query = text(
            """
            SELECT MAX(TRADE_DAYS) AS trade_date
            FROM wind_quant.dbo.AShareCalendar
            WHERE S_INFO_EXCHMARKET = :market
              AND TRADE_DAYS <= :end_date
            """
        )
        df = pd.read_sql(query, self.wind_engine, params={"market": self.calendar_market, "end_date": end_date_str})
        if df.empty or pd.isna(df.loc[0, "trade_date"]):
            return None
        return self._normalize_date_str(df.loc[0, "trade_date"])

    def _get_trading_dates(self, start_date: str, end_date: str) -> List[str]:
        query = text(
            """
            SELECT TRADE_DAYS
            FROM wind_quant.dbo.AShareCalendar
            WHERE S_INFO_EXCHMARKET = :market
              AND TRADE_DAYS BETWEEN :start_date AND :end_date
            ORDER BY TRADE_DAYS
            """
        )
        df = pd.read_sql(
            query,
            self.wind_engine,
            params={"market": self.calendar_market, "start_date": start_date, "end_date": end_date},
        )
        return df["TRADE_DAYS"].astype(str).tolist()

    def _get_overlap_start_date(self, end_date: str, overlap_days: int) -> str:
        if overlap_days <= 0:
            return end_date
        limit = overlap_days + 1
        query = text(
            f"""
            SELECT TOP {limit} TRADE_DAYS
            FROM wind_quant.dbo.AShareCalendar
            WHERE S_INFO_EXCHMARKET = :market
              AND TRADE_DAYS <= :end_date
            ORDER BY TRADE_DAYS DESC
            """
        )
        df = pd.read_sql(query, self.wind_engine, params={"market": self.calendar_market, "end_date": end_date})
        if df.empty:
            return end_date
        idx = min(overlap_days, len(df) - 1)
        return str(df.loc[idx, "TRADE_DAYS"])

    def _get_latest_trade_date_for_pool(self, pool_code: str) -> Optional[str]:
        if not self.db.check_table_exists(self.table_name):
            return None
        query = text(f"SELECT MAX(trade_date) AS trade_date FROM {self.table_name} WHERE pool_code = :pool_code")
        with db_config.get_test_session() as session:
            result = session.execute(query, {"pool_code": pool_code}).scalar()
        return self._normalize_date_str(result)

    def _fetch_index_members(self, trade_date: str, pool_codes: List[str]) -> pd.DataFrame:
        if not pool_codes:
            return pd.DataFrame()
        for code in pool_codes:
            if not re.fullmatch(r"[0-9A-Z\\.]+", code):
                raise ValueError(f"Invalid pool_code: {code}")
        pool_list = ", ".join([f"'{code}'" for code in pool_codes])
        query = text(
            f"""
            SELECT
                S_INFO_WINDCODE AS pool_code,
                S_CON_WINDCODE AS stock_windcode
            FROM wind_quant.dbo.AIndexMembers
            WHERE S_INFO_WINDCODE IN ({pool_list})
              AND S_CON_INDATE <= :trade_date
              AND (S_CON_OUTDATE IS NULL OR S_CON_OUTDATE >= :trade_date)
            """
        )
        return pd.read_sql(query, self.wind_engine, params={"trade_date": trade_date})

    def _prepare_output(self, raw_df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        if raw_df.empty:
            return raw_df
        df = raw_df.copy()
        df["pool_code"] = df["pool_code"].astype(str).str.strip()
        df["stock_code"] = self._normalize_stock_codes(df["stock_windcode"])
        df = df.dropna(subset=["pool_code", "stock_code"])
        df["trade_date"] = pd.to_datetime(trade_date)
        df["signal"] = 1
        df["insert_time"] = datetime.utcnow()
        df = df[["trade_date", "pool_code", "stock_code", "signal", "insert_time"]]
        return df.drop_duplicates(subset=["trade_date", "pool_code", "stock_code"])

    def _save_dataframe(self, df: pd.DataFrame) -> bool:
        return self.db.save_dataframe(
            df=df,
            table_name=self.table_name,
            mode="update",
            index=False,
            pk_fields=["trade_date", "pool_code", "stock_code"],
            batch_size=10000,
            use_parallel=True,
        )

    def _run_range(
        self,
        start_date: str,
        end_date: str,
        pool_codes: List[str],
        pool_start_dates: Optional[Dict[str, str]] = None,
    ) -> bool:
        trading_dates = self._get_trading_dates(start_date, end_date)
        if not trading_dates:
            self.logger.warning(f"No trading dates between {start_date} and {end_date}")
            return True

        pool_codes_display = ", ".join(pool_codes)
        self.logger.info(
            f"Index pool run: {start_date} to {end_date}, dates={len(trading_dates)}, pools={pool_codes_display}"
        )

        buffer_frames = []
        buffer_rows = 0
        total_dates = len(trading_dates)
        start_ts = time.time()
        log_every = 50

        progress_bar = tqdm(
            trading_dates,
            desc="Index pool dates",
            unit="day",
            mininterval=1.0,
        )

        for idx, trade_date in enumerate(progress_bar, 1):
            iter_start = time.time()
            if pool_start_dates:
                active_codes = [code for code in pool_codes if trade_date >= pool_start_dates.get(code, start_date)]
            else:
                active_codes = pool_codes

            day_rows = 0
            day_df = pd.DataFrame()
            if active_codes:
                raw_df = self._fetch_index_members(trade_date, active_codes)
                if not raw_df.empty:
                    day_df = self._prepare_output(raw_df, trade_date)
                    day_rows = len(day_df)

            if day_rows > 0:
                buffer_frames.append(day_df)
                buffer_rows += day_rows

            if buffer_rows >= self.write_batch_rows:
                merged = pd.concat(buffer_frames, ignore_index=True)
                self.logger.info(
                    f"Flushing {len(merged)} rows up to {trade_date} (buffer_rows={buffer_rows})"
                )
                if not self._save_dataframe(merged):
                    return False
                buffer_frames = []
                buffer_rows = 0

            iter_elapsed = time.time() - iter_start
            progress_bar.set_postfix_str(
                f"date={trade_date} pools={len(active_codes)} rows={day_rows} t={iter_elapsed:.2f}s"
            )

            if idx % log_every == 0:
                elapsed = time.time() - start_ts
                avg_time = elapsed / idx
                self.logger.info(
                    f"Progress {idx}/{total_dates}, last_date={trade_date}, avg={avg_time:.2f}s/day"
                )

        if buffer_frames:
            merged = pd.concat(buffer_frames, ignore_index=True)
            self.logger.info(f"Final flush {len(merged)} rows up to {trading_dates[-1]}")
            if not self._save_dataframe(merged):
                return False

        total_elapsed = time.time() - start_ts
        if total_dates > 0:
            self.logger.info(f"Completed {total_dates} dates in {total_elapsed:.2f}s")

        return True

    def run(
        self,
        mode: str = "latest",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        pool_codes: Optional[List[str]] = None,
        overlap_days: Optional[int] = None,
    ) -> bool:
        pool_list = self._normalize_pool_codes(pool_codes) if pool_codes is not None else self.pool_codes
        if not pool_list:
            raise ValueError("pool_codes is required but empty.")
        for code in pool_list:
            if "." not in code:
                self.logger.warning(f"pool_code '{code}' has no suffix; ensure it matches Wind codes.")

        if mode == "date":
            target_date = self._normalize_date_str(start_date or end_date)
            if not target_date:
                raise ValueError("date mode requires a date.")
            return self._run_range(target_date, target_date, pool_list)

        if mode == "range":
            start_date_str = self._normalize_date_str(start_date)
            end_date_str = self._normalize_date_str(end_date)
            if not start_date_str or not end_date_str:
                raise ValueError("range mode requires both start_date and end_date.")
            return self._run_range(start_date_str, end_date_str, pool_list)

        if mode == "init":
            start_date_str = self._normalize_date_str(start_date) or self.init_start_date
            end_date_str = self._get_latest_trading_date(end_date)
            if not start_date_str or not end_date_str:
                raise ValueError("init mode requires valid start/end dates.")
            return self._run_range(start_date_str, end_date_str, pool_list)

        if mode != "latest":
            raise ValueError(f"Unsupported mode: {mode}")

        end_date_str = self._get_latest_trading_date(end_date)
        if not end_date_str:
            raise ValueError("Unable to resolve latest trading date.")

        effective_overlap = overlap_days if overlap_days is not None else self.overlap_days
        pool_start_dates: Dict[str, str] = {}
        for pool_code in pool_list:
            max_date = self._get_latest_trade_date_for_pool(pool_code)
            if max_date:
                start_date_str = self._get_overlap_start_date(max_date, effective_overlap)
            else:
                start_date_str = self.init_start_date
            pool_start_dates[pool_code] = start_date_str

        overall_start = min(pool_start_dates.values())
        return self._run_range(overall_start, end_date_str, pool_list, pool_start_dates=pool_start_dates)
