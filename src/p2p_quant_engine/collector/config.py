"""Centralized configuration constants for the Bybit P2P collector and API adapter."""

# --- Bybit V5 API Base URLs ---
BYBIT_MAINNET_BASE_URL: str = "https://api.bybit.com"
BYBIT_TESTNET_BASE_URL: str = "https://api-testnet.bybit.com"

# --- Bybit V5 P2P Endpoints ---
BYBIT_P2P_ONLINE_ADS_PATH: str = "/v5/p2p/item/online"
BYBIT_P2P_ORDERS_PATH: str = "/v5/p2p/order/simplifyList"
BYBIT_P2P_UPDATE_AD_PATH: str = "/v5/p2p/item/update"

# --- Request & Pagination Defaults ---
DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 10.0
DEFAULT_RECV_WINDOW_MS: str = "5000"
DEFAULT_ORDERBOOK_PAGE_SIZE: int = 20

# --- Payment Method Filter ---
# Optional list of Bybit payment type IDs (e.g., ["14"] for Bank Transfer).
# Empty list fetches all payment methods on the pair.
DEFAULT_PAYMENT_METHOD_FILTER: tuple[str, ...] = ()

# --- Safety Mode ---
# When True, read endpoints hit live Bybit P2P, while ad price/status mutations
# are logged locally without modifying live merchant ads on Bybit.
DEFAULT_DRY_RUN: bool = True
