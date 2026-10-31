"""
Dynamic spread quoting engine using an adapted Avellaneda-Stoikov market-making framework.

Calculates optimal bid and ask spreads across P2P advertising listings as a function
of target return margins, competitive book depth, underlying volatility, and real-time
inventory skew. Skews quotes to aggressively liquidate overweight assets (crypto or fiat)
and protect underweight reserves.

Classes:
    DynamicInventoryQuoter: Formulates reservation prices and optimal spreads based on inventory.
"""
