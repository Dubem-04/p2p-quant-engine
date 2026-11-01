import pytest

from src.scrapers.base_scraper import P2POrderBookSnapshot, VenueAPIError
from src.scrapers.binance_p2p import BinanceP2PScraper


@pytest.fixture
def mock_binance_bids_response():
    """Mock response for tradeType='BUY' (Maker bids waiting to purchase crypto)."""
    return {
        "code": "000000",
        "message": "success",
        "data": [
            {
                "adv": {
                    "advNo": "bid_101",
                    "price": "1.000",
                    "tradableQuantity": "1000.00",
                    "minSingleTransAmount": "50.00",
                    "dynamicMaxSingleTransAmount": "1000.00",
                    "tradeMethods": [{"tradeMethodName": "Bank Transfer"}],
                },
                "advertiser": {
                    "userNo": "user_bid",
                    "nickName": "TopBuyer",
                    "monthOrderCount": 350,
                    "monthFinishRate": 0.995,
                    "userType": "merchant",
                },
            }
        ],
    }


@pytest.fixture
def mock_binance_asks_response():
    """Mock response for tradeType='SELL' (Maker asks offering crypto for sale)."""
    return {
        "code": "000000",
        "message": "success",
        "data": [
            {
                "adv": {
                    "advNo": "ask_202",
                    "price": "1.050",
                    "tradableQuantity": "500.00",
                    "minSingleTransAmount": "20.00",
                    "dynamicMaxSingleTransAmount": "525.00",
                    "tradeMethods": [{"tradeMethodName": "Bank Transfer"}],
                },
                "advertiser": {
                    "userNo": "user_ask",
                    "nickName": "FastSeller",
                    "monthOrderCount": 420,
                    "monthFinishRate": 0.985,
                    "userType": "merchant",
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_binance_poll_order_book_success(
    monkeypatch, mock_binance_bids_response, mock_binance_asks_response
):
    scraper = BinanceP2PScraper()

    # Route mock data depending on trade_type parameter
    async def mock_fetch_side(asset, fiat, trade_type, depth, pay_types=None):
        if trade_type == "BUY":
            return mock_binance_bids_response, 40.0
        return mock_binance_asks_response, 45.0

    monkeypatch.setattr(scraper, "_fetch_side_raw", mock_fetch_side)

    async with scraper:
        snapshot = await scraper.poll_order_book("USDT", "USD", depth=5)

    assert isinstance(snapshot, P2POrderBookSnapshot)
    assert snapshot.venue == "binance"
    assert snapshot.asset == "USDT"
    assert snapshot.fiat == "USD"
    assert len(snapshot.bids) == 1
    assert len(snapshot.asks) == 1

    # Verify BID side (From tradeType='BUY')
    bid = snapshot.bids[0]
    assert bid.ad_id == "bid_101"
    assert bid.side == "BUY"
    assert bid.price == 1.000
    assert bid.merchant_name == "TopBuyer"
    assert bid.merchant_completion_rate == 0.995

    # Verify ASK side (From tradeType='SELL')
    ask = snapshot.asks[0]
    assert ask.ad_id == "ask_202"
    assert ask.side == "SELL"
    assert ask.price == 1.050
    assert ask.merchant_name == "FastSeller"
    assert ask.merchant_completion_rate == 0.985

    # Check non-crossed market spread: Best Ask > Best Bid
    assert ask.price > bid.price


@pytest.mark.asyncio
async def test_binance_api_error_handling(monkeypatch):
    scraper = BinanceP2PScraper()

    async def mock_request(method, url, **kwargs):
        return {"code": "500001", "message": "System busy"}, 20.0

    monkeypatch.setattr(scraper, "_request", mock_request)

    async with scraper:
        with pytest.raises(VenueAPIError, match="Binance P2P API error"):
            await scraper._fetch_side_raw("USDT", "USD", "BUY", depth=5)


def test_binance_order_book_sorting():
    scraper = BinanceP2PScraper()
    raw_payload = {
        # Bids: Buyers competing (highest price is prioritized)
        "bids_raw": [
            {
                "adv": {
                    "advNo": "b1",
                    "price": "0.980",
                    "tradableQuantity": "10",
                    "minSingleTransAmount": "10",
                    "maxSingleTransAmount": "100",
                },
                "advertiser": {
                    "userNo": "u1",
                    "nickName": "B1",
                    "monthOrderCount": 10,
                    "monthFinishRate": 0.95,
                },
            },
            {
                "adv": {
                    "advNo": "b2",
                    "price": "1.000",
                    "tradableQuantity": "10",
                    "minSingleTransAmount": "10",
                    "maxSingleTransAmount": "100",
                },
                "advertiser": {
                    "userNo": "u2",
                    "nickName": "B2",
                    "monthOrderCount": 10,
                    "monthFinishRate": 0.95,
                },
            },
        ],
        # Asks: Sellers competing (lowest price is prioritized)
        "asks_raw": [
            {
                "adv": {
                    "advNo": "a1",
                    "price": "1.080",
                    "tradableQuantity": "10",
                    "minSingleTransAmount": "10",
                    "maxSingleTransAmount": "100",
                },
                "advertiser": {
                    "userNo": "u3",
                    "nickName": "A1",
                    "monthOrderCount": 10,
                    "monthFinishRate": 0.95,
                },
            },
            {
                "adv": {
                    "advNo": "a2",
                    "price": "1.050",
                    "tradableQuantity": "10",
                    "minSingleTransAmount": "10",
                    "maxSingleTransAmount": "100",
                },
                "advertiser": {
                    "userNo": "u4",
                    "nickName": "A2",
                    "monthOrderCount": 10,
                    "monthFinishRate": 0.95,
                },
            },
        ],
    }

    snapshot = scraper.parse_payload(
        raw_payload, asset="USDT", fiat="USD", latency_ms=30.0
    )

    # Bids sorted descending: highest buyer first
    assert snapshot.bids[0].price == 1.000
    assert snapshot.bids[1].price == 0.980
    assert snapshot.bids[0].side == "BUY"

    # Asks sorted ascending: lowest seller first
    assert snapshot.asks[0].price == 1.050
    assert snapshot.asks[1].price == 1.080
    assert snapshot.asks[0].side == "SELL"
