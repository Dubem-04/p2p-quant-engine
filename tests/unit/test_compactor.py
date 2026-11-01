from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from src.storage.compactor import (
    build_hive_partition_dir,
    compact_date,
    purge_expired_archives,
)


@pytest.fixture
def mock_storage_env(tmp_path):
    raw_dir = tmp_path / "data" / "raw"
    processed_dir = tmp_path / "data" / "processed"
    archive_dir = tmp_path / "data" / "archive"

    raw_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)

    return {
        "raw": raw_dir,
        "processed": processed_dir,
        "archive": archive_dir,
    }


def create_mock_parquet_file(
    file_path: Path, venue: str, asset: str, fiat: str, prices: list[float]
):
    now = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    records = []
    for i, p in enumerate(prices):
        records.append(
            {
                "ad_id": f"{venue}_{i}",
                "venue": venue,
                "side": "BUY" if i % 2 == 0 else "SELL",
                "asset": asset,
                "fiat": fiat,
                "price": p,
                "available_crypto": 100.0,
                "min_fiat_limit": 10.0,
                "max_fiat_limit": 1000.0,
                "payment_methods": ["Bank Transfer"],
                "merchant_id": f"m_{i}",
                "merchant_name": f"Merchant_{i}",
                "merchant_completed_orders": 100,
                "merchant_completion_rate": 0.99,
                "is_pro_merchant": False,
                "timestamp_utc": now + timedelta(seconds=i),
                "latency_ms": 50.0,
            }
        )
    df = pl.DataFrame(records)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(file_path)


def test_build_hive_partition_dir(tmp_path):
    partition = build_hive_partition_dir(
        base_dir=tmp_path,
        venue="binance",
        asset="usdt",
        fiat="usd",
        date_str="2026-11-01",
    )
    expected = (
        tmp_path / "venue=binance" / "asset=USDT" / "fiat=USD" / "date=2026-11-01"
    )
    assert partition == expected


def test_compact_date_success(mock_storage_env):
    date_str = "2026-11-01"
    raw_date_dir = mock_storage_env["raw"] / date_str

    # Create 2 micro-files for Binance and 1 for Bybit
    create_mock_parquet_file(
        raw_date_dir / "binance_1.parquet", "binance", "USDT", "USD", [1.0, 1.05]
    )
    create_mock_parquet_file(
        raw_date_dir / "binance_2.parquet", "binance", "USDT", "USD", [0.99, 1.06]
    )
    create_mock_parquet_file(
        raw_date_dir / "bybit_1.parquet", "bybit", "USDT", "USD", [0.98, 1.10]
    )

    results = compact_date(
        target_date=date_str,
        raw_base_dir=mock_storage_env["raw"],
        processed_base_dir=mock_storage_env["processed"],
        archive_base_dir=mock_storage_env["archive"],
    )

    # 4 rows Binance, 2 rows Bybit
    assert len(results) == 2
    assert any("venue=binance" in k and v == 4 for k, v in results.items())
    assert any("venue=bybit" in k and v == 2 for k, v in results.items())

    # Verify Hive directories and data.parquet contents
    binance_file = (
        mock_storage_env["processed"]
        / "venue=binance"
        / "asset=USDT"
        / "fiat=USD"
        / f"date={date_str}"
        / "data.parquet"
    )
    assert binance_file.exists()
    binance_df = pl.read_parquet(binance_file)
    assert binance_df.height == 4

    # Verify files moved to archive and raw is purged
    archive_date_dir = mock_storage_env["archive"] / date_str
    assert archive_date_dir.exists()
    assert len(list(archive_date_dir.glob("*.parquet"))) == 3
    assert not raw_date_dir.exists()


def test_purge_expired_archives(mock_storage_env):
    archive_dir = mock_storage_env["archive"]
    ref_date = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # 10 days ago (expired)
    old_folder = archive_dir / "2026-10-31"
    old_folder.mkdir()

    # 3 days ago (active)
    recent_folder = archive_dir / "2026-11-07"
    recent_folder.mkdir()

    purged = purge_expired_archives(
        archive_base_dir=archive_dir,
        retention_days=7,
        reference_date=ref_date,
    )

    assert len(purged) == 1
    assert purged[0] == old_folder
    assert not old_folder.exists()
    assert recent_folder.exists()
