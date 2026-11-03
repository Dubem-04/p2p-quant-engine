"""
Live Execution Daemon & Health Supervisor.

Coordinates concurrent multi-venue P2P scrapers, in-memory ReplayFrame synthesis,
background Parquet storage writes, quantitative signal evaluation, and alert dispatching
with automated circuit-breaker monitoring.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import polars as pl

from src.execution.alerts import AlertDispatcher
from src.scrapers.base_scraper import P2POrderBookSnapshot
from src.scrapers.binance_p2p import BinanceP2PScraper
from src.scrapers.bybit_p2p import BybitP2PScraper
from src.signals.friction_matrix import FrictionCostEngine
from src.signals.spread_models import OpportunityScanner
from src.storage.orderbook_replay import ReconstructedBook, ReplayFrame

logger = logging.getLogger(__name__)


def save_snapshot_to_parquet(snapshot: P2POrderBookSnapshot, base_dir: Path) -> Path:
    """Persists a P2POrderBookSnapshot to the micro-parquet buffer directory."""
    records = []
    for order in snapshot.bids + snapshot.asks:
        records.append(
            {
                "ad_id": order.ad_id,
                "venue": order.venue,
                "side": order.side,
                "asset": order.asset,
                "fiat": order.fiat,
                "price": float(order.price),
                "available_crypto": float(order.available_crypto),
                "min_fiat_limit": float(order.min_fiat_limit),
                "max_fiat_limit": float(order.max_fiat_limit),
                "payment_methods": order.payment_methods,
                "merchant_id": order.merchant_id,
                "merchant_name": order.merchant_name,
                "merchant_completed_orders": order.merchant_completed_orders,
                "merchant_completion_rate": float(order.merchant_completion_rate),
                "is_pro_merchant": order.is_pro_merchant,
                "timestamp_utc": snapshot.timestamp_utc,
                "latency_ms": float(snapshot.latency_ms),
            }
        )

    df = pl.DataFrame(records)
    target_dir = (
        base_dir
        / snapshot.venue.lower()
        / snapshot.asset.upper()
        / snapshot.fiat.upper()
        / snapshot.timestamp_utc.strftime("%Y-%m-%d")
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    file_path = (
        target_dir
        / f"snapshot_{int(snapshot.timestamp_utc.timestamp() * 1000)}.parquet"
    )
    df.write_parquet(file_path, compression="zstd")
    return file_path


@dataclass
class DaemonMetrics:
    """Operational telemetry and circuit breaker status."""

    start_time: datetime
    uptime_seconds: float
    total_cycles: int
    snapshots_captured: Dict[str, int]
    signals_detected: int
    alerts_dispatched: int
    degraded_venues: List[str] = field(default_factory=list)


class LiveExecutionDaemon:
    """
    Asynchronous daemon orchestrating multi-venue ingestion,
    signal evaluation, and real-time alert dispatching.
    """

    def __init__(
        self,
        asset: str = "USDT",
        fiat: str = "USD",
        target_fiat_amount: float = 1000.0,
        venues: Tuple[str, ...] = ("binance", "bybit"),
        poll_interval_seconds: float = 15.0,
        max_feed_staleness_seconds: float = 45.0,
        raw_storage_dir: Optional[Path] = None,
        scanner: Optional[OpportunityScanner] = None,
        friction_engine: Optional[FrictionCostEngine] = None,
        dispatcher: Optional[AlertDispatcher] = None,
        binance_scraper: Optional[BinanceP2PScraper] = None,
        bybit_scraper: Optional[BybitP2PScraper] = None,
    ):
        self.asset = asset.upper()
        self.fiat = fiat.upper()
        self.target_fiat_amount = target_fiat_amount
        self.venues = tuple(v.lower() for v in venues)
        self.poll_interval_seconds = poll_interval_seconds
        self.max_feed_staleness_seconds = max_feed_staleness_seconds
        self.raw_storage_dir = raw_storage_dir

        self.scanner = scanner or OpportunityScanner()
        self.friction_engine = friction_engine or FrictionCostEngine()
        self.dispatcher = dispatcher

        # Pluggable scraper clients
        self.scrapers: Dict[str, object] = {}
        if "binance" in self.venues:
            self.scrapers["binance"] = binance_scraper or BinanceP2PScraper()
        if "bybit" in self.venues:
            self.scrapers["bybit"] = bybit_scraper or BybitP2PScraper()

        # In-memory snapshot cache: venue -> P2POrderBookSnapshot
        self.latest_snapshots: Dict[str, P2POrderBookSnapshot] = {}

        # Telemetry
        self.start_time: Optional[datetime] = None
        self.total_cycles: int = 0
        self.snapshots_captured: Dict[str, int] = {v: 0 for v in self.venues}
        self.signals_detected: int = 0
        self.alerts_dispatched: int = 0

        # Control flags and background task references
        self.is_running: bool = False
        self._polling_tasks: List[asyncio.Task] = []
        self._evaluation_task: Optional[asyncio.Task] = None
        self._background_storage_tasks: List[asyncio.Task] = []

    def _snapshot_to_book(
        self, snap: P2POrderBookSnapshot, current_time: datetime
    ) -> ReconstructedBook:
        """Converts raw scraper snapshot into ReconstructedBook with staleness verification."""
        staleness = (current_time - snap.timestamp_utc).total_seconds()
        is_stale = staleness > self.max_feed_staleness_seconds

        bids = sorted(snap.bids, key=lambda x: x.price, reverse=True)
        asks = sorted(snap.asks, key=lambda x: x.price, reverse=False)

        return ReconstructedBook(
            venue=snap.venue,
            asset=snap.asset,
            fiat=snap.fiat,
            timestamp_utc=snap.timestamp_utc,
            bids=bids,
            asks=asks,
            is_stale=is_stale,
            staleness_seconds=staleness,
            latency_ms=snap.latency_ms,
        )

    def synthesize_replay_frame(
        self, current_time: Optional[datetime] = None
    ) -> ReplayFrame:
        """Synthesizes in-memory cross-venue ReplayFrame from latest completed polls."""
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        books: Dict[str, ReconstructedBook] = {}
        all_synchronized = True

        for v in self.venues:
            snap = self.latest_snapshots.get(v)
            if snap is None:
                books[v] = ReconstructedBook(
                    venue=v,
                    asset=self.asset,
                    fiat=self.fiat,
                    timestamp_utc=now,
                    is_stale=True,
                    staleness_seconds=float("inf"),
                )
                all_synchronized = False
            else:
                book = self._snapshot_to_book(snap, now)
                books[v] = book
                if book.is_stale:
                    all_synchronized = False

        return ReplayFrame(
            timestamp_utc=now,
            books=books,
            is_synchronized=all_synchronized,
        )

    async def _poll_venue_loop(self, venue: str) -> None:
        """Dedicated async polling loop for a specific venue."""
        scraper = self.scrapers.get(venue)
        if not scraper:
            return

        logger.info("Starting ingestion loop for venue: %s", venue)

        while self.is_running:
            try:
                # Poll order book
                snapshot = await scraper.poll_order_book(
                    asset=self.asset,
                    fiat=self.fiat,
                )

                if snapshot:
                    self.latest_snapshots[venue] = snapshot
                    self.snapshots_captured[venue] = (
                        self.snapshots_captured.get(venue, 0) + 1
                    )

                    # Non-blocking async write to Parquet lake buffer if configured
                    if self.raw_storage_dir:
                        bg_task = asyncio.create_task(
                            asyncio.to_thread(
                                save_snapshot_to_parquet, snapshot, self.raw_storage_dir
                            )
                        )
                        self._background_storage_tasks.append(bg_task)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error during polling for %s: %s", venue, exc)

            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                break

    async def _evaluation_loop(self) -> None:
        """Periodic quantitative evaluation and alert dispatch loop."""
        logger.info("Starting signal evaluation and dispatch loop")

        while self.is_running:
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in evaluation cycle: %s", exc)

            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                break

    async def evaluate_once(
        self, current_time: Optional[datetime] = None
    ) -> List[object]:
        """Runs a single evaluation step over the active memory state."""
        self.total_cycles += 1
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        frame = self.synthesize_replay_frame(now)

        # 1. Scan frame for signals
        raw_signals = self.scanner.scan_frame(
            frame=frame,
            target_fiat_amount=self.target_fiat_amount,
        )

        actionable_signals = []
        for sig in raw_signals:
            self.signals_detected += 1
            # 2. Adjust for banking friction and counterparty risks
            adj = self.friction_engine.evaluate_signal(
                signal=sig,
                source_rail_name=(
                    sig.common_payment_methods[0]
                    if sig.common_payment_methods
                    else None
                ),
            )

            if adj.is_actionable:
                actionable_signals.append(adj)
                # 3. Dispatch alert if dispatcher configured
                if self.dispatcher:
                    results = await self.dispatcher.dispatch(adj, current_time=now)
                    if any(results.values()):
                        self.alerts_dispatched += 1

        # Clean finished storage tasks from tracking list
        self._background_storage_tasks = [
            t for t in self._background_storage_tasks if not t.done()
        ]
        return actionable_signals

    async def start(self) -> None:
        """Starts background ingestion scrapers and evaluation daemon."""
        if self.is_running:
            return

        self.is_running = True
        self.start_time = datetime.now(timezone.utc)

        # Launch scraper tasks
        for v in self.venues:
            task = asyncio.create_task(self._poll_venue_loop(v))
            self._polling_tasks.append(task)

        # Launch evaluation loop
        self._evaluation_task = asyncio.create_task(self._evaluation_loop())

    async def stop(self) -> None:
        """Gracefully cancels active tasks and closes resources."""
        self.is_running = False

        for t in self._polling_tasks:
            t.cancel()

        if self._evaluation_task:
            self._evaluation_task.cancel()

        # Await cancelled tasks
        all_tasks = self._polling_tasks + (
            [self._evaluation_task] if self._evaluation_task else []
        )
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)

        self._polling_tasks.clear()
        self._evaluation_task = None

        # Drain pending storage writes
        if self._background_storage_tasks:
            await asyncio.gather(
                *self._background_storage_tasks, return_exceptions=True
            )
            self._background_storage_tasks.clear()

        # Close dispatcher sessions
        if self.dispatcher:
            await self.dispatcher.close()

    def get_metrics(self) -> DaemonMetrics:
        """Returns telemetry snapshot including feed degradation status."""
        now = datetime.now(timezone.utc)
        uptime = (now - self.start_time).total_seconds() if self.start_time else 0.0

        degraded = []
        for v in self.venues:
            snap = self.latest_snapshots.get(v)
            if (
                snap is None
                or (now - snap.timestamp_utc).total_seconds()
                > self.max_feed_staleness_seconds
            ):
                degraded.append(v)

        return DaemonMetrics(
            start_time=self.start_time or now,
            uptime_seconds=uptime,
            total_cycles=self.total_cycles,
            snapshots_captured=self.snapshots_captured.copy(),
            signals_detected=self.signals_detected,
            alerts_dispatched=self.alerts_dispatched,
            degraded_venues=degraded,
        )
