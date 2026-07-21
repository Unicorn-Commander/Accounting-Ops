"""Shared FRONTIER-LLM config for the CPA-team agents (the 9 tax-specialist
runners + the coordinator; the narrative generator uses the equivalent
``narrative_llm_*`` settings via its trigger).

Why this exists
---------------
These agents synthesise long-form CPA prose/advice — a FRONTIER job. But they
historically resolved their endpoint/model/key from ``LLM_ENDPOINT_URL`` /
``LLM_MODEL`` / ``LLM_API_KEY`` (and ``resolve_llm_api_key``, which prefers
``$LLM_API_KEY``). The vision/OCR work repurposed those ``LLM_*`` vars for the
receipt-VLM (Qwen vision @ :8088), so every specialist runner silently inherited
the VISION model + key → 401 "Missing Authentication header" on OpenRouter.

This helper gives the CPA-team frontier path its OWN config (``CPATEAM_LLM_*``),
defaulting to OpenRouter Sonnet-4.6 + ``OPENROUTER_API_KEY``, and DELIBERATELY
never reads ``LLM_API_KEY`` / ``LLM_ENDPOINT_URL`` / ``LLM_MODEL`` (the vision
knobs). It mirrors the categorizer's ``CATEGORIZER_LLM_*`` split and the
narrative generator's ``narrative_llm_*`` split.

Sovereign / air-gapped fallback: point these at the local 131K Qwen text model —
    CPATEAM_LLM_ENDPOINT_URL = http://<gateway-host>:4000/v1
    CPATEAM_LLM_MODEL        = qwen3.6-35b-p40
    CPATEAM_LLM_API_KEY      = <scoped virtual key>
"""
from __future__ import annotations

import os
import pathlib
from typing import Optional

_DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"


def resolve_cpateam_llm(
    cli_endpoint: Optional[str] = None,
    cli_model: Optional[str] = None,
    cli_key: Optional[str] = None,
    cli_key_file: Optional[str] = None,
) -> tuple[str, str, str]:
    """Resolve ``(endpoint_url, model, api_key)`` for the CPA-team frontier path.

    Precedence:
      endpoint/model : CLI flag  >  ``CPATEAM_LLM_*`` env  >  OpenRouter default.
      api_key        : ``CPATEAM_LLM_API_KEY``  >  --llm-api-key-file  >
                       --llm-api-key  >  ``OPENROUTER_API_KEY``.

    NEVER reads ``LLM_API_KEY`` / ``LLM_ENDPOINT_URL`` / ``LLM_MODEL`` — those
    belong to the receipt-VLM (vision) and would silently mis-route this frontier
    synthesis task.
    """
    endpoint = cli_endpoint or os.environ.get("CPATEAM_LLM_ENDPOINT_URL") or _DEFAULT_ENDPOINT
    model = cli_model or os.environ.get("CPATEAM_LLM_MODEL") or _DEFAULT_MODEL

    key = os.environ.get("CPATEAM_LLM_API_KEY")
    if not key and cli_key_file:
        p = pathlib.Path(cli_key_file).expanduser()
        if p.exists():
            key = p.read_text(encoding="utf-8").strip()
    key = key or cli_key or os.environ.get("OPENROUTER_API_KEY") or ""
    return endpoint, model, key


__all__ = ["resolve_cpateam_llm"]
