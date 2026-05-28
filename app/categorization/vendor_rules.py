"""Vendor rules: regex/substring → canonical category.

Rules are ordered: first match wins. Patterns are case-insensitive
substring matches on the concatenation of (payee + description + memo).

Each rule is a tuple of `(pattern, category, kind, direction)`:

* `pattern`   — lowercase substring matched against the blob
* `category`  — canonical category string (must appear in
  `CATEGORY_TO_ACCOUNT` for the entry to actually post)
* `kind`      — `"revenue" | "expense" | "transfer"`; used by the engine
  to validate the categorization makes sense relative to the txn sign
* `direction` — `"inflow" | "outflow" | "any"`; controls whether this
  rule is even *considered* for the txn's amount sign. `"any"` (the
  default for legacy 3-tuples) keeps prior behavior. Use direction-
  scoped rules when the same vendor substring legitimately appears on
  both sides of the ledger (e.g. PayPal pay-outs to the account vs.
  PayPal-as-payment-processor for an outbound vendor charge).

Categories follow the chart-of-accounts shape we'll formalize in
PHASE-2-CATEGORIZATION.md:

  Revenue · Marketplace      — Stripe / eBay / Shopify payouts
  Revenue · Music            — MLC / SoundExchange / Spotify
  SaaS · AI                  — Anthropic / OpenAI / OpenRouter / Claude / ChatGPT
  SaaS · Email               — Postmark / Mailgun
  SaaS · Productivity        — Notion / Linear / GitHub / Apple / Adobe / Xero / Google
  Infrastructure · VPS       — Hostinger / DigitalOcean / Linode
  Infrastructure · CDN       — Cloudflare / AWS / Fastly
  Infrastructure · Network   — Cisco Meraki / Tailscale / Verizon (telecom)
  Contractor · 1099-NEC      — Named contractors (Aaron will add)
  Inter-entity transfer      — Mercury internal ACH between owned accts
  Banking · Fees             — Wire fees, ACH returns, NSF, overdraft, monthly account fees
  Banking · Interest         — Interest payments
  Travel · Lodging           — Hotels, Airbnb
  Travel · Transport         — Airlines, rental cars, Uber
  Meals                      — Restaurants, food delivery
  Office · Supplies          — Amazon, Staples, generic PayPal outflows
  Inventory · Cost           — Listing-Ops cost basis (Daniel Tran purchases)
  Uncategorized              — Tier 1 unable to assign; Tier 2 will try
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["inflow", "outflow", "any"]

# Rules are 3-tuples `(pattern, category, kind)` for back-compat or
# 4-tuples `(pattern, category, kind, direction)` where direction filters
# the rule against the txn amount sign BEFORE substring matching.
#
# Ordering rule: more-specific patterns must come before less-specific
# (substring) patterns, because the matcher returns on first hit.
VENDOR_RULES: list[tuple] = [
    # ---- Revenue ----
    ("stripe", "Revenue · Marketplace", "revenue"),
    # PayPal: direction-scoped. Inbound transfers from a PayPal balance
    # to the bank account are marketplace revenue payouts; outbound (the
    # bank account paying PayPal or paying a vendor via PayPal's network)
    # is an expense (treated as office supplies — a conservative,
    # CPA-correctable default since the sub-merchant on a PayPal swipe
    # is rarely unambiguous from the bank statement alone).
    ("paypal", "Revenue · Marketplace", "revenue", "inflow"),
    ("paypal", "Office · Supplies", "expense", "outflow"),
    ("shopify payout", "Revenue · Marketplace", "revenue"),
    ("ebay finances", "Revenue · Marketplace", "revenue"),
    ("ebay payout", "Revenue · Marketplace", "revenue"),
    ("ebay order", "Revenue · Marketplace", "revenue"),
    ("ebay:order:", "Revenue · Marketplace", "revenue"),
    ("etsy", "Revenue · Marketplace", "revenue"),
    ("mlc", "Revenue · Music", "revenue"),
    ("soundexchange", "Revenue · Music", "revenue"),
    ("spotify for artists", "Revenue · Music", "revenue"),
    ("distrokid", "Revenue · Music", "revenue"),
    ("tunecore", "Revenue · Music", "revenue"),

    # ---- SaaS · AI ----
    ("anthropic", "SaaS · AI", "expense"),
    ("claude.ai", "SaaS · AI", "expense"),
    ("openai", "SaaS · AI", "expense"),
    ("chatgpt", "SaaS · AI", "expense"),
    ("openrouter", "SaaS · AI", "expense"),
    ("perplexity", "SaaS · AI", "expense"),
    ("replicate", "SaaS · AI", "expense"),
    ("huggingface", "SaaS · AI", "expense"),
    ("lambda labs", "SaaS · AI", "expense"),

    # ---- SaaS · Email ----
    ("postmark", "SaaS · Email", "expense"),
    ("mailgun", "SaaS · Email", "expense"),
    ("sendgrid", "SaaS · Email", "expense"),
    ("resend", "SaaS · Email", "expense"),

    # ---- SaaS · Productivity ----
    ("notion labs", "SaaS · Productivity", "expense"),
    ("linear app", "SaaS · Productivity", "expense"),
    ("github inc", "SaaS · Productivity", "expense"),
    ("github.com", "SaaS · Productivity", "expense"),
    ("1password", "SaaS · Productivity", "expense"),
    ("figma", "SaaS · Productivity", "expense"),
    # Apple billing covers iCloud / App Store subscriptions used as
    # business productivity tools. Direction-scoped to outflow because
    # the rare inbound Apple charge (a refund) belongs in /approvals.
    ("apple.com/bill", "SaaS · Productivity", "expense", "outflow"),
    # Adobe Creative Cloud / Acrobat / single-app subscriptions show up
    # as ADOBE *PRODUCTS, ADOBE INC., ADOBE *ACROPRO, etc.
    ("adobe ", "SaaS · Productivity", "expense", "outflow"),
    ("adobe*", "SaaS · Productivity", "expense", "outflow"),
    ("adobe inc", "SaaS · Productivity", "expense", "outflow"),
    # Xero accounting software. The ACH descriptor is always
    # `Xero Inc DES:Xero ...` — direction-scoped to outflow to keep any
    # future inbound (refund / credit memo) in /approvals.
    ("xero inc", "SaaS · Productivity", "expense", "outflow"),
    ("xero us in", "SaaS · Productivity", "expense", "outflow"),
    # Pictory.ai video-summary subscription; descriptor variants include
    # PICTORY*, PICTORY VIDEO SOFTWARE, PICTORY PICTORY.AI.
    ("pictory.ai", "SaaS · Productivity", "expense", "outflow"),
    ("pictory*", "SaaS · Productivity", "expense", "outflow"),
    # Soundraw.io AI music generator (descriptor: SOUNDRAW INC.).
    ("soundraw", "SaaS · Productivity", "expense", "outflow"),
    # IPVM video-surveillance industry research subscription.
    ("ipvm", "SaaS · Productivity", "expense", "outflow"),
    # Google billing — Google Workspace / Google One / Google Play /
    # Google Cloud all bill as `GOOGLE *<product>`. We can't always
    # disambiguate Workspace vs Cloud from the descriptor; SaaS ·
    # Productivity is the safer default (Workspace dominates by volume
    # on a small-business card). Outflow-only.
    ("google *google", "SaaS · Productivity", "expense", "outflow"),

    # ---- Infrastructure ----
    ("hostinger", "Infrastructure · VPS", "expense"),
    ("digitalocean", "Infrastructure · VPS", "expense"),
    ("linode", "Infrastructure · VPS", "expense"),
    ("vultr", "Infrastructure · VPS", "expense"),
    ("cloudflare", "Infrastructure · CDN", "expense"),
    ("amazon web services", "Infrastructure · CDN", "expense"),
    ("aws", "Infrastructure · CDN", "expense"),
    ("fastly", "Infrastructure · CDN", "expense"),
    ("cisco meraki", "Infrastructure · Network", "expense"),
    ("tailscale", "Infrastructure · Network", "expense"),
    # Verizon Wireless business line. Descriptor variants:
    # VZWRLSS*APOCC, VZWRLSS*BILL PAY, VERIZONWRLSS*RTCCR,
    # VERIZON WRLS IVR. We route to Infrastructure · Network as the
    # closest existing bucket (closer than nothing; the COA does not
    # currently carry a "Utilities · Telecom" account so admin can
    # split later if desired).
    ("vzwrlss", "Infrastructure · Network", "expense", "outflow"),
    ("verizonwrlss", "Infrastructure · Network", "expense", "outflow"),
    ("verizon wrls", "Infrastructure · Network", "expense", "outflow"),

    # ---- Banking ----
    ("wire fee", "Banking · Fees", "expense"),
    ("ach return", "Banking · Fees", "expense"),
    ("nsf fee", "Banking · Fees", "expense"),
    ("overdraft", "Banking · Fees", "expense"),
    # Bank-of-America business account monthly maintenance fee.
    ("monthly fee business adv", "Banking · Fees", "expense", "outflow"),
    ("interest earned", "Banking · Interest", "revenue"),
    ("interest paid", "Banking · Interest", "expense"),

    # ---- Inter-entity transfers ----
    ("mercury · ach transfer", "Inter-entity transfer", "transfer"),
    ("mercury ach", "Inter-entity transfer", "transfer"),
    ("internal transfer", "Inter-entity transfer", "transfer"),

    # ---- Travel ----
    ("airbnb", "Travel · Lodging", "expense"),
    ("marriott", "Travel · Lodging", "expense"),
    ("hilton", "Travel · Lodging", "expense"),
    ("uber", "Travel · Transport", "expense"),
    ("lyft", "Travel · Transport", "expense"),
    ("delta air", "Travel · Transport", "expense"),
    ("united airlines", "Travel · Transport", "expense"),
    ("american airlines", "Travel · Transport", "expense"),
    ("southwest air", "Travel · Transport", "expense"),

    # ---- Meals ----
    ("doordash", "Meals", "expense"),
    ("uber eats", "Meals", "expense"),
    ("grubhub", "Meals", "expense"),
    ("chipotle", "Meals", "expense"),
    ("starbucks", "Meals", "expense"),

    # ---- Office · Supplies ----
    ("amazon.com", "Office · Supplies", "expense"),
    ("amzn mktp", "Office · Supplies", "expense"),
    ("staples", "Office · Supplies", "expense"),
    ("best buy", "Office · Supplies", "expense"),
]


@dataclass(frozen=True)
class RuleMatch:
    pattern: str
    category: str
    kind: str        # "revenue" | "expense" | "transfer"
    direction: Direction = "any"


def _direction_matches(rule_direction: Direction, amount: float | None) -> bool:
    """Return True if a direction-scoped rule is eligible for this amount.

    `amount=None` means "we don't know the sign" — in that case any rule
    is eligible (back-compat with callers that pre-date direction
    awareness). A `rule_direction` of "any" is always eligible.
    """
    if rule_direction == "any" or amount is None:
        return True
    if rule_direction == "inflow":
        return amount > 0
    if rule_direction == "outflow":
        return amount < 0
    return True


def match_vendor_rule(blob: str, amount: float | None = None) -> RuleMatch | None:
    """Find the first rule whose pattern is a substring of blob (lowercased).

    If `amount` is provided, direction-scoped rules whose direction
    doesn't match the amount's sign are skipped. This lets the same
    substring (e.g. "paypal") legitimately appear in multiple rules with
    different category bindings per direction.

    Caller should pre-lowercase blob; we lowercase defensively.
    """
    lo = blob.lower()
    for rule in VENDOR_RULES:
        if len(rule) == 4:
            pattern, category, kind, direction = rule
        else:
            pattern, category, kind = rule
            direction = "any"
        if not _direction_matches(direction, amount):
            continue
        if pattern in lo:
            return RuleMatch(
                pattern=pattern,
                category=category,
                kind=kind,
                direction=direction,
            )
    return None
