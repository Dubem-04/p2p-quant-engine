from pathlib import Path

import pytest

from p2p_quant_engine.risk.guardrails import (
    CounterpartyProfile,
    GuardrailConfig,
    IncomingOrderContext,
    RiskGuardrails,
)


@pytest.fixture
def guardrails(tmp_path: Path) -> RiskGuardrails:
    """Provide isolated RiskGuardrails backed by a temporary JSON state file."""
    cfg = GuardrailConfig(
        state_path=tmp_path / "risk_state.json",
        volatility_window_seconds=180.0,
        max_price_velocity_pct=0.015,
        max_external_divergence_pct=0.03,
        max_orderbook_staleness_seconds=15.0,
        min_wac_profit_buffer_fiat=1.0,
        max_consecutive_api_errors=3,
    )
    return RiskGuardrails(config=cfg)


def _valid_counterparty(**overrides: object) -> CounterpartyProfile:
    """Build a valid CounterpartyProfile with optional field overrides."""
    base: dict[str, object] = {
        "user_id": "user_123",
        "completed_orders": 120,
        "completion_rate": 0.96,
        "account_age_days": 180,
        "kyc_verified": True,
        "name_matches_bank": True,
    }
    base.update(overrides)
    return CounterpartyProfile(**base)  # type: ignore[arg-type]


def test_healthy_market_quote_allowed(guardrails: RiskGuardrails) -> None:
    """Verify stable fair value and fresh snapshot pass market quote checks."""
    d1 = guardrails.evaluate_market_quote(
        fair_value_fiat=1600.00,
        snapshot_age_seconds=2.0,
        external_divergence_pct=0.01,
        timestamp_sec=100.0,
    )
    d2 = guardrails.evaluate_market_quote(
        fair_value_fiat=1604.00,  # +0.25% move within 1.5% limit
        snapshot_age_seconds=1.5,
        external_divergence_pct=0.012,
        timestamp_sec=130.0,
    )

    assert d1.allowed is True
    assert d2.allowed is True


def test_excessive_price_velocity_blocks_quoting(guardrails: RiskGuardrails) -> None:
    """Verify rapid FX spikes within the rolling window trigger a volatility pause."""
    guardrails.evaluate_market_quote(
        fair_value_fiat=1600.00, snapshot_age_seconds=1.0, timestamp_sec=100.0
    )
    # 2.5% jump in 60 seconds (> 1.5% limit)
    decision = guardrails.evaluate_market_quote(
        fair_value_fiat=1640.00, snapshot_age_seconds=1.0, timestamp_sec=160.0
    )

    assert decision.allowed is False
    assert any("velocity" in r.lower() for r in decision.reasons)


def test_stale_snapshot_and_external_divergence_blocked(
    guardrails: RiskGuardrails,
) -> None:
    """Verify stale order books and extreme external divergences are rejected."""
    decision = guardrails.evaluate_market_quote(
        fair_value_fiat=1600.00,
        snapshot_age_seconds=22.0,  # > 15s
        external_divergence_pct=0.045,  # > 3.0%
        timestamp_sec=100.0,
    )

    assert decision.allowed is False
    assert len(decision.reasons) == 2


def test_strict_capital_preservation_blocks_underwater_ask(
    guardrails: RiskGuardrails,
) -> None:
    """Verify Ask quotes below WAC + buffer are blocked to preserve capital."""
    # WAC = 1600.00, min buffer = 1.00 -> min allowed Ask = 1601.00
    blocked = guardrails.evaluate_ask_quote(
        proposed_ask_fiat=1600.50, avg_cost_basis_fiat=1600.00
    )
    allowed = guardrails.evaluate_ask_quote(
        proposed_ask_fiat=1602.00, avg_cost_basis_fiat=1600.00
    )

    assert blocked.allowed is False
    assert "Strict capital preservation" in blocked.reasons[0]
    assert allowed.allowed is True


def test_incoming_order_screening_catches_fraud_and_limit_violations(
    guardrails: RiskGuardrails,
) -> None:
    """
    Verify third-party bank names, low completion rates, and headroom breaches fail.
    """
    bad_cp = _valid_counterparty(
        completed_orders=5,
        completion_rate=0.60,
        account_age_days=4,
        kyc_verified=False,
        name_matches_bank=False,
    )
    order = IncomingOrderContext(
        order_id="ord_bad",
        side="SELL",
        fiat_amount=600_000.0,
        crypto_amount=375.0,
        price_fiat=1600.0,
        counterparty=bad_cp,
        available_bank_headroom_fiat=400_000.0,  # Exceeded by 600k order
    )

    decision = guardrails.evaluate_incoming_order(order)
    assert decision.allowed is False
    assert len(decision.reasons) == 6


def test_kill_switch_auto_trip_and_atomic_persistence(tmp_path: Path) -> None:
    """
    Verify consecutive API errors auto-trip kill switch and persist across reloads.
    """
    state_file = tmp_path / "persisted_risk.json"
    cfg = GuardrailConfig(state_path=state_file, max_consecutive_api_errors=2)

    g1 = RiskGuardrails(config=cfg)
    g1.record_api_error("Timeout 1")
    assert g1.kill_switch.tripped is False

    g1.record_api_error("Timeout 2")
    assert g1.kill_switch.tripped is True

    # Reload fresh instance from disk
    g2 = RiskGuardrails(config=cfg)
    assert g2.kill_switch.tripped is True
    assert "Timeout 2" in (g2.kill_switch.reason or "")

    decision = g2.evaluate_market_quote(
        fair_value_fiat=1600.0, snapshot_age_seconds=1.0
    )
    assert decision.allowed is False

    g2.reset_kill_switch()
    assert g2.kill_switch.tripped is False
