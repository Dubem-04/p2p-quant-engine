"""
Quantitative Spread Models & Arbitrage Detectors for P2P Crypto Markets.

Implements two complementary strategy engines:
1. TakerTakerArbitrageDetector: Cross-venue immediate liquidity sweep detector.
2. DualSidedMakerSpreadModel: Intra-venue passive liquidity provision model with
   maker fees, penny-jumping placement, and inventory skewing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set

import polars as pl

from src.storage.orderbook_replay import ReconstructedBook, ReplayFrame


class StrategyType(str, Enum):
    TAKER_TAKER = "TAKER_TAKER"
    DUAL_MAKER = "DUAL_MAKER"


@dataclass
class ArbitrageSignal:
    """Universal normalized signal emitted by P2P quantitative models."""

    strategy: StrategyType
    asset: str
    fiat: str
    timestamp_utc: datetime
    buy_venue: str
    sell_venue: str
    buy_price: float
    sell_price: float
    gross_spread_pct: float
    net_spread_pct: float
    target_fiat_volume: float
    executable_crypto: float
    common_payment_methods: List[str] = field(default_factory=list)
    buy_merchants_count: int = 1
    sell_merchants_count: int = 1
    inventory_skew_applied: float = 0.0
    is_actionable: bool = True
    metadata: Dict[str, float] = field(default_factory=dict)


class TakerTakerArbitrageDetector:
    """
    Cross-venue spatial arbitrage detector.

    Evaluates instant taker sweeps across directed pairs (Venue A -> Venue B)
    accounting for order book depth (VWAP), ticket sizes, and payment rail overlap.
    """

    def __init__(
        self,
        min_net_spread_pct: float = 0.5,
        default_taker_fee_pct: float = 0.0,
        enforce_payment_overlap: bool = True,
    ):
        self.min_net_spread_pct = min_net_spread_pct
        self.default_taker_fee_pct = default_taker_fee_pct
        self.enforce_payment_overlap = enforce_payment_overlap

    def _get_common_payment_methods(
        self,
        buy_book: ReconstructedBook,
        sell_book: ReconstructedBook,
    ) -> List[str]:
        """Finds overlapping payment methods between buy asks and sell bids."""
        buy_methods: Set[str] = set()
        for ask in buy_book.asks:
            buy_methods.update(ask.payment_methods)

        sell_methods: Set[str] = set()
        for bid in sell_book.bids:
            sell_methods.update(bid.payment_methods)

        return sorted(list(buy_methods.intersection(sell_methods)))

    def evaluate_pair(
        self,
        buy_book: ReconstructedBook,
        sell_book: ReconstructedBook,
        target_fiat_amount: float,
        buy_fee_pct: Optional[float] = None,
        sell_fee_pct: Optional[float] = None,
    ) -> Optional[ArbitrageSignal]:
        """
        Evaluates a directed taker-taker sweep:
        Leg 1: Buy crypto with fiat on buy_book (sweeping asks).
        Leg 2: Sell crypto for fiat on sell_book (sweeping bids).
        """
        if buy_book.is_stale or sell_book.is_stale:
            return None

        # 1. Payment Rail Overlap Check
        common_rails = self._get_common_payment_methods(buy_book, sell_book)
        if self.enforce_payment_overlap and not common_rails:
            return None

        # 2. Execution Sweep against order book depth
        # Leg 1: Sweep asks to buy target fiat worth of crypto
        buy_sweep = buy_book.calculate_sweep_vwap(
            side="BUY",
            target_amount=target_fiat_amount,
            quote_in_fiat=True,
        )
        if not buy_sweep or not buy_sweep.is_fully_filled:
            return None

        # Leg 2: Sweep bids to sell the exact crypto volume acquired
        crypto_to_sell = buy_sweep.filled_crypto
        sell_sweep = sell_book.calculate_sweep_vwap(
            side="SELL",
            target_amount=crypto_to_sell,
            quote_in_fiat=False,
        )
        if not sell_sweep or not sell_sweep.is_fully_filled:
            return None

        buy_vwap = buy_sweep.vwap
        sell_vwap = sell_sweep.vwap

        # Gross Spread = (Sell VWAP - Buy VWAP) / Buy VWAP
        gross_spread_pct = ((sell_vwap - buy_vwap) / buy_vwap) * 100.0

        # Net Spread after taker fees
        f_buy = self.default_taker_fee_pct if buy_fee_pct is None else buy_fee_pct
        f_sell = self.default_taker_fee_pct if sell_fee_pct is None else sell_fee_pct
        net_spread_pct = gross_spread_pct - (f_buy + f_sell)

        if net_spread_pct < self.min_net_spread_pct:
            return None

        timestamp = max(buy_book.timestamp_utc, sell_book.timestamp_utc)

        return ArbitrageSignal(
            strategy=StrategyType.TAKER_TAKER,
            asset=buy_book.asset,
            fiat=buy_book.fiat,
            timestamp_utc=timestamp,
            buy_venue=buy_book.venue,
            sell_venue=sell_book.venue,
            buy_price=buy_vwap,
            sell_price=sell_vwap,
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            target_fiat_volume=target_fiat_amount,
            executable_crypto=crypto_to_sell,
            common_payment_methods=common_rails,
            buy_merchants_count=buy_sweep.merchants_used,
            sell_merchants_count=sell_sweep.merchants_used,
            is_actionable=True,
            metadata={
                "buy_slippage_pct": buy_sweep.effective_slippage_pct,
                "sell_slippage_pct": sell_sweep.effective_slippage_pct,
                "fiat_proceeds": sell_sweep.filled_fiat,
                "net_profit_fiat": sell_sweep.filled_fiat - target_fiat_amount,
            },
        )


class DualSidedMakerSpreadModel:
    """
    Intra-venue dual-sided market making spread model.

    Models passive liquidity provision by posting both Buy and Sell ads on
    a single venue, harvesting the bid-ask spread while accounting for maker
    fees, competitive placement (penny-jumping), and inventory skew.
    """

    def __init__(
        self,
        min_net_margin_pct: float = 0.8,
        default_maker_fee_pct: float = 0.20,
        penny_jump_epsilon: float = 0.01,
        skew_intensity: float = 0.005,
    ):
        """
        Args:
            min_net_margin_pct: Minimum round-trip net profit after dual maker fees.
            default_maker_fee_pct: Platform maker ad fee percentage per leg.
            penny_jump_epsilon: Nominal increment to undercut asks or outbid bids.
            skew_intensity: Price shift percentage per unit of normalized inventory skew.
        """
        self.min_net_margin_pct = min_net_margin_pct
        self.default_maker_fee_pct = default_maker_fee_pct
        self.penny_jump_epsilon = penny_jump_epsilon
        self.skew_intensity = skew_intensity

    def evaluate_venue(
        self,
        book: ReconstructedBook,
        target_fiat_amount: float = 1000.0,
        inventory_skew: float = 0.0,
        maker_fee_pct: Optional[float] = None,
    ) -> Optional[ArbitrageSignal]:
        """
        Evaluates round-trip maker opportunity on a single venue's order book.

        Args:
            book: Point-in-time reconstructed book.
            target_fiat_amount: Intended quoting capital depth.
            inventory_skew: Normalized metric in [-1.0, 1.0].
                +1.0 = Excess crypto (shade prices downward to sell faster).
                -1.0 = Excess fiat (shade prices upward to buy faster).
            maker_fee_pct: Maker fee per leg (defaults to default_maker_fee_pct).
        """
        if book.is_stale or not book.bids or not book.asks:
            return None

        fee_per_leg = (
            self.default_maker_fee_pct if maker_fee_pct is None else maker_fee_pct
        )
        round_trip_fees_pct = fee_per_leg * 2.0

        # Current top-of-book prices
        market_best_bid = book.bids[0].price
        market_best_ask = book.asks[0].price

        if market_best_bid >= market_best_ask:
            return None  # Inverted or crossed book

        # 1. Baseline Penny-Jumping Quotes (Sitting 1 tick inside the spread)
        # We bid slightly higher than current best bid, ask slightly lower than best ask
        proposed_bid = market_best_bid + self.penny_jump_epsilon
        proposed_ask = market_best_ask - self.penny_jump_epsilon

        # Ensure we haven't crossed the spread with our own quotes
        if proposed_bid >= proposed_ask:
            # Spread is too tight (<= 2 ticks) to penny-jump inside
            return None

        # 2. Avellaneda-Stoikov Inventory Skew Adjustment
        # Shift quotes proportionally to inventory skew
        # skew > 0 (long crypto): shift both bid and ask down by skew_offset
        # skew < 0 (long fiat): shift both bid and ask up by skew_offset
        skew_offset = (proposed_ask - proposed_bid) * (
            inventory_skew * self.skew_intensity
        )
        adjusted_bid = proposed_bid - skew_offset
        adjusted_ask = proposed_ask - skew_offset

        # 3. Compute Gross & Net Round-Trip Margin
        gross_spread_pct = ((adjusted_ask - adjusted_bid) / adjusted_bid) * 100.0
        net_spread_pct = gross_spread_pct - round_trip_fees_pct

        if net_spread_pct < self.min_net_margin_pct:
            return None

        common_rails = list(
            set(book.bids[0].payment_methods).intersection(
                set(book.asks[0].payment_methods)
            )
        )

        crypto_capacity = target_fiat_amount / adjusted_bid

        return ArbitrageSignal(
            strategy=StrategyType.DUAL_MAKER,
            asset=book.asset,
            fiat=book.fiat,
            timestamp_utc=book.timestamp_utc,
            buy_venue=book.venue,
            sell_venue=book.venue,
            buy_price=adjusted_bid,
            sell_price=adjusted_ask,
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            target_fiat_volume=target_fiat_amount,
            executable_crypto=crypto_capacity,
            common_payment_methods=common_rails,
            inventory_skew_applied=inventory_skew,
            is_actionable=True,
            metadata={
                "market_best_bid": market_best_bid,
                "market_best_ask": market_best_ask,
                "market_raw_spread_pct": (
                    (market_best_ask - market_best_bid) / market_best_bid
                )
                * 100.0,
                "round_trip_maker_fees_pct": round_trip_fees_pct,
                "net_profit_fiat": (target_fiat_amount * (net_spread_pct / 100.0)),
            },
        )


class OpportunityScanner:
    """
    Unified scanner running both Taker-Taker and Dual-Maker signal models
    over synchronized ReplayFrames or live book snapshots.
    """

    def __init__(
        self,
        taker_detector: Optional[TakerTakerArbitrageDetector] = None,
        maker_model: Optional[DualSidedMakerSpreadModel] = None,
    ):
        self.taker_detector = taker_detector or TakerTakerArbitrageDetector()
        self.maker_model = maker_model or DualSidedMakerSpreadModel()

    def scan_frame(
        self,
        frame: ReplayFrame,
        target_fiat_amount: float = 1000.0,
        inventory_skew: float = 0.0,
    ) -> List[ArbitrageSignal]:
        """Scans a ReplayFrame for all valid Taker-Taker and Dual-Maker opportunities."""
        signals: List[ArbitrageSignal] = []
        venues = list(frame.books.keys())

        # 1. Run Dual-Sided Maker Spread Model on each venue individually
        for _venue_name, book in frame.books.items():
            maker_sig = self.maker_model.evaluate_venue(
                book=book,
                target_fiat_amount=target_fiat_amount,
                inventory_skew=inventory_skew,
            )
            if maker_sig:
                signals.append(maker_sig)

        # 2. Run Cross-Venue Taker-Taker Detector across all directed pairs
        for v_buy in venues:
            for v_sell in venues:
                if v_buy == v_sell:
                    continue

                buy_book = frame.books[v_buy]
                sell_book = frame.books[v_sell]

                taker_sig = self.taker_detector.evaluate_pair(
                    buy_book=buy_book,
                    sell_book=sell_book,
                    target_fiat_amount=target_fiat_amount,
                )
                if taker_sig:
                    signals.append(taker_sig)

        return signals

    @staticmethod
    def signals_to_df(signals: List[ArbitrageSignal]) -> pl.DataFrame:
        """Vectorizes a list of ArbitrageSignals into a native Polars DataFrame."""
        if not signals:
            return pl.DataFrame(
                schema={
                    "timestamp_utc": pl.Datetime("us", "UTC"),
                    "strategy": pl.Utf8,
                    "asset": pl.Utf8,
                    "fiat": pl.Utf8,
                    "buy_venue": pl.Utf8,
                    "sell_venue": pl.Utf8,
                    "buy_price": pl.Float64,
                    "sell_price": pl.Float64,
                    "gross_spread_pct": pl.Float64,
                    "net_spread_pct": pl.Float64,
                    "target_fiat_volume": pl.Float64,
                    "executable_crypto": pl.Float64,
                    "net_profit_fiat": pl.Float64,
                }
            )

        records = [
            {
                "timestamp_utc": s.timestamp_utc,
                "strategy": s.strategy.value,
                "asset": s.asset,
                "fiat": s.fiat,
                "buy_venue": s.buy_venue,
                "sell_venue": s.sell_venue,
                "buy_price": s.buy_price,
                "sell_price": s.sell_price,
                "gross_spread_pct": s.gross_spread_pct,
                "net_spread_pct": s.net_spread_pct,
                "target_fiat_volume": s.target_fiat_volume,
                "executable_crypto": s.executable_crypto,
                "net_profit_fiat": s.metadata.get("net_profit_fiat", 0.0),
            }
            for s in signals
        ]
        return pl.DataFrame(records)
