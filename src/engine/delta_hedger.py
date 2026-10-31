"""
Automated delta-neutral inventory hedging engine using perpetual futures.

Eliminates directional market exposure during fiat settlement holding windows.
Listens for filled P2P buy orders and programmatically executes offsetting short
positions on derivative exchanges (e.g., Binance/Bybit Futures). Closes the hedge
in lockstep when crypto inventory is subsequently sold via P2P sell ads.

Classes:
    DeltaHedger: Manages programmatic derivatives hedging to maintain a net-zero delta profile.
"""
