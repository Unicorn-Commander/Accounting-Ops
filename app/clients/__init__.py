"""External-service HTTP clients used by API routes.

Each client wraps one external dependency (SimpleFIN, eBay Finances MCP,
IMAP MCP, etc.) and returns data in the canonical transaction/account
shape so the categorization + journal pipeline stays source-agnostic.
"""
