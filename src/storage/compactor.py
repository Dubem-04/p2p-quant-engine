"""
Parquet Lake Compactor & Storage Lifecycle Manager.

Consolidates ephemeral micro-parquet files from data/raw/ into Hive-partitioned,
zstd-compressed columnar batches under data/processed/, stages raw files into
data/archive/, and enforces a 7-day cold retention deletion policy.
"""

import argparse
import asyncio
import glob
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import polars as pl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("parquet-compactor")


class CompactorError(Exception):
    """Raised when compaction verification fails or data integrity is violated."""


def build_hive_partition_dir(
    base_dir: Path,
    venue: str,
    asset: str,
    fiat: str,
    date_str: str,
) -> Path:
    """Constructs Hive-style partition directory path."""
    return (
        base_dir
        / f"venue={venue.lower()}"
        / f"asset={asset.upper()}"
        / f"fiat={fiat.upper()}"
        / f"date={date_str}"
    )


def compact_date(
    target_date: str,
    raw_base_dir: Path = Path("data/raw"),
    processed_base_dir: Path = Path("data/processed"),
    archive_base_dir: Path = Path("data/archive"),
    compression_level: int = 7,
) -> Dict[str, int]:
    """
    Compacts all raw micro-files for target_date (YYYY-MM-DD) into Hive partitions.

    Returns:
        Dict mapping partition keys to written row counts.
    """
    raw_date_dir = raw_base_dir / target_date
    if not raw_date_dir.exists():
        logger.warning(
            f"No raw directory found for date: {target_date} ({raw_date_dir})"
        )
        return {}

    raw_files = sorted(glob.glob(str(raw_date_dir / "*.parquet")))
    if not raw_files:
        logger.warning(f"No .parquet files found in {raw_date_dir}")
        return {}

    logger.info(f"Scanning {len(raw_files)} raw micro-files for {target_date}...")

    # 1. Scan and consolidate via Polars LazyFrame
    try:
        lazy_df = pl.scan_parquet(raw_files)
        df = lazy_df.collect()
    except Exception as e:
        raise CompactorError(f"Failed to scan raw files in {raw_date_dir}: {e}") from e

    total_raw_rows = len(df)
    if total_raw_rows == 0:
        logger.warning(f"Raw files contained 0 total rows for {target_date}.")
        return {}

    # 2. Partition by (venue, asset, fiat)
    results = {}
    partition_groups = df.partition_by(["venue", "asset", "fiat"], as_dict=True)

    for (venue, asset, fiat), partition_df in partition_groups.items():
        # Ensure standard sorting for query efficiency and compression
        sorted_partition = partition_df.sort(
            by=["timestamp_utc", "side", "price"],
            descending=[False, False, False],
        )

        target_dir = build_hive_partition_dir(
            processed_base_dir,
            venue=venue,
            asset=asset,
            fiat=fiat,
            date_str=target_date,
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        final_file = target_dir / "data.parquet"
        temp_file = target_dir / "data.parquet.tmp"

        # 3. Atomic Write: write to .tmp first with zstd compression
        sorted_partition.write_parquet(
            temp_file,
            compression="zstd",
            compression_level=compression_level,
        )

        # 4. Integrity Audit: verify written row count
        written_count = pl.read_parquet(temp_file).height
        if written_count != len(sorted_partition):
            temp_file.unlink(missing_ok=True)
            raise CompactorError(
                f"Row count mismatch for {venue} {asset}/{fiat}: "
                f"expected {len(sorted_partition)}, got {written_count}"
            )

        # Atomic replace
        if final_file.exists():
            final_file.unlink()
        temp_file.rename(final_file)

        partition_key = f"venue={venue}/asset={asset}/fiat={fiat}/date={target_date}"
        results[partition_key] = written_count
        logger.info(
            f"Compacted {written_count} rows -> {final_file} "
            f"({final_file.stat().st_size / 1024:.1f} KB)"
        )

    # 5. Archive Staging: Move micro-files from raw/ to archive/
    archive_date_dir = archive_base_dir / target_date
    archive_date_dir.mkdir(parents=True, exist_ok=True)

    for src_file in raw_files:
        dest_file = archive_date_dir / Path(src_file).name
        shutil.move(src_file, dest_file)

    # Remove empty raw folder if clean
    try:
        raw_date_dir.rmdir()
    except OSError:
        pass

    logger.info(
        f"Compaction complete for {target_date}: {total_raw_rows} total rows "
        f"archived to {archive_date_dir}"
    )
    return results


def purge_expired_archives(
    archive_base_dir: Path = Path("data/archive"),
    retention_days: int = 7,
    reference_date: Optional[datetime] = None,
) -> List[Path]:
    """
    Deletes archive date folders older than retention_days (default 7 days).
    """
    if not archive_base_dir.exists():
        return []

    ref_date = reference_date or datetime.now(timezone.utc)
    cutoff_date = (ref_date - timedelta(days=retention_days)).date()
    purged: List[Path] = []

    for item in archive_base_dir.iterdir():
        if item.is_dir():
            try:
                folder_date = datetime.strptime(item.name, "%Y-%m-%d").date()
                if folder_date < cutoff_date:
                    logger.info(
                        f"Purging expired archive: {item} (older than {cutoff_date})"
                    )
                    shutil.rmtree(item)
                    purged.append(item)
            except ValueError:
                # Skip non-date folders
                continue

    return purged


async def run_compactor_daemon(
    interval_hours: float = 1.0,
    retention_days: int = 7,
    raw_dir: str = "data/raw",
    processed_dir: str = "data/processed",
    archive_dir: str = "data/archive",
    stop_event: Optional[asyncio.Event] = None,
):
    """
    Background worker that checks for completed past dates,
    compacts them, and purges expired archives.
    """
    raw_path = Path(raw_dir)
    processed_path = Path(processed_dir)
    archive_path = Path(archive_dir)

    logger.info(
        f"Compactor background worker started (Check interval: {interval_hours}h)."
    )

    while not (stop_event and stop_event.is_set()):
        try:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Scan for completed date folders (any date != today)
            if raw_path.exists():
                for date_folder in sorted(raw_path.iterdir()):
                    if date_folder.is_dir() and date_folder.name != today_str:
                        logger.info(
                            f"Triggering compaction for completed day: {date_folder.name}"
                        )
                        compact_date(
                            target_date=date_folder.name,
                            raw_base_dir=raw_path,
                            processed_base_dir=processed_path,
                            archive_base_dir=archive_path,
                        )

            # Purge archives older than retention window
            purge_expired_archives(archive_path, retention_days=retention_days)

        except Exception as exc:
            logger.error(
                f"Error during scheduled compaction pass: {exc}", exc_info=True
            )

        sleep_seconds = interval_hours * 3600
        if stop_event:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_seconds)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(sleep_seconds)


