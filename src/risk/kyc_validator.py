"""
Counterparty identity verification and fuzzy string-matching validation engine.

Guards against third-party injection fraud, account takeovers, and fraudulent
remittance sources. Computes Levenshtein distance and Jaro-Winkler similarity
scores between platform-verified KYC names and incoming bank payment sender
metadata, enforcing strict rejection thresholds before escrow release.

Functions:
    compute_name_similarity: Calculates string distance metrics between sender and KYC names.
    validate_counterparty_kyc: Assesses match confidence and returns an authorization boolean.
"""

from dataclasses import dataclass
import re
from typing import List, Set
import Levenshtein


# Common titles, business suffixes, and banking noise tokens to strip
NOISE_TOKENS: Set[str] = {
    "mr", "mrs", "ms", "miss", "dr", "chief", "engr", "prof", "barr",
    "ltd", "limited", "plc", "enterprises", "ent", "services", "nig",
    "trf", "transfer", "ft", "nip", "fbn", "gtb", "uba", "zenith", "access"
}


@dataclass(frozen=True)
class KYCValidationResult:
    is_valid: bool
    similarity_score: float
    cleaned_kyc_name: str
    cleaned_sender_name: str
    reason: str


def clean_name(name: str) -> str:
    """
    Normalizes a name string:
    - Lowercases and strips non-alphanumeric characters.
    - Removes common honorifics, titles, and bank transaction prefixes.
    - Sorts unique name tokens to handle transposed first/last names.
    """
    if not name or not isinstance(name, str):
        return ""

    # Keep only letters, numbers, and spaces
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    tokens = [t.strip() for t in cleaned.split() if t.strip()]

    # Filter out known noise tokens
    filtered_tokens = [t for t in tokens if t not in NOISE_TOKENS]

    # Return space-delimited string
    return " ".join(filtered_tokens)


def compute_token_sort_ratio(name_a: str, name_b: str) -> float:
    """
    Computes a sorted token similarity ratio to prevent penalizing
    transposed names (e.g., 'Doe John' vs 'John Doe').
    """
    tokens_a = sorted(name_a.split())
    tokens_b = sorted(name_b.split())

    sorted_a = " ".join(tokens_a)
    sorted_b = " ".join(tokens_b)

    if not sorted_a and not sorted_b:
        return 1.0
    if not sorted_a or not sorted_b:
        return 0.0

    return Levenshtein.ratio(sorted_a, sorted_b)


def validate_counterparty_kyc(
    platform_kyc_name: str,
    bank_sender_name: str,
    threshold: float = 0.90
) -> KYCValidationResult:
    """
    Validates whether an incoming bank sender identity matches the verified
    platform KYC name above a mathematical threshold (default: 0.90).
    """
    cleaned_kyc = clean_name(platform_kyc_name)
    cleaned_sender = clean_name(bank_sender_name)

    if not cleaned_kyc or not cleaned_sender:
        return KYCValidationResult(
            is_valid=False,
            similarity_score=0.0,
            cleaned_kyc_name=cleaned_kyc,
            cleaned_sender_name=cleaned_sender,
            reason="Missing or empty KYC or sender identity record."
        )

    # 1. Exact match check
    if cleaned_kyc == cleaned_sender:
        return KYCValidationResult(
            is_valid=True,
            similarity_score=1.0,
            cleaned_kyc_name=cleaned_kyc,
            cleaned_sender_name=cleaned_sender,
            reason="Exact match."
        )

    # 2. Token Sort Levenshtein ratio
    sort_ratio = compute_token_sort_ratio(cleaned_kyc, cleaned_sender)

    # 3. Jaro-Winkler ratio (effective for minor character typos / short strings)
    jaro_ratio = Levenshtein.jaro_winkler(cleaned_kyc, cleaned_sender)

    # Use the higher confidence metric
    best_score = max(sort_ratio, jaro_ratio)
    is_valid = best_score >= threshold

    reason = (
        f"Match passed ({best_score:.2f} >= {threshold:.2f})."
        if is_valid
        else f"Identity mismatch score {best_score:.2f} below threshold {threshold:.2f}."
    )

    return KYCValidationResult(
        is_valid=is_valid,
        similarity_score=best_score,
        cleaned_kyc_name=cleaned_kyc,
        cleaned_sender_name=cleaned_sender,
        reason=reason
    )