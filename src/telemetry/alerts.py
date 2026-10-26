"""
Operational webhook alerting and notification handler.

Dispatches real-time automated notifications to external communication channels
(Telegram, Discord) regarding critical operational events: new order fills,
counterparty payment receipts, dispute escalations, spread threshold breaches,
circuit-breaker triggers, and daily turnover summaries.

Functions:
    send_telegram_alert: Formats and dispatches asynchronous messages to a Telegram bot.
    send_discord_alert: Emits structured embed payloads to configured Discord webhook channels.
"""