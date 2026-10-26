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