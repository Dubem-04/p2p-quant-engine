from pathlib import Path

import pytest

from p2p_quant_engine.execution.manager import (
    ExchangeOrder,
    ExecutionConfig,
    ExecutionManager,
    OrderBookSnapshot,
)
from p2p_quant_engine.portfolio.ledger import LedgerConfig, PortfolioLedger
from p2p_quant_engine.pricing.quoter import DynamicQuoter
from p2p_quant_engine.pricing.reference import P2PAd, ReferencePriceEstimator
from p2p_quant_engine.risk.account_rotator import AccountRotator, BankAccount
from p2p_quant_engine.risk.guardrails import (
    CounterpartyProfile,
    GuardrailConfig,
    RiskGuardrails,
)


class FakeExchangeClient:
    """In-memory stub simulating Bybit P2P order book and merchant ad endpoints."""

    def __init__(self) -> None:
        self.ads: list[P2PAd] = []
        self.orders: list[ExchangeOrder] = []
        self.fetched_at: float = 100.0
        self.updated_prices: dict[str, list[float]] = {}
        self.ad_statuses: dict[str, bool] = {}
        self.raise_on_fetch: bool = False

    def fetch_orderbook(
        self, crypto_asset: str, fiat_currency: str
    ) -> OrderBookSnapshot:
        if self.raise_on_fetch:
            raise ConnectionError("Bybit P2P timeout")
        return OrderBookSnapshot(ads=self.ads, fetched_at_monotonic=self.fetched_at)

    def fetch_orders(
        self, crypto_asset: str, fiat_currency: str
    ) -> list[ExchangeOrder]:
        return self.orders

    def update_ad_price(self, ad_id: str, new_price_fiat: float) -> None:
        self.updated_prices.setdefault(ad_id, []).append(new_price_fiat)

    def set_ad_status(self, ad_id: str, active: bool) -> None:
        self.ad_statuses[ad_id] = active


def _sample_book() -> list[P2PAd]:
    """Return balanced qualifying BUY/SELL P2P book around 1600.00 NGN."""
    return [
        P2PAd(
            ad_id="comp_ask",
            merchant_id="m_1",
            side="SELL",
            price_fiat=1607.50,
            available_crypto=2000.0,
            min_amount_fiat=50_000.0,
            max_amount_fiat=2_000_000.0,
            completed_orders=300,
            completion_rate=0.98,
        ),
        P2PAd(
            ad_id="comp_bid",
            merchant_id="m_2",
            side="BUY",
            price_fiat=1592.50,
            available_crypto=2000.0,
            min_amount_fiat=50_000.0,
            max_amount_fiat=2_000_000.0,
            completed_orders=300,
            completion_rate=0.98,
        ),
    ]


@pytest.fixture
def engine_bundle(
    tmp_path: Path,
) -> tuple[ExecutionManager, FakeExchangeClient, PortfolioLedger, AccountRotator]:
    """Assemble full ExecutionManager with isolated temporary state files."""
    exchange = FakeExchangeClient()
    exchange.ads = _sample_book()

    ledger = PortfolioLedger(config=LedgerConfig(state_path=tmp_path / "ledger.json"))
    ledger.start_ask_cycle(crypto_amount=10_000.0, acquisition_price_fiat=1595.00)
    ledger.start_bid_cycle(fiat_amount=10_000_000.0)

    rotator = AccountRotator(
        accounts=[
            BankAccount(
                account_id="gtb_1",
                bank_name="GTBank",
                account_number="0123456789",
                daily_volume_limit=5_000_000.0,
                daily_tx_limit=20,
            )
        ],
        state_file=tmp_path / "accounts.json",
    )

    guardrails = RiskGuardrails(
        config=GuardrailConfig(
            state_path=tmp_path / "risk.json",
            max_consecutive_api_errors=2,
        )
    )

    manager = ExecutionManager(
        exchange=exchange,
        estimator=ReferencePriceEstimator(),
        quoter=DynamicQuoter(),
        guardrails=guardrails,
        ledger=ledger,
        account_rotator=rotator,
        config=ExecutionConfig(
            ask_ad_id="my_ask_ad",
            bid_ad_id="my_bid_ad",
            min_reprice_delta_fiat=0.05,
        ),
    )
    return manager, exchange, ledger, rotator


