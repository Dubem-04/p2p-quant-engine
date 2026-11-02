from datetime import date, datetime, timezone

import polars as pl
import pytest

from src.storage.db import DuckDBEngine


@pytest.fixture
def mock_lake_env(tmp_path):
    raw_dir = tmp_path / "data" / "raw"
    processed_dir = tmp_path / "data" / "processed"
    raw_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)

    # Seed 1 Hive-partitioned compacted file
    date_str = "2026-11-01"
    hive_partition = (
        processed_dir / "venue=binance" / "asset=USDT" / "fiat=USD" / f"date={date_str}"
    )
    hive_partition.mkdir(parents=True)

    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    records = [
        {
            "ad_id": "b_1",
            "venue": "binance",
            "side": "BUY",
            "asset": "USDT",
            "fiat": "USD",
            "price": 1.000,
            "available_crypto": 5000.0,
            "min_fiat_limit": 50.0,
            "max_fiat_limit": 5000.0,
            "payment_methods": ["Bank Transfer"],
            "merchant_id": "m_1",
            "merchant_name": "Maker1",
            "merchant_completed_orders": 300,
            "merchant_completion_rate": 0.99,
            "is_pro_merchant": True,
            "timestamp_utc": now,
            "latency_ms": 120.0,
        },
        {
            "ad_id": "b_2",
            "venue": "binance",
            "side": "SELL",
            "asset": "USDT",
            "fiat": "USD",
            "price": 1.050,
            "available_crypto": 2000.0,
            "min_fiat_limit": 20.0,
            "max_fiat_limit": 2100.0,
            "payment_methods": ["Bank Transfer"],
            "merchant_id": "m_2",
            "merchant_name": "Maker2",
            "merchant_completed_orders": 150,
            "merchant_completion_rate": 0.98,
            "is_pro_merchant": False,
            "timestamp_utc": now,
            "latency_ms": 120.0,
        },
    ]

    pl.DataFrame(records).write_parquet(hive_partition / "data.parquet")

    return {
        "raw": raw_dir,
        "processed": processed_dir,
    }


def test_duckdb_engine_empty_dirs(tmp_path):
    empty_raw = tmp_path / "raw"
    empty_processed = tmp_path / "processed"
    empty_raw.mkdir()
    empty_processed.mkdir()

    engine = DuckDBEngine(
        db_path=":memory:",
        processed_base_dir=empty_processed,
        raw_base_dir=empty_raw,
    )

    # Empty views should execute cleanly without failing
    df = engine.query_df("SELECT * FROM v_order_book_unified")
    assert isinstance(df, pl.DataFrame)
    assert len(df) == 0
    engine.close()


def test_duckdb_engine_hive_query(mock_lake_env):
    engine = DuckDBEngine(
        db_path=":memory:",
        processed_base_dir=mock_lake_env["processed"],
        raw_base_dir=mock_lake_env["raw"],
    )

    # Verify query and Hive-extracted columns
    df = engine.query_df(
        "SELECT venue, asset, fiat, date, price, side FROM v_order_book_processed"
    )
    assert len(df) == 2
    assert df["venue"][0] == "binance"
    assert df["asset"][0] == "USDT"
    assert df["fiat"][0] == "USD"
    assert df["date"][0] == date(2026, 11, 1) or str(df["date"][0]) == "2026-11-01"

    engine.close()


def test_get_best_bid_ask(mock_lake_env):
    engine = DuckDBEngine(
        db_path=":memory:",
        processed_base_dir=mock_lake_env["processed"],
        raw_base_dir=mock_lake_env["raw"],
    )

    df_bba = engine.get_best_bid_ask(asset="USDT", fiat="USD", venue="binance")
    assert len(df_bba) == 1
    row = df_bba.to_dicts()[0]

    assert row["best_bid"] == 1.000
    assert row["best_ask"] == 1.050
    assert row["nominal_spread"] == pytest.approx(0.050)
    assert row["spread_pct"] == pytest.approx(5.0)

    engine.close()


def test_get_latency_metrics(mock_lake_env):
    engine = DuckDBEngine(
        db_path=":memory:",
        processed_base_dir=mock_lake_env["processed"],
        raw_base_dir=mock_lake_env["raw"],
    )

    lat_df = engine.get_latency_metrics(venue="binance")
    assert len(lat_df) == 1
    metrics = lat_df.to_dicts()[0]

    assert metrics["venue"] == "binance"
    assert metrics["total_records"] == 2
    assert metrics["p50_latency"] == 120.0
    assert metrics["p99_latency"] == 120.0

    engine.close()
