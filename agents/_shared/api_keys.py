"""Shared API-key resolution for specialist runners.

Why this exists
---------------

Until 2026-05-29 every specialist runner accepted ``--llm-api-key sk-or-v1-...``
on the command line. On <host> ``ps auxwww`` revealed the full key to any
shell user, because the argument lives in ``/proc/<pid>/cmdline``. That is a
P0 leak path for the OpenRouter / OpenAI key.

This helper centralises the resolution precedence so every runner picks up the
key the same way and the CLI arg becomes the LAST resort (with a deprecation
warning logged on stderr).

Resolution precedence (highest first)
-------------------------------------

1. ``$LLM_API_KEY``                — runner-specific, set by the env wrapper
2. ``$OPENROUTER_API_KEY``         — fleet-wide OpenRouter key
3. ``$ANTHROPIC_API_KEY``          — only for the openai-path fallback, not
                                     normally used here (Anthropic-path runners
                                     read this directly via the SDK)
4. ``--llm-api-key-file PATH``     — file with the key (chmod 600)
5. ``--llm-api-key VALUE``         — DEPRECATED, visible via ``ps``
6. ``default`` parameter           — usually a placeholder like ``"local-qwen"``

If the caller passes ``--llm-api-key`` explicitly, a deprecation warning is
emitted to stderr. The CLI arg still wins over ``default`` so older invocations
keep working, but the env / file paths take precedence so a hardened wrapper
can override an accidentally-baked-in CLI arg.

Help-text snippet
-----------------

Every runner reuses :data:`HELP_TEXT_LLM_API_KEY` in its argparse epilog so the
``--help`` documentation stays in lock-step with the actual resolution order.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Optional


HELP_TEXT_LLM_API_KEY = (
    "LLM API key resolution precedence (highest first):\n"
    "  1. $LLM_API_KEY              env var (preferred)\n"
    "  2. $OPENROUTER_API_KEY       env var (fleet-wide OpenRouter key)\n"
    "  3. --llm-api-key-file PATH   read from file (chmod 600 recommended)\n"
    "  4. --llm-api-key VALUE       DEPRECATED — visible via `ps`, prefer env or file\n"
    "  5. built-in placeholder      (used only by --backend openai against a\n"
    "                                local endpoint that ignores the key)"
)


def _read_key_file(path: str) -> str:
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        print(
            f"ERROR: --llm-api-key-file {path!s} does not exist.",
            file=sys.stderr,
        )
        sys.exit(2)
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        print(
            f"ERROR: --llm-api-key-file {path!s} is empty.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Best-effort chmod check; warn only.
    try:
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            print(
                f"WARNING: {path} is world/group-readable (mode {oct(mode)}). "
                "Recommend chmod 600.",
                file=sys.stderr,
            )
    except OSError:
        pass
    return key


def resolve_llm_api_key(
    cli_value: Optional[str],
    cli_file: Optional[str],
    *,
    default: str = "local-qwen",
) -> str:
    """Resolve the LLM API key in the documented precedence order.

    Args:
        cli_value: The value of ``--llm-api-key`` (may be ``None``).
        cli_file:  The value of ``--llm-api-key-file`` (may be ``None``).
        default:   The fallback used when nothing else is available — usually
                   the runner's ``_DEFAULT_LLM_API_KEY`` placeholder.

    Returns:
        The resolved API key string.

    Emits to stderr:
        - A deprecation warning if the caller passed ``--llm-api-key``.
        - A fatal error and ``sys.exit(2)`` if ``--llm-api-key-file`` was
          given but the file is missing or empty.
    """
    env_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENROUTER_KEY")  # legacy alias still seen on <host>
    )
    if env_key:
        return env_key

    if cli_file:
        return _read_key_file(cli_file)

    if cli_value:
        print(
            "WARNING: --llm-api-key passed on the command line. The key is "
            "visible via `ps` to any local user. Prefer $LLM_API_KEY, "
            "$OPENROUTER_API_KEY, or --llm-api-key-file (chmod 600). "
            "This flag is kept for backwards compatibility only.",
            file=sys.stderr,
        )
        return cli_value

    return default


__all__ = ["HELP_TEXT_LLM_API_KEY", "resolve_llm_api_key"]
