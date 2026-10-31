import httpx
import pytest

from src.scrapers.base_scraper import (
    BaseP2PScraper,
    P2POrderBookSnapshot,
    P2POrderLevel,
    RateLimitExceededError,
    ScraperError,
    VenueAPIError,
)

# --- Dummy Concrete Class for Testing ---


class MockScraper(BaseP2PScraper):
    async def _fetch_raw_order_book(self, asset: str, fiat: str, depth: int):
        data, latency = await self._request("GET", "https://api.mockvenue.com/depth")
        return data, latency

    def parse_payload(self, raw_data, asset: str, fiat: str, latency_ms: float):
        bids = [
            P2POrderLevel(
                ad_id="ad_1",
                venue=self.venue_name,
                side="BUY",
                asset=asset,
                fiat=fiat,
                price=1500.0,
                available_crypto=100.0,
                min_fiat_limit=1000.0,
                max_fiat_limit=50000.0,
                payment_methods=["Bank Transfer"],
                merchant_id="m_1",
                merchant_name="MerchantOne",
                merchant_completed_orders=150,
                merchant_completion_rate=0.98,
                is_pro_merchant=True,
            )
        ]
        return P2POrderBookSnapshot(
            venue=self.venue_name,
            asset=asset,
            fiat=fiat,
            bids=bids,
            asks=[],
            latency_ms=latency_ms,
        )


# --- Unit Tests ---


@pytest.mark.asyncio
async def test_session_lifecycle():
    scraper = MockScraper("test_venue")
    assert scraper._client is None

    async with scraper:
        assert scraper._client is not None
        assert not scraper._client.is_closed

    assert scraper._client is None


@pytest.mark.asyncio
async def test_request_outside_context_raises_error():
    scraper = MockScraper("test_venue")
    with pytest.raises(ScraperError, match="HTTP client session is not active"):
        await scraper._request("GET", "https://api.mockvenue.com/test")


@pytest.mark.asyncio
async def test_successful_poll_order_book(monkeypatch):
    scraper = MockScraper("test_venue")

    async def mock_request(method, url, **kwargs):
        return {"data": []}, 12.5

    monkeypatch.setattr(scraper, "_request", mock_request)

    async with scraper:
        snapshot = await scraper.poll_order_book("USDT", "NGN", depth=5)

    assert isinstance(snapshot, P2POrderBookSnapshot)
    assert snapshot.venue == "test_venue"
    assert snapshot.asset == "USDT"
    assert snapshot.fiat == "NGN"
    assert len(snapshot.bids) == 1
    assert snapshot.bids[0].price == 1500.0
    assert snapshot.latency_ms == 12.5


@pytest.mark.asyncio
async def test_rate_limit_backoff_exhaustion(monkeypatch):
    scraper = MockScraper("test_venue", max_retries=2, base_backoff=0.01)

    # Track how many times request is called
    calls = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "too many requests"})

    transport = httpx.MockTransport(mock_handler)

    async with scraper:
        scraper._client._transport = transport
        with pytest.raises(RateLimitExceededError, match="Rate limit exceeded"):
            await scraper._request("GET", "https://api.mockvenue.com/depth")

    # Initial attempt + 2 retries = 3 calls
    assert calls == 3


@pytest.mark.asyncio
async def test_server_error_exhaustion(monkeypatch):
    scraper = MockScraper("test_venue", max_retries=1, base_backoff=0.01)

    calls = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="Bad Gateway")

    transport = httpx.MockTransport(mock_handler)

    async with scraper:
        scraper._client._transport = transport
        with pytest.raises(VenueAPIError, match="Server error 502"):
            await scraper._request("GET", "https://api.mockvenue.com/depth")

    assert calls == 2
