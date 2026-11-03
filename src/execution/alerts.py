"""
Real-Time Alert Formatting, Deduplication & Multi-Channel Dispatchers.

Dispatches actionable P2P arbitrage signals to Telegram and generic Webhooks
with in-memory sliding-window deduplication and microstructural telemetry.
"""

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import httpx

from src.signals.friction_matrix import AdjustedArbitrageSignal
from src.signals.spread_models import StrategyType

logger = logging.getLogger(__name__)


class NotificationChannel(str, Enum):
    TELEGRAM = "TELEGRAM"
    WEBHOOK = "WEBHOOK"


@dataclass
class DeduplicationEntry:
    last_alerted_at: datetime
    last_net_spread_pct: float
    ad_fingerprint: str


class AlertDeduplicator:
    """
    Sliding-window deduplication cache to prevent alert flooding.

    Suppresses recurring signals for identical venue pairs and rails within
    a cooldown window unless the spread expands by more than min_spread_delta_pct.
    """

    def __init__(
        self,
        cooldown_seconds: float = 180.0,
        min_spread_delta_pct: float = 0.50,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.min_spread_delta_pct = min_spread_delta_pct
        self._cache: Dict[str, DeduplicationEntry] = {}

    def _generate_key(self, signal: AdjustedArbitrageSignal) -> Tuple[str, str]:
        """Generates grouping key and fingerprint for the signal."""
        base = signal.base_signal
        group_key = f"{base.strategy.value}:{base.buy_venue}:{base.sell_venue}:{base.asset}:{base.fiat}:{signal.source_rail.value}"
        fingerprint = (
            f"{base.buy_price:.4f}:{base.sell_price:.4f}:{base.target_fiat_volume:.0f}"
        )
        return group_key, fingerprint

    def should_alert(
        self,
        signal: AdjustedArbitrageSignal,
        current_time: Optional[datetime] = None,
    ) -> bool:
        """Determines if the signal qualifies for broadcasting."""
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        group_key, fingerprint = self._generate_key(signal)
        entry = self._cache.get(group_key)

        if entry is None:
            self._cache[group_key] = DeduplicationEntry(
                last_alerted_at=now,
                last_net_spread_pct=signal.net_realizable_roi_pct,
                ad_fingerprint=fingerprint,
            )
            return True

        elapsed = (now - entry.last_alerted_at).total_seconds()
        spread_expansion = signal.net_realizable_roi_pct - entry.last_net_spread_pct

        # Alert if cooldown has expired OR if spread expanded significantly
        if (
            elapsed >= self.cooldown_seconds
            or spread_expansion >= self.min_spread_delta_pct
        ):
            self._cache[group_key] = DeduplicationEntry(
                last_alerted_at=now,
                last_net_spread_pct=signal.net_realizable_roi_pct,
                ad_fingerprint=fingerprint,
            )
            return True

        return False

    def purge_expired(self, current_time: Optional[datetime] = None) -> int:
        """Removes stale cache entries older than 2x the cooldown window."""
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        cutoff = now - timedelta(seconds=self.cooldown_seconds * 2)
        expired_keys = [k for k, v in self._cache.items() if v.last_alerted_at < cutoff]
        for k in expired_keys:
            del self._cache[k]
        return len(expired_keys)


class AlertFormatter:
    """Formats AdjustedArbitrageSignal into presentation-ready text."""

    @staticmethod
    def format_telegram_html(signal: AdjustedArbitrageSignal) -> str:
        """Formats signal as valid Telegram HTML."""
        base = signal.base_signal
        strategy_badge = (
            "⚡ <b>TAKER-TAKER ARBITRAGE</b>"
            if base.strategy == StrategyType.TAKER_TAKER
            else "💎 <b>DUAL-SIDED MAKER</b>"
        )

        direction = (
            f"Buy on <b>{html.escape(base.buy_venue.upper())}</b> ➔ Sell on <b>{html.escape(base.sell_venue.upper())}</b>"
            if base.buy_venue != base.sell_venue
            else f"Market Making on <b>{html.escape(base.buy_venue.upper())}</b>"
        )

        return (
            f"{strategy_badge}\n"
            f"<b>Pair:</b> {html.escape(base.asset)} / {html.escape(base.fiat)}\n"
            f"<b>Routing:</b> {direction}\n"
            f"<b>Payment Rail:</b> <code>{html.escape(signal.source_rail.value)}</code>\n\n"
            f"💵 <b>Trade Sizing:</b>\n"
            f" • Target Fiat: <b>{base.target_fiat_volume:,.2f} {html.escape(base.fiat)}</b>\n"
            f" • Crypto Volume: <b>{base.executable_crypto:,.4f} {html.escape(base.asset)}</b>\n"
            f" • Entry Buy VWAP: <code>{base.buy_price:,.4f}</code>\n"
            f" • Exit Sell VWAP: <code>{base.sell_price:,.4f}</code>\n\n"
            f"📊 <b>Financial Breakdown:</b>\n"
            f" • Gross Spread: <code>+{base.gross_spread_pct:.2f}%</code>\n"
            f" • Rail Friction: <code>-{signal.rail_friction_fee_fiat:,.2f} {html.escape(base.fiat)}</code>\n"
            f" • Risk Haircut: <code>-{signal.risk_haircut_fiat:,.2f} {html.escape(base.fiat)}</code>\n"
            f" • <b>Net Realizable Profit:</b> <b>+{signal.net_realizable_profit_fiat:,.2f} {html.escape(base.fiat)}</b>\n"
            f" • <b>Net Realizable ROI:</b> <b>+{signal.net_realizable_roi_pct:.2f}%</b>\n"
            f" • Expected Settlement: ~{signal.expected_settlement_minutes:.1f} mins\n\n"
            f"⏱ <i>Timestamp: {base.timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}</i>"
        )

    @staticmethod
    def format_webhook_payload(signal: AdjustedArbitrageSignal) -> Dict[str, Any]:
        """Constructs standardized JSON payload for webhooks."""
        base = signal.base_signal
        return {
            "strategy": base.strategy.value,
            "asset": base.asset,
            "fiat": base.fiat,
            "timestamp_utc": base.timestamp_utc.isoformat(),
            "buy_venue": base.buy_venue,
            "sell_venue": base.sell_venue,
            "buy_price": base.buy_price,
            "sell_price": base.sell_price,
            "source_rail": signal.source_rail.value,
            "target_rail": signal.target_rail.value,
            "target_fiat_volume": base.target_fiat_volume,
            "executable_crypto": base.executable_crypto,
            "gross_spread_pct": base.gross_spread_pct,
            "rail_friction_fee_fiat": signal.rail_friction_fee_fiat,
            "risk_haircut_fiat": signal.risk_haircut_fiat,
            "net_realizable_profit_fiat": signal.net_realizable_profit_fiat,
            "net_realizable_roi_pct": signal.net_realizable_roi_pct,
            "expected_settlement_minutes": signal.expected_settlement_minutes,
        }


class TelegramNotifier:
    """Dispatches asynchronous alerts to a Telegram chat via Bot API."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.client = client or httpx.AsyncClient(timeout=10.0)

    async def send_signal(self, signal: AdjustedArbitrageSignal) -> bool:
        """Sends an HTML formatted trade signal to Telegram."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials unset; skipping alert transmission.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        message_html = AlertFormatter.format_telegram_html(signal)
        payload = {
            "chat_id": self.chat_id,
            "text": message_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            response = await self.client.post(url, json=payload)
            if response.status_code == 200:
                return True
            logger.error(
                "Telegram API rejection: %d - %s", response.status_code, response.text
            )
            return False
        except httpx.HTTPError as exc:
            logger.error("Telegram delivery network error: %s", exc)
            return False

    async def close(self) -> None:
        """Closes underlying HTTP client session."""
        await self.client.aclose()


class WebhookNotifier:
    """Dispatches asynchronous JSON alerts to generic webhook endpoints."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.webhook_url = webhook_url
        self.client = client or httpx.AsyncClient(timeout=10.0)

    async def send_signal(self, signal: AdjustedArbitrageSignal) -> bool:
        """Posts structured JSON payload to the configured endpoint."""
        if not self.webhook_url:
            return False

        payload = AlertFormatter.format_webhook_payload(signal)
        try:
            response = await self.client.post(self.webhook_url, json=payload)
            return response.status_code in (200, 201, 202, 204)
        except httpx.HTTPError as exc:
            logger.error("Webhook dispatch failed: %s", exc)
            return False

    async def close(self) -> None:
        """Closes underlying HTTP client session."""
        await self.client.aclose()


class AlertDispatcher:
    """
    Central dispatcher coordinating deduplication and broadcasting.
    """

    def __init__(
        self,
        telegram_notifier: Optional[TelegramNotifier] = None,
        webhook_notifier: Optional[WebhookNotifier] = None,
        deduplicator: Optional[AlertDeduplicator] = None,
    ):
        self.telegram = telegram_notifier
        self.webhook = webhook_notifier
        self.deduplicator = deduplicator or AlertDeduplicator()

    async def dispatch(
        self,
        signal: AdjustedArbitrageSignal,
        current_time: Optional[datetime] = None,
    ) -> Dict[NotificationChannel, bool]:
        """
        Deduplicates signal and broadcasts to all active notification channels.
        """
        results: Dict[NotificationChannel, bool] = {}

        if not signal.is_actionable:
            return results

        if not self.deduplicator.should_alert(signal, current_time):
            return results

        if self.telegram:
            results[NotificationChannel.TELEGRAM] = await self.telegram.send_signal(
                signal
            )

        if self.webhook:
            results[NotificationChannel.WEBHOOK] = await self.webhook.send_signal(
                signal
            )

        return results

    async def close(self) -> None:
        """Tears down child HTTP client resources."""
        if self.telegram:
            await self.telegram.close()
        if self.webhook:
            await self.webhook.close()
