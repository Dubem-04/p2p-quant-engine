from datetime import datetime, timezone

import polars as pl
import pytest

from src.scrapers.base_scraper import P2POrderLevel
from src.signals.spread_models import (
    DualSidedMakerSpreadModel,
    OpportunityScanner,
    StrategyType,
    TakerTakerArbitrageDetector,
)
from src.storage.orderbook_replay import ReconstructedBook, ReplayFrame


@pytest.fixture
def sample_books():
    now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

    # Venue A: Cheap asks (1.01), low bids (0.98) -> Good to BUY on Venue A
    book_a = ReconstructedBook(
        venue="binance",
        asset="USDT",
        fiat="USD",
        timestamp_utc=now,
        bids=[
            P2POrderLevel(
                ad_id="a_b1",
                venue="binance",
                side="BUY",
                asset="USDT",
                fiat="USD",
                price=0.98,
                available_crypto=5000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=5000.0,
                payment_methods=["Bank Transfer", "Zelle"],
                merchant_id="m1",
                merchant_name="M1",
                merchant_completed_orders=200,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
        asks=[
            P2POrderLevel(
                ad_id="a_a1",
                venue="binance",
                side="SELL",
                asset="USDT",
                fiat="USD",
                price=1.01,
                available_crypto=3000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=3030.0,
                payment_methods=["Bank Transfer"],
                merchant_id="m2",
                merchant_name="M2",
                merchant_completed_orders=150,
                merchant_completion_rate=0.98,
                is_pro_merchant=True,
            )
        ],
    )

    # Venue B: High bids (1.04), high asks (1.06) -> Good to SELL on Venue B
    book_b = ReconstructedBook(
        venue="bybit",
        asset="USDT",
        fiat="USD",
        timestamp_utc=now,
        bids=[
            P2POrderLevel(
                ad_id="b_b1",
                venue="bybit",
                side="BUY",
                asset="USDT",
                fiat="USD",
                price=1.04,
                available_crypto=2000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=2080.0,
                payment_methods=["Bank Transfer", "Revolut"],
                merchant_id="m3",
                merchant_name="M3",
                merchant_completed_orders=300,
                merchant_completion_rate=0.99,
                is_pro_merchant=True,
            )
        ],
        asks=[
            P2POrderLevel(
                ad_id="b_a1",
                venue="bybit",
                side="SELL",
                asset="USDT",
                fiat="USD",
                price=1.06,
                available_crypto=2000.0,
                min_fiat_limit=10.0,
                max_fiat_limit=2120.0,
                payment_methods=["Bank Transfer"],
                merchant_id="m4",
                merchant_name="M4",
                merchant_completed_orders=100,
                merchant_completion_rate=0.97,
                is_pro_merchant=False,
            )
        ],
    )

    return book_a, book_b


def test_taker_taker_detector_success(sample_books):
    book_a, book_b = sample_books
    detector = TakerTakerArbitrageDetector(
        min_net_spread_pct=1.0, default_taker_fee_pct=0.0
    )

    # Directed evaluation: Buy on Binance (ask=1.01), Sell on Bybit (bid=1.04)
    signal = detector.evaluate_pair(
        buy_book=book_a,
        sell_book=book_b,
        target_fiat_amount=1000.0,
    )

    assert signal is not None
    assert signal.strategy == StrategyType.TAKER_TAKER
    assert signal.buy_venue == "binance"
    assert signal.sell_venue == "bybit"
    assert signal.buy_price == pytest.approx(1.01)
    assert signal.sell_price == pytest.approx(1.04)
    # Gross spread = (1.04 - 1.01) / 1.01 = 2.97%
    assert signal.gross_spread_pct == pytest.approx(2.9703, rel=1e-3)
    assert signal.net_spread_pct == pytest.approx(2.9703, rel=1e-3)
    assert "Bank Transfer" in signal.common_payment_methods
    assert signal.metadata["net_profit_fiat"] > 0.0


def test_taker_taker_detector_rejects_unprofitable_or_no_overlap(sample_books):
    book_a, book_b = sample_books

    # In reverse: Buy on Bybit (ask=1.06), Sell on Binance (bid=0.98) -> Massive loss
    detector = TakerTakerArbitrageDetector(min_net_spread_pct=0.5)
    reverse_sig = detector.evaluate_pair(
        buy_book=book_b,
        sell_book=book_a,
        target_fiat_amount=1000.0,
    )
    assert reverse_sig is None

    # Replace frozen P2POrderLevel with new instance having no payment overlap
    no_overlap_ask = book_a.asks[0].model_copy(update={"payment_methods": ["CashApp"]})
    book_a_no_overlap = ReconstructedBook(
        venue=book_a.venue,
        asset=book_a.asset,
        fiat=book_a.fiat,
        timestamp_utc=book_a.timestamp_utc,
        bids=book_a.bids,
        asks=[no_overlap_ask],
    )

    no_overlap_sig = detector.evaluate_pair(
        buy_book=book_a_no_overlap,
        sell_book=book_b,
        target_fiat_amount=1000.0,
    )
    assert no_overlap_sig is None


def test_dual_sided_maker_spread_model(sample_books):
    book_a, _ = sample_books
    # Book A: best_bid = 0.98, best_ask = 1.01. Raw spread = 3.06%
    maker_model = DualSidedMakerSpreadModel(
        min_net_margin_pct=1.0,
        default_maker_fee_pct=0.20,  # 0.40% round trip
        penny_jump_epsilon=0.002,
    )

    signal = maker_model.evaluate_venue(
        book=book_a, target_fiat_amount=1000.0, inventory_skew=0.0
    )

    assert signal is not None
    assert signal.strategy == StrategyType.DUAL_MAKER
    assert signal.buy_venue == "binance"
    assert signal.sell_venue == "binance"

    # Penny jumped: bid = 0.98 + 0.002 = 0.982, ask = 1.01 - 0.002 = 1.008
    assert signal.buy_price == pytest.approx(0.982)
    assert signal.sell_price == pytest.approx(1.008)

    gross_pct = ((1.008 - 0.982) / 0.982) * 100.0
    net_pct = gross_pct - 0.40
    assert signal.gross_spread_pct == pytest.approx(gross_pct)
    assert signal.net_spread_pct == pytest.approx(net_pct)
    assert signal.net_spread_pct > 1.0


def test_dual_sided_maker_inventory_skew(sample_books):
    book_a, _ = sample_books
    maker_model = DualSidedMakerSpreadModel(
        min_net_margin_pct=0.5,
        default_maker_fee_pct=0.20,
        penny_jump_epsilon=0.002,
        skew_intensity=0.10,
    )

    # Neutral quote
    neutral_sig = maker_model.evaluate_venue(book=book_a, inventory_skew=0.0)

    # Long crypto (+1.0 skew) -> Should shade quotes down to offload crypto
    long_crypto_sig = maker_model.evaluate_venue(book=book_a, inventory_skew=1.0)

    assert long_crypto_sig is not None
    assert neutral_sig is not None
    assert long_crypto_sig.buy_price < neutral_sig.buy_price
    assert long_crypto_sig.sell_price < neutral_sig.sell_price


def test_opportunity_scanner_and_polars_export(sample_books):
    book_a, book_b = sample_books
    now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

    frame = ReplayFrame(
        timestamp_utc=now,
        books={"binance": book_a, "bybit": book_b},
        is_synchronized=True,
    )

    # Configure scanner with sensitive thresholds matching the fixture spread
    custom_maker = DualSidedMakerSpreadModel(
        min_net_margin_pct=0.5,
        default_maker_fee_pct=0.20,
        penny_jump_epsilon=0.002,
    )
    scanner = OpportunityScanner(maker_model=custom_maker)
    signals = scanner.scan_frame(frame, target_fiat_amount=1000.0)

    # Detects both cross-venue Taker-Taker and intra-venue Maker signals
    assert len(signals) >= 2
    strategies = {s.strategy for s in signals}
    assert StrategyType.TAKER_TAKER in strategies
    assert StrategyType.DUAL_MAKER in strategies

    # Vectorize to Polars DataFrame
    df = scanner.signals_to_df(signals)
    assert isinstance(df, pl.DataFrame)
    assert len(df) == len(signals)
    assert "net_spread_pct" in df.columns
    assert "strategy" in df.columns