def main():
    """CLI interface for on-demand compaction and archive retention enforcement."""
    parser = argparse.ArgumentParser(
        description="Parquet Lake Compaction and Storage Lifecycle Manager."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Specific date to compact (format: YYYY-MM-DD). Defaults to yesterday UTC.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=7,
        help="Number of days to keep files in data/archive/ before purging (default: 7).",
    )
    parser.add_argument(
        "--raw-dir",
        type=str,
        default="data/raw",
        help="Base path to raw micro-parquet files (default: data/raw).",
    )
    parser.add_argument(
        "--processed-dir",
        type=str,
        default="data/processed",
        help="Base path for Hive-partitioned compacted lake (default: data/processed).",
    )
    parser.add_argument(
        "--archive-dir",
        type=str,
        default="data/archive",
        help="Base path for staged archives (default: data/archive).",
    )
    parser.add_argument(
        "--run-retention-purge",
        action="store_true",
        help="Execute archive retention sweep to delete expired date folders.",
    )

    args = parser.parse_args()

    # Determine target date
    target_date = args.date
    if not target_date:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    raw_path = Path(args.raw_dir)
    processed_path = Path(args.processed_dir)
    archive_path = Path(args.archive_dir)

    print(f"[*] Starting compaction for {target_date}...")
    compact_results = compact_date(
        target_date=target_date,
        raw_base_dir=raw_path,
        processed_base_dir=processed_path,
        archive_base_dir=archive_path,
    )
    for partition, count in compact_results.items():
        print(f"  + {partition}: {count} rows")

    if args.run_retention_purge:
        print(f"[*] Purging archives older than {args.retention_days} days...")
        purged = purge_expired_archives(
            archive_path, retention_days=args.retention_days
        )
        print(f"  - Purged {len(purged)} expired directories.")


if __name__ == "__main__":
    main()
