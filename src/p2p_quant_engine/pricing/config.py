"""Centralized fee constants, safety headroom, and default quoting parameters."""

# --- Exchange Fee Constants & Headroom (Rates as decimals: 0.0005 = 0.05%) ---
BYBIT_MAKER_FEE_RATE: float = 0.0000
BYBIT_TAKER_FEE_RATE: float = 0.0000
EXCHANGE_FEE_HEADROOM_RATE: float = 0.0005  # 0.05% buffer against silent fee changes

# Cost rate for sourcing/replenishing crypto inventory in bulk (spot/OTC + buffer)
BULK_ACQUISITION_FEE_RATE: float = 0.0010  # 0.10% spot fee + slippage headroom

# --- Fiat Rail (NGN Bank) Fee Constants & Headroom ---
# Incoming NGN deposit: CBN EMTL (₦50 for inflows >= ₦10,000)
CBN_EMTL_DEPOSIT_FEE_NGN: float = 50.0
DEPOSIT_FEE_HEADROOM_NGN: float = 25.0
TOTAL_DEPOSIT_FEE_NGN: float = (
    CBN_EMTL_DEPOSIT_FEE_NGN + DEPOSIT_FEE_HEADROOM_NGN
)  # ₦75

# Outgoing NGN transfer: NIP interbank charge (~₦53.75 max incl. VAT)
NIP_MAX_TRANSFER_FEE_NGN: float = 53.75
TRANSFER_FEE_HEADROOM_NGN: float = 46.25
TOTAL_TRANSFER_FEE_NGN: float = (
    NIP_MAX_TRANSFER_FEE_NGN + TRANSFER_FEE_HEADROOM_NGN
)  # ₦100.0

# --- Default Quoter Parameters ---
DEFAULT_BASE_HALF_SPREAD_FIAT: float = 8.0
DEFAULT_MIN_NET_PROFIT_FIAT: float = 3.0
DEFAULT_TICK_SIZE_FIAT: float = 0.01
DEFAULT_REFERENCE_TRADE_CRYPTO: float = 100.0
