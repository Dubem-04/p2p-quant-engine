"""Centralized configuration constants for the P2P execution control loop."""

from pathlib import Path

# --- Asset & Market Pair Settings ---
DEFAULT_CRYPTO_ASSET: str = "USDT"
DEFAULT_FIAT_CURRENCY: str = "NGN"

# --- Polling & Repricing Deadband ---
DEFAULT_POLL_INTERVAL_SECONDS: float = 5.0

# Minimum price shift (in fiat) required before sending an update_ad API call.
# Prevents rate-limit churn on tiny 1-kobo micro-fluctuations.
MIN_REPRICE_DELTA_FIAT: float = 0.05

# --- Persistence for Execution State (Flagged Orders & Ad Tracking) ---
DEFAULT_EXECUTION_STATE_PATH: Path = Path("data/execution_state.json")
