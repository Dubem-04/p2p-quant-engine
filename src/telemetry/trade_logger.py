"""
Granular trade execution logger and transaction lifecycle recorder.

Records the end-to-end lifecycle of every P2P transaction: order placement,
counterparty metadata, bank payment receipt, escrow confirmation, fee structures
(platform maker/taker and banking transfer costs), settlement latency, and
net realized spread (PnL). Serves as the primary audit trail for reporting.

Classes:
    TradeLogger: Manages structured transaction logging and persists trade metrics
        to long-term database storage.
"""
