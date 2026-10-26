"""
Abstract base class and interface contract for all exchange P2P order book scrapers.

Defines standardized asynchronous ingestion routines, rate limiting compliance,
exponential backoff retry mechanisms, and normalized data formatting for L2
snapshots. Enforces uniform schema output across heterogeneous exchange APIs.

Classes:
    BaseP2PScraper: Abstract base class providing common network session management,
        error handling, and serialization signatures for venue-specific workers.
"""