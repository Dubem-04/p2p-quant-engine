from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional, Protocol

from p2p_quant_engine.execution.config import (
    DEFAULT_CRYPTO_ASSET,
    DEFAULT_FIAT_CURRENCY,
    DEFAULT_POLL_INTERVAL_SECONDS,
    MIN_REPRICE_DELTA_FIAT,
)
from p2p_quant_engine.portfolio.ledger import PortfolioLedger
from p2p_quant_engine.pricing.quoter import DynamicQuoter
from p2p_quant_engine.pricing.reference import P2PAd, ReferencePriceEstimator
from p2p_quant_engine.risk.account_rotator import AccountRotator
from p2p_quant_engine.risk.guardrails import (
    CounterpartyProfile,
    IncomingOrderContext,
    RiskGuardrails,
)

OrderStatus = Literal["PENDING", "PAID", "COMPLETED", "CANCELLED"]


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Point-in-time P2P order book returned by the collector/exchange client."""

    ads: list[P2PAd]
    fetched_at_monotonic: float
    external_reference_fiat: Optional[float] = None


@dataclass(frozen=True)
class ExchangeOrder:
    """Normalized live P2P order fetched from the exchange."""

    order_id: str
    side: Literal["BUY", "SELL"]
    status: OrderStatus
    crypto_amount: float
    fiat_amount: float
    price_fiat: float
    counterparty: CounterpartyProfile
    exchange_fee_fiat: float = 0.0
    bank_fee_fiat: float = 0.0


@dataclass
class ManagedAdState:
    """Tracks live price and active status of one merchant P2P advertisement."""

    ad_id: str
    side: Literal["BUY", "SELL"]
    posted_price_fiat: Optional[float] = None
    is_active: bool = False
    pause_reason: Optional[str] = None


@dataclass
class FlaggedOrderAlert:
    """Quarantined incoming order requiring manual operator review."""

    order_id: str
    side: Literal["BUY", "SELL"]
    fiat_amount: float
    crypto_amount: float
    reasons: list[str]
    flagged_at: str = field(default_factory=_utc_now_iso)


@dataclass
class TickSummary:
    """Diagnostic summary returned after each execution loop iteration."""

    market_allowed: bool
    fair_value_fiat: Optional[float] = None
    quoted_bid_fiat: Optional[float] = None
    quoted_ask_fiat: Optional[float] = None
    ask_ad_active: bool = False
    bid_ad_active: bool = False
    newly_locked_orders: list[str] = field(default_factory=list)
    settled_orders: list[str] = field(default_factory=list)
    cancelled_orders: list[str] = field(default_factory=list)
    newly_flagged_orders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ExchangeClient(Protocol):
    """Interface implemented by the Bybit P2P collector/adapter."""

    def fetch_orderbook(
        self, crypto_asset: str, fiat_currency: str
    ) -> OrderBookSnapshot:
        """Fetch live BUY and SELL ads for the configured trading pair."""

    def fetch_orders(
        self, crypto_asset: str, fiat_currency: str
    ) -> list[ExchangeOrder]:
        """Fetch active and recently settled/cancelled P2P orders."""

    def update_ad_price(self, ad_id: str, new_price_fiat: float) -> None:
        """Update the posted fiat price of a merchant advertisement."""

    def set_ad_status(self, ad_id: str, active: bool) -> None:
        """Enable (online) or pause (offline) a merchant advertisement."""


@dataclass(frozen=True)
class ExecutionConfig:
    """Runtime settings for ExecutionManager."""

    crypto_asset: str = DEFAULT_CRYPTO_ASSET
    fiat_currency: str = DEFAULT_FIAT_CURRENCY
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    min_reprice_delta_fiat: float = MIN_REPRICE_DELTA_FIAT
    ask_ad_id: Optional[str] = None  # Merchant ad selling crypto for fiat
    bid_ad_id: Optional[str] = None  # Merchant ad buying crypto with fiat


class ExecutionManager:
    """
    Central P2P market-making orchestrator connecting order book collection,
    fair value estimation, quoting, risk guardrails, bank rotation, and ledgering.
    """

    def __init__(
        self,
        exchange: ExchangeClient,
        estimator: ReferencePriceEstimator,
        quoter: DynamicQuoter,
        guardrails: RiskGuardrails,
        ledger: PortfolioLedger,
        account_rotator: AccountRotator,
        config: Optional[ExecutionConfig] = None,
    ) -> None:
        self.exchange = exchange
        self.estimator = estimator
        self.quoter = quoter
        self.guardrails = guardrails
        self.ledger = ledger
        self.account_rotator = account_rotator
        self.config = config or ExecutionConfig()

        self.ask_ad = (
            ManagedAdState(ad_id=self.config.ask_ad_id, side="SELL")
            if self.config.ask_ad_id
            else None
        )
        self.bid_ad = (
            ManagedAdState(ad_id=self.config.bid_ad_id, side="BUY")
            if self.config.bid_ad_id
            else None
        )
        self.order_bank_assignments: dict[str, str] = {}
        self.flagged_orders: dict[str, FlaggedOrderAlert] = {}

    def _pause_ad(self, ad: Optional[ManagedAdState], reason: str) -> None:
        """Take an advertisement offline if currently active."""
        if ad is None:
            return
        if ad.is_active:
            self.exchange.set_ad_status(ad.ad_id, active=False)
            ad.is_active = False
        ad.pause_reason = reason

    def _sync_ad_quote(self, ad: Optional[ManagedAdState], target_price: float) -> None:
        """Activate and reprice an advertisement only if delta exceeds deadband."""
        if ad is None:
            return

        needs_activation = not ad.is_active
        needs_reprice = (
            ad.posted_price_fiat is None
            or abs(target_price - ad.posted_price_fiat)
            >= self.config.min_reprice_delta_fiat
        )

        if needs_reprice:
            self.exchange.update_ad_price(ad.ad_id, target_price)
            ad.posted_price_fiat = target_price

        if needs_activation:
            self.exchange.set_ad_status(ad.ad_id, active=True)
            ad.is_active = True
            ad.pause_reason = None

    def _process_orders(
        self, orders: list[ExchangeOrder], summary: TickSummary
    ) -> None:
        """Reconcile incoming, completed, and cancelled orders with ledger & banks."""
        for order in orders:
            # 1. Handle terminal states for already-locked orders
            if order.status == "CANCELLED":
                if order.order_id in self.ledger.active_orders:
                    self.ledger.cancel_order(order.order_id)
                    self.order_bank_assignments.pop(order.order_id, None)
                    summary.cancelled_orders.append(order.order_id)
                continue

            if order.status == "COMPLETED":
                if order.order_id in self.ledger.active_orders:
                    self.ledger.settle_order(
                        order_id=order.order_id,
                        exchange_fee_fiat=order.exchange_fee_fiat,
                        bank_fee_fiat=order.bank_fee_fiat,
                    )
                    assigned_acc = self.order_bank_assignments.pop(order.order_id, None)
                    if assigned_acc is not None:
                        self.account_rotator.record_transaction(
                            assigned_acc, order.fiat_amount
                        )
                    summary.settled_orders.append(order.order_id)
                continue

            # 2. Screen and lock new PENDING / PAID orders
            if (
                order.order_id in self.ledger.active_orders
                or order.order_id in self.flagged_orders
            ):
                continue

            selected_account = self.account_rotator.get_available_account(
                order.fiat_amount
            )
            headroom = (
                selected_account.remaining_volume
                if selected_account is not None
                else 0.0
            )

            ctx = IncomingOrderContext(
                order_id=order.order_id,
                side=order.side,
                fiat_amount=order.fiat_amount,
                crypto_amount=order.crypto_amount,
                price_fiat=order.price_fiat,
                counterparty=order.counterparty,
                available_bank_headroom_fiat=headroom,
            )
            decision = self.guardrails.evaluate_incoming_order(ctx)

            if not decision.allowed or selected_account is None:
                reasons = list(decision.reasons)
                if selected_account is None:
                    reasons.append("No bank account has sufficient daily headroom.")
                self.flagged_orders[order.order_id] = FlaggedOrderAlert(
                    order_id=order.order_id,
                    side=order.side,
                    fiat_amount=order.fiat_amount,
                    crypto_amount=order.crypto_amount,
                    reasons=reasons,
                )
                summary.newly_flagged_orders.append(order.order_id)
                continue

            # Safe order -> lock escrow in ledger & bind bank account
            self.ledger.lock_order(
                order_id=order.order_id,
                side=order.side,
                crypto_amount=order.crypto_amount,
                price_fiat=order.price_fiat,
            )
            self.order_bank_assignments[order.order_id] = selected_account.account_id
            summary.newly_locked_orders.append(order.order_id)

    def run_tick(self, now_monotonic: float) -> TickSummary:
        """Execute one complete polling, quoting, risk-gating, and order-sync cycle."""
        summary = TickSummary(market_allowed=False)

        # 1. Fetch order book and live orders with API health tracking
        try:
            snapshot = self.exchange.fetch_orderbook(
                self.config.crypto_asset, self.config.fiat_currency
            )
            orders = self.exchange.fetch_orders(
                self.config.crypto_asset, self.config.fiat_currency
            )
            self.guardrails.record_api_success()
        except Exception as exc:  # noqa: BLE001
            self.guardrails.record_api_error(str(exc))
            self._pause_ad(self.ask_ad, "Exchange API error")
            self._pause_ad(self.bid_ad, "Exchange API error")
            summary.notes.append(f"API fetch failed: {exc}")
            return summary

        # 2. Reconcile active/incoming orders first so inventory balances are current
        self._process_orders(orders, summary)

        # 3. Estimate fair value and validate market-wide risk guardrails
        try:
            market_ref = self.estimator.estimate(
                snapshot.ads,
                external_reference_fiat=snapshot.external_reference_fiat,
            )
        except ValueError as exc:
            self._pause_ad(self.ask_ad, "Insufficient qualifying order book liquidity")
            self._pause_ad(self.bid_ad, "Insufficient qualifying order book liquidity")
            summary.notes.append(str(exc))
            return summary

        summary.fair_value_fiat = market_ref.fair_value_fiat
        snapshot_age = max(0.0, now_monotonic - snapshot.fetched_at_monotonic)

        market_gate = self.guardrails.evaluate_market_quote(
            fair_value_fiat=market_ref.fair_value_fiat,
            snapshot_age_seconds=snapshot_age,
            external_divergence_pct=market_ref.external_divergence_pct,
            timestamp_sec=now_monotonic,
        )

        if not market_gate.allowed:
            reason_str = "; ".join(market_gate.reasons)
            self._pause_ad(self.ask_ad, reason_str)
            self._pause_ad(self.bid_ad, reason_str)
            summary.notes.extend(market_gate.reasons)
            return summary

        summary.market_allowed = True

        # 4. Generate optimal competitive quotes
        quote = self.quoter.generate_quotes(
            fair_value_fiat=market_ref.fair_value_fiat,
            best_competitor_bid=market_ref.best_competitor_bid,
            best_competitor_ask=market_ref.best_competitor_ask,
        )
        summary.quoted_bid_fiat = quote.bid_price
        summary.quoted_ask_fiat = quote.ask_price

        # 5. Manage Ask Ad (Sell Crypto) independently
        if self.ledger.is_ask_cycle_exhausted() or self.ledger.active_ask_cycle is None:
            self._pause_ad(self.ask_ad, "Ask cycle exhausted")
        else:
            ask_gate = self.guardrails.evaluate_ask_quote(
                proposed_ask_fiat=quote.ask_price,
                avg_cost_basis_fiat=self.ledger.active_ask_cycle.avg_cost_basis_fiat,
            )
            if not ask_gate.allowed:
                self._pause_ad(self.ask_ad, "; ".join(ask_gate.reasons))
                summary.notes.extend(ask_gate.reasons)
            else:
                self._sync_ad_quote(self.ask_ad, quote.ask_price)

        # 6. Manage Bid Ad (Buy Crypto) independently
        if self.ledger.is_bid_cycle_exhausted() or self.ledger.active_bid_cycle is None:
            self._pause_ad(self.bid_ad, "Bid cycle exhausted")
        else:
            self._sync_ad_quote(self.bid_ad, quote.bid_price)

        summary.ask_ad_active = bool(self.ask_ad and self.ask_ad.is_active)
        summary.bid_ad_active = bool(self.bid_ad and self.bid_ad.is_active)
        return summary
