from datetime import datetime, timezone

import polars as pl
import pytest

from src.scrapers.base_scraper import P2POrderBookSnapshot, P2POrderLevel
from src.scrapers.runner import save_snapshot_to_parquet, snapshot_to_records


@pytest.fixture
def sample_snapshot():
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    bid = P2POrderLevel(
        ad_id="ad_bid_1",
        venue="binance",
        side="BUY",
        asset="USDT",
        fiat="NGN",
        price=1480.0,
        available_crypto=100.0,
        min_fiat_limit=5000.0,
        max_fiat_limit=148000.0,
        payment_methods=["Bank Transfer"],
        merchant_id="m_bid",
        merchant_name="MerchantBid",
        merchant_completed_orders=50,
        merchant_completion_rate=0.98,
        is_pro_merchant=True,
    )
    ask = P2POrderLevel(
        ad_id="ad_ask_1",
        venue="binance",
        side="SELL",
        asset="USDT",
        fiat="NGN",
        price=1495.0,
        available_crypto=150.0,
        min_fiat_limit=5000.0,
        max_fiat_limit=200000.0,
        payment_methods=["Bank Transfer"],
        merchant_id="m_ask",
        merchant_name="MerchantAsk",
        merchant_completed_orders=120,
        merchant_completion_rate=0.99,
        is_pro_merchant=False,
    )
    return P2POrderBookSnapshot(
        venue="binance",
        asset="USDT",
        fiat="NGN",
        timestamp_utc=now,
        bids=[bid],
        asks=[ask],
        latency_ms=45.2,
    )


def test_snapshot_to_records(sample_snapshot):
    records = snapshot_to_records(sample_snapshot)
    assert len(records) == 2
    assert records[0]["ad_id"] == "ad_bid_1"
    assert records[0]["price"] == 1480.0
    assert records[0]["latency_ms"] == 45.2
    assert records[1]["ad_id"] == "ad_ask_1"
    assert records[1]["price"] == 1495.0


def test_save_snapshot_to_parquet(sample_snapshot, tmp_path):
    output_file = save_snapshot_to_parquet(
        snapshot=sample_snapshot, output_base_dir=str(tmp_path)
    )

    assert output_file.exists()
    assert "2026-11-01" in str(output_file.parent)

    # Read back using polars to verify schema
    df = pl.read_parquet(output_file)
    assert df.height == 2
    assert "ad_id" in df.columns
    assert "merchant_completion_rate" in df.columns
    assert df["price"].to_list() == [1480.0, 1495.0]
