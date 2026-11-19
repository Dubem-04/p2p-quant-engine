import pytest

from p2p_quant_engine.pricing.quoter import DynamicQuoter
from p2p_quant_engine.pricing.reference import (
    FilterConfig,
    P2PAd,
    ReferencePriceEstimator,
)


def _make_ad(
    ad_id: str,
    side: str,
    price: float,
    available: float = 1000.0,
    merchant_id: str = "m_valid",
    min_fiat: float = 50_000.0,
    max_fiat: float = 1_000_000.0,
    orders: int = 200,
    rate: float = 0.96,
) -> P2PAd:
    """Helper factory to build valid P2PAd test fixtures concisely."""
    return P2PAd(
        ad_id=ad_id,
        merchant_id=merchant_id,
        side=side,  # type: ignore[arg-type]
        price_fiat=price,
        available_crypto=available,
        min_amount_fiat=min_fiat,
        max_amount_fiat=max_fiat,
        completed_orders=orders,
        completion_rate=rate,
    )


def test_vwap_mid_fair_value_calculation() -> None:
    """Verify Liquidity-Weighted Mid-Price across top qualifying bids and asks."""
    ads = [
        # Qualifying Asks (SELL ads)
        _make_ad("a1", "SELL", price=1607.50, available=200.0),
        _make_ad("a2", "SELL", price=1608.00, available=3000.0),
        _make_ad("a3", "SELL", price=1609.00, available=1800.0),
        # Qualifying Bids (BUY ads)
        _make_ad("b1", "BUY", price=1593.00, available=150.0),
        _make_ad("b2", "BUY", price=1592.50, available=2500.0),
        _make_ad("b3", "BUY", price=1591.00, available=2350.0),
    ]

    estimator = ReferencePriceEstimator()
    ref = estimator.estimate(ads)

    assert ref.vwap_ask_fiat == 1608.34
    assert ref.vwap_bid_fiat == 1591.81
    assert ref.fair_value_fiat == 1600.075
    assert ref.best_competitor_ask == 1607.50
    assert ref.best_competitor_bid == 1593.00
    assert ref.qualifying_ask_count == 3
    assert ref.qualifying_bid_count == 3


def test_filters_out_bait_unreliable_wholesale_and_own_ads() -> None:
    """
    Verify dust ads, low-completion accounts, wholesale tiers, and self are dropped.
    """
    ads = [
        # Junk / excluded Ask ads that would otherwise distort top-of-book
        _make_ad("bait_ask", "SELL", price=1595.00, max_fiat=5_000.0),  # Dust limit
        _make_ad("flaky_ask", "SELL", price=1598.00, rate=0.72),  # Low completion
        _make_ad("newbie_ask", "SELL", price=1599.00, orders=12),  # Too few orders
        _make_ad("whale_ask", "SELL", price=1601.00, min_fiat=5_000_000.0),  # Wholesale
        _make_ad("tiny_liq", "SELL", price=1602.00, available=10.0),  # Low liquidity
        _make_ad("own_ask", "SELL", price=1605.00, merchant_id="my_merchant_id"),
        # Valid Ask ad
        _make_ad("valid_ask", "SELL", price=1608.00, available=1500.0),
        # Junk / excluded Bid ads
        _make_ad("own_bid", "BUY", price=1596.00, merchant_id="my_merchant_id"),
        # Valid Bid ad
        _make_ad("valid_bid", "BUY", price=1592.00, available=1500.0),
    ]

    config = FilterConfig(excluded_merchant_ids=frozenset({"my_merchant_id"}))
    estimator = ReferencePriceEstimator(filter_config=config)
    ref = estimator.estimate(ads)

    assert ref.qualifying_ask_count == 1
    assert ref.qualifying_bid_count == 1
    assert ref.best_competitor_ask == 1608.00
    assert ref.best_competitor_bid == 1592.00
    assert ref.fair_value_fiat == 1600.00


def test_respects_top_n_ads_cap() -> None:
    """Verify only the top N ranked ads per side enter the VWAP calculation."""
    ads = [
        _make_ad("a1", "SELL", price=1606.00, available=1000.0),
        _make_ad("a2", "SELL", price=1608.00, available=1000.0),
        # 3rd ask has huge liquidity at 1650, should be excluded when top_n_ads=2
        _make_ad("a3", "SELL", price=1650.00, available=50_000.0),
        _make_ad("b1", "BUY", price=1594.00, available=1000.0),
        _make_ad("b2", "BUY", price=1592.00, available=1000.0),
        _make_ad("b3", "BUY", price=1550.00, available=50_000.0),
    ]

    estimator = ReferencePriceEstimator(filter_config=FilterConfig(top_n_ads=2))
    ref = estimator.estimate(ads)

    assert ref.vwap_ask_fiat == 1607.00
    assert ref.vwap_bid_fiat == 1593.00
    assert ref.fair_value_fiat == 1600.00


def test_external_reference_divergence_tracking() -> None:
    """
    Verify optional external benchmark divergence percentage is computed accurately.
    """
    ads = [
        _make_ad("a1", "SELL", price=1608.00, available=1000.0),
        _make_ad("b1", "BUY", price=1592.00, available=1000.0),
    ]

    estimator = ReferencePriceEstimator()
    # Fair value = 1600.00 vs external 1568.00 -> |1600 - 1568| / 1568 = 0.020408
    ref = estimator.estimate(ads, external_reference_fiat=1568.00)

    assert ref.external_divergence_pct == pytest.approx(0.020408, rel=1e-4)


def test_end_to_end_integration_with_dynamic_quoter() -> None:
    """Verify MarketReference outputs plug directly into DynamicQuoter."""
    ads = [
        _make_ad("a1", "SELL", price=1607.50, available=2000.0),
        _make_ad("b1", "BUY", price=1592.50, available=2000.0),
    ]

    ref = ReferencePriceEstimator().estimate(ads)
    quote = DynamicQuoter().generate_quotes(
        fair_value_fiat=ref.fair_value_fiat,
        best_competitor_bid=ref.best_competitor_bid,
        best_competitor_ask=ref.best_competitor_ask,
    )

    assert ref.fair_value_fiat == 1600.00
    assert quote.bid_price == 1592.51
    assert quote.ask_price == 1607.49


def test_raises_when_one_side_lacks_qualifying_liquidity() -> None:
    """Verify ValueError is raised if either BUY or SELL side has zero valid ads."""
    ads = [_make_ad("only_ask", "SELL", price=1608.00, available=1000.0)]

    with pytest.raises(ValueError, match="Insufficient qualifying liquidity"):
        ReferencePriceEstimator().estimate(ads)
