"""Stripe connector — Stripe API ingestion via restricted API key.

End-to-end flow:

  Credential capture (app/api/stripe_oauth.py — POST /api/workspaces/{wid}/connectors/stripe/key)
      -> validates the restricted API key against Stripe by calling GET /v1/account
      -> stores blob {api_key, account_id, environment, webhook_secret?, ...}
         encrypted in public.tenant_credentials (kind='stripe_api_key')

  Sync job (scripts/stripe_sync.py + stripe-sync compose service)
      -> hourly: pulls new balance_transactions since `connector_state.last_sync_at`
         -> mapper.map_and_post_balance_transaction(...) -> double-entry journal_entry

  Client (app/connectors/stripe/client.py)
      -> Bearer-auth httpx client; lists charges/refunds/payouts/balance_transactions
         with cursor (starting_after) pagination; retries 429 with exponential backoff;
         carries Stripe-Version: 2024-06-20 on every call

  Mapper (app/connectors/stripe/mapper.py)
      -> charge          DR 1210 Stripe Receivable / CR 4xxx Revenue (configurable)
         payment_refund  reverses charge
         stripe_fee      DR 5520 Payment Processor Fees / CR 1210
         payout          DR 1010 Cash · BoA Primary    / CR 1210
         application_fee / other → CPA review queue

WHY NO STRIPE CONNECT:
  Aaron OWNS the Stripe account directly under MU Inc EIN (00-0000000). He's
  not a platform with sub-merchants — he's the merchant. So a restricted API
  key with READ access to charges/refunds/payouts/balance_transactions/account
  is sufficient. No OAuth, no Connect, no platform fee. Restricted keys can
  be revoked from the Stripe dashboard if compromised.

Webhook URL (Aaron registers in Stripe dashboard):
  https://accounting-ops.magicunicorn.dev/api/connectors/stripe/webhook

  (Note: the hyphenated form is the only one with DNS today. eBay's RuName
  was historically registered to accounting.magicunicorn.dev — non-hyphenated
  — and would need Aaron to either add DNS for it or re-register. For Stripe
  there's no prior registration, so use the resolving form.)
"""

from app.connectors.stripe.client import (
    StripeApiError,
    StripeCredentialBlob,
    StripeClient,
    load_stripe_credential,
)

__all__ = (
    "StripeApiError",
    "StripeCredentialBlob",
    "StripeClient",
    "load_stripe_credential",
)
