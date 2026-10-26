"""
End-to-end integration test suite for the complete order settlement lifecycle.

Simulates the entire operational loop using mocked exchange APIs and banking webhooks:
ad creation, order match ingestion, KYC validation, bank payment parsing,
delta-neutral hedge placement, and automated escrow release.
"""