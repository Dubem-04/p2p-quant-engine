"""
Supervised counterparty fraud and dispute probability classification engine.

Evaluates incoming counterparty trading profiles using historical metrics,
including 30-day completion rates, account longevity, trade frequency, average
release latency, and payment rail risk. Outputs an operational risk score to
automatically accept, flag, or reject incoming trade counterparties.

Classes:
    CounterpartyRiskClassifier: Predicts dispute probability from counterparty metadata.
"""