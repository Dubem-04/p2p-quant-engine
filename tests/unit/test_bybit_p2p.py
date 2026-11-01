import pytest

from src.scrapers.base_scraper import P2POrderBookSnapshot, VenueAPIError
from src.scrapers.bybit_p2p import BybitP2PScraper


@pytest.fixture
def mock_bybit_bids_response():
    """Mock Bybit side=1 (Maker bids)."""
    return {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {
            "items": [
                {
                    "id": "bybit_bid_101",
                    "price": "0.800",
                    "lastQuantity": "2500.0",
                    "minAmount": "50.0",
                    "maxAmount": "2000.0",
                    "payments": ["Bank Transfer"],
                    "userId": "usr_111",
                    "nickName": "BybitBuyer",
                    "recentOrderNum": 210,
                    "recentExecuteRate": 99.0,
                    "authMaker": True,
                }
            ]
        },
    }


@pytest.fixture
def mock_bybit_asks_response():
    """Mock Bybit side=0 (Maker asks)."""
    return {
        "ret_code": 0,
        "ret_msg": "SUCCESS",
        "result": {
            "items": [
                {
                    "id": "bybit_ask_202",
                    "price": "1.200",
                    "lastQuantity": "1500.0",
                    "minAmount": "30.0",
                    "maxAmount": "1800.0",
                    "payments": ["Bank Transfer"],
                    "userId": "usr_222",
                    "nickName": "BybitSeller",
                    "recentOrderNum": 180,
                    "recentExecuteRate": 98.5,
                    "authMaker": False,
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_bybit_poll_order_book_success(
    monkeypatch, mock_bybit_bids_response, mock_bybit_asks_response
):
    scraper = BybitP2PScraper()

    async def mock_fetch_side(asset, fiat, side_code, depth, payments=None):
        if side_code == "1":
            return mock_bybit_bids_response, 50.0
        return mock_bybit_asks_response, 55.0

    monkeypatch.setattr(scraper, "_fetch_side_raw", mock_fetch_side)

    async with scraper:
        snapshot = await scraper.poll_order_book("USDT", "USD", depth=5)

    assert isinstance(snapshot, P2POrderBookSnapshot)
    assert snapshot.venue == "bybit"
    assert snapshot.asset == "USDT"
    assert snapshot.fiat == "USD"
    assert len(snapshot.bids) == 1
    assert len(snapshot.asks) == 1

    bid = snapshot.bids[0]
    assert bid.ad_id == "bybit_bid_101"
    assert bid.side == "BUY"
    assert bid.price == 0.800
    assert bid.merchant_name == "BybitBuyer"
    assert bid.merchant_completion_rate == 0.990
    assert bid.is_pro_merchant is True

    ask = snapshot.asks[0]
    assert ask.ad_id == "bybit_ask_202"
    assert ask.side == "SELL"
    assert ask.price == 1.200
    assert ask.merchant_name == "BybitSeller"
    assert ask.merchant_completion_rate == 0.985
    assert ask.is_pro_merchant is False

    assert ask.price > bid.price


@pytest.mark.asyncio
async def test_bybit_api_error_handling(monkeypatch):
    scraper = BybitP2PScraper()

    async def mock_request(method, url, **kwargs):
        return {"ret_code": 10001, "ret_msg": "Request parameter error"}, 25.0

    monkeypatch.setattr(scraper, "_request", mock_request)

    async with scraper:
        with pytest.raises(VenueAPIError, match="Bybit P2P API error"):
            await scraper._fetch_side_raw("USDT", "USD", "1", depth=5)


def test_bybit_order_book_sorting():
    scraper = BybitP2PScraper()
    raw_payload = {
        "bids_raw": [
            {
                "id": "b1",
                "price": "0.790",
                "lastQuantity": "10",
                "minAmount": "10",
                "maxAmount": "100",
                "userId": "u1",
                "nickName": "B1",
            },
            {
                "id": "b2",
                "price": "0.810",
                "lastQuantity": "10",
                "minAmount": "10",
                "maxAmount": "100",
                "userId": "u2",
                "nickName": "B2",
            },
        ],
        "asks_raw": [
            {
                "id": "a1",
                "price": "1.250",
                "lastQuantity": "10",
                "minAmount": "10",
                "maxAmount": "100",
                "userId": "u3",
                "nickName": "A1",
            },
            {
                "id": "a2",
                "price": "1.210",
                "lastQuantity": "10",
                "minAmount": "10",
                "maxAmount": "100",
                "userId": "u4",
                "nickName": "A2",
            },
        ],
    }

    snapshot = scraper.parse_payload(
        raw_payload, asset="USDT", fiat="USD", latency_ms=35.0
    )

    # Highest bid first
    assert snapshot.bids[0].price == 0.810
    assert snapshot.bids[1].price == 0.790
    assert snapshot.bids[0].side == "BUY"

    # Lowest ask first
    assert snapshot.asks[0].price == 1.210
    assert snapshot.asks[1].price == 1.250
    assert snapshot.asks[0].side == "SELL"
