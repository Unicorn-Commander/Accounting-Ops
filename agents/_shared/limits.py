"""Shared per-call output-token limits for specialist and narrative runners.

The runner modules do not share one reliable model-resolution API, so this
module intentionally uses one fleet-wide default. Claude 5 preparers need
roughly 48K tokens, and the same safe default is used for other models unless
an operator overrides it through the environment.

``AO_MAX_OUTPUT_TOKENS`` is the preferred setting. ``MAX_OUTPUT_TOKENS`` is
kept as a compatibility fallback for existing deployments. The helper is
resolved at each LLM request so a caller cannot retain the old import-time
constant after an environment override is applied.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

MIN_OUTPUT_TOKENS = 1
MAX_OUTPUT_TOKENS_LIMIT = 131_072
DEFAULT_MAX_OUTPUT_TOKENS = 48_000

_OUTPUT_TOKEN_ENV_VARS = ("AO_MAX_OUTPUT_TOKENS", "MAX_OUTPUT_TOKENS")


def parse_max_output_tokens(raw: str, *, variable: str = "AO_MAX_OUTPUT_TOKENS") -> int:
    """Parse and validate one output-token limit from an environment value."""
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"{variable} must be an integer between "
            f"{MIN_OUTPUT_TOKENS} and {MAX_OUTPUT_TOKENS_LIMIT}"
        ) from exc

    if not MIN_OUTPUT_TOKENS <= value <= MAX_OUTPUT_TOKENS_LIMIT:
        raise ValueError(
            f"{variable} must be an integer between "
            f"{MIN_OUTPUT_TOKENS} and {MAX_OUTPUT_TOKENS_LIMIT}"
        )
    return value


def get_max_output_tokens(env: Mapping[str, str] | None = None) -> int:
    """Return the current configured per-call output-token limit.

    ``env`` is injectable for unit tests; normal callers read ``os.environ``.
    An explicitly set preferred variable is validated rather than silently
    falling back to the legacy variable when malformed.
    """
    values = os.environ if env is None else env
    for variable in _OUTPUT_TOKEN_ENV_VARS:
        raw = values.get(variable)
        if raw is not None:
            return parse_max_output_tokens(raw, variable=variable)
    return DEFAULT_MAX_OUTPUT_TOKENS


# Backwards-compatible snapshot for external imports. Internal runners use
# get_max_output_tokens() at request time so this name cannot freeze their
# configured budget.
MAX_OUTPUT_TOKENS: int = get_max_output_tokens()

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_OUTPUT_TOKENS_LIMIT",
    "MIN_OUTPUT_TOKENS",
    "get_max_output_tokens",
    "parse_max_output_tokens",
]
