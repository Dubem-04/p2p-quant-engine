"""
Automated bank payment verification parser and transaction reconciliation engine.

Ingests and normalizes incoming payment alerts from banking webhooks, secure SMS
gateways, and encrypted email notifications. Extracts transaction reference numbers,
sender identities, and credited amounts, matching them against pending open orders
before authorizing automated crypto release.

Classes:
    BankPaymentParser: Parses raw remittance messages and matches funds to open P2P orders.
"""
