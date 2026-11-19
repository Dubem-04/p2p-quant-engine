import pytest

from p2p_quant_engine.pricing.quoter import DynamicQuoter, FeeConfig, QuoterConfig


@pytest.fixture
def default_quoter() -> DynamicQuoter:
    """Quoter initialized with default Bybit/CBN fee headroom and spread parameters."""
    return DynamicQuoter()


def test_baseline_quotes_without_competitors(default_quoter: DynamicQuoter) -> None:
    """Verify baseline bid/ask quotes, fee buffers, and spread without competitors."""
    # Fair value = 1600.00 NGN, default trade size = 100 crypto
    # Ask buffer: (1600 * 0.0005) + (1600 * 0.0010) + (75 / 100) + 3.00 = 6.15
    #   -> ask_floor = 1606.15
    # Bid buffer: (1600 * 0.0005) + (100 / 100) + 3.00 = 4.80
    #   -> bid_ceiling = 1595.20
    # Base half spread = 8.00 -> raw_bid = 1592.00, raw_ask = 1608.00
    result = default_quoter.generate_quotes(fair_value_fiat=1600.00)

    assert result.ask_floor == 1606.15
    assert result.bid_ceiling == 1595.20
    assert result.bid_price == 1592.00
    assert result.ask_price == 1608.00
    assert result.spread_fiat == 16.00


def test_competitive_undercutting_within_profitable_bounds(
    default_quoter: DynamicQuoter,
) -> None:
    """Verify quoter steps 1 tick ahead of competitors within profit bounds."""
    result = default_quoter.generate_quotes(
        fair_value_fiat=1600.00,
        best_competitor_bid=1594.00,  # Inside [1592.00, 1595.20] -> bid 1594.01
        best_competitor_ask=1607.50,  # Inside [1606.15, 1608.00] -> ask 1607.49
    )

    assert result.bid_price == 1594.01
    assert result.ask_price == 1607.49
    assert result.spread_fiat == 13.48


def test_competitive_clamping_at_profit_floor_and_ceiling(
    default_quoter: DynamicQuoter,
) -> None:
    """Verify quoter refuses to follow competitors past ask_floor or bid_ceiling."""
    result = default_quoter.generate_quotes(
        fair_value_fiat=1600.00,
        best_competitor_bid=1598.50,  # Above bid_ceiling (1595.20) -> clamp 1595.20
        best_competitor_ask=1603.00,  # Below ask_floor (1606.15) -> clamp 1606.15
    )

    assert result.bid_price == 1595.20
    assert result.ask_price == 1606.15
    assert result.spread_fiat == 10.95


def test_ignores_uncompetitive_outer_quotes(default_quoter: DynamicQuoter) -> None:
    """Verify quoter keeps baseline quote when competitors sit outside base spread."""
    result = default_quoter.generate_quotes(
        fair_value_fiat=1600.00,
        best_competitor_bid=1588.00,  # Worse than raw_bid (1592.00) -> keep 1592.00
        best_competitor_ask=1615.00,  # Worse than raw_ask (1608.00) -> keep 1608.00
    )

    assert result.bid_price == 1592.00
    assert result.ask_price == 1608.00


def test_small_trade_amortizes_flat_bank_fees_widely(
    default_quoter: DynamicQuoter,
) -> None:
    """Verify smaller trade sizes widen profit boundaries to cover flat bank fees."""
    # Trade size = 10 crypto:
    # Deposit fee/unit = 75 / 10 = 7.50 -> ask_floor = 1600 + 6.15 + 6.75 = 1612.90
    # Transfer fee/unit = 100 / 10 = 10.00 -> bid_ceiling = 1600 - 4.80 - 9.00 = 1586.20
    # Since raw_ask (1608.00) < ask_floor (1612.90), ask clamps up to 1612.90.
    # Since raw_bid (1592.00) > bid_ceiling (1586.20), bid clamps down to 1586.20.
    result = default_quoter.generate_quotes(
        fair_value_fiat=1600.00,
        trade_size_crypto=10.0,
    )

    assert result.ask_floor == 1612.90
    assert result.bid_ceiling == 1586.20
    assert result.ask_price == 1612.90
    assert result.bid_price == 1586.20


def test_custom_fee_and_quoter_configs() -> None:
    """Verify quoter respects custom asset/exchange configurations."""
    custom_fees = FeeConfig(
        maker_fee_rate=0.001,
        taker_fee_rate=0.001,
        bulk_acquisition_fee_rate=0.002,
        fiat_deposit_fee=50.0,
        fiat_transfer_fee=50.0,
    )
    custom_config = QuoterConfig(
        base_half_spread_fiat=10.0,
        min_net_profit_fiat=5.0,
        tick_size_fiat=0.05,
        reference_trade_crypto=50.0,
    )
    quoter = DynamicQuoter(fee_config=custom_fees, quoter_config=custom_config)
    result = quoter.generate_quotes(
        fair_value_fiat=1000.00, best_competitor_ask=1009.50
    )

    # Ask buffer: (1000*0.001) + (1000*0.002) + (50/50) + 5.0 = 9.0 -> floor = 1009.00
    # Competitor ask 1009.50 - 0.05 tick = 1009.45 (safely >= 1009.00 floor)
    assert result.ask_floor == 1009.00
    assert result.ask_price == 1009.45


@pytest.mark.parametrize("invalid_price", [0.0, -100.0])
def test_invalid_fair_value_raises_value_error(
    default_quoter: DynamicQuoter, invalid_price: float
) -> None:
    """Verify non-positive fair values are rejected."""
    with pytest.raises(ValueError, match="fair_value_fiat must be positive"):
        default_quoter.generate_quotes(fair_value_fiat=invalid_price)


@pytest.mark.parametrize("invalid_size", [0.0, -5.0])
def test_invalid_trade_size_raises_value_error(
    default_quoter: DynamicQuoter, invalid_size: float
) -> None:
    """Verify non-positive trade sizes are rejected."""
    with pytest.raises(ValueError, match="trade_size_crypto must be positive"):
        default_quoter.generate_quotes(
            fair_value_fiat=1600.00, trade_size_crypto=invalid_size
        )
