"""
Level-2 order book ingestion worker for Bybit P2P liquidity venues.

Handles asynchronous polling of Bybit P2P order listings across targeted fiat
currencies. Captures counterparty merchant badges, pricing premiums relative to
spot, available inventory volumes, and min/max order thresholds to enable
cross-venue arbitrage detection and competitive quotation tracking.

Functions:
    poll_bybit_listings: Queries Bybit P2P order book feeds with session reuse.
    standardize_bybit_order: Normalizes Bybit ad schemas into system-wide L2 representation.
"""

import asyncio
from typing import Any, Dict, List, Optional

from src.scrapers.base_scraper import (
    BaseP2PScraper,
    P2POrderBookSnapshot,
    P2POrderLevel,
    VenueAPIError,
)


class BybitP2PScraper(BaseP2PScraper):
    """
    Bybit P2P L2 Order Book Scraper.
    """

    SEARCH_URL = "https://api2.bybit.com/fiat/otc/item/online"

    # Browser headers required by Bybit edge gateway
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.bybit.com",
        "Referer": "https://www.bybit.com/en/p2p/",
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
    }

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 3,
        base_backoff: float = 1.0,
    ):
        super().__init__(
            venue_name="bybit",
            timeout=timeout,
            max_retries=max_retries,
            base_backoff=base_backoff,
        )

    async def _fetch_side_raw(
        self,
        asset: str,
        fiat: str,
        side_code: str,
        depth: int,
        payments: Optional[List[str]] = None,
    ) -> tuple[Dict[str, Any], float]:
        """
        Queries Bybit OTC online search endpoint for a specific side code:
        - side_code="0": Maker asks (sellers offering higher prices).
        - side_code="1": Maker bids (buyers paying lower prices).
        """
        payload = {
            "tokenId": asset.upper(),
            "currencyId": fiat.upper(),
            "side": str(side_code),
            "size": str(depth),
            "page": "1",
            "amount": "",
            "authMaker": False,
            "canTrade": False,
            "payment": payments or [],
        }

        data, latency_ms = await self._request(
            "POST",
            self.SEARCH_URL,
            json=payload,
            headers=self.DEFAULT_HEADERS,
        )

        if not isinstance(data, dict) or data.get("ret_code") != 0:
            err_msg = (
                data.get("ret_msg") if isinstance(data, dict) else "Malformed payload"
            )
            raise VenueAPIError(f"Bybit P2P API error for side {side_code}: {err_msg}")

        return data, latency_ms

    async def _fetch_raw_order_book(
        self,
        asset: str,
        fiat: str,
        depth: int = 5,
    ) -> tuple[Dict[str, Any], float]:
        """
        Concurrently queries both sides of the book via HTTP/2 multiplexing.
        - side="1" -> Bids (Buyers)
        - side="0" -> Asks (Sellers)
        """
        (bids_data, bids_lat), (asks_data, asks_lat) = await asyncio.gather(
            self._fetch_side_raw(asset, fiat, side_code="1", depth=depth),
            self._fetch_side_raw(asset, fiat, side_code="0", depth=depth),
        )

        composite_raw = {
            "bids_raw": bids_data.get("result", {}).get("items", []),
            "asks_raw": asks_data.get("result", {}).get("items", []),
        }

        effective_latency = max(bids_lat, asks_lat)
        return composite_raw, effective_latency

    def _parse_single_ad(
        self,
        item: Dict[str, Any],
        side: str,
        asset: str,
        fiat: str,
    ) -> Optional[P2POrderLevel]:
        """Defensively maps a single Bybit ad item to P2POrderLevel."""
        try:
            ad_id = str(item.get("id", ""))
            price = float(item.get("price", 0.0))
            available_crypto = float(item.get("lastQuantity", 0.0))
            min_fiat_limit = float(item.get("minAmount", 0.0))
            max_fiat_limit = float(item.get("maxAmount", 0.0))

            # Payment methods: list of strings or string payment names
            raw_payments = item.get("payments", []) or []
            payment_methods = [
                (
                    p
                    if isinstance(p, str)
                    else str(p.get("paymentName", p.get("paymentType", "")))
                )
                for p in raw_payments
            ]
            payment_methods = [pm for pm in payment_methods if pm]

            merchant_id = str(item.get("userId", ""))
            merchant_name = str(item.get("nickName", "Unknown"))
            merchant_completed_orders = int(item.get("recentOrderNum", 0))

            # recentExecuteRate can be 98 or 0.98; normalize to 0.0-1.0
            raw_rate = float(item.get("recentExecuteRate", 0.0))
            completion_rate = raw_rate / 100.0 if raw_rate > 1.0 else raw_rate

            is_pro = bool(item.get("authMaker") or item.get("isOnline", False))

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
            return None

    def parse_payload(
        self,
        raw_data: Any,
        asset: str,
        fiat: str,
        latency_ms: float,
    ) -> P2POrderBookSnapshot:
        """
        Parses composite Bybit raw payload into a validated P2POrderBookSnapshot.
        Sorts bids descending by price and asks ascending by price.
        """
        if not isinstance(raw_data, dict):
            raise VenueAPIError("Invalid payload shape: expected composite dictionary.")

        raw_bids = raw_data.get("bids_raw", [])
        raw_asks = raw_data.get("asks_raw", [])

        # Parse BIDS (side="BUY")
        parsed_bids: List[P2POrderLevel] = []
        for item in raw_bids:
            level = self._parse_single_ad(item, side="BUY", asset=asset, fiat=fiat)
            if level:
                parsed_bids.append(level)

        # Parse ASKS (side="SELL")
        parsed_asks: List[P2POrderLevel] = []
        for item in raw_asks:
            level = self._parse_single_ad(item, side="SELL", asset=asset, fiat=fiat)
            if level:
                parsed_asks.append(level)

        # Standard L2 Order Book sorting:
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
