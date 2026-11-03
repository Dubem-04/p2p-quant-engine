from datetime import datetime, timezone

import pytest

from src.signals.friction_matrix import (
    CanonicalRail,
    CounterpartyRiskModel,
    FrictionCostEngine,
    PaymentRailCostMatrix,
    PaymentRailNormalizer,
)
from src.signals.spread_models import ArbitrageSignal, StrategyType


@pytest.fixture
def mock_signal():
    return ArbitrageSignal(
        strategy=StrategyType.TAKER_TAKER,
        asset="USDT",
        fiat="USD",
        timestamp_utc=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        buy_venue="binance",
        sell_venue="bybit",
        buy_price=1.00,
        sell_price=1.025,
        gross_spread_pct=2.5,
        net_spread_pct=2.5,
        target_fiat_volume=1000.0,
        executable_crypto=1000.0,
        common_payment_methods=["Bank Transfer"],
        metadata={"net_profit_fiat": 25.0},
    )


def test_payment_rail_normalizer():
    assert (
        PaymentRailNormalizer.normalize("Kuda Microfinance Bank") == CanonicalRail.KUDA
    )
    assert (
        PaymentRailNormalizer.normalize("OPay Digital Services") == CanonicalRail.OPAY
    )
    assert PaymentRailNormalizer.normalize("PalmPay App") == CanonicalRail.PALMPAY
    assert (
        PaymentRailNormalizer.normalize("Bank Transfer (Nigeria)")
        == CanonicalRail.BANK_TRANSFER_NGN
    )
    assert PaymentRailNormalizer.normalize("Revolut Pay") == CanonicalRail.REVOLUT
    assert PaymentRailNormalizer.normalize("Zelle QuickPay") == CanonicalRail.ZELLE
    assert PaymentRailNormalizer.normalize("Wise Transfer") == CanonicalRail.WISE
    assert PaymentRailNormalizer.normalize("Chipper") == CanonicalRail.CHIPPER_CASH
    assert (
        PaymentRailNormalizer.normalize("Random Foreign Wire")
        == CanonicalRail.GENERIC_BANK
    )
    assert PaymentRailNormalizer.normalize("") == CanonicalRail.UNKNOWN


def test_payment_rail_cost_matrix():
    matrix = PaymentRailCostMatrix()

    # Intra-rail Kuda transfer (same rail: fixed fee = 10.0, 0% variable)
    fee, duration = matrix.calculate_transfer_friction(
        source=CanonicalRail.KUDA,
        target=CanonicalRail.KUDA,
        amount_fiat=100_000.0,
    )
    assert fee == 10.0
    assert duration == 3.0

    # Cross-rail transfer without explicit override: Kuda to Generic Bank
    # Base fee (10) + default inter-bank surcharge (50 + 0.001 * 100_000 = 150) = 160.0
    cross_fee, cross_duration = matrix.calculate_transfer_friction(
        source=CanonicalRail.KUDA,
        target=CanonicalRail.GENERIC_BANK,
        amount_fiat=100_000.0,
    )
    assert cross_fee == 160.0
    assert cross_duration == 25.0

    # Pairwise override
    matrix.set_pairwise_cost(
        CanonicalRail.KUDA,
        CanonicalRail.OPAY,
        fixed_fee=15.0,
        variable_fee_pct=0.0,
        extra_lag_minutes=1.0,
    )
    ov_fee, ov_duration = matrix.calculate_transfer_friction(
        source=CanonicalRail.KUDA,
        target=CanonicalRail.OPAY,
        amount_fiat=50_000.0,
    )
    # Base fee (10) + override (15) = 25
    assert ov_fee == 25.0
    assert ov_duration == 4.0


def test_counterparty_risk_model():
    risk_model = CounterpartyRiskModel(
        target_completion_rate=0.98, min_orders_benchmark=100
    )

    # Perfect counterparty: 0.99 rate, 300 orders, pro merchant
    hc_perfect = risk_model.compute_haircut_pct(
        completion_rate=0.99, completed_orders=300, is_pro=True
    )
    assert hc_perfect == 0.0

    # Weak counterparty: 0.90 rate (0.08 deficit -> 0.16 haircut), 50 orders (0.5 deficit -> 0.125 haircut), non-pro
    hc_weak = risk_model.compute_haircut_pct(
        completion_rate=0.90, completed_orders=50, is_pro=False
    )
    assert hc_weak > 0.0
    assert hc_weak == pytest.approx(0.16 + 0.125)


def test_friction_cost_engine_acceptance(mock_signal):
    engine = FrictionCostEngine(min_realizable_roi_pct=0.50)

    # Nominal profit = $25 on $1000 volume (2.5%)
    # Same-rail Revolut: $0 fixed, $0 variable
    # Perfect merchants: $0 haircut
    adjusted = engine.evaluate_signal(
        signal=mock_signal,
        source_rail_name="Revolut",
        target_rail_name="Revolut",
        buy_merchant_completion_rate=0.99,
        buy_merchant_completed_orders=500,
        buy_merchant_is_pro=True,
    )

    assert adjusted.is_actionable is True
    assert adjusted.rail_friction_fee_fiat == 0.0
    assert adjusted.risk_haircut_fiat == 0.0
    assert adjusted.net_realizable_profit_fiat == 25.0
    assert adjusted.net_realizable_roi_pct == pytest.approx(2.5)


def test_friction_cost_engine_ticket_limit_rejection(mock_signal):
    engine = FrictionCostEngine()

    # Zelle has a max single ticket limit of $2,000
    mock_signal.target_fiat_volume = 3000.0
    mock_signal.metadata["net_profit_fiat"] = 75.0

    adjusted = engine.evaluate_signal(
        signal=mock_signal,
        source_rail_name="Zelle",
        target_rail_name="Zelle",
    )

    assert adjusted.is_actionable is False
    assert "exceeds single ticket limit" in adjusted.rejection_reason


def test_friction_cost_engine_roi_hurdle_rejection(mock_signal):
    engine = FrictionCostEngine(min_realizable_roi_pct=1.0)

    # Nominal profit = $10 on $1000 volume (1.0% gross)
    # Generic bank fee = $25 + 0.05% ($0.50) = $25.50
    # Net profit is negative -> rejected
    mock_signal.metadata["net_profit_fiat"] = 10.0

    adjusted = engine.evaluate_signal(
        signal=mock_signal,
        source_rail_name="Generic Bank Wire",
        target_rail_name="Generic Bank Wire",
    )

    assert adjusted.is_actionable is False
    assert adjusted.net_realizable_profit_fiat < 0.0
    assert "below hurdle threshold" in adjusted.rejection_reason
