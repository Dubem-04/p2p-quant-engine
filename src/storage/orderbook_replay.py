"""
Point-in-Time Level-2 Order Book Reconstructor and Historical Replay Engine.

Reconstructs cross-venue order books from Hive/raw lake storage, providing
as-of temporal alignment, merchant/ticket limit filtering, and volume-weighted
average price (VWAP) execution sweep curves for backtesting and signal modeling.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

import polars as pl

from src.scrapers.base_scraper import P2POrderLevel
from src.storage.db import DuckDBEngine


@dataclass
class SweepResult:
    """Result of an execution sweep against an order book side."""

    target_amount: float
    quote_in_fiat: bool
    filled_crypto: float
    filled_fiat: float
    vwap: float
    merchants_used: int
    is_fully_filled: bool
    effective_slippage_pct: float


@dataclass
class ReconstructedBook:
    """Point-in-time reconstructed Level-2 order book for a single venue."""

    venue: str
    asset: str
    fiat: str
    timestamp_utc: datetime
    bids: List[P2POrderLevel] = field(default_factory=list)
    asks: List[P2POrderLevel] = field(default_factory=list)
    is_stale: bool = False
    staleness_seconds: float = 0.0
    latency_ms: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def nominal_spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return ((self.best_ask - self.best_bid) / self.best_bid) * 100.0
        return None

    def filter_by_ticket_size(self, fiat_amount: float) -> "ReconstructedBook":
        """Returns a new book keeping only ads where min_limit <= fiat_amount <= max_limit."""
        filtered_bids = [
            b for b in self.bids if b.min_fiat_limit <= fiat_amount <= b.max_fiat_limit
        ]
        filtered_asks = [
            a for a in self.asks if a.min_fiat_limit <= fiat_amount <= a.max_fiat_limit
        ]
        return ReconstructedBook(
            venue=self.venue,
            asset=self.asset,
            fiat=self.fiat,
            timestamp_utc=self.timestamp_utc,
            bids=filtered_bids,
            asks=filtered_asks,
            is_stale=self.is_stale,
            staleness_seconds=self.staleness_seconds,
            latency_ms=self.latency_ms,
        )

    def filter_by_merchant(
        self,
        min_completion_rate: float = 0.95,
        min_completed_orders: int = 50,
        require_pro: bool = False,
    ) -> "ReconstructedBook":
        """Filters order levels by merchant track record and verification status."""

        def is_valid(level: P2POrderLevel) -> bool:
            if level.merchant_completion_rate < min_completion_rate:
                return False
            if level.merchant_completed_orders < min_completed_orders:
                return False
            if require_pro and not level.is_pro_merchant:
                return False
            return True

        return ReconstructedBook(
            venue=self.venue,
            asset=self.asset,
            fiat=self.fiat,
            timestamp_utc=self.timestamp_utc,
            bids=[b for b in self.bids if is_valid(b)],
            asks=[a for a in self.asks if is_valid(a)],
            is_stale=self.is_stale,
            staleness_seconds=self.staleness_seconds,
            latency_ms=self.latency_ms,
        )

    def calculate_sweep_vwap(
        self,
        side: str,
        target_amount: float,
        quote_in_fiat: bool = True,
    ) -> Optional[SweepResult]:
        """
        Calculates execution VWAP by sweeping through sorted order levels,
        taking merchant ticket limits and available quantity into account.

        Args:
            side: "BUY" (sweeping asks to buy crypto) or "SELL" (sweeping bids to sell crypto).
            target_amount: Target volume to execute (in fiat or crypto).
            quote_in_fiat: True if target_amount is denominated in fiat, False if crypto.
        """
        levels = self.asks if side.upper() == "BUY" else self.bids
        if not levels or target_amount <= 0:
            return None

        remaining = target_amount
        total_crypto = 0.0
        total_fiat = 0.0
        merchants_used = 0

        best_price = levels[0].price

        for level in levels:
            if remaining <= 0:
                break

            # Check if order can meet minimum ticket threshold
            avail_crypto = level.available_crypto
            price = level.price
            max_fiat_capacity = min(level.max_fiat_limit, avail_crypto * price)

            if quote_in_fiat:
                # Remaining fiat to fill
                fiat_to_take = min(remaining, max_fiat_capacity)
                if (
                    fiat_to_take < level.min_fiat_limit
                    and remaining >= level.min_fiat_limit
                ):
                    continue  # Can't meet min ticket size on this ad
                crypto_taken = fiat_to_take / price
                total_fiat += fiat_to_take
                total_crypto += crypto_taken
                remaining -= fiat_to_take
            else:
                # Remaining crypto to fill
                crypto_capacity = max_fiat_capacity / price
                crypto_to_take = min(remaining, crypto_capacity)
                fiat_taken = crypto_to_take * price
                if (
                    fiat_taken < level.min_fiat_limit
                    and (remaining * price) >= level.min_fiat_limit
                ):
                    continue
                total_crypto += crypto_to_take
                total_fiat += fiat_taken
                remaining -= crypto_to_take

            merchants_used += 1

        if total_crypto <= 0:
            return None

        vwap = total_fiat / total_crypto
        is_fully_filled = remaining <= 1e-6
        slippage_pct = abs((vwap - best_price) / best_price) * 100.0

        return SweepResult(
            target_amount=target_amount,
            quote_in_fiat=quote_in_fiat,
            filled_crypto=total_crypto,
            filled_fiat=total_fiat,
            vwap=vwap,
            merchants_used=merchants_used,
            is_fully_filled=is_fully_filled,
            effective_slippage_pct=slippage_pct,
        )


@dataclass
class ReplayFrame:
    """Time-synchronized snapshot across multiple venues on a uniform grid."""

    timestamp_utc: datetime
    books: Dict[str, ReconstructedBook]
    is_synchronized: bool

    @property
    def binance(self) -> Optional[ReconstructedBook]:
        return self.books.get("binance")

    @property
    def bybit(self) -> Optional[ReconstructedBook]:
        return self.books.get("bybit")


class OrderBookReconstructor:
    """
    Historical point-in-time order book replayer with bulk vectorized scanning.
    """

    def __init__(
        self,
        db_engine: DuckDBEngine,
        max_staleness_seconds: float = 45.0,
    ):
        self.db = db_engine
        self.max_staleness_seconds = max_staleness_seconds

    def _rows_to_levels(self, df_side: pl.DataFrame, side: str) -> List[P2POrderLevel]:
        """Converts Polars DataFrame slice to validated P2POrderLevel objects."""
        if df_side.is_empty():
            return []

        levels = []
        for row in df_side.iter_rows(named=True):
            try:
                payments = row.get("payment_methods") or []
                if isinstance(payments, str):
                    payments = [p.strip() for p in payments.split(",") if p.strip()]

                levels.append(
                    P2POrderLevel(
                        ad_id=str(row["ad_id"]),
                        venue=str(row["venue"]),
                        side=side,
                        asset=str(row["asset"]),
                        fiat=str(row["fiat"]),
                        price=float(row["price"]),
                        available_crypto=float(row["available_crypto"]),
                        min_fiat_limit=float(row["min_fiat_limit"]),
                        max_fiat_limit=float(row["max_fiat_limit"]),
                        payment_methods=payments,
                        merchant_id=str(row["merchant_id"]),
                        merchant_name=str(row["merchant_name"]),
                        merchant_completed_orders=int(
                            row["merchant_completed_orders"] or 0
                        ),
                        merchant_completion_rate=float(
                            row["merchant_completion_rate"] or 0.0
                        ),
                        is_pro_merchant=bool(row["is_pro_merchant"]),
                    )
                )
            except (ValueError, TypeError, KeyError):
                continue
        return levels

    def get_snapshot_at(
        self,
        venue: str,
        asset: str,
        fiat: str,
        point_in_time: datetime,
    ) -> ReconstructedBook:
        """
        Reconstructs the point-in-time order book for a single venue at an exact instant.
        Finds the closest snapshot where timestamp_utc <= point_in_time.
        """
        if point_in_time.tzinfo is None:
            point_in_time = point_in_time.replace(tzinfo=timezone.utc)
        else:
            point_in_time = point_in_time.astimezone(timezone.utc)

        lookback_start = point_in_time - timedelta(
            seconds=self.max_staleness_seconds * 2
        )

        sql = """
                SELECT *
                FROM v_order_book_unified
                WHERE venue = ?
                AND asset = ?
                AND fiat = ?
                AND timestamp_utc <= ?
                AND timestamp_utc >= ?
                ORDER BY timestamp_utc DESC;
            """
        df = self.db.query_df(
            sql,
            [
                venue.lower(),
                asset.upper(),
                fiat.upper(),
                point_in_time,
                lookback_start,
            ],
        )

        if df.is_empty():
            return ReconstructedBook(
                venue=venue,
                asset=asset,
                fiat=fiat,
                timestamp_utc=point_in_time,
                is_stale=True,
                staleness_seconds=float("inf"),
            )

        # Standardize timestamp timezone to UTC
        if df["timestamp_utc"].dtype.time_zone != "UTC":
            df = df.with_columns(pl.col("timestamp_utc").dt.convert_time_zone("UTC"))

        latest_ts = df["timestamp_utc"][0]
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)

        staleness = (point_in_time - latest_ts).total_seconds()
        is_stale = staleness > self.max_staleness_seconds

        snap_df = df.filter(pl.col("timestamp_utc") == latest_ts)

        bids_df = snap_df.filter(pl.col("side") == "BUY").sort(
            by="price", descending=True
        )
        asks_df = snap_df.filter(pl.col("side") == "SELL").sort(
            by="price", descending=False
        )

        latency_ms = float(snap_df["latency_ms"].mean() or 0.0)

        return ReconstructedBook(
            venue=venue,
            asset=asset,
            fiat=fiat,
            timestamp_utc=latest_ts,
            bids=self._rows_to_levels(bids_df, "BUY"),
            asks=self._rows_to_levels(asks_df, "SELL"),
            is_stale=is_stale,
            staleness_seconds=staleness,
            latency_ms=latency_ms,
        )

    def replay_stream(
        self,
        asset: str,
        fiat: str,
        start_time: datetime,
        end_time: datetime,
        venues: Tuple[str, ...] = ("binance", "bybit"),
        step_seconds: float = 15.0,
    ) -> Iterator[ReplayFrame]:
        """
        Executes a bulk scan query across [start_time - max_staleness, end_time],
        and yields synchronized ReplayFrames at uniform time steps.
        """
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        else:
            start_time = start_time.astimezone(timezone.utc)

        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        else:
            end_time = end_time.astimezone(timezone.utc)

        fetch_start = start_time - timedelta(seconds=self.max_staleness_seconds)

        venues_placeholder = ", ".join(["?" for _ in venues])
        sql = f"""
            SELECT *
            FROM v_order_book_unified
            WHERE asset = ?
              AND fiat = ?
              AND venue IN ({venues_placeholder})
              AND timestamp_utc >= ?
              AND timestamp_utc <= ?
            ORDER BY timestamp_utc ASC;
        """
        params = (
            [asset.upper(), fiat.upper()]
            + [v.lower() for v in venues]
            + [fetch_start, end_time]
        )
        bulk_df = self.db.query_df(sql, params)

        # Standardize timestamp timezone to UTC
        if not bulk_df.is_empty() and bulk_df["timestamp_utc"].dtype.time_zone != "UTC":
            bulk_df = bulk_df.with_columns(
                pl.col("timestamp_utc").dt.convert_time_zone("UTC")
            )

        # Pre-group data by venue in memory
        venue_dfs = {}
        for v in venues:
            v_lower = v.lower()
            venue_dfs[v_lower] = bulk_df.filter(pl.col("venue") == v_lower)

        current_step = start_time
        while current_step <= end_time:
            frame_books: Dict[str, ReconstructedBook] = {}
            all_venues_fresh = True

            for v in venues:
                v_lower = v.lower()
                v_df = venue_dfs.get(v_lower, pl.DataFrame())

                asof_df = v_df.filter(pl.col("timestamp_utc") <= current_step)

                if asof_df.is_empty():
                    book = ReconstructedBook(
                        venue=v_lower,
                        asset=asset,
                        fiat=fiat,
                        timestamp_utc=current_step,
                        is_stale=True,
                        staleness_seconds=float("inf"),
                    )
                    all_venues_fresh = False
                else:
                    latest_ts = asof_df["timestamp_utc"][-1]
                    if latest_ts.tzinfo is None:
                        latest_ts = latest_ts.replace(tzinfo=timezone.utc)

                    staleness = (current_step - latest_ts).total_seconds()
                    is_stale = staleness > self.max_staleness_seconds

                    if is_stale:
                        all_venues_fresh = False

                    snap_df = asof_df.filter(pl.col("timestamp_utc") == latest_ts)
                    bids_df = snap_df.filter(pl.col("side") == "BUY").sort(
                        by="price", descending=True
                    )
                    asks_df = snap_df.filter(pl.col("side") == "SELL").sort(
                        by="price", descending=False
                    )
                    lat = float(snap_df["latency_ms"].mean() or 0.0)

                    book = ReconstructedBook(
                        venue=v_lower,
                        asset=asset,
                        fiat=fiat,
                        timestamp_utc=latest_ts,
                        bids=self._rows_to_levels(bids_df, "BUY"),
                        asks=self._rows_to_levels(asks_df, "SELL"),
                        is_stale=is_stale,
                        staleness_seconds=staleness,
                        latency_ms=lat,
                    )

                frame_books[v_lower] = book

            yield ReplayFrame(
                timestamp_utc=current_step,
                books=frame_books,
                is_synchronized=all_venues_fresh,
            )

            current_step += timedelta(seconds=step_seconds)
