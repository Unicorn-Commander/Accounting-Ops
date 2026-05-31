"""Connectors package — clients for external financial APIs.

Each connector module lives under app/connectors/<name>/ and exposes:
  - client.py — async HTTP client w/ auth refresh
  - mapper.py — converts the connector's wire shape to journal_entry rows

eBay Finances is the first connector that does OAuth-authorization-code at the
app level (SimpleFIN uses a one-shot setup-token claim). Future Stripe, Plaid
connectors will follow the same shape.
"""
