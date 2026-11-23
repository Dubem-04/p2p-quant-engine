import hashlib
import hmac
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx

from p2p_quant_engine.collector.config import (
    BYBIT_MAINNET_BASE_URL,
    BYBIT_P2P_ONLINE_ADS_PATH,
    BYBIT_P2P_ORDERS_PATH,
    BYBIT_P2P_UPDATE_AD_PATH,
    DEFAULT_DRY_RUN,
    DEFAULT_ORDERBOOK_CACHE_TTL_SECONDS,
    DEFAULT_ORDERBOOK_MAX_STALE_SECONDS,
    DEFAULT_ORDERBOOK_PAGE_SIZE,
    DEFAULT_PAYMENT_METHOD_FILTER,
    DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
    DEFAULT_RECV_WINDOW_MS,
    DEFAULT_REQUEST_PACING_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from p2p_quant_engine.execution.manager import (
    ExchangeOrder,
    OrderBookSnapshot,
    OrderStatus,
)
from p2p_quant_engine.pricing.reference import P2PAd
from p2p_quant_engine.risk.guardrails import CounterpartyProfile


class BybitAPIError(RuntimeError):
    """Raised when Bybit P2P returns a non-zero retCode or HTTP transport error."""

    def __init__(self, message: str, ret_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.ret_code = ret_code


# Actionable hints appended to well-known Bybit config/auth error codes.
_RET_CODE_HINTS: dict[int, str] = {
    10002: "Local clock is out of sync with Bybit; resync Windows time.",
    10010: "Request IP is not in the API key's whitelist; update it in Bybit.",
}

# Bybit echoes the signing input (timestamp + API KEY + recv_window + body) in
# 10004 "Error sign" messages. Strip it so keys never reach logs or screenshots.
_ORIGIN_STRING_RE = re.compile(r"origin_string\[.*\]", re.DOTALL)


def _redact(message: str) -> str:
    """Remove Bybit's echoed signing string from an error message."""
    return _ORIGIN_STRING_RE.sub("origin_string[REDACTED]", message)


# Longest single pre-request sleep when Bybit's headers say the window is spent.
_MAX_COOLDOWN_SLEEP_SECONDS = 5.0


@dataclass(frozen=True)
class BybitCollectorConfig:
    """Runtime settings for BybitP2PCollector."""

    base_url: str = BYBIT_MAINNET_BASE_URL
    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    request_pacing_seconds: float = DEFAULT_REQUEST_PACING_SECONDS
    orderbook_cache_ttl_seconds: float = DEFAULT_ORDERBOOK_CACHE_TTL_SECONDS
    orderbook_max_stale_seconds: float = DEFAULT_ORDERBOOK_MAX_STALE_SECONDS
    rate_limit_backoff_seconds: tuple[float, ...] = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
    recv_window_ms: str = DEFAULT_RECV_WINDOW_MS
    orderbook_page_size: int = DEFAULT_ORDERBOOK_PAGE_SIZE
    payment_methods: tuple[str, ...] = DEFAULT_PAYMENT_METHOD_FILTER
    dry_run: bool = DEFAULT_DRY_RUN


class BybitP2PCollector:
    """
    Bybit V5 P2P adapter implementing the ExchangeClient protocol for
    live order book collection, order syncing, and merchant ad management.
    """

    def __init__(
        self,
        config: Optional[BybitCollectorConfig] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or BybitCollectorConfig()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
        )
        self.dry_run_actions: list[dict[str, Any]] = []
        self._rate_limit_reset_epoch: float = 0.0
        self._time_offset_ms: int = 0
        self._time_synced: bool = False
        self._cached_side_ads: dict[str, tuple[float, list[P2PAd]]] = {}

    def close(self) -> None:
        """Close the underlying HTTP connection pool if owned by this instance."""
        if self._owns_client:
            self._client.close()

    # --- V5 Request Signing & Transport ---

    def _sign_payload(self, timestamp_ms: str, json_body: str) -> str:
        """Compute Bybit V5 HMAC-SHA256 signature."""
        if not self.config.api_key or not self.config.api_secret:
            raise BybitAPIError(
                "Missing BYBIT_API_KEY or BYBIT_API_SECRET for authenticated call."
            )
        raw_sig = (
            f"{timestamp_ms}{self.config.api_key}"
            f"{self.config.recv_window_ms}{json_body}"
        )
        return hmac.new(
            self.config.api_secret.encode("utf-8"),
            raw_sig.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _respect_rate_limit_window(self) -> None:
        """Pause before sending if Bybit signaled zero remaining quota in window."""
        if not self._owns_client:
            return
        now_epoch = time.time()
        if self._rate_limit_reset_epoch > now_epoch:
            wait_sec = min(
                _MAX_COOLDOWN_SLEEP_SECONDS,
                (self._rate_limit_reset_epoch - now_epoch) + 0.25,
            )
            time.sleep(wait_sec)
        elif self.config.request_pacing_seconds > 0:
            time.sleep(self.config.request_pacing_seconds)

    def _record_rate_limit_headers(self, headers: httpx.Headers) -> None:
        """Track Bybit V5 rate-limit headers to avoid 10006 throttling."""
        remaining_raw = headers.get("x-bapi-limit-status")
        reset_raw = headers.get("x-bapi-limit-reset-timestamp")
        if remaining_raw is not None and reset_raw is not None:
            try:
                remaining = int(remaining_raw)
                reset_ms = float(reset_raw)
                if remaining <= 1:
                    self._rate_limit_reset_epoch = reset_ms / 1000.0
            except ValueError:
                pass

    def _sync_server_time(self) -> None:
        """Measure local-vs-Bybit clock offset via the public /v5/market/time."""
        try:
            t0 = int(time.time() * 1000)
            resp = self._client.get("/v5/market/time")
            t1 = int(time.time() * 1000)
            server_ms = int(resp.json()["time"])
        except (httpx.HTTPError, KeyError, ValueError):
            return  # Non-fatal: fall back to the local clock.
        self._time_offset_ms = server_ms - (t0 + t1) // 2
        self._time_synced = True

    def _now_ms(self) -> int:
        """Current time in ms, corrected by the measured Bybit clock offset."""
        return int(time.time() * 1000) + self._time_offset_ms

    def _post(
        self, path: str, payload: dict[str, Any], authenticated: bool = False
    ) -> dict[str, Any]:
        """
        POST to Bybit P2P with live-network resilience:
        - one clock resync + retry on 10002 (timestamp skew)
        - short backoff retries on 10006 (intermittent throttle)
        Injected test clients skip both so unit tests stay instant.
        """
        backoffs = self.config.rate_limit_backoff_seconds if self._owns_client else ()
        throttle_retries = 0
        clock_resynced = False
        while True:
            try:
                return self._post_once(path, payload, authenticated)
            except BybitAPIError as exc:
                if (
                    exc.ret_code == 10002
                    and authenticated
                    and self._owns_client
                    and not clock_resynced
                ):
                    self._sync_server_time()
                    clock_resynced = True
                    continue
                if exc.ret_code == 10006 and throttle_retries < len(backoffs):
                    delay = backoffs[throttle_retries]
                    time.sleep(delay + delay * random.uniform(0.0, 0.25))
                    throttle_retries += 1
                    continue
                raise

    def _post_once(
        self, path: str, payload: dict[str, Any], authenticated: bool
    ) -> dict[str, Any]:
        """Single signed/unsigned POST that validates retCode == 0."""
        self._respect_rate_limit_window()
        if authenticated and self._owns_client and not self._time_synced:
            self._sync_server_time()

        compact_body = json.dumps(payload, separators=(",", ":"))
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if authenticated:
            timestamp_ms = str(self._now_ms())
            headers["X-BAPI-API-KEY"] = self.config.api_key
            headers["X-BAPI-TIMESTAMP"] = timestamp_ms
            headers["X-BAPI-RECV-WINDOW"] = self.config.recv_window_ms
            headers["X-BAPI-SIGN"] = self._sign_payload(timestamp_ms, compact_body)

        try:
            response = self._client.post(
                path, content=compact_body.encode("utf-8"), headers=headers
            )
            self._record_rate_limit_headers(response.headers)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise BybitAPIError(f"HTTP error calling {path}: {exc}") from exc

        ret_code = data.get("retCode", data.get("ret_code", -1))
        if ret_code != 0:
            ret_msg = _redact(
                str(data.get("retMsg", data.get("ret_msg", "Unknown Bybit error")))
            )
            hint = _RET_CODE_HINTS.get(ret_code)
            suffix = f" -> {hint}" if hint else ""
            raise BybitAPIError(
                f"Bybit error on {path} [{ret_code}]: {ret_msg}{suffix}",
                ret_code=ret_code,
            )

        return data.get("result", {})

    # --- Normalization Helpers ---

    @staticmethod
    def _parse_ad(raw: dict[str, Any], merchant_side: Literal["BUY", "SELL"]) -> P2PAd:
        """Convert a raw Bybit P2P item dictionary into a normalized P2PAd."""
        raw_rate = float(raw.get("recentExecuteRate", 0.0))
        completion_rate = raw_rate / 100.0 if raw_rate > 1.0 else raw_rate

        return P2PAd(
            ad_id=str(raw["id"]),
            merchant_id=str(raw["userId"]),
            side=merchant_side,
            price_fiat=float(raw["price"]),
            available_crypto=float(raw["lastQuantity"]),
            min_amount_fiat=float(raw["minAmount"]),
            max_amount_fiat=float(raw["maxAmount"]),
            completed_orders=int(raw.get("recentOrderNum", 0)),
            completion_rate=round(completion_rate, 4),
        )

    @staticmethod
    def _map_order_status(raw_status: int) -> OrderStatus:
        """Map Bybit P2P numeric order status codes to internal OrderStatus."""
        if raw_status == 50:
            return "COMPLETED"
        if raw_status == 40:
            return "CANCELLED"
        if raw_status == 20:
            return "PAID"
        return "PENDING"

    @classmethod
    def _parse_order(cls, raw: dict[str, Any]) -> ExchangeOrder:
        """Convert a raw Bybit P2P order row into a normalized ExchangeOrder."""
        side: Literal["BUY", "SELL"] = "SELL" if int(raw.get("side", 0)) == 1 else "BUY"
        raw_rate = float(raw.get("targetCompletionRate", 100.0))
        completion_rate = raw_rate / 100.0 if raw_rate > 1.0 else raw_rate

        cp = CounterpartyProfile(
            user_id=str(raw.get("targetUserId", "")),
            completed_orders=int(raw.get("targetCompletedOrders", 0)),
            completion_rate=round(completion_rate, 4),
            account_age_days=int(raw.get("targetAccountAgeDays", 365)),
            kyc_verified=bool(raw.get("targetKycVerified", True)),
            name_matches_bank=bool(raw.get("nameMatchesBank", True)),
        )

        return ExchangeOrder(
            order_id=str(raw["id"]),
            side=side,
            status=cls._map_order_status(int(raw.get("status", 10))),
            crypto_amount=float(raw["notifyTokenQuantity"]),
            fiat_amount=float(raw["amount"]),
            price_fiat=float(raw["price"]),
            counterparty=cp,
            exchange_fee_fiat=float(raw.get("fee", 0.0)),
        )

    # --- ExchangeClient Protocol Implementation ---

    def fetch_orderbook(
        self, crypto_asset: str, fiat_currency: str
    ) -> OrderBookSnapshot:
        """Fetch top qualifying SELL (Ask) and BUY (Bid) ads from Bybit P2P."""
        ads: list[P2PAd] = []
        side_fetch_times: list[float] = []

        # Bybit side "0" = public buying crypto -> merchant SELL (Ask) ads
        # Bybit side "1" = public selling crypto -> merchant BUY (Bid) ads
        side_map: tuple[tuple[str, Literal["BUY", "SELL"]], ...] = (
            ("0", "SELL"),
            ("1", "BUY"),
        )

        for bybit_side, normalized_side in side_map:
            cache_key = f"{crypto_asset}:{fiat_currency}:{bybit_side}"
            now_mono = time.monotonic()
            cached = self._cached_side_ads.get(cache_key)
            if (
                self._owns_client
                and cached is not None
                and (now_mono - cached[0]) < self.config.orderbook_cache_ttl_seconds
            ):
                ads.extend(cached[1])
                side_fetch_times.append(cached[0])
                continue

            payload: dict[str, Any] = {
                "tokenId": crypto_asset,
                "currencyId": fiat_currency,
                "side": bybit_side,
                "size": str(self.config.orderbook_page_size),
                "page": "1",
            }
            if self.config.payment_methods:
                payload["payment"] = list(self.config.payment_methods)

            # Bybit's docs/SDK sign this endpoint too (X-BAPI-* headers required).
            try:
                result = self._post(
                    BYBIT_P2P_ONLINE_ADS_PATH, payload, authenticated=True
                )
            except BybitAPIError as exc:
                # Ride out throttling/transport blips with recent data only;
                # auth/config errors (10002, 10010, ...) must still surface.
                if (
                    exc.ret_code in (None, 10006)
                    and cached is not None
                    and (time.monotonic() - cached[0])
                    <= self.config.orderbook_max_stale_seconds
                ):
                    ads.extend(cached[1])
                    side_fetch_times.append(cached[0])
                    continue
                raise
            side_ads = [
                self._parse_ad(item, merchant_side=normalized_side)
                for item in result.get("items", [])
            ]
            fetched_at = time.monotonic()
            self._cached_side_ads[cache_key] = (fetched_at, side_ads)
            ads.extend(side_ads)
            side_fetch_times.append(fetched_at)

        # Snapshot age = age of its OLDEST side, so staleness guardrails stay honest.
        return OrderBookSnapshot(
            ads=ads,
            fetched_at_monotonic=min(side_fetch_times),
        )

    def fetch_orders(
        self, crypto_asset: str, fiat_currency: str
    ) -> list[ExchangeOrder]:
        """Fetch merchant P2P orders for the configured asset pair."""
        payload = {
            "tokenId": crypto_asset,
            "currencyId": fiat_currency,
            "page": 1,
            "size": self.config.orderbook_page_size,
        }
        result = self._post(BYBIT_P2P_ORDERS_PATH, payload, authenticated=True)
        return [self._parse_order(row) for row in result.get("items", [])]

    def update_ad_price(self, ad_id: str, new_price_fiat: float) -> None:
        """Update the posted fiat price of a merchant P2P advertisement."""
        if self.config.dry_run:
            self.dry_run_actions.append(
                {"action": "update_ad_price", "ad_id": ad_id, "price": new_price_fiat}
            )
            return

        payload = {"id": ad_id, "price": f"{new_price_fiat:.2f}"}
        self._post(BYBIT_P2P_UPDATE_AD_PATH, payload, authenticated=True)

    def set_ad_status(self, ad_id: str, active: bool) -> None:
        """Enable (actionType=1) or pause (actionType=0) a merchant P2P ad."""
        if self.config.dry_run:
            self.dry_run_actions.append(
                {"action": "set_ad_status", "ad_id": ad_id, "active": active}
            )
            return

        payload = {"id": ad_id, "actionType": "1" if active else "0"}
        self._post(BYBIT_P2P_UPDATE_AD_PATH, payload, authenticated=True)
