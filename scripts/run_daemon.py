#!/usr/bin/env python3
"""
Production Entry Point for the Live P2P Quantitative Execution Daemon.

Loads configuration from environment variables and command-line arguments,
initializes scrapers, quantitative spread models, friction matrices, alert
dispatchers, and runs the LiveExecutionDaemon under active signal handling.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from src.execution.alerts import (
    AlertDeduplicator,
    AlertDispatcher,
    TelegramNotifier,
    WebhookNotifier,
)
from src.execution.daemon import LiveExecutionDaemon
from src.signals.friction_matrix import FrictionCostEngine, PaymentRailCostMatrix
from src.signals.spread_models import (
    DualSidedMakerSpreadModel,
    OpportunityScanner,
    TakerTakerArbitrageDetector,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("p2p-daemon-runner")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run P2P Quant Engine Live Ingestion & Execution Daemon"
    )

    parser.add_argument(
        "--asset",
        type=str,
        default=os.getenv("P2P_ASSET", "USDT"),
        help="Crypto asset symbol (default: USDT)",
    )
    parser.add_argument(
        "--fiat",
        type=str,
        default=os.getenv("P2P_FIAT", "NGN"),
        help="Fiat currency code (default: NGN)",
    )
    parser.add_argument(
        "--target-fiat",
        type=float,
        default=float(os.getenv("P2P_TARGET_FIAT", "500000.0")),
        help="Target quote sizing in fiat for book sweeps (default: 500,000.0)",
    )
    parser.add_argument(
        "--venues",
        type=str,
        default=os.getenv("P2P_VENUES", "binance,bybit"),
        help="Comma-separated exchange venues (default: binance,bybit)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("P2P_POLL_INTERVAL", "12.0")),
        help="Polling cadence in seconds (default: 12.0)",
    )
    parser.add_argument(
        "--max-staleness",
        type=float,
        default=float(os.getenv("P2P_MAX_STALENESS", "45.0")),
        help="Maximum feed staleness cutoff in seconds (default: 45.0)",
    )
    parser.add_argument(
        "--min-roi",
        type=float,
        default=float(os.getenv("P2P_MIN_ROI_PCT", "0.40")),
        help="Minimum net realizable ROI %% to trigger alerts (default: 0.40)",
    )
    parser.add_argument(
        "--raw-storage-dir",
        type=str,
        default=os.getenv("P2P_RAW_STORAGE_DIR", "data/raw"),
        help="Directory to persist background raw micro-Parquet buffers (default: data/raw)",
    )
    parser.add_argument(
        "--telegram-token",
        type=str,
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot API Token",
    )
    parser.add_argument(
        "--telegram-chat-id",
        type=str,
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="Telegram Target Chat ID",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=os.getenv("WEBHOOK_URL", ""),
        help="Generic JSON Webhook URL endpoint",
    )
    parser.add_argument(
        "--dedup-cooldown",
        type=float,
        default=float(os.getenv("P2P_DEDUP_COOLDOWN", "180.0")),
        help="Alert deduplication cooldown in seconds (default: 180.0)",
    )
    parser.add_argument(
        "--disable-storage",
        action="store_true",
        default=os.getenv("P2P_DISABLE_STORAGE", "false").lower() in ("true", "1"),
        help="Disable background micro-Parquet disk writes",
    )

    return parser.parse_args()


async def metrics_reporter_loop(
    daemon: LiveExecutionDaemon, interval_seconds: float = 60.0
) -> None:
    """Periodically logs high-level operational metrics and health status."""
    while daemon.is_running:
        try:
            await asyncio.sleep(interval_seconds)
            m = daemon.get_metrics()
            degraded_str = ", ".join(m.degraded_venues) if m.degraded_venues else "None"
            logger.info(
                "Telemetry: Uptime=%.0fs | Cycles=%d | Signals=%d | Alerts=%d | Snapshots=%s | Degraded=[%s]",
                m.uptime_seconds,
                m.total_cycles,
                m.signals_detected,
                m.alerts_dispatched,
                m.snapshots_captured,
                degraded_str,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Telemetry reporting error: %s", exc)


async def main() -> None:
    args = parse_args()

    venues = tuple(v.strip().lower() for v in args.venues.split(",") if v.strip())
    raw_dir = None if args.disable_storage else Path(args.raw_storage_dir)

    logger.info("==================================================")
    logger.info(" Starting P2P Quant Engine - Live Execution Daemon")
    logger.info(
        " Asset: %s | Fiat: %s | Target Size: %s",
        args.asset,
        args.fiat,
        f"{args.target_fiat:,.2f}",
    )
    logger.info(
        " Venues: %s | Cadence: %.1fs | Min Net ROI: %.2f%%",
        venues,
        args.poll_interval,
        args.min_roi,
    )
    logger.info(
        " Raw Lake Writes: %s", "Disabled" if args.disable_storage else str(raw_dir)
    )
    logger.info("==================================================")

    # 1. Initialize Signal Models
    taker_detector = TakerTakerArbitrageDetector(
        min_net_spread_pct=args.min_roi,
        default_taker_fee_pct=0.0,
        enforce_payment_overlap=True,
    )
    maker_model = DualSidedMakerSpreadModel(
        min_net_margin_pct=args.min_roi,
        default_maker_fee_pct=0.20,
        penny_jump_epsilon=0.01,
    )
    scanner = OpportunityScanner(taker_detector=taker_detector, maker_model=maker_model)

    # 2. Initialize Friction & Banking Engine
    cost_matrix = PaymentRailCostMatrix()
    friction_engine = FrictionCostEngine(
        cost_matrix=cost_matrix,
        min_realizable_roi_pct=args.min_roi,
    )

    # 3. Initialize Alert Dispatcher
    tg_notifier = None
    if args.telegram_token and args.telegram_chat_id:
        tg_notifier = TelegramNotifier(
            bot_token=args.telegram_token,
            chat_id=args.telegram_chat_id,
        )
        logger.info("Telegram alerting enabled for chat %s", args.telegram_chat_id)

    wh_notifier = None
    if args.webhook_url:
        wh_notifier = WebhookNotifier(webhook_url=args.webhook_url)
        logger.info("Webhook alerting enabled for %s", args.webhook_url)

    deduplicator = AlertDeduplicator(
        cooldown_seconds=args.dedup_cooldown,
        min_spread_delta_pct=0.50,
    )
    dispatcher = AlertDispatcher(
        telegram_notifier=tg_notifier,
        webhook_notifier=wh_notifier,
        deduplicator=deduplicator,
    )

    # 4. Instantiate Live Daemon
    daemon = LiveExecutionDaemon(
        asset=args.asset,
        fiat=args.fiat,
        target_fiat_amount=args.target_fiat,
        venues=venues,
        poll_interval_seconds=args.poll_interval,
        max_feed_staleness_seconds=args.max_staleness,
        raw_storage_dir=raw_dir,
        scanner=scanner,
        friction_engine=friction_engine,
        dispatcher=dispatcher,
    )

    # 5. Attach OS Signals for Graceful Teardown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received. Stopping daemon...")
        stop_event.set()

    # Note: signal handlers differ between POSIX and Windows (SIGINT/SIGTERM)
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)
    else:
        # On Windows, signal.signal handles keyboard interrupt
        signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
        signal.signal(signal.SIGTERM, lambda s, f: stop_event.set())

    # 6. Start Daemon & Telemetry Tasks
    await daemon.start()
    reporter_task = asyncio.create_task(
        metrics_reporter_loop(daemon, interval_seconds=30.0)
    )

    logger.info("Live daemon active. Press Ctrl+C to terminate.")

    # Wait for termination signal
    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Initiating graceful shutdown sequence...")
        reporter_task.cancel()
        await daemon.stop()
        logger.info("Daemon cleanly stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
