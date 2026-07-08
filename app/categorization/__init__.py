"""Deterministic + LLM-assisted transaction categorization.

Two-tier system:
  Tier 1: deterministic vendor rules (vendor_rules.py). Covers ~70%+ of
          recurring transactions. Fast, auditable, no external calls.
  Tier 2: LLM fallback via ``LLMCategorizer`` (llm.py). Reserved for
          transactions Tier 1 can't confidently classify. Model-agnostic:
          targets any OpenAI-compatible endpoint (local Ollama / vLLM or
          cloud OpenRouter / OpenAI / Anthropic).

Categorical output: status ∈ {auto, suggest, unknown}. Never numeric
confidence in the main ``CategorySuggestion`` — mirrors the same convention
used in worker_extractions and receipt_transaction_link.  The LLM module
does expose a ``confidence`` float in its own ``LLMCategorySuggestion`` for
logging / audit purposes only.
"""

from app.categorization.engine import (
    apply_learned_pattern,
    categorize_transaction,
    categorize_transaction_async,
    CategorySuggestion,
)
from app.categorization.llm import LLMCategorizer, LLMCategorySuggestion

__all__ = [
    "apply_learned_pattern",
    "categorize_transaction",
    "categorize_transaction_async",
    "CategorySuggestion",
    "LLMCategorizer",
    "LLMCategorySuggestion",
]
