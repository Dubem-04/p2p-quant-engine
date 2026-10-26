<!--
DOCUMENT: docs/runbooks/manual_settlement.md
PURPOSE & CONTRIBUTION:
Standard Operating Procedure (SOP) for operators handling Phase 1 trades and manual edge cases.
-->

# Standard Operating Procedure: Manual Order Settlement & Reconciliation

### Operational Scope:
Provides an operator checklist for order verification to ensure fraud elimination:
1. **Identity Verification**: Step-by-step verification of incoming bank sender names against KYC platform records.
2. **Fund Clearance Checks**: Protocol for confirming cleared, non-reversible funds before escrow release.
3. **Escrow Release Protocol**: Strict verification sequence to prevent premature crypto release.
4. **Audit Trail Logging**: Required fields to capture in the database immediately upon order fill.