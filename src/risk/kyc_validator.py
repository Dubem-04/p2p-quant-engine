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