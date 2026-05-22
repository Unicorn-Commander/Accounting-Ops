"""Vendor rules: regex/substring → canonical category.

Rules are ordered: first match wins. Patterns are case-insensitive
substring matches on the concatenation of (payee + description + memo).

Categories follow the chart-of-accounts shape we'll formalize in
PHASE-2-CATEGORIZATION.md:

  Revenue · Marketplace      — Stripe / eBay / Shopify payouts
  Revenue · Music            — MLC / SoundExchange / Spotify
  SaaS · AI                  — Anthropic / OpenAI / OpenRouter
  SaaS · Email               — Postmark / Mailgun
  SaaS · Productivity        — Notion / Linear / GitHub
  Infrastructure · VPS       — Hostinger / DigitalOcean / Linode
  Infrastructure · CDN       — Cloudflare / AWS / Fastly
  Infrastructure · Network   — Cisco Meraki / Tailscale
  Contractor · 1099-NEC      — Named contractors (Aaron will add)
  Inter-entity transfer      — Mercury internal ACH between owned accts
  Banking · Fees             — Wire fees, ACH returns, NSF
  Banking · Interest         — Interest payments
  Travel · Lodging           — Hotels, Airbnb
  Travel · Transport         — Airlines, rental cars, Uber
  Meals                      — Restaurants, food delivery
  Office · Supplies          — Amazon, Staples
  Inventory · Cost           — Listing-Ops cost basis (Daniel Tran purchases)
  Uncategorized              — Tier 1 unable to assign; Tier 2 will try
"""

from __future__ import annotations

from dataclasses import dataclass

# (pattern_substring_lowercase, category, kind)
# kind: "revenue" | "expense" | "transfer" — used by the engine to validate
# the categorization makes sense relative to the txn sign.
VENDOR_RULES: list[tuple[str, str, str]] = [
    # ---- Revenue ----
    ("stripe", "Revenue · Marketplace", "revenue"),
    ("paypal", "Revenue · Marketplace", "revenue"),
    ("shopify payout", "Revenue · Marketplace", "revenue"),
    ("ebay finances", "Revenue · Marketplace", "revenue"),
    ("ebay payout", "Revenue · Marketplace", "revenue"),
    ("etsy", "Revenue · Marketplace", "revenue"),
    ("mlc", "Revenue · Music", "revenue"),
    ("soundexchange", "Revenue · Music", "revenue"),
    ("spotify for artists", "Revenue · Music", "revenue"),
    ("distrokid", "Revenue · Music", "revenue"),
    ("tunecore", "Revenue · Music", "revenue"),

    # ---- SaaS · AI ----
    ("anthropic", "SaaS · AI", "expense"),
    ("openai", "SaaS · AI", "expense"),
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

    # ---- Banking ----
    ("wire fee", "Banking · Fees", "expense"),
    ("ach return", "Banking · Fees", "expense"),
    ("nsf fee", "Banking · Fees", "expense"),
    ("overdraft", "Banking · Fees", "expense"),
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
    kind: str  # "revenue" | "expense" | "transfer"


def match_vendor_rule(blob: str) -> RuleMatch | None:
    """Find the first rule whose pattern is a substring of blob (lowercased).

    Caller should pre-lowercase blob; we lowercase defensively.
    """
    lo = blob.lower()
    for pattern, category, kind in VENDOR_RULES:
        if pattern in lo:
            return RuleMatch(pattern=pattern, category=category, kind=kind)
    return None
