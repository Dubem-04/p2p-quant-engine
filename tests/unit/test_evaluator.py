from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from src.scrapers.base_scraper import P2POrderLevel
from src.signals.evaluator import (
    BacktestMode,
    BacktestSummary,
    CapitalLedger,
    StrategyEvaluator,
)
from src.signals.friction_matrix import CanonicalRail
from src.signals.spread_models import DualSidedMakerSpreadModel, OpportunityScanner
from src.storage.orderbook_replay import ReconstructedBook, ReplayFrame


@pytest.fixture
def mock_replay_frames():
    t0 = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=15)
    t2 = t0 + timedelta(seconds=30)

    # Frame 0: Cross-venue Taker dislocation (Binance ask 1.00, Bybit bid 1.03) -> 3% spread
    book_binance_t0 = ReconstructedBook(
        venue="binance",
        asset="USDT",
        fiat="USD",
        timestamp_utc=t0,
        bids=[
            P2POrderLevel(
                ad_id="bin_b",
                venue="binance",
                side="BUY",
                asset="USDT",
                fiat="USD",
                price=0.98,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Revolut"],
                merchant_id="m1",
                merchant_name="M1",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
        asks=[
            P2POrderLevel(
                ad_id="bin_a",
                venue="binance",
                side="SELL",
                asset="USDT",
                fiat="USD",
                price=1.00,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Revolut"],
                merchant_id="m2",
                merchant_name="M2",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
    )
    book_bybit_t0 = ReconstructedBook(
        venue="bybit",
        asset="USDT",
        fiat="USD",
        timestamp_utc=t0,
        bids=[
            P2POrderLevel(
                ad_id="byb_b",
                venue="bybit",
                side="BUY",
                asset="USDT",
                fiat="USD",
                price=1.03,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Revolut"],
                merchant_id="m3",
                merchant_name="M3",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
        asks=[
            P2POrderLevel(
                ad_id="byb_a",
                venue="bybit",
                side="SELL",
                asset="USDT",
                fiat="USD",
                price=1.05,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Revolut"],
                merchant_id="m4",
                merchant_name="M4",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
    )

    f0 = ReplayFrame(
        timestamp_utc=t0,
        books={"binance": book_binance_t0, "bybit": book_bybit_t0},
        is_synchronized=True,
    )

    # Frame 1: Market normalizes (No cross-venue taker, but wide intra-venue spread on Bybit: bid 1.00, ask 1.04)
    book_bybit_t1 = ReconstructedBook(
        venue="bybit",
        asset="USDT",
        fiat="USD",
        timestamp_utc=t1,
        bids=[
            P2POrderLevel(
                ad_id="byb_b1",
                venue="bybit",
                side="BUY",
                asset="USDT",
                fiat="USD",
                price=1.00,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Revolut"],
                merchant_id="m3",
                merchant_name="M3",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
        asks=[
            P2POrderLevel(
                ad_id="byb_a1",
                venue="bybit",
                side="SELL",
                asset="USDT",
                fiat="USD",
                price=1.04,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Revolut"],
                merchant_id="m4",
                merchant_name="M4",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
    )
    f1 = ReplayFrame(
        timestamp_utc=t1,
        books={"binance": book_binance_t0, "bybit": book_bybit_t1},
        is_synchronized=True,
    )

    # Frame 2: Neutral tick
    f2 = ReplayFrame(
        timestamp_utc=t2,
        books={"binance": book_binance_t0, "bybit": book_bybit_t1},
        is_synchronized=True,
    )

    return [f0, f1, f2]


def test_capital_ledger_operations():
    ledger = CapitalLedger(
        initial_fiat=10_000.0,
        initial_crypto=5_000.0,
        venues=("binance", "bybit"),
        default_rail=CanonicalRail.REVOLUT,
    )

    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 10_000.0
    assert ledger.crypto_balances["binance"] == 2500.0
    assert ledger.crypto_balances["bybit"] == 2500.0

    t0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    # Execute cycle: spend $1,000 fiat, deliver 1,000 USDT on bybit, receive 1,000 USDT on binance, receive $1,025 fiat locked for 5m
    ledger.execute_taker_cycle(
        buy_venue="binance",
        sell_venue="bybit",
        rail=CanonicalRail.REVOLUT,
        fiat_spent=1000.0,
        crypto_acquired=1000.0,
        fiat_received=1025.0,
        settlement_minutes=5.0,
        current_time=t0,
    )

    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 9000.0
    assert ledger.crypto_balances["bybit"] == 1500.0
    assert ledger.crypto_balances["binance"] == 3500.0
    assert len(ledger.in_flight_locks) == 1
    assert ledger.total_fiat() == 10_025.0

    # Advance time 3 mins -> still locked
    ledger.release_matured_locks(t0 + timedelta(minutes=3))
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 9000.0

    # Advance time 6 mins -> released
    released = ledger.release_matured_locks(t0 + timedelta(minutes=6))
    assert released == 1025.0
    assert ledger.fiat_balances[CanonicalRail.REVOLUT] == 10_025.0
    assert len(ledger.in_flight_locks) == 0


def test_strategy_evaluator_taker_only(mock_replay_frames):
    custom_maker = DualSidedMakerSpreadModel(
        penny_jump_epsilon=0.002, min_net_margin_pct=0.5
    )
    scanner = OpportunityScanner(maker_model=custom_maker)
    evaluator = StrategyEvaluator(
        scanner=scanner,
        initial_fiat=10_000.0,
        initial_crypto=10_000.0,
        default_rail=CanonicalRail.REVOLUT,
    )

    summary = evaluator.run_backtest(
        replay_stream=iter(mock_replay_frames),
        mode=BacktestMode.TAKER_ONLY,
        trade_size_fiat=1000.0,
    )

    assert isinstance(summary, BacktestSummary)
    assert summary.total_trades >= 1
    assert summary.taker_trades_count >= 1
    assert summary.maker_trades_count == 0
    assert summary.total_net_pnl_fiat > 0.0
    assert isinstance(summary.trade_log_df, pl.DataFrame)
    assert len(summary.trade_log_df) == summary.total_trades


def test_strategy_evaluator_maker_only(mock_replay_frames):
    custom_maker = DualSidedMakerSpreadModel(
        penny_jump_epsilon=0.002, min_net_margin_pct=0.5
    )
    scanner = OpportunityScanner(maker_model=custom_maker)
    evaluator = StrategyEvaluator(
        scanner=scanner,
        initial_fiat=10_000.0,
        initial_crypto=10_000.0,
        default_rail=CanonicalRail.REVOLUT,
    )

    summary = evaluator.run_backtest(
        replay_stream=iter(mock_replay_frames),
        mode=BacktestMode.MAKER_ONLY,
        trade_size_fiat=1000.0,
    )

    assert summary.total_trades >= 1
    assert summary.maker_trades_count >= 1
    assert summary.taker_trades_count == 0
    assert summary.total_net_pnl_fiat > 0.0


def test_strategy_evaluator_adaptive_mode(mock_replay_frames):
    custom_maker = DualSidedMakerSpreadModel(
        penny_jump_epsilon=0.002, min_net_margin_pct=0.5
    )
    scanner = OpportunityScanner(maker_model=custom_maker)
    evaluator = StrategyEvaluator(
        scanner=scanner,
        initial_fiat=10_000.0,
        initial_crypto=10_000.0,
        default_rail=CanonicalRail.REVOLUT,
    )

    summary = evaluator.run_backtest(
        replay_stream=iter(mock_replay_frames),
        mode=BacktestMode.ADAPTIVE,
        trade_size_fiat=1000.0,
    )

    # In adaptive mode: captures Taker opportunity in Frame 0 and Maker opportunity in Frame 1
    assert summary.total_trades >= 2
    assert summary.taker_trades_count >= 1
    assert summary.maker_trades_count >= 1
    assert summary.win_rate_pct == 100.0
    assert summary.total_roi_pct > 0.0
