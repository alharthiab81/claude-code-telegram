"""US stock market monitoring: pre-market movers scan and watchlist alerts.

This package is intentionally independent of the Claude agent pipeline —
it talks to a market-data provider (Finnhub) and to Telegram directly, so
scheduled runs are fast and don't spend Claude API calls on numeric checks.
"""
