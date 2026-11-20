import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from p2p_quant_engine.portfolio.config import (
    CRYPTO_DECIMAL_PLACES,
    DEFAULT_LEDGER_STATE_PATH,
    FIAT_DECIMAL_PLACES,
    MIN_TRADABLE_CRYPTO_THRESHOLD,
    MIN_TRADABLE_FIAT_THRESHOLD,
)

TradeSide = Literal["BUY", "SELL"]


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ActiveEscrowOrder:
    """Tracks funds locked in an open, unsettled P2P order."""

    order_id: str
    side: TradeSide  # "SELL" locks crypto; "BUY" commits fiat
    crypto_amount: float
    fiat_amount: float
    price_fiat: float
    created_at: str = field(default_factory=_utc_now_iso)


@dataclass
class TradeFillRecord:
    """Immutable audit record of a completed P2P trade fill and its fees."""

    order_id: str
    cycle_id: str
    side: TradeSide
    crypto_amount: float
    fiat_amount: float
    price_fiat: float
    exchange_fee_fiat: float
    bank_fee_fiat: float
    cost_basis_fiat_per_crypto: float
    realized_net_pnl_fiat: float
    settled_at: str = field(default_factory=_utc_now_iso)


@dataclass
class AskCycleState:
    """State of the active crypto-selling (Ask) cycle."""

    cycle_id: str
    initial_crypto: float
    available_crypto: float
    locked_crypto: float
    avg_cost_basis_fiat: float
    funding_fee_fiat: float = 0.0
    total_crypto_sold: float = 0.0
    total_fiat_received: float = 0.0
    total_fees_fiat: float = 0.0
    realized_net_pnl_fiat: float = 0.0
    trade_count: int = 0
    started_at: str = field(default_factory=_utc_now_iso)
    closed_at: Optional[str] = None


@dataclass
class BidCycleState:
    """State of the active crypto-buying (Bid) cycle."""

    cycle_id: str
    initial_fiat: float
    available_fiat: float
    locked_fiat: float
    funding_fee_fiat: float = 0.0
    total_fiat_spent: float = 0.0
    total_crypto_acquired: float = 0.0
    total_fees_fiat: float = 0.0
    trade_count: int = 0
    started_at: str = field(default_factory=_utc_now_iso)
    closed_at: Optional[str] = None


@dataclass
class LedgerConfig:
    """Configurable thresholds and storage path for PortfolioLedger."""

    state_path: Path = DEFAULT_LEDGER_STATE_PATH
    min_tradable_crypto: float = MIN_TRADABLE_CRYPTO_THRESHOLD
    min_tradable_fiat: float = MIN_TRADABLE_FIAT_THRESHOLD


