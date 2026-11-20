import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from p2p_quant_engine.risk.config import (
    DEFAULT_RISK_STATE_PATH,
    MAX_CONSECUTIVE_API_ERRORS,
    MAX_EXTERNAL_DIVERGENCE_PCT,
    MAX_ORDER_AMOUNT_FIAT,
    MAX_ORDERBOOK_STALENESS_SECONDS,
    MAX_PRICE_VELOCITY_PCT,
    MIN_COUNTERPARTY_ACCOUNT_AGE_DAYS,
    MIN_COUNTERPARTY_COMPLETION_RATE,
    MIN_COUNTERPARTY_ORDERS,
    MIN_ORDER_AMOUNT_FIAT,
    MIN_WAC_PROFIT_BUFFER_FIAT,
    REQUIRE_KYC_NAME_MATCH,
    VOLATILITY_WINDOW_SECONDS,
)


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RiskDecision:
    """Structured outcome of a pre-trade or pre-quote risk check."""

    allowed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CounterpartyProfile:
    """Normalized counterparty attributes for an incoming P2P order."""

    user_id: str
    completed_orders: int
    completion_rate: float  # Decimal between 0.0 and 1.0
    account_age_days: int
    kyc_verified: bool
    name_matches_bank: bool


@dataclass(frozen=True)
class IncomingOrderContext:
    """Order parameters validated before locking escrow or releasing funds."""

    order_id: str
    side: str  # "BUY" or "SELL"
    fiat_amount: float
    crypto_amount: float
    price_fiat: float
    counterparty: CounterpartyProfile
    available_bank_headroom_fiat: Optional[float] = None


@dataclass(frozen=True)
class GuardrailConfig:
    """Configurable thresholds for RiskGuardrails."""

    state_path: Path = DEFAULT_RISK_STATE_PATH
    volatility_window_seconds: float = VOLATILITY_WINDOW_SECONDS
    max_price_velocity_pct: float = MAX_PRICE_VELOCITY_PCT
    max_external_divergence_pct: float = MAX_EXTERNAL_DIVERGENCE_PCT
    max_orderbook_staleness_seconds: float = MAX_ORDERBOOK_STALENESS_SECONDS
    min_wac_profit_buffer_fiat: float = MIN_WAC_PROFIT_BUFFER_FIAT
    min_counterparty_orders: int = MIN_COUNTERPARTY_ORDERS
    min_counterparty_completion_rate: float = MIN_COUNTERPARTY_COMPLETION_RATE
    min_counterparty_account_age_days: int = MIN_COUNTERPARTY_ACCOUNT_AGE_DAYS
    min_order_amount_fiat: float = MIN_ORDER_AMOUNT_FIAT
    max_order_amount_fiat: float = MAX_ORDER_AMOUNT_FIAT
    require_kyc_name_match: bool = REQUIRE_KYC_NAME_MATCH
    max_consecutive_api_errors: int = MAX_CONSECUTIVE_API_ERRORS


@dataclass
class KillSwitchState:
    """Persisted emergency circuit-breaker state."""

    tripped: bool = False
    reason: Optional[str] = None
    tripped_at: Optional[str] = None
    consecutive_api_errors: int = 0


