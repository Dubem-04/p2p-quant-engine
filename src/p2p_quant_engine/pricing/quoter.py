from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from p2p_quant_engine.pricing.config import (
    BULK_ACQUISITION_FEE_RATE,
    BYBIT_MAKER_FEE_RATE,
    BYBIT_TAKER_FEE_RATE,
    DEFAULT_BASE_HALF_SPREAD_FIAT,
    DEFAULT_MIN_NET_PROFIT_FIAT,
    DEFAULT_REFERENCE_TRADE_CRYPTO,
    DEFAULT_TICK_SIZE_FIAT,
    EXCHANGE_FEE_HEADROOM_RATE,
    TOTAL_DEPOSIT_FEE_NGN,
    TOTAL_TRANSFER_FEE_NGN,
)


@dataclass(frozen=True)
class FeeConfig:
    """Exchange, bulk sourcing, and fiat rail fee parameters with headroom."""

    maker_fee_rate: float = BYBIT_MAKER_FEE_RATE + EXCHANGE_FEE_HEADROOM_RATE
    taker_fee_rate: float = BYBIT_TAKER_FEE_RATE + EXCHANGE_FEE_HEADROOM_RATE
    bulk_acquisition_fee_rate: float = BULK_ACQUISITION_FEE_RATE
    fiat_deposit_fee: float = TOTAL_DEPOSIT_FEE_NGN
    fiat_transfer_fee: float = TOTAL_TRANSFER_FEE_NGN


@dataclass(frozen=True)
class QuoterConfig:
    """Market-making spread and competitive positioning parameters."""

    base_half_spread_fiat: float = DEFAULT_BASE_HALF_SPREAD_FIAT
    min_net_profit_fiat: float = DEFAULT_MIN_NET_PROFIT_FIAT
    tick_size_fiat: float = DEFAULT_TICK_SIZE_FIAT
    reference_trade_crypto: float = DEFAULT_REFERENCE_TRADE_CRYPTO


@dataclass(frozen=True)
class QuoteResult:
    """Computed bid/ask quotes and diagnostic pricing boundaries."""

    bid_price: float  # Price at which we BUY crypto (outgoing fiat transfer)
    ask_price: float  # Price at which we SELL crypto (incoming fiat deposit)
    bid_ceiling: float  # Highest allowed BUY price before violating min profit
    ask_floor: float  # Lowest allowed SELL price before violating min profit
    spread_fiat: float  # Final quoted spread (ask_price - bid_price)


class DynamicQuoter:
    """Computes full-cycle fee-aware, competitively clamped P2P quotes."""

    def __init__(
        self,
        fee_config: Optional[FeeConfig] = None,
        quoter_config: Optional[QuoterConfig] = None,
    ) -> None:
        self.fees = fee_config or FeeConfig()
        self.config = quoter_config or QuoterConfig()

    def _round_tick(self, price: float) -> float:
        """Round price cleanly to the configured exchange tick size."""
        tick = Decimal(str(self.config.tick_size_fiat))
        quantized = Decimal(str(price)).quantize(tick, rounding=ROUND_HALF_UP)
        return float(quantized)

    def _ask_cost_buffer(self, fair_value: float, trade_size_crypto: float) -> float:
        """
        Total fee + min net profit buffer per 1 crypto unit when SELLING crypto (Ask).
        Accounts for:
          1. Exchange maker fee
          2. Bulk crypto acquisition/replenishment cost rate
          3. Incoming fiat deposit fee (amortized over trade size)
          4. Minimum required net profit per unit
        """
        if trade_size_crypto <= 0:
            raise ValueError("trade_size_crypto must be positive.")

        exchange_fee = fair_value * self.fees.maker_fee_rate
        sourcing_fee = fair_value * self.fees.bulk_acquisition_fee_rate
        deposit_fee_per_unit = self.fees.fiat_deposit_fee / trade_size_crypto
        return (
            exchange_fee
            + sourcing_fee
            + deposit_fee_per_unit
            + self.config.min_net_profit_fiat
        )

    def _bid_cost_buffer(self, fair_value: float, trade_size_crypto: float) -> float:
        """
        Total fee + min net profit buffer per 1 crypto unit when BUYING crypto (Bid).
        Accounts for:
          1. Exchange maker fee
          2. Outgoing fiat bank transfer fee (amortized over trade size)
          3. Minimum required net profit per unit
        """
        if trade_size_crypto <= 0:
            raise ValueError("trade_size_crypto must be positive.")

        exchange_fee = fair_value * self.fees.maker_fee_rate
        transfer_fee_per_unit = self.fees.fiat_transfer_fee / trade_size_crypto
        return exchange_fee + transfer_fee_per_unit + self.config.min_net_profit_fiat

    def generate_quotes(
        self,
        fair_value_fiat: float,
        best_competitor_bid: Optional[float] = None,
        best_competitor_ask: Optional[float] = None,
        trade_size_crypto: Optional[float] = None,
    ) -> QuoteResult:
        """
        Calculate optimal BUY (bid) and SELL (ask) quotes around fair value.

        Args:
            fair_value_fiat: Current true mid-market price of 1 crypto unit in fiat.
            best_competitor_bid: Highest qualifying competitor BUY price (optional).
            best_competitor_ask: Lowest qualifying competitor SELL price (optional).
            trade_size_crypto: Expected trade size to amortize bank fees (optional).
        """
        if fair_value_fiat <= 0:
            raise ValueError("fair_value_fiat must be positive.")

        ref_size = (
            trade_size_crypto
            if trade_size_crypto is not None
            else self.config.reference_trade_crypto
        )

        # 1. Asymmetric Hard Profit Boundaries (Ask Floor vs. Bid Ceiling)
        ask_floor = self._round_tick(
            fair_value_fiat + self._ask_cost_buffer(fair_value_fiat, ref_size)
        )
        bid_ceiling = self._round_tick(
            fair_value_fiat - self._bid_cost_buffer(fair_value_fiat, ref_size)
        )

        # Baseline target quotes clamped so base spread never breaches profit margins
        raw_bid = min(fair_value_fiat - self.config.base_half_spread_fiat, bid_ceiling)
        raw_ask = max(fair_value_fiat + self.config.base_half_spread_fiat, ask_floor)

        # 2. Top-of-Book Competitive Undercutting (Bounded by Profit Floor & Ceiling)
        if best_competitor_bid is not None:
            competitive_bid = best_competitor_bid + self.config.tick_size_fiat
            final_bid = min(max(raw_bid, competitive_bid), bid_ceiling)
        else:
            final_bid = raw_bid

        if best_competitor_ask is not None:
            competitive_ask = best_competitor_ask - self.config.tick_size_fiat
            final_ask = max(min(raw_ask, competitive_ask), ask_floor)
        else:
            final_ask = raw_ask

        final_bid = self._round_tick(final_bid)
        final_ask = self._round_tick(final_ask)

        if final_bid >= final_ask:
            raise RuntimeError(
                f"Crossed market quote generated: bid={final_bid} >= ask={final_ask}"
            )

        return QuoteResult(
            bid_price=final_bid,
            ask_price=final_ask,
            bid_ceiling=bid_ceiling,
            ask_floor=ask_floor,
            spread_fiat=self._round_tick(final_ask - final_bid),
        )
