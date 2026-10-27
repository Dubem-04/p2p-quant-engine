# p2p-quant-engine

[![CI Build](https://github.com/your-username/p2p-quant-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/your-username/p2p-quant-engine/actions)
[![Data Pipeline Audit](https://github.com/your-username/p2p-quant-engine/actions/workflows/data_sync.yml/badge.svg)](https://github.com/your-username/p2p-quant-engine/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Linter: Ruff](https://img.shields.io/badge/linter-ruff-red.svg)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Autonomous High-Turnover Bilateral Liquidity Provision, Risk Optimization, and Settlement Infrastructure.**

---

## Table of Contents
1. [Project Overview](#1-project-overview)
2. [Market Microstructure Inefficiencies Solved](#2-market-microstructure-inefficiencies-solved)
3. [Mathematical & Quantitative Foundations](#3-mathematical--quantitative-foundations)
4. [Repository Architecture & Scaffolding](#4-repository-architecture--scaffolding)
5. [Comprehensive File & Module Reference](#5-comprehensive-file--module-reference)
6. [Git Branching Model & Development Standards](#6-git-branching-model--development-standards)
7. [System Workflow & Real-World Execution Scenarios](#7-system-workflow--real-world-execution-scenarios)
   - [Scenario 1: The Golden Path (Two-Sided Market Making & Hedging)](#scenario-1-the-golden-path-two-sided-market-making--hedging)
   - [Scenario 2: Adverse Market Conditions (Spot Crash & Inventory Skew)](#scenario-2-adverse-market-conditions-spot-crash--inventory-skew)
   - [Scenario 3: Defensive Intervention (Third-Party Fraud & Dispute Circuit Breaker)](#scenario-3-defensive-intervention-third-party-fraud--dispute-circuit-breaker)
   - [Scenario 4: Quantitative Research, Strategy Backtesting & CI/CD Lifecycle](#scenario-4-quantitative-research-strategy-backtesting--cicd-lifecycle)
8. [Setup, Installation & Quickstart](#8-setup-installation--quickstart)
9. [Operational Runbooks & Production Safety Rules](#9-operational-runbooks--production-safety-rules)

---

## 1. Project Overview

`p2p-quant-engine` is an event-driven quantitative trading system and algorithmic execution desk designed for high-frequency, two-sided liquidity provision across peer-to-peer (P2P) crypto-fiat exchange rails (e.g., Binance, Bybit, OKX, Noones).

Conventional retail P2P operations rely on static pricing spreadsheets, manual counterparty negotiation, unhedged inventory, and vulnerable banking rails. In contrast, `p2p-quant-engine` formalizes bilateral P2P exchange as an **asymmetric market-making and counterparty credit risk problem**.

The engine operates an end-to-end, asynchronous pipeline that:
- Ingests high-frequency Level 2 (L2) order book depth and competitor quotation rates.
- Evaluates short-horizon (5–30 min) spot price drift using machine learning (LightGBM) to defend against adverse selection.
- Quotes two-sided bid/ask ads dynamically using an adapted Avellaneda-Stoikov inventory skew model.
- Eliminates directional crypto holding risk via automated delta-neutral perpetual futures hedging.
- Detects fraudulent remittance attempts using fuzzy string-matching KYC verification (Levenshtein distance) and NLP chat parsing.
- Automatically reconciles inbound fiat bank transfers via encrypted SMS, email, and bank webhook parsers before triggering escrow releases.

---

## 2. Market Microstructure Inefficiencies Solved

| Market Vulnerability | Industry Root Cause | `p2p-quant-engine` Algorithmic Defense |
| :--- | :--- | :--- |
| **Adverse Selection / Holding Drift** | Crypto drops while holding inventory between a fiat buy and a subsequent fiat sell, wiping out profit margins. | **Delta-Neutral Perpetual Hedging:** Programmatically opens short futures positions on fills, locking in the fiat spread. **Drift Classifier:** Pulls buy bids prior to sharp spot drops. |
| **Third-Party Payment Injection** | Scammers use compromised bank accounts under fraudulent names to send fiat, resulting in criminal police reports and bank liens. | **Fuzzy KYC String Matching:** Calculates Levenshtein/Jaro-Winkler distance between verified exchange names and bank remittance senders, auto-blocking mismatches. |
| **Account Freezes / PND Liens** | Rapid transaction velocity through a single retail bank account triggers Anti-Money Laundering (AML) Post-No-Debit (PND) restrictions. | **Bank Rail Allocator:** Distributes fiat turnover dynamically across partitioned banking rails based on institution-specific daily caps. |
| **Dead Inventory / Capital Trap** | Unbalanced inventory (100% crypto or 100% fiat) stalls operations, leading to zero velocity and opportunity loss. | **Inventory Skew Quoter:** Dynamically discounts overweight assets to restore neutral cash float while widening spreads on scarce assets. |

---

## 3. Mathematical & Quantitative Foundations

The engine runs on verified mathematical formulations governing capital velocity, reservation pricing, and operational limits:

### 3.1 Net Realized Round-Trip Spread
Calculates exact return per full cycle, accounting for maker/taker exchange fees and wire friction:

$$\text{Net Spread} = \frac{P_{\text{sell}} \cdot (1 - f_{\text{platform\_sell}} - f_{\text{bank\_out}})}{P_{\text{buy}} \cdot (1 + f_{\text{platform\_buy}} + f_{\text{bank\_in}})} - 1$$

*Where $P_{\text{sell}}$ is the fiat received per unit of crypto, $P_{\text{buy}}$ is the fiat paid, and $f$ represents fee percentages.*

### 3.2 Capital Turnover Velocity ($V$)
Measures the operational efficiency and recycling speed of deployed working capital:

$$V = \frac{\text{Total Traded Cumulative Volume}}{2 \cdot C_{\text{avg}}}$$

*Division by 2 normalizes for both buy and sell legs of a complete round-trip.*

### 3.3 Target Working Capital Allocation ($C_0$)
Determines initial liquid capital needed to hit a target periodic profit ($P_{\text{target}}$):

$$C_0 = \frac{P_{\text{target}}}{V \cdot r}$$

*Where $V$ is cycle velocity and $r$ is the net realized margin per cycle.*

### 3.4 Adapted Avellaneda-Stoikov Inventory Reservation Price
Calculates the dynamic reservation price ($R$) as a function of mid-market price ($S$), inventory deviation from target ($q$), holding time horizon ($T - t$), asset volatility ($\sigma$), and risk aversion ($\gamma$):

$$R(S, q, t) = S - q \cdot \gamma \cdot \sigma^2 \cdot (T - t)$$

*As inventory leans heavily into crypto ($q > 0$), the reservation price skews downward, lowering sell prices to liquidate stock quickly and widening buy quotes to deter further accumulation.*

### 3.5 Maximum Movable Throughput Bottleneck
Identifies the governing operational constraint across banking channels, compliance limits, and settlement delays:

$$\text{Max Volume}_{\text{period}} = \min \left( \sum_{i=1}^B L_{\text{bank},i}, \, L_{\text{platform\_KYC}}, \, \frac{C_{\text{liquid}} \cdot T_{\text{period}}}{\bar{t}_{\text{settlement}}} \right)$$

### 3.6 Risk-Adjusted Return on Capital (RAROC)
Evaluates desk profitability penalizing counterparty dispute lockups and fraud overhead:

$$\text{RAROC} = \frac{\text{Gross Revenue} - \text{Friction Fees} - \mathbb{E}[\text{Losses from Disputes/Frauds}]}{\text{Average Capital Allocated}}$$

---

## 4. Repository Architecture & Scaffolding

```text
p2p-quant-engine/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                 # Automated testing, formatting, and linting pipeline
│   │   └── data_sync.yml          # Scheduled cron health monitor for scrapers & DB
│   └── ISSUE_TEMPLATE/
│       ├── task.md                # Standard issue template for engineering deliverables
│       └── research.md            # Standard issue template for ML/quant experiments
├── configs/
│   ├── dev.yaml                   # Sandbox environment, mock endpoints, verbose logging
│   ├── prod.yaml                  # Production endpoints, connection pools, webhook URLs
│   └── risk_params.yaml           # Global risk limits, KYC thresholds, circuit breakers
├── data/
│   ├── raw/                       # Immutable raw L2 order book dumps and tick JSON files
│   ├── processed/                 # Normalized, timestamp-aligned Parquet feature stores
│   └── backtests/                 # Simulation outputs, equity curves, stress-test logs
├── docs/
│   ├── architecture.md            # End-to-end component specifications & dataflows
│   ├── math_spec.md               # Mathematical derivations (Avellaneda, RAROC, Velocity)
│   └── runbooks/
│       ├── manual_settlement.md   # Standard operating procedure for manual trade clearing
│       └── dispute_escalation.md  # Incident response protocol for fraud & PND bank holds
├── notebooks/
│   ├── 01_eda_orderbooks.ipynb    # L2 queue depth, spread thickness & volume analysis
│   ├── 02_model_drift.ipynb       # LightGBM spot drift training and cross-validation
│   └── 03_backtest_sim.ipynb      # Event-driven inventory skew backtest and simulation
├── src/
│   ├── scrapers/
│   │   ├── base_scraper.py        # Abstract base class: async session, backoff & limits
│   │   ├── binance_p2p.py         # Binance P2P L2 order book polling worker
│   │   └── bybit_p2p.py           # Bybit P2P L2 order book polling worker
│   ├── telemetry/
│   │   ├── db_client.py           # TimescaleDB / PostgreSQL async connection pool client
│   │   ├── trade_logger.py        # Transaction recorder for latency, spreads, and fees
│   │   └── alerts.py              # Real-time Telegram and Discord operational webhook alerts
│   ├── risk/
│   │   ├── kyc_validator.py       # Levenshtein distance string similarity KYC verification
│   │   ├── exposure_manager.py    # Float exposure controller and dispute circuit breaker
│   │   └── bank_rail_alloc.py     # Multi-bank throughput optimizer & AML velocity balancer
│   ├── models/
│   │   ├── spot_drift.py          # LightGBM short-horizon (5-30m) spot drift forecaster
│   │   ├── counterparty_risk.py   # Supervised fraud/dispute probability scoring model
│   │   └── dynamic_quoter.py      # Adapted Avellaneda-Stoikov inventory skew pricing engine
│   └── engine/
│       ├── order_manager.py       # Exchange Merchant API ad lifecycle controller
│       ├── bank_parser.py         # Bank SMS/email/webhook clearing reconciliation engine
│       ├── delta_hedger.py        # Automated perpetual futures short hedging manager
│       └── nlp_chat_monitor.py    # Trade chat semantic monitor for scam keywords & coercion
├── tests/
│   ├── unit/
│   │   ├── test_kyc_validator.py  # Unit tests for fuzzy name matching edge cases
│   │   └── test_dynamic_quoter.py # Unit tests for quoting sanity and inventory skew bounds
│   └── integration/
│       └── test_order_lifecycle.py# End-to-end integration test across mocked exchange APIs
├── .gitignore                     # Ignores bytecode, environments, raw data & API secrets
├── pyproject.toml                 # Package dependencies, build tools, Black/Ruff/Pytest config
└── README.md                      # Executive repository documentation and system manual
```

---

## 5. Comprehensive File & Module Reference

### Infrastructure, Automation & CI/CD
* **`.github/workflows/ci.yml`**: Continuous integration workflow triggered on all pushes and pull requests to `main` and `staging`. Sets up Python 3.11, validates code formatting using Black, runs linting checks via Ruff, and executes the Pytest suite across unit and integration tests.
* **`.github/workflows/data_sync.yml`**: Scheduled cron monitor running hourly. Executes lightweight test pings against exchange P2P quotation endpoints and TimescaleDB hypertable ingestion pipelines to verify API schema stability and detect scraper stalls.
* **`.github/ISSUE_TEMPLATE/task.md`**: Markdown template standardizing technical deliverables. Enforces clear definitions of affected subsystems, mathematical requirements, API limits, and explicit acceptance criteria before implementation.
* **`.github/ISSUE_TEMPLATE/research.md`**: Template structuring quantitative and ML experiments. Enforces formal documentation of hypotheses, training data windows, baseline metrics, backtest evaluations, and promotion decisions.

### Configuration & Data Tier
* **`configs/dev.yaml`**: Configuration profile for local developer testing, sandbox mocking, and paper trading. Connects to testnet endpoints, mock bank webhook servers, and enables debug logging.
* **`configs/prod.yaml`**: Production configuration file. Houses live exchange API gateways, production TimescaleDB connection strings, Telegram/Discord alert channels, and concurrency pool dimensions.
* **`configs/risk_params.yaml`**: Strict operational constraints. Defines maximum single-ticket exposure ratio ($\le 15\%$), dispute lockup ceiling ($20\%$), minimum KYC string similarity ($90\%$), and daily per-bank velocity limits.
* **`data/raw/`**: Storage directory for immutable, raw JSON and CSV captures of Level-2 order book snapshots, competitor advertisements, and order flow tick records streamed from live scrapers.
* **`data/processed/`**: Houses cleaned, timestamp-aligned, and feature-engineered Parquet datasets. Optimized for rapid IO during LightGBM model training and offline strategy calibration.
* **`data/backtests/`**: Repository for simulation run artifacts, including simulated trade logs, equity curves, drawdown series, and Monte Carlo stress-test summaries.

### Documentation & Operational Runbooks
* **`docs/architecture.md`**: Structural engineering blueprint specifying the event-driven interaction patterns between scrapers, risk engines, dynamic quoters, and execution managers.
* **`docs/math_spec.md`**: Mathematical reference documenting derivations and implementations for net spreads, Avellaneda-Stoikov reservation prices, capital velocity, and RAROC.
* **`docs/runbooks/manual_settlement.md`**: Standard Operating Procedure (SOP) guiding human operators through manual trade verification, sender KYC confirmation, bank fund clearance, and escrow release checklists.
* **`docs/runbooks/dispute_escalation.md`**: Crisis response protocol detailing procedures for third-party fraud detection, Post-No-Debit bank holds, and evidence compilation for exchange moderators.

### Research & Quantitative Notebooks
* **`notebooks/01_eda_orderbooks.ipynb`**: Exploratory data analysis environment examining L2 queue depth distributions, spread thickness across venues, order arrival seasonality, and competitor merchant pricing premiums.
* **`notebooks/02_model_drift.ipynb`**: Machine learning experimentation notebook. Tracks feature importance, cross-validation, and ROC-AUC metrics for the LightGBM short-horizon spot price drift model.
* **`notebooks/03_backtest_sim.ipynb`**: Event-driven backtesting notebook replaying historical order book data to simulate inventory skew quoting, fill probabilities, capital recycling rates, and drawdown profiles.

### Data Ingestion & Market Microstructure
* **`src/scrapers/base_scraper.py`**: Abstract base class defining standardized asynchronous HTTP/WebSocket sessions, exponential backoff retries, rate-limiting handlers, and uniform data model serialization.
* **`src/scrapers/binance_p2p.py`**: Asynchronous worker scraping Binance P2P quotation endpoints. Parses the top 5 bids and asks, merchant completion rates, payment rails, and order size limits into standardized L2 snapshots.
* **`src/scrapers/bybit_p2p.py`**: Asynchronous worker polling Bybit P2P order listings. Normalizes ad schemas, pricing premiums relative to spot, and merchant ratings for cross-venue arbitrage detection.

### System Telemetry & Event Logging
* **`src/telemetry/db_client.py`**: Asynchronous connection pool manager and ORM abstraction layer for TimescaleDB/PostgreSQL. Handles high-throughput writes for tick data and hypertable retention policies.
* **`src/telemetry/trade_logger.py`**: Transaction lifecycle auditor. Records timestamps, counterparty IDs, quoted vs. realized spreads, exchange maker/taker fees, banking friction, and net realized PnL.
* **`src/telemetry/alerts.py`**: Asynchronous notification dispatcher emitting structured operational alerts and daily turnover summaries to Discord and Telegram webhook channels.

### Risk Management & Compliance
* **`src/risk/kyc_validator.py`**: Anti-fraud string matching engine. Computes Levenshtein and Jaro-Winkler similarity distances between exchange verified KYC names and bank remittance senders to block third-party payment scams.
* **`src/risk/exposure_manager.py`**: Working capital allocation controller. Enforces single-ticket limits relative to liquid reserves and trips automated trading pauses if capital locked in disputes exceeds configured bounds.
* **`src/risk/bank_rail_alloc.py`**: Multi-account load balancer tracking daily fiat throughput across bank accounts. Directs transfers to accounts with available limits to prevent AML velocity violations.

### Predictive Modeling & Dynamic Quoting
* **`src/models/spot_drift.py`**: LightGBM/XGBoost inference module predicting 5–30 minute directional spot movements using order flow imbalance (OFI) and spot volatility to prevent adverse selection during market sell-offs.
* **`src/models/counterparty_risk.py`**: Supervised classification engine scoring counterparties using trade history, 30-day completion rates, and account age to flag or reject high-risk accounts.
* **`src/models/dynamic_quoter.py`**: Pricing engine implementing the adapted Avellaneda-Stoikov market-making model. Skews bid/ask quotes based on inventory imbalance to restore neutral cash float.

### Execution & Automated Settlement Engine
* **`src/engine/order_manager.py`**: Merchant API orchestrator automating ad publication, real-time price updates, ad pausing, and limit recalibration across connected exchanges.
* **`src/engine/bank_parser.py`**: Transaction reconciliation engine normalizing bank webhooks, SMS alerts, and encrypted emails to verify cleared fiat receipt before authorizing crypto releases.
* **`src/engine/delta_hedger.py`**: Programmatic derivatives manager that automatically opens short perpetual futures contracts on filled buy orders to neutralize spot price exposure during settlement windows.
* **`src/engine/nlp_chat_monitor.py`**: NLP semantic auditor monitoring active order chat messages for scam flags, off-platform communication invites, third-party payment admissions, or chargeback threats.

### Quality Assurance & Configuration Core
* **`tests/unit/test_kyc_validator.py`**: Unit test suite validating fuzzy string matching against edge cases: typos, transposed names, missing middle names, and fraudulent injections.
* **`tests/unit/test_dynamic_quoter.py`**: Unit test suite confirming mathematical quoting integrity across zero inventory, extreme skew, and volatile spot market regimes.
* **`tests/integration/test_order_lifecycle.py`**: End-to-end integration test simulating complete order lifecycles across mocked exchange APIs, bank parsers, and hedging handlers.
* **`.gitignore`**: Security and environment filter preventing API keys, bank credentials, bytecode caches, virtual environments, and large Parquet datasets from entering version control.
* **`pyproject.toml`**: Declarative project configuration file defining dependencies, Python 3.11+ version constraints, build systems, and standardized settings for Black, Ruff, and Pytest.

---

## 6. Git Branching Model & Development Standards

```text
main (Production: Live Trading Desk)
  ▲
  │ (Tagged Releases & Emergency Hotfixes)
staging (Integration: Paper Trading & Sandboxes)
  ▲
  ├── feature/order-book-scraper (Discrete Feature Development)
  ├── feature/levenshtein-kyc-validator
  └── experiment/lightgbm-drift-forecaster (Quantitative / ML Testing)
```

The repository follows a strict multi-tier Git branching workflow:
1. **`main`**: Represents the live, production-tested trading engine. Direct commits are restricted; code must be merged via approved pull requests from `staging` with all CI tests passing.
2. **`staging`**: Integration branch running against paper trading environments and exchange testnets. Used to validate full system choreography before deploying to live capital.
3. **`feature/<component-name>`**: Branch dedicated to individual features or bug fixes (e.g., `feature/bank-sms-parser`). Created from and merged back into `staging`.
4. **`experiment/<hypothesis-name>`**: Dedicated branch for quantitative research, feature engineering, and model training (e.g., `experiment/order-flow-imbalance`). Merged into `staging` only when validated by backtest results.

---

## 7. System Workflow & Real-World Execution Scenarios

### Scenario 1: The Golden Path (Two-Sided Market Making & Hedging)
*Demonstrates standard two-sided quoting, maker buy fulfillment, programmatic delta hedging, maker sell settlement, bank verification, and profit logging.*

```text
[src/scrapers/base_scraper.py]
         │ (Ingests L2 Order Books)
[src/scrapers/binance_p2p.py] & [src/scrapers/bybit_p2p.py]
         │ (Persists Ticks to TimescaleDB)
[src/telemetry/db_client.py] ──► [data/raw/]
         │
[src/models/dynamic_quoter.py] ◄── Pulls config from [configs/risk_params.yaml]
         │ (Calculates Symmetrical Spreads)
[src/engine/order_manager.py] ──► Publishes Buy & Sell Ads via Exchange APIs
         │
[COUNTERPARTY FILLS BUY AD (1,000 USDT)]
         ├── [src/models/counterparty_risk.py] ──► Low Risk Score (0.04)
         ├── [src/risk/exposure_manager.py] ──► Order Size Validated (< 15% Cap)
         ├── [src/risk/bank_rail_alloc.py] ──► Selects Optimal Bank Rail A
         └── [src/engine/delta_hedger.py] ──► Opens 1,000 USDT Short Futures Position
         │
[COUNTERPARTY FILLS SELL AD (1,000 USDT)]
         ├── [src/engine/nlp_chat_monitor.py] ──► Scans Chat (Clean, No Scam Flags)
         ├── [src/engine/bank_parser.py] ──► Receives Bank Remittance Webhook
         ├── [src/risk/kyc_validator.py] ──► Name Similarity Score: 95% (Pass)
         ├── [src/engine/order_manager.py] ──► Releases Crypto from Escrow
         └── [src/engine/delta_hedger.py] ──► Closes Short Futures Position
         │
[POST-SETTLEMENT TELEMETRY]
         ├── [src/telemetry/trade_logger.py] ──► Records Realized PnL (+1.42%) & Latency
         └── [src/telemetry/alerts.py] ──► Emits Discord/Telegram Notification
```

1. **Initialization:** The environment loads dependencies from `pyproject.toml` and parameters from `configs/prod.yaml` and `configs/risk_params.yaml`. System components initialize following the concurrency guidelines in `docs/architecture.md`.
2. **Telemetry Ingestion:** `src/scrapers/binance_p2p.py` and `src/scrapers/bybit_p2p.py` (inheriting from `src/scrapers/base_scraper.py`) continuously poll L2 order books. Snapshots are stored in `data/raw/` and written to TimescaleDB via `src/telemetry/db_client.py`.
3. **Quoting:** `src/models/dynamic_quoter.py` applies the Avellaneda-Stoikov formulation from `docs/math_spec.md`. Because inventory is balanced, it sets competitive bid and ask spreads. `src/engine/order_manager.py` publishes live maker buy and sell ads on Binance and Bybit.
4. **Buy Fill & Hedging:** A counterparty fills the maker buy ad for 1,000 USDT. `src/models/counterparty_risk.py` evaluates the seller's history and approves the trade. `src/risk/exposure_manager.py` verifies the order does not breach the 15% single-ticket limit. `src/risk/bank_rail_alloc.py` selects Bank Account A based on remaining daily wire limits. Upon acquiring spot USDT, `src/engine/delta_hedger.py` automatically opens a 1,000 USDT short perpetual futures contract on Bybit Futures, locking in the fiat spread.
5. **Sell Fill & Rebalancing:** A buyer fills the maker sell ad. `src/engine/nlp_chat_monitor.py` verifies chat messages contain no coercion or third-party claims. The buyer sends fiat to Bank Account B. `src/engine/bank_parser.py` parses the bank transaction alert and extracts sender metadata. `src/risk/kyc_validator.py` confirms a 95% name match against exchange KYC records. `src/engine/order_manager.py` releases the crypto, and `src/engine/delta_hedger.py` closes the short futures hedge.
6. **Logging & Notification:** `src/telemetry/trade_logger.py` records fees, execution slippage, latency, and net spread to PostgreSQL. `src/telemetry/alerts.py` sends a success notification to Discord and Telegram.

---

### Scenario 2: Adverse Market Conditions (Spot Crash & Inventory Skew)
*Demonstrates how predictive machine learning and dynamic inventory models prevent adverse selection and balance capital during sharp market sell-offs.*

```text
[SPOT MARKET CRASH: -4% IN 8 MINUTES]
         │
[src/models/spot_drift.py] (LightGBM) ──► Computes Order Flow Imbalance & Volatility
         │ (Predicts Strong Downward Drift)
[src/engine/order_manager.py] ──► Cancels Active Maker Buy Ads Immediately
         │
[INVENTORY EVALUATION: 75% Crypto / 25% Fiat]
         │
[src/models/dynamic_quoter.py] ──► Applies Inventory Skew Equations (docs/math_spec.md)
         ├── Reservation Price Skewed Downward
         └── Maker Sell Spread Narrowed (Undercuts Competitors by 0.1%)
         │
[src/engine/order_manager.py] ──► Updates Sell Ad on Exchange Boards
         │
[MARKET TAKERS EXECUTE AGGRESSIVE BUY ORDERS]
         ├── Crypto Inventory Successfully Liquidated
         ├── [src/engine/bank_parser.py] ──► Confirms Inbound Fiat Settlements
         └── [src/risk/exposure_manager.py] ──► Rebalanced to 60% Fiat / 40% Crypto
```

1. **Drift Warning:** Spot markets decline sharply. `src/models/spot_drift.py` analyzes order flow imbalance and volume acceleration on the central spot order book, predicting sustained downward momentum over the next 15 minutes.
2. **Defensive Pull:** `src/engine/order_manager.py` cancels all active Maker Buy ads, preventing market participants from dumping depreciating inventory into the engine's books.
3. **Inventory Rebalancing:** The engine currently holds 75% crypto and 25% fiat. Following equations in `docs/math_spec.md`, `src/models/dynamic_quoter.py` lowers the reservation price, narrowing the sell spread to undercut the best market ask by 0.1% while widening buy spreads.
4. **Execution:** `src/engine/order_manager.py` posts the discounted sell ad. Takers fill the order rapidly, liquidating crypto stock into fiat. `src/engine/bank_parser.py` reconciles the incoming wire transfers. `src/risk/exposure_manager.py` confirms inventory has returned to safe operational balance.

---

### Scenario 3: Defensive Intervention (Third-Party Fraud & Dispute Circuit Breaker)
*Demonstrates real-time fraud mitigation, semantic chat auditing, fuzzy string KYC rejection, circuit-breaker halts, and runbook escalation.*

```text
[BUYER INITIATES $1,500 ORDER ON BYBIT]
         │ (Verified Name: "Michael Adebayo")
[src/engine/nlp_chat_monitor.py] ◄── Ingests Chat: "Paying from my brother's account..."
         │ (Flags Prohibited Keyword Match)
[src/engine/bank_parser.py] ◄── Receives Inbound Wire of $1,500 from "Samuel Okon"
         │
[src/risk/kyc_validator.py] ──► Levenshtein Distance Check ("Samuel Okon" vs. "Michael Adebayo")
         │ (Similarity Score: 18% -> REJECT ESCROW RELEASE)
[BUYER FILES DISPUTE: $1,500 (22% of Desk Capital) Locked in Escrow]
         │
[src/risk/exposure_manager.py] ──► Dispute Ratio > 20% Limit (configs/risk_params.yaml)
         │ (TRIPS SYSTEM CIRCUIT BREAKER)
[src/engine/order_manager.py] ──► Pauses All Active Ads Across All Exchanges
         │
[EMERGENCY ESCALATION & INCIDENT RESPONSE]
         ├── [src/telemetry/alerts.py] ──► Dispatches Critical Telegram Alert
         ├── Operator follows [docs/runbooks/dispute_escalation.md]
         ├── Operator references [docs/runbooks/manual_settlement.md]
         └── [src/telemetry/trade_logger.py] ──► Blacklists Counterparty ID
```

1. **Order Initiation & Chat Flag:** A counterparty creates an order to purchase $1,500 of crypto under verified exchange name *"Michael Adebayo"*. In chat, the counterparty states: *"I'm paying from my brother's account, release fast please."* `src/engine/nlp_chat_monitor.py` flags the message for prohibited terms ("brother's account", "third party").
2. **Inbound Wire & Identity Check:** An incoming wire of $1,500 arrives in Bank Account B. `src/engine/bank_parser.py` extracts the remitter name: *"Samuel Okon"*. `src/risk/kyc_validator.py` executes a fuzzy string comparison against *"Michael Adebayo"*, calculating an 18% similarity score. This fails the 90% threshold in `configs/risk_params.yaml`, and automated release is blocked.
3. **Circuit-Breaker Activation:** The buyer marks the trade as paid and opens a dispute, locking the $1,500 in escrow (representing 22% of total desk working capital). `src/risk/exposure_manager.py` detects that the dispute ratio ($R_{\text{lock}} = 0.22$) exceeds the 20% limit in `configs/risk_params.yaml` and trips the circuit breaker, instructing `src/engine/order_manager.py` to pause all active ads across all venues.
4. **Resolution:** `src/telemetry/alerts.py` broadcasts an emergency Telegram alert. The operator executes the response protocol in `docs/runbooks/dispute_escalation.md` to submit bank evidence to exchange moderators. For manual handling, the operator consults `docs/runbooks/manual_settlement.md` to coordinate a refund to the original bank account. `src/telemetry/trade_logger.py` records the incident and blacklists the user ID in `src/models/counterparty_risk.py`.

---

### Scenario 4: Quantitative Research, Strategy Backtesting & CI/CD Lifecycle
*Demonstrates feature engineering, ML model training, historical order book replay backtesting, automated unit/integration testing, and deployment.*

```text
[RESEARCH HYPOTHESIS: Queue-Depth Weighted Pricing Improves Velocity]
         │
[Open Research Issue via .github/ISSUE_TEMPLATE/research.md]
         │
[CREATE BRANCH: feature/queue-weighted-skew]
         │
[data/raw/] (L2 Order Book Snapshots)
         │ (ETL Pipeline Processing)
[data/processed/] (Parquet Feature Store)
         │
[NOTEBOOK RESEARCH & VALIDATION]
         ├── [notebooks/01_eda_orderbooks.ipynb] ──► Queue Exhaustion Rates
         ├── [notebooks/02_model_drift.ipynb] ──► LightGBM Retraining & ROC-AUC
         └── [notebooks/03_backtest_sim.ipynb] ──► Event-Driven Simulation -> [data/backtests/]
         │
[CODEBASE IMPLEMENTATION]
         ├── Updates [src/models/dynamic_quoter.py]
         └── Updates Formulations in [docs/math_spec.md]
         │
[LOCAL SANDBOX TESTING (configs/dev.yaml)]
         ├── [tests/unit/test_dynamic_quoter.py] ──► Formula Boundary Assertions
         ├── [tests/unit/test_kyc_validator.py] ──► String Matching Edge Cases
         └── [tests/integration/test_order_lifecycle.py] ──► End-to-End Mock Run
         │
[PULL REQUEST TO STAGING]
         │
[.github/workflows/ci.yml] (GitHub Actions)
         ├── Environment Build (Python 3.11 from pyproject.toml)
         ├── Formatting & Linting Checks (Black & Ruff)
         └── Pytest Suite Execution (All Tests Pass)
         │
[MERGE TO MAIN (Tagged Production Release)]
```

1. **Hypothesis & Backlog:** To evaluate if weighting spreads by order book queue depth increases turnover velocity, the developer creates a tracking issue via `.github/ISSUE_TEMPLATE/research.md` and branches to `feature/queue-weighted-skew`.
2. **Feature Engineering & Research:** The developer extracts raw tick data from `data/raw/` into normalized Parquet feature tables in `data/processed/`. In `notebooks/01_eda_orderbooks.ipynb`, queue depletion rates are analyzed across trading hours. In `notebooks/02_model_drift.ipynb`, the developer retrains the LightGBM model in `src/models/spot_drift.py`. In `notebooks/03_backtest_sim.ipynb`, the developer simulates 30 days of historical L2 order book data, logging equity curves and RAROC metrics to `data/backtests/`. Updated mathematical derivations are committed to `docs/math_spec.md`.
3. **Local Testing:** Using sandbox settings in `configs/dev.yaml`, the developer executes test suites: `tests/unit/test_dynamic_quoter.py` validates spread boundaries, `tests/unit/test_kyc_validator.py` checks fuzzy name matching edge cases, and `tests/integration/test_order_lifecycle.py` validates end-to-end order orchestration against mock endpoints.
4. **CI/CD Validation & Merge:** The developer opens a Pull Request into `staging`. GitHub Actions triggers `.github/workflows/ci.yml`, installing dependencies from `pyproject.toml`, executing Ruff/Black format checks, and running Pytest. Concurrently, `.github/workflows/data_sync.yml` confirms ingestion endpoints remain healthy. Following approval, the PR is merged into `main` and deployed to production.

---

## 8. Setup, Installation & Quickstart

### Prerequisites
* Python 3.11 or higher
* PostgreSQL 15+ with TimescaleDB extension enabled
* Git and GitHub CLI (`gh`)

### Installation
```bash
# 1. Clone the repository
git clone https://github.com/your-username/p2p-quant-engine.git
cd p2p-quant-engine

# 2. Set up virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install core and development dependencies
pip install --upgrade pip
pip install -e ".[dev]"

# 4. Configure local environment variables
cp configs/dev.yaml configs/local_secrets.yaml
# Edit configs/local_secrets.yaml with testnet keys and DB credentials (ignored by git)
```

### Running Test Suites
```bash
# Run all unit and integration tests
pytest

# Run tests with coverage report
pytest --cov=src tests/

# Execute code formatting and lint checks
black --check .
ruff check .
```

### Initializing Ingestion Workers
```bash
# Launch Binance P2P L2 scraper worker
python -m src.scrapers.binance_p2p

# Launch Bybit P2P L2 scraper worker
python -m src.scrapers.bybit_p2p
```

---

## 9. Operational Runbooks & Production Safety Rules

To protect working capital against bank freezes, fraud, and exchange liquidity issues, all automated and human operations must adhere to three foundational rules:

1. **Strict Counterparty KYC Verification:**  
   Automated escrow release must never occur if the Levenshtein string similarity between the bank remittance sender name and verified exchange KYC identity is below **90%**. Under no circumstances are third-party payments accepted. Refer to `docs/runbooks/manual_settlement.md` for edge-case resolutions.

2. **Position Sizing and Dispute Lockup Limits:**  
   - No individual order ticket may exceed **15%** of total liquid working capital.
   - If total capital locked in active disputes or payment delays exceeds **20%**, the engine automatically trips a system-wide circuit breaker and pauses all live advertisements. Refer to `docs/runbooks/dispute_escalation.md` for formal dispute resolution scripts.

3. **Delta-Neutral Derivatives Hedging:**  
   All crypto inventory acquired through maker buy orders must be matched immediately with an offsetting short perpetual futures contract to neutralize market risk. Unhedged overnight inventory holdings are strictly prohibited.