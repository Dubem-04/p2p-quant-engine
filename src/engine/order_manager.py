"""
Central order lifecycle orchestrator and merchant advertisement controller.

Coordinates active P2P ad listings across supported exchange venues via merchant APIs.
Automates ad publication, dynamic price modifications, ad pauses, and limit
adjustments in response to signals emitted by the dynamic quoter, exposure manager,
and spot drift models.

Classes:
    OrderLifecycleManager: Manages live state, creation, and cancellation of exchange listings.
"""