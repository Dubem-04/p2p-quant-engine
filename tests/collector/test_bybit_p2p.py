import hashlib
import hmac
import json

import httpx
import pytest

from p2p_quant_engine.collector.bybit_p2p import (
    BybitAPIError,
    BybitCollectorConfig,
    BybitP2PCollector,
)


def _mock_client(handler: object) -> httpx.Client:
    """Create an httpx.Client backed by a MockTransport handler."""
    return httpx.Client(
        base_url="https://api.bybit.com",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_fetch_orderbook_normalizes_sides_and_applies_payment_filter() -> None:
    """Verify Bybit side 0/1 maps to SELL/BUY P2PAd and passes payment filter."""
    captured_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_payloads.append(body)
        if body["side"] == "0":
            items = [
                {
                    "id": "ask_1",
                    "userId": "merchant_a",
                    "price": "1608.50",
                    "lastQuantity": "2500.0",
                    "minAmount": "50000",
                    "maxAmount": "2000000",
                    "recentOrderNum": 320,
                    "recentExecuteRate": 97.5,
                }
            ]
        else:
            items = [
                {
                    "id": "bid_1",
                    "userId": "merchant_b",
                    "price": "1591.50",
                    "lastQuantity": "1800.0",
                    "minAmount": "50000",
                    "maxAmount": "1500000",
                    "recentOrderNum": 210,
                    "recentExecuteRate": 95.0,
                }
            ]
        return httpx.Response(200, json={"retCode": 0, "result": {"items": items}})

    cfg = BybitCollectorConfig(payment_methods=("14",))
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    snapshot = collector.fetch_orderbook("USDT", "NGN")

    assert len(snapshot.ads) == 2
    assert snapshot.ads[0].side == "SELL"
    assert snapshot.ads[0].price_fiat == 1608.50
    assert snapshot.ads[0].completion_rate == 0.975
    assert snapshot.ads[1].side == "BUY"
    assert snapshot.ads[1].price_fiat == 1591.50
    assert all(p["payment"] == ["14"] for p in captured_payloads)


def test_authenticated_orders_fetch_signs_headers_and_parses_orders() -> None:
    """Verify V5 HMAC-SHA256 headers and ExchangeOrder normalization."""
    captured_headers: dict[str, str] = {}
    captured_body: str = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers, captured_body
        captured_headers = dict(request.headers)
        captured_body = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "items": [
                        {
                            "id": "ord_9001",
                            "side": 1,
                            "status": 50,
                            "notifyTokenQuantity": "500.0",
                            "amount": "804000.0",
                            "price": "1608.00",
                            "fee": "0.0",
                            "targetUserId": "buyer_77",
                            "targetCompletedOrders": 95,
                            "targetCompletionRate": 98.0,
                        }
                    ]
                },
            },
        )

    cfg = BybitCollectorConfig(
        api_key="test_key",
        api_secret="test_secret",
        recv_window_ms="5000",
    )
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    orders = collector.fetch_orders("USDT", "NGN")

    # Verify HMAC-SHA256 signature matches Bybit V5 specification
    ts = captured_headers["x-bapi-timestamp"]
    expected_sig = hmac.new(
        b"test_secret",
        f"{ts}test_key5000{captured_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert captured_headers["x-bapi-sign"] == expected_sig
    assert captured_headers["x-bapi-api-key"] == "test_key"

    assert len(orders) == 1
    assert orders[0].order_id == "ord_9001"
    assert orders[0].side == "SELL"
    assert orders[0].status == "COMPLETED"
    assert orders[0].counterparty.completion_rate == 0.98


def test_dry_run_mode_intercepts_ad_mutations_without_network_calls() -> None:
    """Verify dry_run=True records ad mutations locally without calling Bybit."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"retCode": 0})

    cfg = BybitCollectorConfig(dry_run=True)
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))

    collector.update_ad_price("ad_ask", 1607.49)
    collector.set_ad_status("ad_ask", active=True)

    assert called is False
    assert len(collector.dry_run_actions) == 2
    assert collector.dry_run_actions[0]["action"] == "update_ad_price"


def test_nonzero_retcode_raises_bybit_api_error() -> None:
    """Verify Bybit retCode != 0 raises BybitAPIError for guardrail tracking."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"retCode": 10001, "retMsg": "Invalid parameter"}
        )

    collector = BybitP2PCollector(client=_mock_client(handler))
    with pytest.raises(BybitAPIError, match="Invalid parameter"):
        collector.fetch_orderbook("USDT", "NGN")
