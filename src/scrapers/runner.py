"""
Multi-venue ingestion daemon and raw data persistence runner.

Continuously polls configured P2P venue scrapers at fixed intervals,
structures Level-2 order book snapshots into tabular records, and
persists them into date-partitioned Parquet files under data/raw/.
"""

import asyncio
import logging
import signal
from pathlib import Path
from typing import List

import polars as pl

from src.scrapers.base_scraper import BaseP2PScraper, P2POrderBookSnapshot
from src.scrapers.binance_p2p import BinanceP2PScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("p2p-scraper-runner")


def snapshot_to_records(snapshot: P2POrderBookSnapshot) -> List[dict]:
    """Flattens bids and asks into a list of normalized tabular dictionaries."""
    records = []
    for level in snapshot.bids + snapshot.asks:
        row = level.model_dump()
        row["timestamp_utc"] = snapshot.timestamp_utc
        row["latency_ms"] = snapshot.latency_ms
        records.append(row)
    return records


def save_snapshot_to_parquet(
    snapshot: P2POrderBookSnapshot,
    output_base_dir: str = "data/raw",
) -> Path:
    """
    Serializes a snapshot into a date-partitioned Parquet file.
    Output: data/raw/YYYY-MM-DD/<venue>_<asset>_<fiat>_<timestamp_ms>.parquet
    """
    now = snapshot.timestamp_utc
    date_str = now.strftime("%Y-%m-%d")
    partition_dir = Path(output_base_dir) / date_str
    partition_dir.mkdir(parents=True, exist_ok=True)

    timestamp_ms = int(now.timestamp() * 1000)
    filename = (
        f"{snapshot.venue}_{snapshot.asset}_{snapshot.fiat}_{timestamp_ms}.parquet"
    )
    file_path = partition_dir / filename

    records = snapshot_to_records(snapshot)
    if not records:
        logger.warning(
            f"No bid/ask levels parsed for {snapshot.venue} {snapshot.asset}/{snapshot.fiat}"
        )
        return file_path

    df = pl.DataFrame(records)
    df.write_parquet(file_path)
    return file_path


async def run_venue_poller(
    scraper: BaseP2PScraper,
    asset: str,
    fiat: str,
    depth: int = 10,
    interval_seconds: float = 15.0,
    output_dir: str = "data/raw",
    stop_event: asyncio.Event = None,
):
    """Execution loop for an individual venue scraper instance."""
    logger.info(
        f"Starting ingestion loop for {scraper.venue_name} ({asset}/{fiat}) "
        f"at {interval_seconds}s cadence."
    )

    async with scraper:
        while not (stop_event and stop_event.is_set()):
            cycle_start = asyncio.get_event_loop().time()
            try:
                snapshot = await scraper.poll_order_book(
                    asset=asset, fiat=fiat, depth=depth
                )
                file_path = save_snapshot_to_parquet(
                    snapshot=snapshot, output_base_dir=output_dir
                )
                logger.info(
                    f"[{scraper.venue_name.upper()}] Captured {len(snapshot.bids)} bids / "
                    f"{len(snapshot.asks)} asks in {snapshot.latency_ms:.1f}ms -> {file_path.name}"
                )
            except Exception as exc:
                logger.error(
                    f"[{scraper.venue_name.upper()}] Scraping cycle failure: {exc}",
                    exc_info=False,
                )

            # Sleep remaining interval to maintain steady cadence
            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_duration = max(0.0, interval_seconds - elapsed)

            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(sleep_duration)


async def main():
    """Entry point initializing active venue scrapers with graceful shutdown."""
    stop_event = asyncio.Event()

    # Graceful shutdown handler for Ctrl+C
    def _handle_shutdown():
        logger.info("Shutdown signal received. Finalizing in-flight scrapes...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_shutdown)
        except NotImplementedError:
            # Signal handlers not implemented on Windows event loop
            pass

    # Registered scrapers to run concurrently
    scrapers = [
        BinanceP2PScraper(),
        # BybitP2PScraper() will plug in here next
    ]

    tasks = [
        asyncio.create_task(
            run_venue_poller(
                scraper=scraper,
                asset="USDT",
                fiat="USD",
                depth=10,
                interval_seconds=15.0,
                stop_event=stop_event,
            )
        )
        for scraper in scrapers
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Ingestion daemon stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
