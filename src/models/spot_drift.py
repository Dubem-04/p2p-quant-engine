"""
Short-horizon spot price drift forecaster and adverse selection mitigation model.

Trains and evaluates LightGBM/XGBoost models to predict 5-to-30 minute directional
spot price trajectories using order flow imbalance (OFI), high-frequency volatility,
and spot order book queues. Signals the quoting engine to pull or widen buy quotes
ahead of sudden market crashes to prevent holding devalued inventory.

Classes:
    SpotDriftForecaster: Feature ingestion, model inference, and drift classification wrapper.
"""