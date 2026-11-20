from pathlib import Path

import pytest

from p2p_quant_engine.portfolio.ledger import LedgerConfig, PortfolioLedger


@pytest.fixture
def ledger(tmp_path: Path) -> PortfolioLedger:
    """Provide an isolated PortfolioLedger backed by a temporary JSON path."""
    state_file = tmp_path / "test_ledger.json"
    config = LedgerConfig(
        state_path=state_file,
        min_tradable_crypto=20.0,
        min_tradable_fiat=30_000.0,
    )
    return PortfolioLedger(config=config)


def test_independent_cycle_initialization_and_funding_fees(
    ledger: PortfolioLedger,
) -> None:
    """Verify Ask and Bid cycles initialize independently with bulk funding fees."""
    ask = ledger.start_ask_cycle(
        crypto_amount=10_000.0,
        acquisition_price_fiat=1595.00,
        funding_fee_fiat=1_000.0,  # Adds 0.10 NGN/USDT to cost basis -> 1595.10
    )
    bid = ledger.start_bid_cycle(
        fiat_amount=10_000_000.0,
        funding_fee_fiat=50.0,  # CBN EMTL bulk deposit levy
    )

    assert ask.cycle_id == "ASK-0001"
    assert ask.available_crypto == 10_000.0
    assert ask.avg_cost_basis_fiat == 1595.10
    assert ask.realized_net_pnl_fiat == -1_000.0

    assert bid.cycle_id == "BID-0001"
    assert bid.available_fiat == 10_000_000.0
    assert bid.funding_fee_fiat == 50.0
    assert not ledger.is_ask_cycle_exhausted()
    assert not ledger.is_bid_cycle_exhausted()


def test_sell_order_escrow_lock_and_net_pnl_settlement(
    ledger: PortfolioLedger,
) -> None:
    """Verify SELL order locks crypto, deducts fees on settlement, and logs net PnL."""
    ledger.start_ask_cycle(
        crypto_amount=10_000.0,
        acquisition_price_fiat=1595.00,
        funding_fee_fiat=0.0,
    )

    # Customer buys 1,000 USDT at 1608.00 NGN
    ledger.lock_order(
        "ord_sell_1", side="SELL", crypto_amount=1_000.0, price_fiat=1608.00
    )
    assert ledger.active_ask_cycle is not None
    assert ledger.active_ask_cycle.available_crypto == 9_000.0
    assert ledger.active_ask_cycle.locked_crypto == 1_000.0

    # Settle with 75 NGN incoming bank deposit fee + 804 NGN exchange fee
    fill = ledger.settle_order(
        "ord_sell_1", exchange_fee_fiat=804.0, bank_fee_fiat=75.0
    )

    # Gross spread = (1608.00 - 1595.00) * 1000 = 13,000 NGN
    # Net PnL = 13,000 - (804 + 75) = 12,121.00 NGN
    assert fill.realized_net_pnl_fiat == 12_121.00
    assert ledger.active_ask_cycle.locked_crypto == 0.0
    assert ledger.active_ask_cycle.realized_net_pnl_fiat == 12_121.00


def test_buy_order_updates_weighted_average_cost_basis(
    ledger: PortfolioLedger,
) -> None:
    """
    Verify BUY fills blend into active crypto pool via Weighted-Average Cost Basis.
    """
    ledger.start_ask_cycle(crypto_amount=1_000.0, acquisition_price_fiat=1600.00)
    ledger.start_bid_cycle(fiat_amount=5_000_000.0)

    # Buy 1,000 USDT at 1590.00 NGN with 100 NGN outgoing bank transfer fee
    # Effective buy unit cost = (1,590,000 + 100) / 1,000 = 1590.10 NGN
    ledger.lock_order(
        "ord_buy_1", side="BUY", crypto_amount=1_000.0, price_fiat=1590.00
    )
    ledger.settle_order("ord_buy_1", exchange_fee_fiat=0.0, bank_fee_fiat=100.0)

    assert ledger.active_ask_cycle is not None
    assert ledger.active_bid_cycle is not None
    # Blended WAC: ((1000 * 1600.00) + (1000 * 1590.10)) / 2000 = 1595.05
    assert ledger.active_ask_cycle.available_crypto == 2_000.0
    assert ledger.active_ask_cycle.avg_cost_basis_fiat == 1595.05
    assert ledger.active_bid_cycle.available_fiat == 3_410_000.0


def test_order_cancellation_restores_escrow_balance(ledger: PortfolioLedger) -> None:
    """Verify cancelling a locked order restores funds to available balance."""
    ledger.start_ask_cycle(crypto_amount=500.0, acquisition_price_fiat=1600.00)
    ledger.lock_order(
        "ord_cancel", side="SELL", crypto_amount=200.0, price_fiat=1608.00
    )
    ledger.cancel_order("ord_cancel")

    assert ledger.active_ask_cycle is not None
    assert ledger.active_ask_cycle.available_crypto == 500.0
    assert ledger.active_ask_cycle.locked_crypto == 0.0


def test_cycle_exhaustion_and_archival_on_replenishment(
    ledger: PortfolioLedger,
) -> None:
    """Verify exhaustion detection and historical archival when starting a new cycle."""
    ledger.start_ask_cycle(crypto_amount=100.0, acquisition_price_fiat=1600.00)
    ledger.lock_order("ord_1", side="SELL", crypto_amount=90.0, price_fiat=1608.00)

    # Not exhausted while 90 USDT is still locked in escrow
    assert not ledger.is_ask_cycle_exhausted()

    ledger.settle_order("ord_1", exchange_fee_fiat=0.0, bank_fee_fiat=75.0)
    # Now available = 10.0 (< 20.0 threshold) and locked = 0.0 -> exhausted!
    assert ledger.is_ask_cycle_exhausted()

    # Replenishing archives ASK-0001 and opens ASK-0002
    new_ask = ledger.start_ask_cycle(
        crypto_amount=5_000.0, acquisition_price_fiat=1602.00
    )
    assert new_ask.cycle_id == "ASK-0002"
    assert len(ledger.completed_ask_cycles) == 1
    assert ledger.completed_ask_cycles[0].cycle_id == "ASK-0001"
    assert ledger.completed_ask_cycles[0].closed_at is not None


def test_atomic_json_persistence_across_restarts(tmp_path: Path) -> None:
    """Verify state survives reload from disk cleanly."""
    state_file = tmp_path / "persisted_ledger.json"
    cfg = LedgerConfig(state_path=state_file)

    ledger_1 = PortfolioLedger(config=cfg)
    ledger_1.start_ask_cycle(crypto_amount=2_000.0, acquisition_price_fiat=1595.00)
    ledger_1.lock_order(
        "ord_persist", side="SELL", crypto_amount=500.0, price_fiat=1605.00
    )
    ledger_1.settle_order("ord_persist", exchange_fee_fiat=0.0, bank_fee_fiat=75.0)

    # Reload fresh instance from same JSON file
    ledger_2 = PortfolioLedger(config=cfg)
    assert ledger_2.active_ask_cycle is not None
    assert ledger_2.active_ask_cycle.cycle_id == "ASK-0001"
    assert ledger_2.active_ask_cycle.available_crypto == 1_500.0
    assert ledger_2.active_ask_cycle.realized_net_pnl_fiat == 4_925.00
    assert len(ledger_2.trade_history) == 1