class PortfolioLedger:
    """
    Crash-safe accounting ledger managing independent Ask and Bid trading cycles,
    escrow locks, weighted-average cost basis, and realized net PnL.
    """

    def __init__(self, config: Optional[LedgerConfig] = None) -> None:
        self.config = config or LedgerConfig()
        self.ask_sequence: int = 0
        self.bid_sequence: int = 0
        self.active_ask_cycle: Optional[AskCycleState] = None
        self.active_bid_cycle: Optional[BidCycleState] = None
        self.active_orders: dict[str, ActiveEscrowOrder] = {}
        self.completed_ask_cycles: list[AskCycleState] = []
        self.completed_bid_cycles: list[BidCycleState] = []
        self.trade_history: list[TradeFillRecord] = []

        if self.config.state_path.exists():
            self._load_state()

    # --- Persistence (Atomic JSON) ---

    def _save_state(self) -> None:
        """Persist ledger state atomically via temporary file replacement."""
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config.state_path.with_suffix(".tmp")

        payload: dict[str, Any] = {
            "ask_sequence": self.ask_sequence,
            "bid_sequence": self.bid_sequence,
            "active_ask_cycle": (
                asdict(self.active_ask_cycle) if self.active_ask_cycle else None
            ),
            "active_bid_cycle": (
                asdict(self.active_bid_cycle) if self.active_bid_cycle else None
            ),
            "active_orders": {
                oid: asdict(order) for oid, order in self.active_orders.items()
            },
            "completed_ask_cycles": [asdict(c) for c in self.completed_ask_cycles],
            "completed_bid_cycles": [asdict(c) for c in self.completed_bid_cycles],
            "trade_history": [asdict(t) for t in self.trade_history],
        }

        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        os.replace(tmp_path, self.config.state_path)

    def _load_state(self) -> None:
        """Load persisted ledger state from JSON."""
        with self.config.state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        self.ask_sequence = data.get("ask_sequence", 0)
        self.bid_sequence = data.get("bid_sequence", 0)

        ask_data = data.get("active_ask_cycle")
        self.active_ask_cycle = AskCycleState(**ask_data) if ask_data else None

        bid_data = data.get("active_bid_cycle")
        self.active_bid_cycle = BidCycleState(**bid_data) if bid_data else None

        self.active_orders = {
            oid: ActiveEscrowOrder(**odata)
            for oid, odata in data.get("active_orders", {}).items()
        }
        self.completed_ask_cycles = [
            AskCycleState(**c) for c in data.get("completed_ask_cycles", [])
        ]
        self.completed_bid_cycles = [
            BidCycleState(**c) for c in data.get("completed_bid_cycles", [])
        ]
        self.trade_history = [
            TradeFillRecord(**t) for t in data.get("trade_history", [])
        ]

    # --- Cycle Management ---

    def start_ask_cycle(
        self,
        crypto_amount: float,
        acquisition_price_fiat: float,
        funding_fee_fiat: float = 0.0,
    ) -> AskCycleState:
        """
        Archive any existing Ask cycle and open a new Ask (sell crypto) cycle.

        Args:
            crypto_amount: Newly funded crypto quantity (e.g., 10,000 USDT).
            acquisition_price_fiat: Unit cost paid to source this crypto batch.
            funding_fee_fiat: Total bulk sourcing/deposit fee paid to fund the batch.
        """
        if crypto_amount <= 0 or acquisition_price_fiat <= 0:
            raise ValueError(
                "crypto_amount and acquisition_price_fiat must be positive."
            )
        if funding_fee_fiat < 0:
            raise ValueError("funding_fee_fiat cannot be negative.")
        if self.active_ask_cycle and self.active_ask_cycle.locked_crypto > 0:
            raise RuntimeError(
                "Cannot close active Ask cycle while crypto remains locked in escrow."
            )

        if self.active_ask_cycle is not None:
            self.active_ask_cycle.closed_at = _utc_now_iso()
            self.completed_ask_cycles.append(self.active_ask_cycle)

        self.ask_sequence += 1
        effective_unit_cost = (
            (crypto_amount * acquisition_price_fiat) + funding_fee_fiat
        ) / crypto_amount

        self.active_ask_cycle = AskCycleState(
            cycle_id=f"ASK-{self.ask_sequence:04d}",
            initial_crypto=round(crypto_amount, CRYPTO_DECIMAL_PLACES),
            available_crypto=round(crypto_amount, CRYPTO_DECIMAL_PLACES),
            locked_crypto=0.0,
            avg_cost_basis_fiat=round(effective_unit_cost, 4),
            funding_fee_fiat=round(funding_fee_fiat, FIAT_DECIMAL_PLACES),
            total_fees_fiat=round(funding_fee_fiat, FIAT_DECIMAL_PLACES),
            realized_net_pnl_fiat=round(-funding_fee_fiat, FIAT_DECIMAL_PLACES),
        )
        self._save_state()
        return self.active_ask_cycle

    def start_bid_cycle(
        self,
        fiat_amount: float,
        funding_fee_fiat: float = 0.0,
    ) -> BidCycleState:
        """
        Archive any existing Bid cycle and open a new Bid (buy crypto) cycle.

        Args:
            fiat_amount: Newly funded fiat capital (e.g., ₦10,000,000).
            funding_fee_fiat: Bulk bank deposit fee incurred (e.g., ₦50 CBN EMTL).
        """
        if fiat_amount <= 0:
            raise ValueError("fiat_amount must be positive.")
        if funding_fee_fiat < 0:
            raise ValueError("funding_fee_fiat cannot be negative.")
        if self.active_bid_cycle and self.active_bid_cycle.locked_fiat > 0:
            raise RuntimeError(
                "Cannot close active Bid cycle while fiat remains locked in orders."
            )

        if self.active_bid_cycle is not None:
            self.active_bid_cycle.closed_at = _utc_now_iso()
            self.completed_bid_cycles.append(self.active_bid_cycle)

        self.bid_sequence += 1
        self.active_bid_cycle = BidCycleState(
            cycle_id=f"BID-{self.bid_sequence:04d}",
            initial_fiat=round(fiat_amount, FIAT_DECIMAL_PLACES),
            available_fiat=round(fiat_amount, FIAT_DECIMAL_PLACES),
            locked_fiat=0.0,
            funding_fee_fiat=round(funding_fee_fiat, FIAT_DECIMAL_PLACES),
            total_fees_fiat=round(funding_fee_fiat, FIAT_DECIMAL_PLACES),
        )
        self._save_state()
        return self.active_bid_cycle

    def is_ask_cycle_exhausted(self) -> bool:
        """Return True if Ask cycle has no pending escrow and is below min crypto."""
        if self.active_ask_cycle is None:
            return True
        return (
            self.active_ask_cycle.locked_crypto == 0
            and self.active_ask_cycle.available_crypto < self.config.min_tradable_crypto
        )

    def is_bid_cycle_exhausted(self) -> bool:
        """Return True if Bid cycle has no pending orders and is below min fiat."""
        if self.active_bid_cycle is None:
            return True
        return (
            self.active_bid_cycle.locked_fiat == 0
            and self.active_bid_cycle.available_fiat < self.config.min_tradable_fiat
        )

    # --- Escrow & Order Settlement ---

    def lock_order(
        self,
        order_id: str,
        side: TradeSide,
        crypto_amount: float,
        price_fiat: float,
    ) -> ActiveEscrowOrder:
        """Reserve available crypto (SELL) or fiat (BUY) into active escrow."""
        if order_id in self.active_orders:
            raise ValueError(f"Order {order_id} is already locked.")
        if crypto_amount <= 0 or price_fiat <= 0:
            raise ValueError("crypto_amount and price_fiat must be positive.")

        fiat_amount = round(crypto_amount * price_fiat, FIAT_DECIMAL_PLACES)

        if side == "SELL":
            if self.active_ask_cycle is None:
                raise RuntimeError("No active Ask cycle to lock crypto from.")
            if crypto_amount > self.active_ask_cycle.available_crypto:
                raise ValueError("Insufficient available crypto in active Ask cycle.")
            self.active_ask_cycle.available_crypto = round(
                self.active_ask_cycle.available_crypto - crypto_amount,
                CRYPTO_DECIMAL_PLACES,
            )
            self.active_ask_cycle.locked_crypto = round(
                self.active_ask_cycle.locked_crypto + crypto_amount,
                CRYPTO_DECIMAL_PLACES,
            )
        elif side == "BUY":
            if self.active_bid_cycle is None:
                raise RuntimeError("No active Bid cycle to lock fiat from.")
            if fiat_amount > self.active_bid_cycle.available_fiat:
                raise ValueError("Insufficient available fiat in active Bid cycle.")
            self.active_bid_cycle.available_fiat = round(
                self.active_bid_cycle.available_fiat - fiat_amount,
                FIAT_DECIMAL_PLACES,
            )
            self.active_bid_cycle.locked_fiat = round(
                self.active_bid_cycle.locked_fiat + fiat_amount,
                FIAT_DECIMAL_PLACES,
            )
        else:
            raise ValueError(f"Unsupported trade side: {side}")

        order = ActiveEscrowOrder(
            order_id=order_id,
            side=side,
            crypto_amount=round(crypto_amount, CRYPTO_DECIMAL_PLACES),
            fiat_amount=fiat_amount,
            price_fiat=price_fiat,
        )
        self.active_orders[order_id] = order
        self._save_state()
        return order

    def cancel_order(self, order_id: str) -> None:
        """
        Release locked escrow funds back to available balance on order cancellation.
        """
        order = self.active_orders.pop(order_id, None)
        if order is None:
            raise KeyError(f"Active order {order_id} not found.")

        if order.side == "SELL" and self.active_ask_cycle is not None:
            self.active_ask_cycle.locked_crypto = round(
                self.active_ask_cycle.locked_crypto - order.crypto_amount,
                CRYPTO_DECIMAL_PLACES,
            )
            self.active_ask_cycle.available_crypto = round(
                self.active_ask_cycle.available_crypto + order.crypto_amount,
                CRYPTO_DECIMAL_PLACES,
            )
        elif order.side == "BUY" and self.active_bid_cycle is not None:
            self.active_bid_cycle.locked_fiat = round(
                self.active_bid_cycle.locked_fiat - order.fiat_amount,
                FIAT_DECIMAL_PLACES,
            )
            self.active_bid_cycle.available_fiat = round(
                self.active_bid_cycle.available_fiat + order.fiat_amount,
                FIAT_DECIMAL_PLACES,
            )

        self._save_state()

    def settle_order(
        self,
        order_id: str,
        exchange_fee_fiat: float = 0.0,
        bank_fee_fiat: float = 0.0,
    ) -> TradeFillRecord:
        """
        Finalize a locked order, record exact fees, update WAC, and compute net PnL.
        """
        if exchange_fee_fiat < 0 or bank_fee_fiat < 0:
            raise ValueError("Fees cannot be negative.")

        order = self.active_orders.pop(order_id, None)
        if order is None:
            raise KeyError(f"Active order {order_id} not found.")

        total_order_fees = exchange_fee_fiat + bank_fee_fiat

        if order.side == "SELL":
            if self.active_ask_cycle is None:
                raise RuntimeError("Active Ask cycle missing during SELL settlement.")

            cycle = self.active_ask_cycle
            cycle.locked_crypto = round(
                cycle.locked_crypto - order.crypto_amount, CRYPTO_DECIMAL_PLACES
            )
            cycle.total_crypto_sold = round(
                cycle.total_crypto_sold + order.crypto_amount, CRYPTO_DECIMAL_PLACES
            )
            cycle.total_fiat_received = round(
                cycle.total_fiat_received + order.fiat_amount, FIAT_DECIMAL_PLACES
            )
            cycle.total_fees_fiat = round(
                cycle.total_fees_fiat + total_order_fees, FIAT_DECIMAL_PLACES
            )

            # Realized Net PnL = (Sell Price - WAC) * Qty - Order Fees
            gross_spread_pnl = (
                order.price_fiat - cycle.avg_cost_basis_fiat
            ) * order.crypto_amount
            net_pnl = round(gross_spread_pnl - total_order_fees, FIAT_DECIMAL_PLACES)

            cycle.realized_net_pnl_fiat = round(
                cycle.realized_net_pnl_fiat + net_pnl, FIAT_DECIMAL_PLACES
            )
            cycle.trade_count += 1

            fill = TradeFillRecord(
                order_id=order.order_id,
                cycle_id=cycle.cycle_id,
                side="SELL",
                crypto_amount=order.crypto_amount,
                fiat_amount=order.fiat_amount,
                price_fiat=order.price_fiat,
                exchange_fee_fiat=round(exchange_fee_fiat, FIAT_DECIMAL_PLACES),
                bank_fee_fiat=round(bank_fee_fiat, FIAT_DECIMAL_PLACES),
                cost_basis_fiat_per_crypto=cycle.avg_cost_basis_fiat,
                realized_net_pnl_fiat=net_pnl,
            )

        else:  # BUY side
            if self.active_bid_cycle is None:
                raise RuntimeError("Active Bid cycle missing during BUY settlement.")

            bid_cycle = self.active_bid_cycle
            bid_cycle.locked_fiat = round(
                bid_cycle.locked_fiat - order.fiat_amount, FIAT_DECIMAL_PLACES
            )
            bid_cycle.total_fiat_spent = round(
                bid_cycle.total_fiat_spent + order.fiat_amount, FIAT_DECIMAL_PLACES
            )
            bid_cycle.total_crypto_acquired = round(
                bid_cycle.total_crypto_acquired + order.crypto_amount,
                CRYPTO_DECIMAL_PLACES,
            )
            bid_cycle.total_fees_fiat = round(
                bid_cycle.total_fees_fiat + total_order_fees, FIAT_DECIMAL_PLACES
            )
            bid_cycle.trade_count += 1

            # Effective unit cost of acquired crypto includes order fees
            effective_buy_unit_cost = (
                order.fiat_amount + total_order_fees
            ) / order.crypto_amount

            # Update Weighted-Average Cost Basis if an Ask cycle is currently active
            if self.active_ask_cycle is not None:
                ask_cycle = self.active_ask_cycle
                existing_pool = ask_cycle.available_crypto + ask_cycle.locked_crypto
                new_total_pool = existing_pool + order.crypto_amount
                if new_total_pool > 0:
                    blended_wac = (
                        (existing_pool * ask_cycle.avg_cost_basis_fiat)
                        + (order.crypto_amount * effective_buy_unit_cost)
                    ) / new_total_pool
                    ask_cycle.avg_cost_basis_fiat = round(blended_wac, 4)
                ask_cycle.available_crypto = round(
                    ask_cycle.available_crypto + order.crypto_amount,
                    CRYPTO_DECIMAL_PLACES,
                )

            fill = TradeFillRecord(
                order_id=order.order_id,
                cycle_id=bid_cycle.cycle_id,
                side="BUY",
                crypto_amount=order.crypto_amount,
                fiat_amount=order.fiat_amount,
                price_fiat=order.price_fiat,
                exchange_fee_fiat=round(exchange_fee_fiat, FIAT_DECIMAL_PLACES),
                bank_fee_fiat=round(bank_fee_fiat, FIAT_DECIMAL_PLACES),
                cost_basis_fiat_per_crypto=round(effective_buy_unit_cost, 4),
                realized_net_pnl_fiat=round(-total_order_fees, FIAT_DECIMAL_PLACES),
            )

        self.trade_history.append(fill)
        self._save_state()
        return fill
