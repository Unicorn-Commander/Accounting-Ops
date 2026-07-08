"""eBay Finances connector — Sell Finances API ingestion.

End-to-end flow:

  OAuth (app/api/ebay_oauth.py)
      -> stores blob {client_id, client_secret, refresh_token, environment, ...}
         encrypted in public.tenant_credentials (kind='ebay_finances')

  Sync job (scripts/ebay_sync.py + ebay-sync compose service)
      -> hourly: pulls new payouts, then transactions for each payout
         -> mapper.map_transaction(...) -> double-entry journal_entry

  Client (app/connectors/ebay/client.py)
      -> auto-refresh access token (~2h life) using the long-lived refresh
         token (~18mo); calls /sell/finances/v1/payout|transaction|payout_summary

  Mapper (app/connectors/ebay/mapper.py)
      -> SALE        DR 1220 eBay Payouts Receivable / CR 4150 eBay Revenue
         FEE         DR 5520 Payment Processor Fees  / CR 1220
         REFUND      reverses SALE
         NON_SALE    typically a fee/credit; routes per type detail
         PAYOUT      DR 1010 Cash · BoA Primary       / CR 1220
"""

from app.connectors.ebay.client import (
    EbayApiError,
    EbayCredentialBlob,
    EbayFinancesClient,
    load_ebay_credential,
)

__all__ = (
    "EbayApiError",
    "EbayCredentialBlob",
    "EbayFinancesClient",
    "load_ebay_credential",
)
