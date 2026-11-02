from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from src.scrapers.base_scraper import P2POrderLevel
from src.storage.db import DuckDBEngine
from src.storage.orderbook_replay import (
    OrderBookReconstructor,
    ReconstructedBook,
)


@pytest.fixture
def sample_levels():
    bids = [
        P2POrderLevel(
            ad_id="b1",
            venue="binance",
            side="BUY",
            asset="USDT",
            fiat="USD",
            price=1.000,
            available_crypto=1000.0,
            min_fiat_limit=50.0,
            max_fiat_limit=1000.0,
            payment_methods=["Bank Transfer"],
            merchant_id="m1",
            merchant_name="TopMerchant",
            merchant_completed_orders=500,
            merchant_completion_rate=0.99,
            is_pro_merchant=True,
        ),
        P2POrderLevel(
            ad_id="b2",
            venue="binance",
            side="BUY",
            asset="USDT",
            fiat="USD",
            price=0.990,
            available_crypto=5000.0,
            min_fiat_limit=100.0,
            max_fiat_limit=5000.0,
            payment_methods=["Zelle"],
            merchant_id="m2",
            merchant_name="BasicMerchant",
            merchant_completed_orders=20,
            merchant_completion_rate=0.90,
            is_pro_merchant=False,
        ),
    ]

    asks = [
        P2POrderLevel(
            ad_id="a1",
            venue="binance",
            side="SELL",
            asset="USDT",
            fiat="USD",
            price=1.020,
            available_crypto=500.0,
            min_fiat_limit=20.0,
            max_fiat_limit=510.0,
            payment_methods=["Bank Transfer"],
            merchant_id="m3",
            merchant_name="AskMaker1",
            merchant_completed_orders=250,
            merchant_completion_rate=0.98,
            is_pro_merchant=True,
        ),
        P2POrderLevel(
            ad_id="a2",
            venue="binance",
            side="SELL",
            asset="USDT",
            fiat="USD",
            price=1.050,
            available_crypto=2000.0,
            min_fiat_limit=100.0,
            max_fiat_limit=2100.0,
            payment_methods=["Bank Transfer"],
            merchant_id="m4",
            merchant_name="AskMaker2",
            merchant_completed_orders=150,
            merchant_completion_rate=0.96,
            is_pro_merchant=False,
        ),
    ]
    return bids, asks


def test_reconstructed_book_properties(sample_levels):
    bids, asks = sample_levels
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    book = ReconstructedBook(
        venue="binance",
        asset="USDT",
        fiat="USD",
        timestamp_utc=now,
        bids=bids,
        asks=asks,
    )

    assert book.best_bid == 1.000
    assert book.best_ask == 1.020
    assert book.nominal_spread == pytest.approx(0.020)
    assert book.spread_pct == pytest.approx((0.020 / 1.000) * 100.0)


def test_reconstructed_book_filtering(sample_levels):
    bids, asks = sample_levels
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    book = ReconstructedBook(
        venue="binance",
        asset="USDT",
        fiat="USD",
        timestamp_utc=now,
        bids=bids,
        asks=asks,
    )

    # 1. Filter by ticket size = $60 (b2 has min 100, a2 has min 100)
    filtered_ticket = book.filter_by_ticket_size(60.0)
    assert len(filtered_ticket.bids) == 1
    assert filtered_ticket.bids[0].ad_id == "b1"
    assert len(filtered_ticket.asks) == 1
    assert filtered_ticket.asks[0].ad_id == "a1"

    # 2. Filter by merchant reputation (min 50 orders, min 95% rate)
    filtered_merchant = book.filter_by_merchant(
        min_completion_rate=0.95, min_completed_orders=50
    )
    assert len(filtered_merchant.bids) == 1
    assert filtered_merchant.bids[0].ad_id == "b1"
    assert len(filtered_merchant.asks) == 2


def test_sweep_vwap_calculation(sample_levels):
    bids, asks = sample_levels
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    book = ReconstructedBook(
        venue="binance",
        asset="USDT",
        fiat="USD",
        timestamp_utc=now,
        bids=bids,
        asks=asks,
    )

    # Sweep $400 USD against ASKS (can be filled completely by a1 at 1.02)
    sweep_1 = book.calculate_sweep_vwap(
        side="BUY", target_amount=400.0, quote_in_fiat=True
    )
    assert sweep_1 is not None
    assert sweep_1.is_fully_filled is True
    assert sweep_1.filled_fiat == 400.0
    assert sweep_1.vwap == pytest.approx(1.02)
    assert sweep_1.merchants_used == 1
    assert sweep_1.effective_slippage_pct == pytest.approx(0.0)

    # Sweep $1000 USD against ASKS (exceeds a1 capacity of $510, consumes part of a2 at 1.05)
    sweep_2 = book.calculate_sweep_vwap(
        side="BUY", target_amount=1000.0, quote_in_fiat=True
    )
    assert sweep_2 is not None
    assert sweep_2.is_fully_filled is True
    assert sweep_2.merchants_used == 2
    # 510 fiat @ 1.02 (500 crypto) + 490 fiat @ 1.05 (466.6667 crypto)
    expected_crypto = 500.0 + (490.0 / 1.05)
    expected_vwap = 1000.0 / expected_crypto
    assert sweep_2.vwap == pytest.approx(expected_vwap, rel=1e-3)
    assert sweep_2.effective_slippage_pct > 0.0


