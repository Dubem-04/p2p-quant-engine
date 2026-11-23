"""Centralized risk thresholds, volatility windows, and kill-switch settings."""

from pathlib import Path

# --- Persistence Settings ---
DEFAULT_RISK_STATE_PATH: Path = Path("data/risk_state.json")

# --- Market Volatility & Staleness Guardrails ---
VOLATILITY_WINDOW_SECONDS: float = 180.0  # 3-minute rolling lookback window
MAX_PRICE_VELOCITY_PCT: float = 0.015  # Pause quoting if price moves > 1.5% in window
MAX_EXTERNAL_DIVERGENCE_PCT: float = (
    0.030  # Pause if P2P diverges > 3.0% from benchmark
)
MAX_ORDERBOOK_STALENESS_SECONDS: float = 120.0  # Reject snapshots older than 75s

# --- Strict Capital Preservation (WAC Underwater Guard) ---
# Minimum buffer above Weighted-Average Cost Basis before allowing Ask (Sell) quotes
MIN_WAC_PROFIT_BUFFER_FIAT: float = 1.0  # Must sell at least ₦1.00 above unit WAC

# --- Counterparty & Order Acceptance Thresholds ---
MIN_COUNTERPARTY_ORDERS: int = 20
MIN_COUNTERPARTY_COMPLETION_RATE: float = 0.85  # 85% minimum completion rate
MIN_COUNTERPARTY_ACCOUNT_AGE_DAYS: int = 30
MIN_ORDER_AMOUNT_FIAT: float = 20_000.0  # Reject dust orders eaten by bank fees
MAX_ORDER_AMOUNT_FIAT: float = 5_000_000.0  # Cap single-order exposure
REQUIRE_KYC_NAME_MATCH: bool = True

# --- Circuit Breaker Settings ---
MAX_CONSECUTIVE_API_ERRORS: int = 5
