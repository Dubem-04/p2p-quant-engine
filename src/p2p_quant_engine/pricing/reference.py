from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

from p2p_quant_engine.pricing.config import (
    MAX_QUALIFYING_MIN_AMOUNT_FIAT,
    MIN_AVAILABLE_CRYPTO_LIQUIDITY,
    MIN_MERCHANT_COMPLETED_ORDERS,
    MIN_MERCHANT_COMPLETION_RATE,
    MIN_QUALIFYING_MAX_AMOUNT_FIAT,
    REFERENCE_TOP_N_ADS,
)

AdSide = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class P2PAd:
    """Normalized representation of a live P2P advertisement on an exchange."""

    ad_id: str
    merchant_id: str
    side: AdSide  # "SELL" = merchant sells crypto (Ask); "BUY" = merchant buys (Bid)
    price_fiat: float
    available_crypto: float
    min_amount_fiat: float
    max_amount_fiat: float
    completed_orders: int
    completion_rate: float  # Decimal between 0.0 and 1.0 (e.g., 0.95 = 95%)


@dataclass(frozen=True)
class FilterConfig:
    """Quality and liquidity thresholds for filtering noisy/bait P2P ads."""

    min_completed_orders: int = MIN_MERCHANT_COMPLETED_ORDERS
    min_completion_rate: float = MIN_MERCHANT_COMPLETION_RATE
    min_qualifying_max_amount_fiat: float = MIN_QUALIFYING_MAX_AMOUNT_FIAT
    max_qualifying_min_amount_fiat: float = MAX_QUALIFYING_MIN_AMOUNT_FIAT
    min_available_crypto: float = MIN_AVAILABLE_CRYPTO_LIQUIDITY
    top_n_ads: int = REFERENCE_TOP_N_ADS
    excluded_merchant_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MarketReference:
    """Computed fair value benchmark and top-of-book competitor context."""

    fair_value_fiat: float
    vwap_bid_fiat: float
    vwap_ask_fiat: float
    best_competitor_bid: Optional[float]
    best_competitor_ask: Optional[float]
    qualifying_bid_count: int
    qualifying_ask_count: int
    external_divergence_pct: Optional[float] = None


class ReferencePriceEstimator:
    """Filters P2P order book ads and computes Liquidity-Weighted Mid-Price (VWAP)."""

    def __init__(self, filter_config: Optional[FilterConfig] = None) -> None:
        self.config = filter_config or FilterConfig()

    def _is_qualifying(self, ad: P2PAd) -> bool:
        """Return True if the ad passes merchant reliability and liquidity checks."""
        if ad.price_fiat <= 0 or ad.available_crypto <= 0:
            return False
        if ad.merchant_id in self.config.excluded_merchant_ids:
            return False
        if ad.completed_orders < self.config.min_completed_orders:
            return False
        if ad.completion_rate < self.config.min_completion_rate:
            return False
        if ad.available_crypto < self.config.min_available_crypto:
            return False
        if ad.max_amount_fiat < self.config.min_qualifying_max_amount_fiat:
            return False
        if ad.min_amount_fiat > self.config.max_qualifying_min_amount_fiat:
            return False
        return True

    @staticmethod
    def _compute_vwap(ads: list[P2PAd]) -> float:
        """Compute available-liquidity-weighted average price across given ads."""
        total_crypto = sum(ad.available_crypto for ad in ads)
        if total_crypto <= 0:
            raise ValueError("Total available crypto liquidity must be positive.")
        weighted_sum = sum(ad.price_fiat * ad.available_crypto for ad in ads)
        return weighted_sum / total_crypto

    def estimate(
        self,
        ads: Iterable[P2PAd],
        external_reference_fiat: Optional[float] = None,
    ) -> MarketReference:
        """
        Filter raw P2P ads and compute the fair value benchmark.

        Args:
            ads: Iterable of raw P2PAd items from both sides of the order book.
            external_reference_fiat: Optional external benchmark rate for
            divergence check.
        """
        if external_reference_fiat is not None and external_reference_fiat <= 0:
            raise ValueError("external_reference_fiat must be positive when provided.")

        qualifying_bids: list[P2PAd] = []
        qualifying_asks: list[P2PAd] = []

        for ad in ads:
            if not self._is_qualifying(ad):
                continue
            if ad.side == "BUY":
                qualifying_bids.append(ad)
            elif ad.side == "SELL":
                qualifying_asks.append(ad)
            else:
                raise ValueError(f"Unsupported ad side: {ad.side}")

        if not qualifying_bids or not qualifying_asks:
            raise ValueError(
                "Insufficient qualifying liquidity: need at least one valid BUY "
                f"({len(qualifying_bids)}) and SELL ({len(qualifying_asks)}) ad."
            )

        # Sort Bids descending (highest price first) and Asks ascending (lowest first)
        qualifying_bids.sort(key=lambda a: a.price_fiat, reverse=True)
        qualifying_asks.sort(key=lambda a: a.price_fiat)

        top_bids = qualifying_bids[: self.config.top_n_ads]
        top_asks = qualifying_asks[: self.config.top_n_ads]

        vwap_bid = self._compute_vwap(top_bids)
        vwap_ask = self._compute_vwap(top_asks)
        fair_value = (vwap_bid + vwap_ask) / 2.0

        divergence_pct: Optional[float] = None
        if external_reference_fiat is not None:
            divergence_pct = (
                abs(fair_value - external_reference_fiat) / external_reference_fiat
            )

        return MarketReference(
            fair_value_fiat=round(fair_value, 4),
            vwap_bid_fiat=round(vwap_bid, 4),
            vwap_ask_fiat=round(vwap_ask, 4),
            best_competitor_bid=top_bids[0].price_fiat,
            best_competitor_ask=top_asks[0].price_fiat,
            qualifying_bid_count=len(qualifying_bids),
            qualifying_ask_count=len(qualifying_asks),
            external_divergence_pct=(
                round(divergence_pct, 6) if divergence_pct is not None else None
            ),
        )
