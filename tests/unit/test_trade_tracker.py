from datetime import datetime, timedelta, timezone

import pytest

from src.execution.trade_tracker import LiveTradeTracker, TradeStatus
from src.signals.evaluator import CapitalLedger
from src.signals.friction_matrix import AdjustedArbitrageSignal, CanonicalRail
from src.signals.spread_models import ArbitrageSignal, StrategyType


@pytest.fixture
def mock_signal():
    base = ArbitrageSignal(
        strategy=StrategyType.TAKER_TAKER,
        asset="USDT",
        fiat="USD",
        timestamp_utc=datetime(2026, 11, 3, 17, 0, 0, tzinfo=timezone.utc),
        buy_venue="binance",
        sell_venue="bybit",
        buy_price=1.0000,
        sell_price=1.0300,
        gross_spread_pct=3.0,
        net_spread_pct=3.0,
        target_fiat_volume=1000.0,
        executable_crypto=1000.0,
        common_payment_methods=["Revolut"],
        metadata={"net_profit_fiat": 30.0},
    )
    return AdjustedArbitrageSignal(
        base_signal=base,
        source_rail=CanonicalRail.REVOLUT,
        target_rail=CanonicalRail.REVOLUT,
        nominal_profit_fiat=30.0,
        rail_friction_fee_fiat=0.0,
        risk_haircut_fiat=0.0,
        net_realizable_profit_fiat=30.0,
        net_realizable_roi_pct=3.0,
        is_actionable=True,
        expected_settlement_minutes=5.0,
    )


@pytest.fixture
def ledger():
    return CapitalLedger(
        initial_fiat=5000.0,
        initial_crypto=4000.0,
        venues=("binance", "bybit"),
        default_rail=CanonicalRail.REVOLUT,
    )


def test_trade_lifecycle_happy_path(mock_signal, ledger):
    tracker = LiveTradeTracker(ledger=ledger)
    t0 = datetime(2026, 11, 3, 17, 0, 0, tzinfo=timezone.utc)

    # 1. Register Signal
    record = tracker.register_signal(mock_signal, current_time=t0)
    assert record is not None
    assert record.status == TradeStatus.DETECTED
    trade_id = record.trade_id

    # Initial ledger balances
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 5000.0
    assert ledger.crypto_balances["bybit"] == 2000.0
    assert ledger.crypto_balances["binance"] == 2000.0

    # 2. Claim Trade -> Locks $1000 fiat and 1000 USDT on Bybit
    claimed = tracker.claim_trade(trade_id, current_time=t0 + timedelta(seconds=10))
    assert claimed is True
    assert record.status == TradeStatus.CLAIMED
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 4000.0
    assert ledger.crypto_balances["bybit"] == 1000.0

    # 3. Mark Fiat Sent
    fiat_sent = tracker.mark_fiat_sent(
        trade_id, current_time=t0 + timedelta(seconds=30)
    )
    assert fiat_sent is True
    assert record.status == TradeStatus.FIAT_SENT

    # 4. Mark Settled -> Principal + profit returned, crypto credited on Binance
    settled = tracker.mark_settled(trade_id, current_time=t0 + timedelta(minutes=2))
    assert settled is True
    assert record.status == TradeStatus.COMPLETED
    assert trade_id not in tracker.active_trades
    assert trade_id in tracker.historical_trades

    # Verify ledger updated with +$30 profit and crypto re-balanced
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 5030.0
    assert ledger.crypto_balances["bybit"] == 1000.0
    assert ledger.crypto_balances["binance"] == 3000.0


def test_trade_cancellation_restores_locked_capital(mock_signal, ledger):
    tracker = LiveTradeTracker(ledger=ledger)
    t0 = datetime(2026, 11, 3, 17, 0, 0, tzinfo=timezone.utc)

    record = tracker.register_signal(mock_signal, current_time=t0)
    tracker.claim_trade(record.trade_id, current_time=t0)

    # Balances deducted
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 4000.0
    assert ledger.crypto_balances["bybit"] == 1000.0

    # Cancel trade
    cancelled = tracker.cancel_trade(
        record.trade_id,
        reason="Counterparty cancelled",
        current_time=t0 + timedelta(minutes=1),
    )
    assert cancelled is True
    assert record.status == TradeStatus.CANCELLED
    assert record.cancellation_reason == "Counterparty cancelled"

    # Locked capital fully restored with 0 loss
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 5000.0
    assert ledger.crypto_balances["bybit"] == 2000.0


def test_insufficient_capital_claim_rejection(mock_signal, ledger):
    # Set signal target higher than available fiat
    mock_signal.base_signal.target_fiat_volume = 10_000.0
    tracker = LiveTradeTracker(ledger=ledger)

    record = tracker.register_signal(mock_signal)
    claimed = tracker.claim_trade(record.trade_id)

    assert claimed is False
    assert record.status == TradeStatus.DETECTED
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 5000.0


def test_expire_stale_detected_signals(mock_signal, ledger):
    tracker = LiveTradeTracker(ledger=ledger, detection_ttl_seconds=60.0)
    t0 = datetime(2026, 11, 3, 17, 0, 0, tzinfo=timezone.utc)

    record = tracker.register_signal(mock_signal, current_time=t0)
    assert record.trade_id in tracker.active_trades

    # Check after 30s -> not expired
    expired_count = tracker.expire_stale_detected(
        current_time=t0 + timedelta(seconds=30)
    )
    assert expired_count == 0
    assert record.trade_id in tracker.active_trades

    # Check after 70s -> expired and moved to historical
    expired_count = tracker.expire_stale_detected(
        current_time=t0 + timedelta(seconds=70)
    )
    assert expired_count == 1
    assert record.trade_id not in tracker.active_trades
    assert record.trade_id in tracker.historical_trades
    assert record.status == TradeStatus.EXPIRED