def test_healthy_tick_activates_ads_and_respects_reprice_deadband(
    engine_bundle: tuple[
        ExecutionManager, FakeExchangeClient, PortfolioLedger, AccountRotator
    ],
) -> None:
    """
    Verify initial quotes post cleanly and tiny shifts inside deadband do not churn.
    """
    manager, exchange, _, _ = engine_bundle

    # Tick 1: posts Ask @ 1607.49 and Bid @ 1592.51
    t1 = manager.run_tick(now_monotonic=101.0)
    assert t1.market_allowed is True
    assert t1.ask_ad_active is True
    assert t1.bid_ad_active is True
    assert exchange.updated_prices["my_ask_ad"] == [1607.49]
    assert exchange.updated_prices["my_bid_ad"] == [1592.51]

    # Tick 2: competitor shifts by 0.02 NGN (below 0.05 deadband) -> no new API update
    exchange.ads[0] = P2PAd(
        ad_id="comp_ask",
        merchant_id="m_1",
        side="SELL",
        price_fiat=1607.48,
        available_crypto=2000.0,
        min_amount_fiat=50_000.0,
        max_amount_fiat=2_000_000.0,
        completed_orders=300,
        completion_rate=0.98,
    )
    exchange.fetched_at = 105.0
    manager.run_tick(now_monotonic=106.0)
    assert len(exchange.updated_prices["my_ask_ad"]) == 1


def test_underwater_wac_pauses_ask_ad_while_keeping_bid_ad_active(
    engine_bundle: tuple[
        ExecutionManager, FakeExchangeClient, PortfolioLedger, AccountRotator
    ],
) -> None:
    """Verify Ask ad pauses when market drops below WAC, while Bid keeps buying."""
    manager, exchange, ledger, _ = engine_bundle
    # Raise WAC to 1610.00 so quoted Ask (1607.49) violates strict capital preservation
    ledger.start_ask_cycle(crypto_amount=5_000.0, acquisition_price_fiat=1610.00)

    summary = manager.run_tick(now_monotonic=101.0)
    assert summary.market_allowed is True
    assert summary.ask_ad_active is False
    assert summary.bid_ad_active is True


def test_order_lifecycle_locks_quarantines_and_settles_with_bank_rotation(
    engine_bundle: tuple[
        ExecutionManager, FakeExchangeClient, PortfolioLedger, AccountRotator
    ],
) -> None:
    """Verify valid orders lock/settle with bank usage and risky orders quarantine."""
    manager, exchange, ledger, rotator = engine_bundle

    good_cp = CounterpartyProfile("u_good", 100, 0.98, 200, True, True)
    bad_cp = CounterpartyProfile("u_bad", 5, 0.50, 3, False, False)

    exchange.orders = [
        ExchangeOrder(
            order_id="ord_good",
            side="SELL",
            status="PENDING",
            crypto_amount=500.0,
            fiat_amount=803_745.0,
            price_fiat=1607.49,
            counterparty=good_cp,
            bank_fee_fiat=75.0,
        ),
        ExchangeOrder(
            order_id="ord_bad",
            side="SELL",
            status="PENDING",
            crypto_amount=200.0,
            fiat_amount=321_498.0,
            price_fiat=1607.49,
            counterparty=bad_cp,
        ),
    ]

    t1 = manager.run_tick(now_monotonic=101.0)
    assert t1.newly_locked_orders == ["ord_good"]
    assert t1.newly_flagged_orders == ["ord_bad"]
    assert "ord_bad" in manager.flagged_orders
    assert ledger.active_ask_cycle is not None
    assert ledger.active_ask_cycle.locked_crypto == 500.0

    # Complete ord_good on next tick
    exchange.orders[0] = ExchangeOrder(
        order_id="ord_good",
        side="SELL",
        status="COMPLETED",
        crypto_amount=500.0,
        fiat_amount=803_745.0,
        price_fiat=1607.49,
        counterparty=good_cp,
        bank_fee_fiat=75.0,
    )
    exchange.fetched_at = 105.0
    t2 = manager.run_tick(now_monotonic=106.0)

    assert t2.settled_orders == ["ord_good"]
    assert ledger.active_ask_cycle.locked_crypto == 0.0
    assert rotator.accounts["gtb_1"].current_volume == 803_745.0


def test_consecutive_api_failures_trip_kill_switch_and_pause_ads(
    engine_bundle: tuple[
        ExecutionManager, FakeExchangeClient, PortfolioLedger, AccountRotator
    ],
) -> None:
    """Verify repeated exchange timeouts pause ads and auto-trip the kill switch."""
    manager, exchange, _, _ = engine_bundle
    manager.run_tick(now_monotonic=101.0)  # Activate ads first

    exchange.raise_on_fetch = True
    manager.run_tick(now_monotonic=105.0)
    manager.run_tick(now_monotonic=110.0)

    assert manager.guardrails.kill_switch.tripped is True
    assert exchange.ad_statuses["my_ask_ad"] is False
    assert exchange.ad_statuses["my_bid_ad"] is False
