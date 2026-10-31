"""
Abstract base class and interface contract for all exchange P2P order book scrapers.

Defines standardized asynchronous ingestion routines, rate limiting compliance,
exponential backoff retry mechanisms, and normalized data formatting for L2
snapshots. Enforces uniform schema output across heterogeneous exchange APIs.

Classes:
    BaseP2PScraper: Abstract base class providing common network session management,
        error handling, and serialization signatures for venue-specific workers.
"""

import asyncio
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

# --- Custom Exceptions ---


class ScraperError(Exception):
    """Base exception for all scraper-related failures."""

    pass


class RateLimitExceededError(ScraperError):
    """Raised when an exchange endpoint returns 429 and retries are exhausted."""

    pass


class VenueAPIError(ScraperError):
    """Raised when an exchange endpoint returns persistent 5xx or invalid responses."""

    pass


# --- Normalized Pydantic Schemas ---


class P2POrderLevel(BaseModel):
    """Normalized Level-2 order book entry representing a single advertisement."""

    model_config = ConfigDict(frozen=True)

    ad_id: str = Field(description="Unique advertisement or order ID on the venue")
    venue: str = Field(description="Venue identifier (e.g., binance, bybit)")
    side: str = Field(
        description="Trade side: 'BUY' (maker buys crypto) or 'SELL' (maker sells crypto)"
    )
    asset: str = Field(description="Base asset (e.g., USDT)")
    fiat: str = Field(description="Quote fiat currency (e.g., NGN, KES, USD)")
    price: float = Field(gt=0, description="Price per unit in fiat")
    available_crypto: float = Field(
        ge=0, description="Available crypto quantity to trade"
    )
    min_fiat_limit: float = Field(ge=0, description="Minimum order size in fiat")
    max_fiat_limit: float = Field(gt=0, description="Maximum order size in fiat")
    payment_methods: List[str] = Field(
        default_factory=list, description="Supported payment rails"
    )
    merchant_id: str = Field(description="Counterparty unique merchant ID")
    merchant_name: str = Field(description="Counterparty display name")
    merchant_completed_orders: int = Field(ge=0, description="Completed order count")
    merchant_completion_rate: float = Field(
        ge=0.0, le=1.0, description="Completion rate from 0.0 to 1.0"
    )
    is_pro_merchant: bool = Field(
        default=False, description="Whether merchant has verified/pro badge"
    )


class P2POrderBookSnapshot(BaseModel):
    """Point-in-time normalized Level-2 snapshot of bids and asks for a currency pair."""

    model_config = ConfigDict(frozen=True)

    venue: str
    asset: str
    fiat: str
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    bids: List[P2POrderLevel] = Field(
        default_factory=list, description="Buyer ads, sorted descending by price"
    )
    asks: List[P2POrderLevel] = Field(
        default_factory=list, description="Seller ads, sorted ascending by price"
    )
    latency_ms: float = Field(
        ge=0.0, description="HTTP request round-trip latency in milliseconds"
    )


# --- Base Scraper Class ---


class BaseP2PScraper(ABC):
    """
    Abstract asynchronous base scraper implementing the Template Method pattern.
    Provides session management, jittered exponential backoff, and latency tracking.
    """

    def __init__(
        self,
        venue_name: str,
        timeout: float = 10.0,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ):
        self.venue_name = venue_name
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Initializes persistent HTTP/2 client session."""
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(self.timeout),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Closes HTTP/2 client session cleanly."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, **kwargs) -> tuple[Any, float]:
        """
        Executes HTTP request with jittered exponential backoff and monotonic latency timing.

        Returns:
            tuple[Any, float]: (parsed_json_response, latency_ms)

        Raises:
            RateLimitExceededError: If rate limit (429) retries are exhausted.
            VenueAPIError: If 5xx errors or network drops persist after retries.
        """
        if self._client is None or self._client.is_closed:
            raise ScraperError(
                "HTTP client session is not active. Use 'async with' context manager."
            )

        start_time = time.perf_counter()

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
                latency_ms = (time.perf_counter() - start_time) * 1000.0

                if response.status_code == 200:
                    return response.json(), latency_ms

                if response.status_code == 429:
                    if attempt == self.max_retries:
                        raise RateLimitExceededError(
                            f"Rate limit exceeded on {self.venue_name} after {self.max_retries} retries."
                        )
                    # Jittered exponential backoff
                    sleep_time = min(
                        self.max_backoff, self.base_backoff * (2**attempt)
                    ) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(sleep_time)
                    continue

                if response.status_code >= 500:
                    if attempt == self.max_retries:
                        raise VenueAPIError(
                            f"Server error {response.status_code} on {self.venue_name} after {self.max_retries} retries."
                        )
                    sleep_time = min(
                        self.max_backoff, self.base_backoff * (2**attempt)
                    ) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(sleep_time)
                    continue

                # Any other unexpected 4xx client errors
                response.raise_for_status()

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt == self.max_retries:
                    raise VenueAPIError(
                        f"Network failure reaching {self.venue_name}: {str(exc)}"
                    ) from exc
                sleep_time = min(
                    self.max_backoff, self.base_backoff * (2**attempt)
                ) + random.uniform(0.1, 0.5)
                await asyncio.sleep(sleep_time)

        raise VenueAPIError(f"Request failed unexpectedly on {self.venue_name}.")

    # --- Concrete Template Method ---

    async def poll_order_book(
        self, asset: str, fiat: str, depth: int = 5
    ) -> P2POrderBookSnapshot:
        """
        Public template method: fetches raw data via subclass implementation
        and returns a validated P2POrderBookSnapshot.
        """
        raw_data, latency_ms = await self._fetch_raw_order_book(
            asset=asset, fiat=fiat, depth=depth
        )
        return self.parse_payload(
            raw_data=raw_data, asset=asset, fiat=fiat, latency_ms=latency_ms
        )

    # --- Subclass Implementation Contracts ---

    @abstractmethod
    async def _fetch_raw_order_book(
        self, asset: str, fiat: str, depth: int
    ) -> tuple[Any, float]:
        """Queries the venue-specific endpoint. Must return (raw_payload, latency_ms)."""
        pass

    @abstractmethod
    def parse_payload(
        self, raw_data: Any, asset: str, fiat: str, latency_ms: float
    ) -> P2POrderBookSnapshot:
        """Transforms raw venue-specific payload into a normalized P2POrderBookSnapshot."""
        pass
