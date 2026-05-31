"""Connectors package — clients for external data sources.

Each connector module lives under ``app/connectors/<name>/`` and exposes:
  - ``client.py`` — async HTTP client with auth refresh
  - ``mapper.py`` — converts the connector's wire shape to journal_entry rows
    (financial connectors only)

Inventory:
  - ``ebay/`` — eBay Sell-Finances API (OAuth authorization-code; first
    connector that does full OAuth at the app level; SimpleFIN uses a
    one-shot setup-token claim instead).
  - ``stripe/`` — Stripe REST API via restricted API key (no OAuth — Aaron
    owns the Stripe account under MU Inc EIN, so a read-only restricted
    key plus webhook signing secret is sufficient).
  - ``regulatory_feeds`` — IRS / SCDOR / Treasury feed scrapers feeding
    ``public.regulatory_feed_entry`` (consumed by Sentinel for proactive
    update detection).
"""
