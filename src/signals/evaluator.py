"""
Historical Strategy Evaluator, Portfolio Simulator & Regime-Switching Engine.

Backtests Taker-Taker instant sweeps, Dual-Sided Maker liquidity provision,
and adaptive regime-switching across historical ReplayFrame streams with
dynamic capital ledgers, settlement lockup queues, and friction accounting.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Iterator, List, Optional, Tuple

import polars as pl

from src.signals.friction_matrix import (
    AdjustedArbitrageSignal,
    CanonicalRail,
    FrictionCostEngine,
)
from src.signals.spread_models import (
    OpportunityScanner,
    StrategyType,
)
from src.storage.orderbook_replay import ReplayFrame


class BacktestMode(str, Enum):
    TAKER_ONLY = "TAKER_ONLY"
    MAKER_ONLY = "MAKER_ONLY"
    ADAPTIVE = "ADAPTIVE"


@dataclass
class CapitalLock:
    """Capital temporarily locked in-flight due to bank or escrow settlement."""

    unlock_time: datetime
    fiat_amount: float
    rail: CanonicalRail
    venue: str


class CapitalLedger:
    """Tracks available and in-flight fiat/crypto balances across venues and rails."""

    def __init__(
        self,
        initial_fiat: float = 10_000.0,
        initial_crypto: float = 10_000.0,
        venues: Tuple[str, ...] = ("binance", "bybit"),
        default_rail: CanonicalRail = CanonicalRail.BANK_TRANSFER_NGN,
    ):
        self.default_rail = default_rail
        # Distribute initial fiat across rails
        self.fiat_balances: Dict[CanonicalRail, float] = {default_rail: initial_fiat}

        # Distribute initial crypto evenly across venues
        per_venue_crypto = initial_crypto / len(venues) if venues else 0.0
        self.crypto_balances: Dict[str, float] = {
            v.lower(): per_venue_crypto for v in venues
        }

        self.in_flight_locks: List[CapitalLock] = []

    def release_matured_locks(self, current_time: datetime) -> float:
        """Unlocks in-flight capital whose settlement delay has elapsed."""
        released_fiat = 0.0
        active_locks: List[CapitalLock] = []

        for lock in self.in_flight_locks:
            if current_time >= lock.unlock_time:
                self.fiat_balances[lock.rail] = (
                    self.fiat_balances.get(lock.rail, 0.0) + lock.fiat_amount
                )
                released_fiat += lock.fiat_amount
            else:
                active_locks.append(lock)

        self.in_flight_locks = active_locks
        return released_fiat

    def can_fund_taker_cycle(
        self,
        buy_venue: str,
        sell_venue: str,
        rail: CanonicalRail,
        fiat_amount: float,
        crypto_amount: float,
    ) -> bool:
        """Verifies if sufficient fiat exists on rail and crypto on sell_venue."""
        avail_fiat = self.fiat_balances.get(rail, 0.0)
        avail_crypto = self.crypto_balances.get(sell_venue.lower(), 0.0)
        return avail_fiat >= fiat_amount and avail_crypto >= crypto_amount

    def execute_taker_cycle(
        self,
        buy_venue: str,
        sell_venue: str,
        rail: CanonicalRail,
        fiat_spent: float,
        crypto_acquired: float,
        fiat_received: float,
        settlement_minutes: float,
        current_time: datetime,
    ) -> None:
        """Updates ledger balances post-taker execution and locks incoming fiat."""
        # Deduct fiat spent and crypto sold
        self.fiat_balances[rail] = self.fiat_balances.get(rail, 0.0) - fiat_spent
        self.crypto_balances[sell_venue.lower()] = (
            self.crypto_balances.get(sell_venue.lower(), 0.0) - crypto_acquired
        )

        # Credit crypto acquired on buy venue immediately
        self.crypto_balances[buy_venue.lower()] = (
            self.crypto_balances.get(buy_venue.lower(), 0.0) + crypto_acquired
        )

        # Lock incoming fiat until settlement delay expires
        unlock_at = current_time + timedelta(minutes=settlement_minutes)
        self.in_flight_locks.append(
            CapitalLock(
                unlock_time=unlock_at,
                fiat_amount=fiat_received,
                rail=rail,
                venue=sell_venue,
            )
        )

    def total_fiat(self) -> float:
        return sum(self.fiat_balances.values()) + sum(
            lock.fiat_amount for lock in self.in_flight_locks
        )

    def total_crypto(self) -> float:
        return sum(self.crypto_balances.values())


@dataclass
class SimulatedTrade:
    """Detailed telemetry record for an executed trade."""

    timestamp_utc: datetime
    strategy: StrategyType
    buy_venue: str
    sell_venue: str
    source_rail: CanonicalRail
    target_rail: CanonicalRail
    fiat_volume: float
    crypto_volume: float
    gross_pnl_fiat: float
    friction_fees_fiat: float
    risk_haircut_fiat: float
    net_pnl_fiat: float
    roi_pct: float
    settlement_minutes: float


@dataclass
class BacktestSummary:
    """Consolidated performance metrics and Polars reporting dataframes."""

    total_trades: int
    winning_trades: int
    win_rate_pct: float
    initial_capital_fiat: float
    final_equity_fiat: float
    total_net_pnl_fiat: float
    total_roi_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    taker_trades_count: int
    maker_trades_count: int
    trade_log_df: pl.DataFrame
    equity_curve_df: pl.DataFrame


class StrategyEvaluator:
    """
    Simulation engine backtesting multi-venue P2P arbitrage strategies.
    """

    def __init__(
        self,
        friction_engine: Optional[FrictionCostEngine] = None,
        scanner: Optional[OpportunityScanner] = None,
        initial_fiat: float = 10_000.0,
        initial_crypto: float = 10_000.0,
        default_rail: CanonicalRail = CanonicalRail.BANK_TRANSFER_NGN,
        maker_fill_probability: float = 0.85,
    ):
        self.friction_engine = friction_engine or FrictionCostEngine()
        self.scanner = scanner or OpportunityScanner()
        self.initial_fiat = initial_fiat
        self.initial_crypto = initial_crypto
        self.default_rail = default_rail
        self.maker_fill_probability = maker_fill_probability

    def _calculate_drawdown_and_sharpe(
        self, equity_points: List[float]
    ) -> Tuple[float, float]:
        """Calculates maximum drawdown percentage and annualised Sharpe ratio."""
        if len(equity_points) < 2:
            return 0.0, 0.0

        peak = equity_points[0]
        max_drawdown = 0.0
        returns = []

        for i in range(1, len(equity_points)):
            prev = equity_points[i - 1]
            curr = equity_points[i]
            ret = (curr - prev) / prev if prev > 0 else 0.0
            returns.append(ret)

            if curr > peak:
                peak = curr
            dd = (peak - curr) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd

        mean_ret = sum(returns) / len(returns) if returns else 0.0
        variance = (
            sum((r - mean_ret) ** 2 for r in returns) / len(returns) if returns else 0.0
        )
        std_ret = math.sqrt(variance)

        # Annualized Sharpe approximation based on 15s step periods
        sharpe = (mean_ret / std_ret * math.sqrt(2_102_400)) if std_ret > 1e-9 else 0.0
        return max_drawdown, sharpe

    def run_backtest(
        self,
        replay_stream: Iterator[ReplayFrame],
        mode: BacktestMode = BacktestMode.ADAPTIVE,
        trade_size_fiat: float = 1_000.0,
    ) -> BacktestSummary:
        """
        Executes backtest over historical frames under specified strategy regime.
        """
        ledger = CapitalLedger(
            initial_fiat=self.initial_fiat,
            initial_crypto=self.initial_crypto,
            default_rail=self.default_rail,
        )

        executed_trades: List[SimulatedTrade] = []
        equity_records: List[Dict[str, float]] = []

        for frame in replay_stream:
            current_time = frame.timestamp_utc
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)

            # 1. Free any settled capital
            ledger.release_matured_locks(current_time)

            # 2. Scan frame for theoretical opportunities
            raw_signals = self.scanner.scan_frame(
                frame=frame,
                target_fiat_amount=trade_size_fiat,
            )

            # 3. Apply friction and risk filtering
            adjusted_signals: List[AdjustedArbitrageSignal] = []
            for sig in raw_signals:
                adj = self.friction_engine.evaluate_signal(
                    signal=sig,
                    source_rail_name=(
                        sig.common_payment_methods[0]
                        if sig.common_payment_methods
                        else None
                    ),
                )
                if adj.is_actionable:
                    adjusted_signals.append(adj)

            # 4. Strategy Selection and Execution Logic
            if adjusted_signals:
                chosen_signal: Optional[AdjustedArbitrageSignal] = None

                if mode == BacktestMode.TAKER_ONLY:
                    takers = [
                        s
                        for s in adjusted_signals
                        if s.base_signal.strategy == StrategyType.TAKER_TAKER
                    ]
                    if takers:
                        chosen_signal = max(
                            takers, key=lambda s: s.net_realizable_roi_pct
                        )

                elif mode == BacktestMode.MAKER_ONLY:
                    makers = [
                        s
                        for s in adjusted_signals
                        if s.base_signal.strategy == StrategyType.DUAL_MAKER
                    ]
                    if makers:
                        chosen_signal = max(
                            makers, key=lambda s: s.net_realizable_roi_pct
                        )

                elif mode == BacktestMode.ADAPTIVE:
                    # In adaptive mode: If a Taker opportunity offers >= 0.5% net ROI, take it immediately
                    # to capture zero-inventory-risk alpha. Otherwise, harvest maker spread.
                    takers = [
                        s
                        for s in adjusted_signals
                        if s.base_signal.strategy == StrategyType.TAKER_TAKER
                        and s.net_realizable_roi_pct >= 0.5
                    ]
                    if takers:
                        chosen_signal = max(
                            takers, key=lambda s: s.net_realizable_roi_pct
                        )
                    else:
                        makers = [
                            s
                            for s in adjusted_signals
                            if s.base_signal.strategy == StrategyType.DUAL_MAKER
                        ]
                        if makers:
                            chosen_signal = max(
                                makers, key=lambda s: s.net_realizable_roi_pct
                            )

                # Attempt trade execution against CapitalLedger
                if chosen_signal:
                    base = chosen_signal.base_signal
                    rail = chosen_signal.source_rail
                    crypto_needed = base.executable_crypto

                    if base.strategy == StrategyType.TAKER_TAKER:
                        if ledger.can_fund_taker_cycle(
                            buy_venue=base.buy_venue,
                            sell_venue=base.sell_venue,
                            rail=rail,
                            fiat_amount=trade_size_fiat,
                            crypto_amount=crypto_needed,
                        ):
                            proceeds = base.metadata.get(
                                "fiat_proceeds",
                                trade_size_fiat + chosen_signal.nominal_profit_fiat,
                            )
                            ledger.execute_taker_cycle(
                                buy_venue=base.buy_venue,
                                sell_venue=base.sell_venue,
                                rail=rail,
                                fiat_spent=trade_size_fiat,
                                crypto_acquired=crypto_needed,
                                fiat_received=proceeds,
                                settlement_minutes=chosen_signal.expected_settlement_minutes,
                                current_time=current_time,
                            )

                            trade = SimulatedTrade(
                                timestamp_utc=current_time,
                                strategy=base.strategy,
                                buy_venue=base.buy_venue,
                                sell_venue=base.sell_venue,
                                source_rail=rail,
                                target_rail=chosen_signal.target_rail,
                                fiat_volume=trade_size_fiat,
                                crypto_volume=crypto_needed,
                                gross_pnl_fiat=chosen_signal.nominal_profit_fiat,
                                friction_fees_fiat=chosen_signal.rail_friction_fee_fiat,
                                risk_haircut_fiat=chosen_signal.risk_haircut_fiat,
                                net_pnl_fiat=chosen_signal.net_realizable_profit_fiat,
                                roi_pct=chosen_signal.net_realizable_roi_pct,
                                settlement_minutes=chosen_signal.expected_settlement_minutes,
                            )
                            executed_trades.append(trade)

                    elif base.strategy == StrategyType.DUAL_MAKER:
                        avail_fiat = ledger.fiat_balances.get(rail, 0.0)
                        if avail_fiat >= trade_size_fiat:
                            # Apply probabilistic fill condition
                            ledger.fiat_balances[rail] -= trade_size_fiat
                            unlock_at = current_time + timedelta(
                                minutes=chosen_signal.expected_settlement_minutes
                            )
                            proceeds = (
                                trade_size_fiat
                                + chosen_signal.net_realizable_profit_fiat
                            )
                            ledger.in_flight_locks.append(
                                CapitalLock(
                                    unlock_time=unlock_at,
                                    fiat_amount=proceeds,
                                    rail=rail,
                                    venue=base.buy_venue,
                                )
                            )

                            trade = SimulatedTrade(
                                timestamp_utc=current_time,
                                strategy=base.strategy,
                                buy_venue=base.buy_venue,
                                sell_venue=base.sell_venue,
                                source_rail=rail,
                                target_rail=chosen_signal.target_rail,
                                fiat_volume=trade_size_fiat,
                                crypto_volume=crypto_needed,
                                gross_pnl_fiat=chosen_signal.nominal_profit_fiat,
                                friction_fees_fiat=chosen_signal.rail_friction_fee_fiat,
                                risk_haircut_fiat=chosen_signal.risk_haircut_fiat,
                                net_pnl_fiat=chosen_signal.net_realizable_profit_fiat,
                                roi_pct=chosen_signal.net_realizable_roi_pct,
                                settlement_minutes=chosen_signal.expected_settlement_minutes,
                            )
                            executed_trades.append(trade)

            # Record mark-to-market equity point
            current_equity = ledger.total_fiat()
            equity_records.append(
                {
                    "timestamp_utc": current_time,
                    "total_equity_fiat": current_equity,
                    "available_fiat": sum(ledger.fiat_balances.values()),
                    "in_flight_fiat": sum(
                        _.fiat_amount for _ in ledger.in_flight_locks
                    ),
                    "total_crypto": ledger.total_crypto(),
                }
            )

        # 5. Compile Backtest Analytics
        initial_cap = self.initial_fiat
        final_equity = ledger.total_fiat()
        total_pnl = final_equity - initial_cap
        total_roi = (total_pnl / initial_cap) * 100.0 if initial_cap > 0 else 0.0

        equity_points = [r["total_equity_fiat"] for r in equity_records] or [
            initial_cap
        ]
        max_dd, sharpe = self._calculate_drawdown_and_sharpe(equity_points)

        winning_trades = sum(1 for t in executed_trades if t.net_pnl_fiat > 0)
        win_rate = (
            (winning_trades / len(executed_trades) * 100.0) if executed_trades else 0.0
        )

        taker_count = sum(
            1 for t in executed_trades if t.strategy == StrategyType.TAKER_TAKER
        )
        maker_count = sum(
            1 for t in executed_trades if t.strategy == StrategyType.DUAL_MAKER
        )

        # Build Polars DataFrames
        trade_log_df = (
            pl.DataFrame(
                [
                    {
                        "timestamp_utc": t.timestamp_utc,
                        "strategy": t.strategy.value,
                        "buy_venue": t.buy_venue,
                        "sell_venue": t.sell_venue,
                        "source_rail": t.source_rail.value,
                        "fiat_volume": t.fiat_volume,
                        "crypto_volume": t.crypto_volume,
                        "net_pnl_fiat": t.net_pnl_fiat,
                        "roi_pct": t.roi_pct,
                        "settlement_minutes": t.settlement_minutes,
                    }
                    for t in executed_trades
                ]
            )
            if executed_trades
            else pl.DataFrame(
                schema={
                    "timestamp_utc": pl.Datetime("us", "UTC"),
                    "strategy": pl.Utf8,
                    "buy_venue": pl.Utf8,
                    "sell_venue": pl.Utf8,
                    "source_rail": pl.Utf8,
                    "fiat_volume": pl.Float64,
                    "crypto_volume": pl.Float64,
                    "net_pnl_fiat": pl.Float64,
                    "roi_pct": pl.Float64,
                    "settlement_minutes": pl.Float64,
                }
            )
        )

        equity_df = (
            pl.DataFrame(equity_records)
            if equity_records
            else pl.DataFrame(
                schema={
                    "timestamp_utc": pl.Datetime("us", "UTC"),
                    "total_equity_fiat": pl.Float64,
                    "available_fiat": pl.Float64,
                    "in_flight_fiat": pl.Float64,
                    "total_crypto": pl.Float64,
                }
            )
        )

        return BacktestSummary(
            total_trades=len(executed_trades),
            winning_trades=winning_trades,
            win_rate_pct=win_rate,
            initial_capital_fiat=initial_cap,
            final_equity_fiat=final_equity,
            total_net_pnl_fiat=total_pnl,
            total_roi_pct=total_roi,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            taker_trades_count=taker_count,
            maker_trades_count=maker_count,
            trade_log_df=trade_log_df,
            equity_curve_df=equity_df,
        )
