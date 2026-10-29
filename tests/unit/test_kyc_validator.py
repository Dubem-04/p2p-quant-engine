"""
Unit test suite for the counterparty KYC fuzzy string matching validator.

Tests name matching logic across edge cases: typos, reversed first/last names,
middle name omissions, special characters, and blatant mismatches. Asserts that
similarity thresholds accurately flag unverified third-party remitters.
"""

import pytest
from src.risk.kyc_validator import clean_name, validate_counterparty_kyc


def test_clean_name_removes_titles_and_noise():
    raw = "MR. Johnathan Chukwudi Doe /TRF/NIP"
    cleaned = clean_name(raw)
    assert "mr" not in cleaned
    assert "trf" not in cleaned
    assert "nip" not in cleaned
    assert "johnathan chukwudi doe" == cleaned


def test_exact_name_match():
    res = validate_counterparty_kyc("Jane Smith", "Jane Smith")
    assert res.is_valid is True
    assert res.similarity_score == 1.0


def test_transposed_first_last_names():
    # E.g., 'Emeka Okonkwo' vs 'Okonkwo Emeka'
    res = validate_counterparty_kyc("Emeka Okonkwo", "Okonkwo Emeka")
    assert res.is_valid is True
    assert res.similarity_score == 1.0


def test_minor_typo_within_threshold():
    # Minor single-character variance
    res = validate_counterparty_kyc("Jonathan Doe", "Johnathan Doe", threshold=0.90)
    assert res.is_valid is True
    assert res.similarity_score >= 0.90


def test_blatant_third_party_rejection():
    # Fraud attempt: KYC is Michael Adebayo, sender is Samuel Okon
    res = validate_counterparty_kyc("Michael Adebayo", "Samuel Okon", threshold=0.90)
    assert res.is_valid is False
    assert res.similarity_score < 0.50


def test_empty_string_rejection():
    res = validate_counterparty_kyc("", "John Doe")
    assert res.is_valid is False
    assert res.similarity_score == 0.0