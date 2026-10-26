"""
Unit test suite for the counterparty KYC fuzzy string matching validator.

Tests name matching logic across edge cases: typos, reversed first/last names,
middle name omissions, special characters, and blatant mismatches. Asserts that
similarity thresholds accurately flag unverified third-party remitters.
"""