"""Deterministic + LLM-assisted transaction categorization.

Two-tier system:
  Tier 1: deterministic vendor rules (vendor_rules.py). Covers ~70%+ of
          recurring transactions. Fast, auditable, no external calls.
  Tier 2: LLM fallback via local Qwen 3.6 with GBNF grammar. Reserved
          for transactions Tier 1 can't confidently classify.

Categorical output: status ∈ {auto, suggest, unknown}. Never numeric
confidence — mirrors the same convention used in worker_extractions and
receipt_transaction_link.
"""

from app.categorization.engine import categorize_transaction, CategorySuggestion

__all__ = ["categorize_transaction", "CategorySuggestion"]
