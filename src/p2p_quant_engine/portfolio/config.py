"""Centralized configuration constants for portfolio inventory and cycle tracking."""

from pathlib import Path

# --- Persistence Settings ---
DEFAULT_LEDGER_STATE_PATH: Path = Path("data/portfolio_ledger.json")

# --- Cycle Exhaustion Thresholds ---
# Ask cycle (crypto pool) is exhausted when available crypto drops below this limit
# AND no crypto remains locked in active escrow.
MIN_TRADABLE_CRYPTO_THRESHOLD: float = 20.0  # e.g., 20 USDT minimum ad order size

# Bid cycle (fiat pool) is exhausted when available fiat drops below this limit
# AND no fiat remains committed to active orders.
MIN_TRADABLE_FIAT_THRESHOLD: float = 30_000.0  # e.g., ₦30,000 minimum ad order size

# Optional percentage-based exhaustion warning threshold (5% of initial cycle capital)
CYCLE_LOW_BALANCE_WARNING_RATIO: float = 0.05

# --- Accounting Precision ---
FIAT_DECIMAL_PLACES: int = 2
CRYPTO_DECIMAL_PLACES: int = 6
