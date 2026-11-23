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

    cfg = BybitCollectorConfig(
        api_key="test_key",
        api_secret="test_secret",
        payment_methods=("14",),
    )
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

    cfg = BybitCollectorConfig(api_key="test_key", api_secret="test_secret")
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    with pytest.raises(BybitAPIError, match="Invalid parameter"):
        collector.fetch_orderbook("USDT", "NGN")


def test_online_ads_request_is_signed() -> None:
    """Bybit requires X-BAPI-* headers on /v5/p2p/item/online too."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"retCode": 0, "result": {"items": []}})

    cfg = BybitCollectorConfig(api_key="test_key", api_secret="test_secret")
    BybitP2PCollector(config=cfg, client=_mock_client(handler)).fetch_orderbook(
        "USDT", "NGN"
    )
    assert seen and all("x-bapi-sign" in h for h in seen)


def test_clock_skew_10002_resyncs_time_and_retries_once() -> None:
    """A 10002 triggers /v5/market/time sync, then one retry with corrected ts."""
    skew_ms = 10_000  # Bybit is 10s ahead of the local clock, as in the real log
    post_timestamps: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            import time as _t

            return httpx.Response(
                200, json={"retCode": 0, "time": int(_t.time() * 1000) + skew_ms}
            )
        ts = int(request.headers["x-bapi-timestamp"])
        post_timestamps.append(ts)
        import time as _t

        server_now = int(_t.time() * 1000) + skew_ms
        if server_now - ts > 5000:
            return httpx.Response(
                200, json={"retCode": 10002, "retMsg": "invalid request, timestamp"}
            )
        return httpx.Response(200, json={"retCode": 0, "result": {"items": []}})

    cfg = BybitCollectorConfig(api_key="k", api_secret="s", request_pacing_seconds=0.0)
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    collector._owns_client = True  # exercise live-network-only behaviour
    collector.fetch_orders("USDT", "NGN")

    assert len(post_timestamps) == 1  # initial sync already fixed the clock
    assert abs(collector._time_offset_ms - skew_ms) < 500


def test_error_carries_ret_code_and_hint() -> None:
    """10010 surfaces ret_code and an actionable IP-whitelist hint."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 10010, "retMsg": "Unmatched IP"})

    cfg = BybitCollectorConfig(api_key="k", api_secret="s")
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    with pytest.raises(BybitAPIError, match="whitelist") as info:
        collector.fetch_orders("USDT", "NGN")
    assert info.value.ret_code == 10010


def test_live_fetch_orderbook_gets_both_sides_in_one_call() -> None:
    """Live mode must fetch BUY and SELL sides on the same tick (no stagger)."""
    sides: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            import time as _t

            return httpx.Response(
                200, json={"retCode": 0, "time": int(_t.time() * 1000)}
            )
        sides.append(json.loads(request.content)["side"])
        return httpx.Response(200, json={"retCode": 0, "result": {"items": []}})

    cfg = BybitCollectorConfig(api_key="k", api_secret="s", request_pacing_seconds=0.0)
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    collector._owns_client = True
    collector.fetch_orderbook("USDT", "NGN")
    assert sorted(sides) == ["0", "1"]


def _live_collector(handler: object, **overrides: object) -> BybitP2PCollector:
    """Collector behaving like live mode (retries/cache on) but with no sleeps."""
    cfg = BybitCollectorConfig(
        api_key="k",
        api_secret="s",
        request_pacing_seconds=0.0,
        rate_limit_backoff_seconds=(0.0, 0.0, 0.0),
        **overrides,  # type: ignore[arg-type]
    )
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    collector._owns_client = True
    collector._time_synced = True  # skip /v5/market/time in these tests
    return collector


def test_10006_is_retried_with_backoff_then_succeeds() -> None:
    """Two throttled responses followed by success must not raise."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(
                200, json={"retCode": 10006, "retMsg": "Too many visits"}
            )
        return httpx.Response(200, json={"retCode": 0, "result": {"items": []}})

    _live_collector(handler).fetch_orders("USDT", "NGN")
    assert calls["n"] == 3


def test_10006_exhausted_retries_still_raises() -> None:
    """Persistent throttling must surface as BybitAPIError(ret_code=10006)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 10006, "retMsg": "Too many visits"})

    with pytest.raises(BybitAPIError) as info:
        _live_collector(handler).fetch_orders("USDT", "NGN")
    assert info.value.ret_code == 10006


def test_throttled_side_falls_back_to_recent_cache_and_reports_true_age() -> None:
    """
    A throttled refresh reuses the last good side; snapshot age is the oldest side.
    """
    state = {"throttle": False}
    item = {
        "id": "a1",
        "userId": "m1",
        "price": "1500",
        "lastQuantity": "100",
        "minAmount": "1000",
        "maxAmount": "100000",
        "recentOrderNum": 50,
        "recentExecuteRate": 99,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if state["throttle"]:
            return httpx.Response(
                200, json={"retCode": 10006, "retMsg": "Too many visits"}
            )
        return httpx.Response(200, json={"retCode": 0, "result": {"items": [item]}})

    collector = _live_collector(handler, orderbook_cache_ttl_seconds=0.0)
    first = collector.fetch_orderbook("USDT", "NGN")
    assert len(first.ads) == 2

    state["throttle"] = True
    second = collector.fetch_orderbook("USDT", "NGN")
    assert len(second.ads) == 2  # served from cache
    assert second.fetched_at_monotonic <= first.fetched_at_monotonic + 0.001


def test_auth_errors_are_never_masked_by_stale_cache() -> None:
    """10010 must raise even when a fresh cache exists."""
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(
                200, json={"retCode": 10010, "retMsg": "Unmatched IP"}
            )
        return httpx.Response(200, json={"retCode": 0, "result": {"items": []}})

    collector = _live_collector(handler, orderbook_cache_ttl_seconds=0.0)
    collector.fetch_orderbook("USDT", "NGN")
    state["fail"] = True
    with pytest.raises(BybitAPIError, match="whitelist"):
        collector.fetch_orderbook("USDT", "NGN")


def test_sign_error_message_never_echoes_api_key() -> None:
    """Bybit's 10004 message contains the API key inside origin_string; redact it."""
    leaked = '17954398884827ZdDXH9aPErrhbeOeY5000{"tokenId":"USDT","payment":["14"]}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "retCode": 10004,
                "retMsg": f"Error sign, please check origin_string[{leaked}]",
            },
        )

    cfg = BybitCollectorConfig(api_key="k", api_secret="s")
    collector = BybitP2PCollector(config=cfg, client=_mock_client(handler))
    with pytest.raises(BybitAPIError) as info:
        collector.fetch_orders("USDT", "NGN")
    assert "ZdDXH9aP" not in str(info.value)
    assert "REDACTED" in str(info.value)
    assert info.value.ret_code == 10004
