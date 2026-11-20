"""Centralized configuration constants for timeframe performance reporting."""

from datetime import timedelta, timezone
from pathlib import Path

# --- Timezone & Display Formatting ---
# West Africa Time (WAT = UTC+1) used as default for naive date/datetime queries
DEFAULT_REPORT_TIMEZONE = timezone(timedelta(hours=1), name="WAT")
FIAT_SYMBOL: str = "₦"
CRYPTO_SYMBOL: str = "USDT"

# --- Default Export Directory ---
DEFAULT_REPORT_EXPORT_DIR: Path = Path("data/reports")
