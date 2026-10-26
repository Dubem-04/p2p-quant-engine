<!--
DIRECTORY: data/
PURPOSE:
Defines the local data tier hierarchy, storage protocols, and serialization
standards for raw market snapshots, engineered feature tables, and backtest logs.
-->

# Data Directory Hierarchy & Storage Protocols

This folder houses non-version-controlled datasets, cache files, and local
analytical extracts. The root repository ignores large data files via `.gitignore`.

### Subdirectories:
- **`data/raw/`**:
  - Contains raw, unmanipulated JSON/CSV dumps of Level-2 order book snapshots
    streamed from exchange scrapers.
  - Serves as the immutable historical record of raw market events and competitor ads.
- **`data/processed/`**:
  - Contains processed, normalized, and timestamp-aligned Parquet feature stores.
  - Optimized for fast reading by machine learning pipelines (`LightGBM`, `XGBoost`),
    containing calculated Order Flow Imbalance (OFI), queue depths, and rolling spreads.
- **`data/backtests/`**:
  - Stores output run artifacts from simulation engines: trade fill logs,
    hypothetical equity curves, slippage metrics, and Monte Carlo stress-test results.