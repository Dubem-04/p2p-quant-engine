import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.execution.alerts import AlertDispatcher
from src.execution.daemon import LiveExecutionDaemon
from src.scrapers.base_scraper import P2POrderBookSnapshot, P2POrderLevel
from src.signals.spread_models import DualSidedMakerSpreadModel, OpportunityScanner


@pytest.fixture
def sample_snapshot_factory():
    def create_snapshot(venue: str, bid_price: float, ask_price: float, ts: datetime):
        return P2POrderBookSnapshot(
            venue=venue,
            asset="USDT",
            fiat="USD",
            timestamp_utc=ts,
            bids=[
                P2POrderLevel(
                    ad_id=f"{venue}_b1",
                    venue=venue,
                    side="BUY",
                    asset="USDT",
                    fiat="USD",
                    price=bid_price,
                    available_crypto=5000.0,
                    min_fiat_limit=10.0,
                    max_fiat_limit=5000.0,
                    payment_methods=["Revolut"],
                    merchant_id="m1",
                    merchant_name="M1",
                    merchant_completed_orders=200,
                    merchant_completion_rate=0.99,
                    is_pro_merchant=True,
                )
            ],
            asks=[
                P2POrderLevel(
                    ad_id=f"{venue}_a1",
                    venue=venue,
                    side="SELL",
                    asset="USDT",
                    fiat="USD",
                    price=ask_price,
                    available_crypto=5000.0,
                    min_fiat_limit=10.0,
                    max_fiat_limit=5000.0,
                    payment_methods=["Revolut"],
                    merchant_id="m2",
                    merchant_name="M2",
                    merchant_completed_orders=200,
                    merchant_completion_rate=0.99,
                    is_pro_merchant=True,
                )
            ],
            latency_ms=25.0,
        )

    return create_snapshot


def test_synthesize_replay_frame(sample_snapshot_factory):
    t0 = datetime(2026, 11, 3, 16, 0, 0, tzinfo=timezone.utc)
    daemon = LiveExecutionDaemon(
        venues=("binance", "bybit"), max_feed_staleness_seconds=30.0
    )

    # 1. No snapshots populated -> books marked stale, not synchronized
    frame_empty = daemon.synthesize_replay_frame(current_time=t0)
    assert frame_empty.is_synchronized is False
    assert frame_empty.books["binance"].is_stale is True

    # 2. Both venues populated and fresh
    snap_bin = sample_snapshot_factory("binance", 0.99, 1.01, t0)
    snap_byb = sample_snapshot_factory("bybit", 1.03, 1.05, t0)
    daemon.latest_snapshots["binance"] = snap_bin
    daemon.latest_snapshots["bybit"] = snap_byb

    frame_fresh = daemon.synthesize_replay_frame(current_time=t0 + timedelta(seconds=5))
    assert frame_fresh.is_synchronized is True
    assert frame_fresh.books["binance"].best_ask == 1.01
    assert frame_fresh.books["bybit"].best_bid == 1.03

    # 3. One venue becomes stale (> 30s)
    frame_stale = daemon.synthesize_replay_frame(
        current_time=t0 + timedelta(seconds=40)
    )
    assert frame_stale.is_synchronized is False
    assert frame_stale.books["binance"].is_stale is True


@pytest.mark.asyncio
async def test_evaluate_once_triggers_signals_and_alerts(sample_snapshot_factory):
    t0 = datetime(2026, 11, 3, 16, 0, 0, tzinfo=timezone.utc)

    # Cross-venue dislocation: Binance ask 1.00, Bybit bid 1.03 -> 3% spread
    snap_bin = sample_snapshot_factory("binance", 0.98, 1.00, t0)
    snap_byb = sample_snapshot_factory("bybit", 1.03, 1.05, t0)

    custom_maker = DualSidedMakerSpreadModel(
        penny_jump_epsilon=0.002, min_net_margin_pct=0.5
    )
    scanner = OpportunityScanner(maker_model=custom_maker)

    mock_dispatcher = AsyncMock(spec=AlertDispatcher)
    mock_dispatcher.dispatch.return_value = {"TELEGRAM": True}

    daemon = LiveExecutionDaemon(
        venues=("binance", "bybit"),
        scanner=scanner,
        dispatcher=mock_dispatcher,
        target_fiat_amount=1000.0,
    )
    daemon.latest_snapshots["binance"] = snap_bin
    daemon.latest_snapshots["bybit"] = snap_byb

    actionable = await daemon.evaluate_once(current_time=t0)

    assert len(actionable) >= 1
    assert daemon.signals_detected >= 1
    assert daemon.alerts_dispatched >= 1
    assert mock_dispatcher.dispatch.called


@pytest.mark.asyncio
async def test_metrics_circuit_breaker(sample_snapshot_factory):
    t0 = datetime.now(timezone.utc)
    daemon = LiveExecutionDaemon(
        venues=("binance", "bybit"), max_feed_staleness_seconds=20.0
    )
    daemon.start_time = t0

    # Only Binance is updated
    daemon.latest_snapshots["binance"] = sample_snapshot_factory(
        "binance", 0.99, 1.01, t0
    )

    metrics = daemon.get_metrics()
    assert "bybit" in metrics.degraded_venues
    assert "binance" not in metrics.degraded_venues


@pytest.mark.asyncio
async def test_daemon_lifecycle(sample_snapshot_factory, tmp_path):
    t0 = datetime.now(timezone.utc)
    mock_bin_scraper = AsyncMock()
    mock_bin_scraper.poll_order_book.return_value = sample_snapshot_factory(
        "binance", 0.99, 1.01, t0
    )

    mock_byb_scraper = AsyncMock()
    mock_byb_scraper.poll_order_book.return_value = sample_snapshot_factory(
        "bybit", 1.02, 1.04, t0
    )

    daemon = LiveExecutionDaemon(
        venues=("binance", "bybit"),
        poll_interval_seconds=0.05,
        raw_storage_dir=tmp_path,
        binance_scraper=mock_bin_scraper,
        bybit_scraper=mock_byb_scraper,
    )

    await daemon.start()
    assert daemon.is_running is True

    # Allow polling loop to run for a brief moment
    await asyncio.sleep(0.15)

    assert daemon.snapshots_captured["binance"] >= 1
    assert daemon.snapshots_captured["bybit"] >= 1

    await daemon.stop()
    assert daemon.is_running is False
    assert len(daemon._polling_tasks) == 0