@pytest.fixture
def mock_replay_db(tmp_path):
    raw_dir = tmp_path / "data" / "raw"
    processed_dir = tmp_path / "data" / "processed"
    raw_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)

    t0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=15)

    binance_records = [
        # Binance at t0
        {
            "ad_id": "bin_b1",
            "venue": "binance",
            "side": "BUY",
            "asset": "USDT",
            "fiat": "USD",
            "price": 1.00,
            "available_crypto": 100.0,
            "min_fiat_limit": 10.0,
            "max_fiat_limit": 100.0,
            "payment_methods": ["Bank Transfer"],
            "merchant_id": "m1",
            "merchant_name": "M1",
            "merchant_completed_orders": 100,
            "merchant_completion_rate": 0.99,
            "is_pro_merchant": False,
            "timestamp_utc": t0,
            "latency_ms": 30.0,
        },
        # Binance at t1
        {
            "ad_id": "bin_b2",
            "venue": "binance",
            "side": "BUY",
            "asset": "USDT",
            "fiat": "USD",
            "price": 1.01,
            "available_crypto": 150.0,
            "min_fiat_limit": 10.0,
            "max_fiat_limit": 150.0,
            "payment_methods": ["Bank Transfer"],
            "merchant_id": "m1",
            "merchant_name": "M1",
            "merchant_completed_orders": 100,
            "merchant_completion_rate": 0.99,
            "is_pro_merchant": False,
            "timestamp_utc": t1,
            "latency_ms": 30.0,
        },
    ]

    bybit_records = [
        # Bybit at t0
        {
            "ad_id": "byb_b1",
            "venue": "bybit",
            "side": "BUY",
            "asset": "USDT",
            "fiat": "USD",
            "price": 0.98,
            "available_crypto": 200.0,
            "min_fiat_limit": 10.0,
            "max_fiat_limit": 200.0,
            "payment_methods": ["Bank Transfer"],
            "merchant_id": "m2",
            "merchant_name": "M2",
            "merchant_completed_orders": 80,
            "merchant_completion_rate": 0.97,
            "is_pro_merchant": True,
            "timestamp_utc": t0,
            "latency_ms": 35.0,
        },
    ]

    bin_dir = (
        processed_dir / "venue=binance" / "asset=USDT" / "fiat=USD" / "date=2026-11-01"
    )
    byb_dir = (
        processed_dir / "venue=bybit" / "asset=USDT" / "fiat=USD" / "date=2026-11-01"
    )
    bin_dir.mkdir(parents=True)
    byb_dir.mkdir(parents=True)

    pl.DataFrame(binance_records).write_parquet(bin_dir / "data.parquet")
    pl.DataFrame(bybit_records).write_parquet(byb_dir / "data.parquet")

    engine = DuckDBEngine(
        db_path=":memory:",
        processed_base_dir=processed_dir,
        raw_base_dir=raw_dir,
    )
    yield engine
    engine.close()


def test_order_book_reconstructor_get_snapshot_at(mock_replay_db):
    reconstructor = OrderBookReconstructor(mock_replay_db, max_staleness_seconds=30.0)

    t0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    target = t0 + timedelta(seconds=5)

    # Reconstruct Binance at t0 + 5s -> should fetch t0 book (price = 1.00)
    book = reconstructor.get_snapshot_at("binance", "USDT", "USD", point_in_time=target)
    assert book.is_stale is False
    assert book.best_bid == 1.00
    assert book.staleness_seconds == pytest.approx(5.0)

    # Reconstruct Bybit at t0 + 60s -> should be marked stale
    late_target = t0 + timedelta(seconds=60)
    stale_book = reconstructor.get_snapshot_at(
        "bybit", "USDT", "USD", point_in_time=late_target
    )
    assert stale_book.is_stale is True


def test_order_book_reconstructor_replay_stream(mock_replay_db):
    reconstructor = OrderBookReconstructor(mock_replay_db, max_staleness_seconds=20.0)

    t0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_end = t0 + timedelta(seconds=30)

    frames = list(
        reconstructor.replay_stream(
            asset="USDT",
            fiat="USD",
            start_time=t0,
            end_time=t_end,
            step_seconds=15.0,
        )
    )

    # Frames at t0, t0+15, t0+30
    assert len(frames) == 3

    # Frame 0 (t0): both binance and bybit have ticks at t0
    f0 = frames[0]
    assert f0.is_synchronized is True
    assert f0.binance.best_bid == 1.00
    assert f0.bybit.best_bid == 0.98

    # Frame 1 (t0 + 15): binance updated to 1.01, bybit book carried over from t0 (15s old <= 20s)
    f1 = frames[1]
    assert f1.binance.best_bid == 1.01
    assert f1.bybit.best_bid == 0.98

    # Frame 2 (t0 + 30): bybit tick is now 30s old > 20s staleness limit -> is_synchronized = False
    f2 = frames[2]
    assert f2.bybit.is_stale is True
    assert f2.is_synchronized is False
