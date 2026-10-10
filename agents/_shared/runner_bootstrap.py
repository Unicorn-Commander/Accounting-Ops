"""Tiny sys.path bootstrap so individual specialist runners can be invoked
directly with ``python agents/.../run_specialist.py`` and still import
``agents._shared``.

Runners that ``from agents._shared.limits import get_max_output_tokens`` need
the repo root on ``sys.path``. Containers usually have ``/app`` on the path; ad-hoc
``uv run python agents/...`` invocations from the host don't. This helper does
the insertion once so each runner only needs::

    from agents._shared.runner_bootstrap import ensure_repo_root
    ensure_repo_root(__file__)
    from agents._shared.limits import get_max_output_tokens

idempotent: re-calling it does nothing.
"""

from __future__ import annotations

import pathlib
import sys


def ensure_repo_root(runner_file: str) -> None:
    """Insert the repo root into ``sys.path`` if it isn't already there.

    Args:
        runner_file: Pass ``__file__`` from the caller. The repo root is
            assumed to be three levels up from the runner file (e.g.
            ``agents/tax_specialists/<name>/run_specialist.py``) — that holds
            for every current specialist + the narrative generator
            (``agents/cpa_team/narrative_generator/run_narrative_generator.py``).
    """
    here = pathlib.Path(runner_file).resolve().parent
    # agents/<a>/<b>/run_*.py  -> repo root is parents[2]
    repo_root = here.parent.parent
    s = str(repo_root)
    if s not in sys.path:
        sys.path.insert(0, s)


__all__ = ["ensure_repo_root"]
