"""
DuckDB Vectorized OLAP Analytics Engine for P2P Order Book Lake.

Manages zero-copy analytical views over Hive-partitioned compacted Parquet files
and hot ingestion buffers, enabling sub-millisecond predicate pushdown and
direct Polars/Arrow integration for quantitative signal modeling.
"""

import glob
import logging
from pathlib import Path
from typing import Any, List, Optional, Union

import duckdb
import polars as pl

logger = logging.getLogger("duckdb-engine")


class DuckDBEngine:
    """
    High-performance analytical engine backed by DuckDB and Apache Arrow.
    """

    # Valid SELECT statement yielding typed NULL columns for empty fallback
    EMPTY_SELECT_COLS = """
        CAST(NULL AS VARCHAR) AS ad_id,
        CAST(NULL AS VARCHAR) AS venue,
        CAST(NULL AS VARCHAR) AS side,
        CAST(NULL AS VARCHAR) AS asset,
        CAST(NULL AS VARCHAR) AS fiat,
        CAST(NULL AS DOUBLE) AS price,
        CAST(NULL AS DOUBLE) AS available_crypto,
        CAST(NULL AS DOUBLE) AS min_fiat_limit,
        CAST(NULL AS DOUBLE) AS max_fiat_limit,
        CAST([] AS VARCHAR[]) AS payment_methods,
        CAST(NULL AS VARCHAR) AS merchant_id,
        CAST(NULL AS VARCHAR) AS merchant_name,
        CAST(NULL AS BIGINT) AS merchant_completed_orders,
        CAST(NULL AS DOUBLE) AS merchant_completion_rate,
        CAST(NULL AS BOOLEAN) AS is_pro_merchant,
        CAST(NULL AS TIMESTAMPTZ) AS timestamp_utc,
        CAST(NULL AS DOUBLE) AS latency_ms
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        processed_base_dir: Union[str, Path] = "data/processed",
        raw_base_dir: Union[str, Path] = "data/raw",
        threads: int = 4,
        memory_limit: str = "4GB",
    ):
        self.db_path = str(db_path)
        self.processed_dir = Path(processed_base_dir).resolve()
        self.raw_dir = Path(raw_base_dir).resolve()

        self.con = duckdb.connect(self.db_path)
        self.con.execute(f"SET threads = {threads};")
        self.con.execute(f"SET memory_limit = '{memory_limit}';")
        self.con.execute("SET preserve_insertion_order = false;")

        self.refresh_views()

    def _normalize_path(self, path: Path) -> str:
        """Converts Windows paths to forward slashes for DuckDB SQL compatibility."""
        return str(path).replace("\\", "/")

    def _has_parquet_files(self, pattern: str) -> bool:
        """Checks if any files match the glob pattern before attaching DuckDB views."""
        return len(glob.glob(pattern, recursive=True)) > 0

    def refresh_views(self) -> None:
        """
        Creates or updates zero-copy analytical views over lake partitions.
        Falls back to empty tables if directories are unpopulated.
        """
        # 1. Compacted Hive Lake View
        processed_glob = self._normalize_path(self.processed_dir / "**" / "*.parquet")
        if self._has_parquet_files(processed_glob):
            self.con.execute(f"""
                CREATE OR REPLACE VIEW v_order_book_processed AS
                SELECT * FROM read_parquet(
                    '{processed_glob}',
                    hive_partitioning = true,
                    union_by_name = true
                );
            """)
            has_processed = True
        else:
            self.con.execute(f"""
                CREATE OR REPLACE VIEW v_order_book_processed AS
                SELECT
                    CAST(NULL AS VARCHAR) AS date,
                    {self.EMPTY_SELECT_COLS}
                WHERE 1=0;
            """)
            has_processed = False

        # 2. Raw Ephemeral Ingestion Buffer View
        raw_glob = self._normalize_path(self.raw_dir / "**" / "*.parquet")
        if self._has_parquet_files(raw_glob):
            self.con.execute(f"""
                CREATE OR REPLACE VIEW v_order_book_raw AS
                SELECT * FROM read_parquet(
                    '{raw_glob}',
                    union_by_name = true
                );
            """)
            has_raw = True
        else:
            self.con.execute(f"""
                CREATE OR REPLACE VIEW v_order_book_raw AS
                SELECT {self.EMPTY_SELECT_COLS}
                WHERE 1=0;
            """)
            has_raw = False

        # 3. Unified View (Combines historical cold partitions and hot raw ticks)
        if has_processed and has_raw:
            self.con.execute("""
                CREATE OR REPLACE VIEW v_order_book_unified AS
                SELECT * EXCLUDE (date) FROM v_order_book_processed
                UNION ALL
                SELECT * FROM v_order_book_raw;
            """)
        elif has_processed:
            self.con.execute("""
                CREATE OR REPLACE VIEW v_order_book_unified AS
                SELECT * EXCLUDE (date) FROM v_order_book_processed;
            """)
        else:
            self.con.execute("""
                CREATE OR REPLACE VIEW v_order_book_unified AS
                SELECT * FROM v_order_book_raw;
            """)

    def query(self, sql: str, params: Optional[List[Any]] = None) -> duckdb.DuckDBPyRelation:
        """Executes SQL query returning a DuckDB relation."""
        if params is not None:
            return self.con.sql(sql, params=params)
        return self.con.sql(sql)

    def query_df(self, sql: str, params: Optional[List[Any]] = None) -> pl.DataFrame:
        """Executes SQL query returning a native Polars DataFrame via zero-copy Arrow."""
        rel = self.query(sql, params)
        reader = rel.arrow()
        table = reader.read_all()

        if table.num_rows == 0:
            if table.schema and len(table.schema) > 0:
                try:
                    return pl.from_arrow(table)
                except Exception:
                    pass
            return pl.DataFrame({col: [] for col in rel.columns})
        return pl.from_arrow(table)

    def get_best_bid_ask(
        self,
        asset: str = "USDT",
        fiat: str = "USD",
        venue: Optional[str] = None,
        date: Optional[str] = None,
    ) -> pl.DataFrame:
        """
        Computes historical top-of-book (Level-1: best bid, best ask, nominal spread)
        per timestamp snapshot.
        """
        where_clauses = ["asset = ?", "fiat = ?"]
        params: List[Any] = [asset.upper(), fiat.upper()]

        if venue:
            where_clauses.append("venue = ?")
            params.append(venue.lower())

        target_view = "v_order_book_processed" if date else "v_order_book_unified"
        if date:
            where_clauses.append("CAST(date AS VARCHAR) = ?")
            params.append(str(date))

        where_stmt = " AND ".join(where_clauses)

        sql = f"""
            WITH ranked_books AS (
                SELECT
                    timestamp_utc,
                    venue,
                    asset,
                    fiat,
                    MAX(CASE WHEN side = 'BUY' THEN price END) AS best_bid,
                    MIN(CASE WHEN side = 'SELL' THEN price END) AS best_ask,
                    AVG(latency_ms) AS avg_latency_ms
                FROM {target_view}
                WHERE {where_stmt}
                GROUP BY timestamp_utc, venue, asset, fiat
            )
            SELECT
                timestamp_utc,
                venue,
                asset,
                fiat,
                best_bid,
                best_ask,
                (best_ask - best_bid) AS nominal_spread,
                ((best_ask - best_bid) / best_bid) * 100.0 AS spread_pct,
                avg_latency_ms
            FROM ranked_books
            ORDER BY timestamp_utc ASC, venue ASC;
        """
        return self.query_df(sql, params)

    def get_order_book_snapshot(
        self,
        venue: str,
        asset: str,
        fiat: str,
        timestamp_utc: str,
    ) -> pl.DataFrame:
        """Reconstructs the full Level-2 order book at an exact timestamp."""
        sql = """
            SELECT
                side,
                price,
                available_crypto,
                min_fiat_limit,
                max_fiat_limit,
                merchant_name,
                merchant_completion_rate,
                is_pro_merchant,
                payment_methods
            FROM v_order_book_unified
            WHERE venue = ?
              AND asset = ?
              AND fiat = ?
              AND timestamp_utc = ?
            ORDER BY
                CASE WHEN side = 'BUY' THEN price END DESC,
                CASE WHEN side = 'SELL' THEN price END ASC;
        """
        return self.query_df(
            sql, [venue.lower(), asset.upper(), fiat.upper(), timestamp_utc]
        )

    def get_latency_metrics(
        self,
        venue: Optional[str] = None,
        date: Optional[str] = None,
    ) -> pl.DataFrame:
        """Computes scraping latency percentiles (p50, p95, p99)."""
        where_clauses = []
        params: List[Any] = []

        if venue:
            where_clauses.append("venue = ?")
            params.append(venue.lower())
        if date:
            where_clauses.append("CAST(date AS VARCHAR) = ?")
            params.append(str(date))

        where_stmt = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        target_view = "v_order_book_processed" if date else "v_order_book_unified"

        sql = f"""
            SELECT
                venue,
                COUNT(*) AS total_records,
                ROUND(AVG(latency_ms), 2) AS mean_latency,
                ROUND(QUANTILE_CONT(latency_ms, 0.50), 2) AS p50_latency,
                ROUND(QUANTILE_CONT(latency_ms, 0.95), 2) AS p95_latency,
                ROUND(QUANTILE_CONT(latency_ms, 0.99), 2) AS p99_latency
            FROM {target_view}
            {where_stmt}
            GROUP BY venue
            ORDER BY venue ASC;
        """
        return self.query_df(sql, params)

    def close(self) -> None:
        """Closes the underlying DuckDB connection."""
        self.con.close()
