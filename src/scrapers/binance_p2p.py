"""
Level-2 order book ingestion worker for Binance P2P exchange rails.

Executes asynchronous HTTP/REST requests against Binance P2P public quotation
endpoints to collect top-of-book bids and asks, merchant completion rates,
order limits, and supported fiat payment rails (e.g., Bank Transfer, NGN/USD).
Normalizes incoming payload into structured order book level snapshots.

Functions:
    fetch_order_book_depth: Queries the Binance P2P API for current active advertisements.
    parse_ad_payload: Transforms raw API dictionary response into structured Level-2 models.
"""

import asyncio
from typing import Any, Dict, List, Optional

from src.scrapers.base_scraper import (
    BaseP2PScraper,
    P2POrderBookSnapshot,
    P2POrderLevel,
    VenueAPIError,
)


class BinanceP2PScraper(BaseP2PScraper):
    """
    Binance P2P L2 Order Book Scraper.
    """

    SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 3,
        base_backoff: float = 1.0,
    ):
        super().__init__(
            venue_name="binance",
            timeout=timeout,
            max_retries=max_retries,
            base_backoff=base_backoff,
        )

    async def _fetch_side_raw(
        self,
        asset: str,
        fiat: str,
        trade_type: str,
        depth: int,
        pay_types: Optional[List[str]] = None,
    ) -> tuple[Dict[str, Any], float]:
        """
        Queries the Binance adv search endpoint for a specific tradeType.
        tradeType='BUY' returns maker asks (sellers).
        tradeType='SELL' returns maker bids (buyers).
        """
        payload = {
            "asset": asset.upper(),
            "fiat": fiat.upper(),
            "merchantCheck": False,
            "page": 1,
            "payTypes": pay_types or [],
            "publisherType": None,
            "rows": depth,
            "tradeType": trade_type.upper(),
        }

        data, latency_ms = await self._request("POST", self.SEARCH_URL, json=payload)

        if not isinstance(data, dict) or data.get("code") != "000000":
            err_msg = (
                data.get("message") if isinstance(data, dict) else "Malformed response"
            )
            raise VenueAPIError(f"Binance P2P API error for {trade_type}: {err_msg}")

        return data, latency_ms

    async def _fetch_raw_order_book(
        self,
        asset: str,
        fiat: str,
        depth: int = 5,
    ) -> tuple[Dict[str, Any], float]:
        """
        Concurrently queries both sides of the book via HTTP/2 multiplexing.

        Binance API Semantics:
        - tradeType="BUY":  Returns ads at lower/bid prices -> BIDS (side="BUY")
        - tradeType="SELL": Returns ads at higher/ask prices -> ASKS (side="SELL")
        """
        (bids_data, bids_lat), (asks_data, asks_lat) = await asyncio.gather(
            self._fetch_side_raw(asset, fiat, trade_type="BUY", depth=depth),
            self._fetch_side_raw(asset, fiat, trade_type="SELL", depth=depth),
        )

        composite_raw = {
            "bids_raw": bids_data.get("data", []),
            "asks_raw": asks_data.get("data", []),
        }

        effective_latency = max(bids_lat, asks_lat)
        return composite_raw, effective_latency

    def _parse_single_ad(
        self,
        ad_wrapper: Dict[str, Any],
        side: str,
        asset: str,
        fiat: str,
    ) -> Optional[P2POrderLevel]:
        """Defensively maps a single raw ad item to P2POrderLevel."""
        try:
            adv = ad_wrapper.get("adv", {})
            advertiser = ad_wrapper.get("advertiser", {})

            ad_id = str(adv.get("advNo", ""))
            price = float(adv.get("price", 0.0))
            available_crypto = float(
                adv.get("tradableQuantity") or adv.get("surplusAmount") or 0.0
            )
            min_fiat_limit = float(adv.get("minSingleTransAmount", 0.0))
            max_fiat_limit = float(
                adv.get("dynamicMaxSingleTransAmount")
                or adv.get("maxSingleTransAmount")
                or 0.0
            )

            # Extract payment method names
            trade_methods = adv.get("tradeMethods", []) or []
            payment_methods = [
                tm.get("tradeMethodName") or tm.get("identifier", "")
                for tm in trade_methods
                if isinstance(tm, dict)
            ]
            payment_methods = [pm for pm in payment_methods if pm]

            merchant_id = str(advertiser.get("userNo", ""))
            merchant_name = str(advertiser.get("nickName", "Unknown"))
            merchant_completed_orders = int(advertiser.get("monthOrderCount", 0))

            # Finish rate can be returned as 0.985 or 98.5
            raw_rate = float(advertiser.get("monthFinishRate", 0.0))
            completion_rate = raw_rate / 100.0 if raw_rate > 1.0 else raw_rate

            is_pro = advertiser.get("userType") == "merchant"

            if price <= 0 or max_fiat_limit <= 0 or not ad_id:
                return None

            return P2POrderLevel(
                ad_id=ad_id,
                venue=self.venue_name,
                side=side,
                asset=asset,
                fiat=fiat,
                price=price,
                available_crypto=available_crypto,
                min_fiat_limit=min_fiat_limit,
                max_fiat_limit=max_fiat_limit,
                payment_methods=payment_methods,
                merchant_id=merchant_id,
                merchant_name=merchant_name,
                merchant_completed_orders=merchant_completed_orders,
                merchant_completion_rate=completion_rate,
                is_pro_merchant=is_pro,
            )
        except (ValueError, TypeError, KeyError):
            # Skip corrupted ad entries rather than crashing the pipeline
            return None

    def parse_payload(
        self,
        raw_data: Any,
        asset: str,
        fiat: str,
        latency_ms: float,
    ) -> P2POrderBookSnapshot:
        """
        Parses composite raw payload into a validated P2POrderBookSnapshot.
        Sorts bids descending by price and asks ascending by price.
        """
        if not isinstance(raw_data, dict):
            raise VenueAPIError("Invalid payload shape: expected composite dictionary.")

        raw_bids = raw_data.get("bids_raw", [])
        raw_asks = raw_data.get("asks_raw", [])

        # 1. Parse BIDS (side="BUY")
        parsed_bids: List[P2POrderLevel] = []
        for item in raw_bids:
            level = self._parse_single_ad(item, side="BUY", asset=asset, fiat=fiat)
            if level:
                parsed_bids.append(level)

        # 2. Parse ASKS (side="SELL")
        parsed_asks: List[P2POrderLevel] = []
        for item in raw_asks:
            level = self._parse_single_ad(item, side="SELL", asset=asset, fiat=fiat)
            if level:
                parsed_asks.append(level)

        # 3. Sort standard L2 book:
        # Bids: Best bid (highest buying price) first
        # Asks: Best ask (lowest selling price) first
        sorted_bids = sorted(parsed_bids, key=lambda x: x.price, reverse=True)
        sorted_asks = sorted(parsed_asks, key=lambda x: x.price, reverse=False)

        return P2POrderBookSnapshot(
            venue=self.venue_name,
            asset=asset,
            fiat=fiat,
            bids=sorted_bids,
            asks=sorted_asks,
            latency_ms=latency_ms,
        )
