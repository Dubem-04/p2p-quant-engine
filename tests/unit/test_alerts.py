from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.execution.alerts import (
    AlertDeduplicator,
    AlertDispatcher,
    AlertFormatter,
    NotificationChannel,
    TelegramNotifier,
    WebhookNotifier,
)
from src.signals.friction_matrix import AdjustedArbitrageSignal, CanonicalRail
from src.signals.spread_models import ArbitrageSignal, StrategyType


@pytest.fixture
def mock_adjusted_signal():
    base = ArbitrageSignal(
        strategy=StrategyType.TAKER_TAKER,
        asset="USDT",
        fiat="USD",
        timestamp_utc=datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone.utc),
        buy_venue="binance",
        sell_venue="bybit",
        buy_price=1.0000,
        sell_price=1.0250,
        gross_spread_pct=2.50,
        net_spread_pct=2.50,
        target_fiat_volume=1000.0,
        executable_crypto=1000.0,
        common_payment_methods=["Revolut"],
        metadata={"net_profit_fiat": 25.0},
    )
    return AdjustedArbitrageSignal(
        base_signal=base,
        source_rail=CanonicalRail.REVOLUT,
        target_rail=CanonicalRail.REVOLUT,
        nominal_profit_fiat=25.0,
        rail_friction_fee_fiat=0.0,
        risk_haircut_fiat=0.0,
        net_realizable_profit_fiat=25.0,
        net_realizable_roi_pct=2.50,
        is_actionable=True,
        expected_settlement_minutes=3.0,
    )


def test_alert_formatter_telegram_html(mock_adjusted_signal):
    text = AlertFormatter.format_telegram_html(mock_adjusted_signal)
    assert "TAKER-TAKER ARBITRAGE" in text
    assert "USDT / USD" in text
    assert "BINANCE" in text
    assert "BYBIT" in text
    assert "REVOLUT" in text
    assert "+2.50%" in text
    assert "+25.00 USD" in text


def test_alert_formatter_webhook_payload(mock_adjusted_signal):
    payload = AlertFormatter.format_webhook_payload(mock_adjusted_signal)
    assert payload["strategy"] == "TAKER_TAKER"
    assert payload["buy_venue"] == "binance"
    assert payload["sell_venue"] == "bybit"
    assert payload["net_realizable_roi_pct"] == 2.50
    assert payload["source_rail"] == "REVOLUT"


def test_alert_deduplicator_cooldown_and_expansion(mock_adjusted_signal):
    t0 = datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone.utc)
    dedup = AlertDeduplicator(cooldown_seconds=180.0, min_spread_delta_pct=0.50)

    # 1. First alert -> Accepted
    assert dedup.should_alert(mock_adjusted_signal, current_time=t0) is True

    # 2. Duplicate alert after 30s with same spread -> Suppressed
    t1 = t0 + timedelta(seconds=30)
    assert dedup.should_alert(mock_adjusted_signal, current_time=t1) is False

    # 3. Duplicate alert after 30s but spread expands by >= 0.5% (2.5% -> 3.1%) -> Accepted
    mock_adjusted_signal.net_realizable_roi_pct = 3.10
    assert dedup.should_alert(mock_adjusted_signal, current_time=t1) is True

    # 4. Same spread after 200s (cooldown elapsed) -> Accepted
    t2 = t1 + timedelta(seconds=200)
    assert dedup.should_alert(mock_adjusted_signal, current_time=t2) is True


def test_alert_deduplicator_purge(mock_adjusted_signal):
    t0 = datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone.utc)
    dedup = AlertDeduplicator(cooldown_seconds=60.0)

    dedup.should_alert(mock_adjusted_signal, current_time=t0)
    assert len(dedup._cache) == 1

    # After 150s (> 2x cooldown of 120s), purge removes it
    purged = dedup.purge_expired(current_time=t0 + timedelta(seconds=150))
    assert purged == 1
    assert len(dedup._cache) == 0


@pytest.mark.asyncio
async def test_telegram_notifier_dispatch(mock_adjusted_signal):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/botTEST_TOKEN/sendMessage"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = TelegramNotifier(
            bot_token="TEST_TOKEN", chat_id="12345", client=client
        )
        success = await notifier.send_signal(mock_adjusted_signal)
        assert success is True


@pytest.mark.asyncio
async def test_webhook_notifier_dispatch(mock_adjusted_signal):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://webhook.site/test"
        return httpx.Response(200, json={"received": True})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = WebhookNotifier(
            webhook_url="https://webhook.site/test", client=client
        )
        success = await notifier.send_signal(mock_adjusted_signal)
        assert success is True


@pytest.mark.asyncio
async def test_alert_dispatcher_full_flow(mock_adjusted_signal):
    telegram_called = False
    webhook_called = False

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal telegram_called, webhook_called
        if "telegram" in str(request.url):
            telegram_called = True
        elif "webhook" in str(request.url):
            webhook_called = True
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tg = TelegramNotifier(bot_token="TOKEN", chat_id="CHAT", client=client)
        wh = WebhookNotifier(webhook_url="https://webhook.local/test", client=client)
        dispatcher = AlertDispatcher(telegram_notifier=tg, webhook_notifier=wh)

        results = await dispatcher.dispatch(mock_adjusted_signal)

        assert results[NotificationChannel.TELEGRAM] is True
        assert results[NotificationChannel.WEBHOOK] is True
        assert telegram_called is True
        assert webhook_called is True
