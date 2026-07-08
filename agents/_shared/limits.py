"""Shared per-call limits for every specialist / narrative runner.

This module is the single source of truth for the per-LLM-call output token
budget used by all tax specialists and the CPA narrative generator. Centralised
here so a fleet-wide tuning (e.g. bumping the budget for a longer draft) is a
one-line change instead of an N-specialist scavenger hunt.

Imported by:

- ``agents/tax_specialists/form_1120_preparer/run_specialist.py``
- ``agents/tax_specialists/officer_compensation_specialist/run_specialist.py``
- ``agents/tax_specialists/sc_state_specialist/run_specialist.py``
- ``agents/tax_specialists/1099_specialist/run_specialist.py``
- ``agents/tax_specialists/nol_schedule_l_analyst/run_specialist.py``
- ``agents/tax_specialists/audit_defense_specialist/run_specialist.py``
- ``agents/tax_specialists/federal_strategy_drafter/run_specialist.py``
- ``agents/tax_specialists/state_strategy_drafter/run_specialist.py``
- ``agents/tax_specialists/personal_1040_specialist/run_specialist.py``
- ``agents/cpa_team/narrative_generator/run_narrative_generator.py``
"""

from __future__ import annotations

#: Per-call output token budget for specialist + narrative LLM calls.
#:
#: **Why 16384 is the floor, not the ceiling:**
#:
#: The original budget of 8192 tokens turned out to be too small for our
#: specialist workflow. A typical specialist run emits between 8 and 15 tool
#: calls (``draft_memo``, ``flag_for_cpa_review``, ``get_period_journal``,
#: etc.) plus a final Markdown draft. The 8K budget left the model with no
#: headroom — drafts got truncated mid-section, tool-call ``arguments`` JSON
#: blobs blew the budget silently on some endpoints, and the CPA narrative
#: generator (which emits a longer narrative-style document) was hitting the
#: cap reliably.
#:
#: Bumping to 16384 gives the model enough room for:
#:
#: - 8–15 tool-call arguments JSON blobs
#: - The full DRAFT Markdown output (cover, sections 1–8, memo IDs, flag IDs)
#: - Some headroom for chain-of-thought before the model commits to output
#:
#: If individual runners later need a tighter budget (e.g. cost-sensitive
#: paths), import this and override locally — don't lower this constant for
#: everyone. Coordinator-style runners that only emit a tiny routing decision
#: (e.g. ``max_tokens=64`` in ``coordinator/run_coordinator.py``) intentionally
#: do NOT import this constant.
MAX_OUTPUT_TOKENS: int = 16384

__all__ = ["MAX_OUTPUT_TOKENS"]