class RiskGuardrails:
    """
    Pre-trade risk engine enforcing volatility halts, strict WAC capital preservation,
    counterparty screening, and a crash-safe emergency kill switch.
    """

    def __init__(self, config: Optional[GuardrailConfig] = None) -> None:
        self.config = config or GuardrailConfig()
        self.kill_switch = KillSwitchState()
        self._price_history: deque[tuple[float, float]] = deque()

        if self.config.state_path.exists():
            self._load_state()

    # --- Persistence (Atomic JSON) ---

    def _save_state(self) -> None:
        """Persist kill-switch state atomically via temporary file replacement."""
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config.state_path.with_suffix(".tmp")

        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(asdict(self.kill_switch), f, indent=2)

        os.replace(tmp_path, self.config.state_path)

    def _load_state(self) -> None:
        """Load persisted kill-switch state from disk."""
        with self.config.state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.kill_switch = KillSwitchState(**data)

    # --- Kill Switch & System Health ---

    def trip_kill_switch(self, reason: str) -> None:
        """Immediately halt all quoting and persist circuit-breaker state."""
        if not reason.strip():
            raise ValueError("Kill-switch reason cannot be empty.")
        self.kill_switch.tripped = True
        self.kill_switch.reason = reason
        self.kill_switch.tripped_at = _utc_now_iso()
        self._save_state()

    def reset_kill_switch(self) -> None:
        """Clear kill switch and reset consecutive error counter."""
        self.kill_switch = KillSwitchState(tripped=False)
        self._price_history.clear()
        self._save_state()

    def record_api_success(self) -> None:
        """Reset consecutive API error counter after a healthy exchange call."""
        if self.kill_switch.consecutive_api_errors > 0:
            self.kill_switch.consecutive_api_errors = 0
            self._save_state()

    def record_api_error(self, error_detail: str = "Exchange API error") -> None:
        """Increment error counter and trip kill switch if threshold is breached."""
        self.kill_switch.consecutive_api_errors += 1
        if (
            self.kill_switch.consecutive_api_errors
            >= self.config.max_consecutive_api_errors
        ):
            self.trip_kill_switch(
                f"Auto-tripped after {self.kill_switch.consecutive_api_errors} "
                f"consecutive API errors: {error_detail}"
            )
        else:
            self._save_state()

    # --- Market & Price Velocity Checks ---

    def observe_fair_value(
        self, fair_value_fiat: float, timestamp_sec: Optional[float] = None
    ) -> None:
        """Record a fair value observation and prune samples outside lookback window."""
        if fair_value_fiat <= 0:
            raise ValueError("fair_value_fiat must be positive.")
        now = timestamp_sec if timestamp_sec is not None else time.monotonic()
        cutoff = now - self.config.volatility_window_seconds

        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()

        self._price_history.append((now, fair_value_fiat))

    def _compute_window_velocity_pct(self) -> float:
        """Return max relative price swing within the active rolling window."""
        if len(self._price_history) < 2:
            return 0.0
        prices = [p for _, p in self._price_history]
        min_p = min(prices)
        max_p = max(prices)
        if min_p <= 0:
            return 0.0
        return (max_p - min_p) / min_p

    def evaluate_market_quote(
        self,
        fair_value_fiat: float,
        snapshot_age_seconds: float,
        external_divergence_pct: Optional[float] = None,
        timestamp_sec: Optional[float] = None,
    ) -> RiskDecision:
        """
        Validate whether market conditions permit posting or updating P2P quotes.
        """
        reasons: list[str] = []

        if self.kill_switch.tripped:
            reasons.append(f"Kill switch is tripped: {self.kill_switch.reason}")

        if snapshot_age_seconds < 0:
            raise ValueError("snapshot_age_seconds cannot be negative.")
        if snapshot_age_seconds > self.config.max_orderbook_staleness_seconds:
            reasons.append(
                f"Stale order book ({snapshot_age_seconds:.1f}s > "
                f"{self.config.max_orderbook_staleness_seconds:.1f}s limit)."
            )

        self.observe_fair_value(fair_value_fiat, timestamp_sec=timestamp_sec)
        velocity = self._compute_window_velocity_pct()
        if velocity > self.config.max_price_velocity_pct:
            reasons.append(
                f"Excessive price velocity ({velocity:.2%} > "
                f"{self.config.max_price_velocity_pct:.2%} window limit)."
            )

        if (
            external_divergence_pct is not None
            and external_divergence_pct > self.config.max_external_divergence_pct
        ):
            reasons.append(
                f"External benchmark divergence ({external_divergence_pct:.2%} > "
                f"{self.config.max_external_divergence_pct:.2%} limit)."
            )

        return RiskDecision(allowed=len(reasons) == 0, reasons=reasons)

    # --- Strict Capital Preservation (Ask Side) ---

    def evaluate_ask_quote(
        self,
        proposed_ask_fiat: float,
        avg_cost_basis_fiat: float,
    ) -> RiskDecision:
        """
        Enforce strict capital preservation: block SELL ads when market Ask drops
        below Weighted-Average Cost Basis + minimum profit buffer.
        """
        if proposed_ask_fiat <= 0 or avg_cost_basis_fiat <= 0:
            raise ValueError("Prices and cost basis must be positive.")

        min_allowed_ask = avg_cost_basis_fiat + self.config.min_wac_profit_buffer_fiat
        if proposed_ask_fiat < min_allowed_ask:
            return RiskDecision(
                allowed=False,
                reasons=[
                    f"Strict capital preservation: proposed Ask "
                    f"({proposed_ask_fiat:.2f}) "
                    f"is below WAC floor ({min_allowed_ask:.2f})."
                ],
            )
        return RiskDecision(allowed=True)

    # --- Counterparty & Order Acceptance Screening ---

    def evaluate_incoming_order(self, order: IncomingOrderContext) -> RiskDecision:
        """Screen an incoming P2P order before locking escrow or releasing funds."""
        reasons: list[str] = []

        if self.kill_switch.tripped:
            reasons.append(f"Kill switch is tripped: {self.kill_switch.reason}")

        if order.fiat_amount < self.config.min_order_amount_fiat:
            reasons.append(
                f"Order fiat ({order.fiat_amount:.2f}) below minimum "
                f"({self.config.min_order_amount_fiat:.2f})."
            )
        if order.fiat_amount > self.config.max_order_amount_fiat:
            reasons.append(
                f"Order fiat ({order.fiat_amount:.2f}) exceeds maximum "
                f"({self.config.max_order_amount_fiat:.2f})."
            )

        if (
            order.available_bank_headroom_fiat is not None
            and order.fiat_amount > order.available_bank_headroom_fiat
        ):
            reasons.append(
                f"Order fiat ({order.fiat_amount:.2f}) exceeds assigned bank "
                f"headroom ({order.available_bank_headroom_fiat:.2f})."
            )

        cp = order.counterparty
        if not cp.kyc_verified:
            reasons.append("Counterparty is not KYC verified.")
        if self.config.require_kyc_name_match and not cp.name_matches_bank:
            reasons.append("Third-party payment detected: bank name mismatches KYC.")
        if cp.completed_orders < self.config.min_counterparty_orders:
            reasons.append(
                f"Counterparty orders ({cp.completed_orders}) below minimum "
                f"({self.config.min_counterparty_orders})."
            )
        if cp.completion_rate < self.config.min_counterparty_completion_rate:
            reasons.append(
                f"Counterparty completion rate ({cp.completion_rate:.2%}) below "
                f"minimum ({self.config.min_counterparty_completion_rate:.2%})."
            )
        if cp.account_age_days < self.config.min_counterparty_account_age_days:
            reasons.append(
                f"Counterparty account age ({cp.account_age_days}d) below minimum "
                f"({self.config.min_counterparty_account_age_days}d)."
            )

        return RiskDecision(allowed=len(reasons) == 0, reasons=reasons)
