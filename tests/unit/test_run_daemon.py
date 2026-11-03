import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from scripts.run_daemon import metrics_reporter_loop, parse_args
from src.execution.daemon import DaemonMetrics, LiveExecutionDaemon


def test_parse_args_defaults():
    with patch("sys.argv", ["run_daemon.py"]):
        args = parse_args()
        assert args.asset == "USDT"
        assert args.fiat == "NGN"
        assert args.target_fiat == 500000.0
        assert args.venues == "binance,bybit"
        assert args.poll_interval == 12.0
        assert args.min_roi == 0.40
        assert args.disable_storage is False


def test_parse_args_custom_overrides():
    custom_argv = [
        "run_daemon.py",
        "--asset",
        "BTC",
        "--fiat",
        "USD",
        "--target-fiat",
        "2500.0",
        "--venues",
        "binance",
        "--poll-interval",
        "5.0",
        "--min-roi",
        "1.20",
        "--telegram-token",
        "T_TOKEN",
        "--telegram-chat-id",
        "99999",
        "--disable-storage",
    ]
    with patch("sys.argv", custom_argv):
        args = parse_args()
        assert args.asset == "BTC"
        assert args.fiat == "USD"
        assert args.target_fiat == 2500.0
        assert args.venues == "binance"
        assert args.poll_interval == 5.0
        assert args.min_roi == 1.20
        assert args.telegram_token == "T_TOKEN"
        assert args.telegram_chat_id == "99999"
        assert args.disable_storage is True


@pytest.mark.asyncio
async def test_metrics_reporter_loop():
    mock_daemon = AsyncMock(spec=LiveExecutionDaemon)
    mock_daemon.is_running = True
    mock_daemon.get_metrics.return_value = DaemonMetrics(
        start_time=datetime.now(timezone.utc),
        uptime_seconds=60.0,
        total_cycles=5,
        snapshots_captured={"binance": 5, "bybit": 5},
        signals_detected=2,
        alerts_dispatched=1,
        degraded_venues=[],
    )

    # Run loop briefly and cancel
    task = asyncio.create_task(
        metrics_reporter_loop(mock_daemon, interval_seconds=0.05)
    )
    await asyncio.sleep(0.12)
    mock_daemon.is_running = False
    task.cancel()

    assert mock_daemon.get_metrics.called
