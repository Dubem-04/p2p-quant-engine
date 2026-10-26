<!--
DOCUMENT: docs/architecture.md
PURPOSE & CONTRIBUTION:
Serves as the high-level system architecture blueprint and engineering specification.
-->

# System Architecture & Component Interaction

### System Scope:
Outlines end-to-end design patterns across asynchronous workers, event-driven
pipelines, inter-process communication, and external platform adapters.

### Key Sections:
1. **Data Ingestion Loop**: Architecture of async scraping workers and rate-limit managers.
2. **Quoting & Pricing Core**: State machine managing real-time inventory adjustments.
3. **Execution & Settlement Engine**: Interaction between bank parsers and merchant APIs.
4. **Safety & Circuit Breakers**: Global fail-safe rules and dispute lockup boundaries.