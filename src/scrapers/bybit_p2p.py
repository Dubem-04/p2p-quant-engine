"""
Level-2 order book ingestion worker for Bybit P2P liquidity venues.

Handles asynchronous polling of Bybit P2P order listings across targeted fiat
currencies. Captures counterparty merchant badges, pricing premiums relative to
spot, available inventory volumes, and min/max order thresholds to enable
cross-venue arbitrage detection and competitive quotation tracking.

Functions:
    poll_bybit_listings: Queries Bybit P2P order book feeds with session reuse.
    standardize_bybit_order: Normalizes Bybit ad schemas into system-wide L2 representation.
"""
