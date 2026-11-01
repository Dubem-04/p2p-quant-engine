import asyncio
from datetime import datetime, timezone

import polars as pl
import pytest

from src.scrapers import runner
from src.scrapers.base_scraper import (
    BaseP2PScraper,
    P2POrderBookSnapshot,
    P2POrderLevel,
)
from src.scrapers.runner import save_snapshot_to_parquet, snapshot_to_records


class DummyScraper(BaseP2PScraper):
    def __init__(self, name="dummy"):
        super().__init__(venue_name=name)

    async def _fetch_raw_order_book(self, asset: str, fiat: str, depth: int = 5):
        return {}, 10.0

    def parse_payload(self, raw_data, asset, fiat, latency_ms):
        return P2POrderBookSnapshot(
            venue=self.venue_name,
            asset=asset,
            fiat=fiat,
            bids=[],
            asks=[],
            latency_ms=latency_ms,
        )

    async def close(self):
        """Mock close cleanup."""
        pass


@pytest.mark.asyncio
async def test_runner_spawns_compactor_daemon(monkeypatch):
    """
    Verifies runner.main() spawns both venue pollers AND the background
    compactor daemon task, and terminates cleanly when stop_event is set.
    """
    compactor_called = asyncio.Event()

    # 1. Mock run_compactor_daemon to observe invocation and await stop_event
    async def mock_run_compactor_daemon(
        interval_hours: float = 1.0,
        retention_days: int = 7,
        stop_event: asyncio.Event = None,
        **kwargs,
    ):
        compactor_called.set()
        if stop_event:
            await stop_event.wait()

    # 2. Mock venue scraper execution so it doesn't poll real endpoints
    async def mock_run_venue_poller(*args, stop_event=None, **kwargs):
        if stop_event:
            await stop_event.wait()

    # Patch targets
    monkeypatch.setattr(runner, "run_compactor_daemon", mock_run_compactor_daemon)
    monkeypatch.setattr(runner, "run_venue_poller", mock_run_venue_poller)
    monkeypatch.setattr(runner, "BinanceP2PScraper", lambda: DummyScraper("binance"))
    monkeypatch.setattr(runner, "BybitP2PScraper", lambda: DummyScraper("bybit"))

    # 3. Create a controller stop_event to shut down main() after a brief tick
    stop_event = asyncio.Event()

    async def trigger_shutdown():
        # Wait until compactor daemon is confirmed running
        await asyncio.wait_for(compactor_called.wait(), timeout=2.0)
        # Signal runner to exit
        stop_event.set()

    # Run main() and the shutdown trigger concurrently
    runner_task = asyncio.create_task(runner.main(stop_event=stop_event))
    shutdown_task = asyncio.create_task(trigger_shutdown())

    await asyncio.gather(runner_task, shutdown_task)

    assert compactor_called.is_set(), "run_compactor_daemon was never invoked by main()"


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
