"""
Interactive Trade Lifecycle State Machine & Capital Ledger Synchronizer.

Coordinates operational P2P trade lifecycle transitions (DETECTED -> CLAIMED ->
FIAT_SENT -> SETTLED -> COMPLETED) and keeps multi-venue fiat and crypto
capital ledgers locked and balanced in real time.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from src.signals.evaluator import CapitalLedger
from src.signals.friction_matrix import AdjustedArbitrageSignal
from src.signals.spread_models import StrategyType

logger = logging.getLogger(__name__)


class TradeStatus(str, Enum):
    DETECTED = "DETECTED"
    CLAIMED = "CLAIMED"
    FIAT_SENT = "FIAT_SENT"
    SETTLED = "SETTLED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# Valid transition state graph
VALID_TRANSITIONS: Dict[TradeStatus, List[TradeStatus]] = {
    TradeStatus.DETECTED: [
        TradeStatus.CLAIMED,
        TradeStatus.EXPIRED,
        TradeStatus.CANCELLED,
    ],
    TradeStatus.CLAIMED: [TradeStatus.FIAT_SENT, TradeStatus.CANCELLED],
    TradeStatus.FIAT_SENT: [TradeStatus.SETTLED, TradeStatus.CANCELLED],
    TradeStatus.SETTLED: [TradeStatus.COMPLETED],
    TradeStatus.COMPLETED: [],
    TradeStatus.CANCELLED: [],
    TradeStatus.EXPIRED: [],
}


@dataclass
class TradeRecord:
    """Operational trade entity tracked through execution."""

    trade_id: str
    signal: AdjustedArbitrageSignal
    status: TradeStatus
    detected_at: datetime
    claimed_at: Optional[datetime] = None
    fiat_sent_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    realized_profit_fiat: Optional[float] = None
    locked_fiat: float = 0.0
    locked_crypto: float = 0.0
    notes: List[str] = field(default_factory=list)


class LiveTradeTracker:
    """
    State machine supervisor managing trade lifecycles and CapitalLedger synchronization.
    """

    def __init__(
        self,
        ledger: CapitalLedger,
        detection_ttl_seconds: float = 120.0,
    ):
        self.ledger = ledger
        self.detection_ttl_seconds = detection_ttl_seconds
        self.active_trades: Dict[str, TradeRecord] = {}
        self.historical_trades: Dict[str, TradeRecord] = {}

    def register_signal(
        self,
        signal: AdjustedArbitrageSignal,
        current_time: Optional[datetime] = None,
    ) -> Optional[TradeRecord]:
        """Registers a newly detected signal into the execution tracker."""
        if not signal.is_actionable:
            return None

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        trade_id = f"trd_{uuid.uuid4().hex[:10]}"
        record = TradeRecord(
            trade_id=trade_id,
            signal=signal,
            status=TradeStatus.DETECTED,
            detected_at=now,
        )
        self.active_trades[trade_id] = record
        return record

    def claim_trade(
        self,
        trade_id: str,
        current_time: Optional[datetime] = None,
    ) -> bool:
        """
        Transitions trade to CLAIMED and reserves capital in the ledger.
        """
        record = self.active_trades.get(trade_id)
        if not record or record.status != TradeStatus.DETECTED:
            return False

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        base = record.signal.base_signal
        rail = record.signal.source_rail
        fiat_amount = base.target_fiat_volume
        crypto_amount = base.executable_crypto

        # Verify and lock capital in ledger
        avail_fiat = self.ledger.fiat_balances.get(rail, 0.0)
        if avail_fiat < fiat_amount:
            logger.warning(
                "Insufficient fiat on rail %s to claim trade %s", rail.value, trade_id
            )
            return False

        if base.strategy == StrategyType.TAKER_TAKER:
            avail_crypto = self.ledger.crypto_balances.get(base.sell_venue.lower(), 0.0)
            if avail_crypto < crypto_amount:
                logger.warning(
                    "Insufficient crypto on venue %s to claim trade %s",
                    base.sell_venue,
                    trade_id,
                )
                return False

            # Lock fiat and sell-leg crypto
            self.ledger.fiat_balances[rail] -= fiat_amount
            self.ledger.crypto_balances[base.sell_venue.lower()] -= crypto_amount
            record.locked_fiat = fiat_amount
            record.locked_crypto = crypto_amount

        elif base.strategy == StrategyType.DUAL_MAKER:
            self.ledger.fiat_balances[rail] -= fiat_amount
            record.locked_fiat = fiat_amount

        record.status = TradeStatus.CLAIMED
        record.claimed_at = now
        return True

    def mark_fiat_sent(
        self,
        trade_id: str,
        current_time: Optional[datetime] = None,
    ) -> bool:
        """Transitions trade to FIAT_SENT once bank transfer is initiated."""
        record = self.active_trades.get(trade_id)
        if not record or record.status != TradeStatus.CLAIMED:
            return False

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        record.status = TradeStatus.FIAT_SENT
        record.fiat_sent_at = now
        return True

    def mark_settled(
        self,
        trade_id: str,
        realized_profit_fiat: Optional[float] = None,
        current_time: Optional[datetime] = None,
    ) -> bool:
        """
        Transitions trade to SETTLED, releases capital, and deposits profit into ledger.
        """
        record = self.active_trades.get(trade_id)
        if not record or record.status != TradeStatus.FIAT_SENT:
            return False

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        base = record.signal.base_signal
        rail = record.signal.source_rail
        net_profit = (
            realized_profit_fiat
            if realized_profit_fiat is not None
            else record.signal.net_realizable_profit_fiat
        )

        # Deposit returned principal + profit back to ledger
        if base.strategy == StrategyType.TAKER_TAKER:
            # Principal fiat returns to rail plus net profit
            self.ledger.fiat_balances[rail] = (
                self.ledger.fiat_balances.get(rail, 0.0)
                + record.locked_fiat
                + net_profit
            )
            # Acquired crypto is now fully credited to buy venue
            self.ledger.crypto_balances[base.buy_venue.lower()] = (
                self.ledger.crypto_balances.get(base.buy_venue.lower(), 0.0)
                + base.executable_crypto
            )

        elif base.strategy == StrategyType.DUAL_MAKER:
            self.ledger.fiat_balances[rail] = (
                self.ledger.fiat_balances.get(rail, 0.0)
                + record.locked_fiat
                + net_profit
            )

        record.locked_fiat = 0.0
        record.locked_crypto = 0.0
        record.realized_profit_fiat = net_profit
        record.status = TradeStatus.SETTLED
        record.settled_at = now

        # Automatically complete and archive
        record.status = TradeStatus.COMPLETED
        record.completed_at = now
        self.historical_trades[trade_id] = record
        del self.active_trades[trade_id]
        return True

    def cancel_trade(
        self,
        trade_id: str,
        reason: str = "User cancelled",
        current_time: Optional[datetime] = None,
    ) -> bool:
        """
        Cancels trade and returns any locked capital back to available balances.
        """
        record = self.active_trades.get(trade_id)
        if not record or record.status in (
            TradeStatus.COMPLETED,
            TradeStatus.CANCELLED,
            TradeStatus.EXPIRED,
        ):
            return False

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        base = record.signal.base_signal
        rail = record.signal.source_rail

        # Restore locked capital to ledger
        if record.locked_fiat > 0:
            self.ledger.fiat_balances[rail] = (
                self.ledger.fiat_balances.get(rail, 0.0) + record.locked_fiat
            )
            record.locked_fiat = 0.0

        if record.locked_crypto > 0:
            self.ledger.crypto_balances[base.sell_venue.lower()] = (
                self.ledger.crypto_balances.get(base.sell_venue.lower(), 0.0)
                + record.locked_crypto
            )
            record.locked_crypto = 0.0

        record.status = TradeStatus.CANCELLED
        record.cancelled_at = now
        record.cancellation_reason = reason

        self.historical_trades[trade_id] = record
        del self.active_trades[trade_id]
        return True

    def expire_stale_detected(self, current_time: Optional[datetime] = None) -> int:
        """Prunes unclaimed signals that have remained in DETECTED beyond TTL."""
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        expired_ids = []
        for tid, record in list(self.active_trades.items()):
            if record.status == TradeStatus.DETECTED:
                age = (now - record.detected_at).total_seconds()
                if age >= self.detection_ttl_seconds:
                    record.status = TradeStatus.EXPIRED
                    record.cancelled_at = now
                    record.cancellation_reason = f"Detection TTL exceeded ({age:.1f}s >= {self.detection_ttl_seconds}s)"
                    self.historical_trades[tid] = record
                    expired_ids.append(tid)
                    del self.active_trades[tid]

        return len(expired_ids)
