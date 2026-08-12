"""Enumerate an app's registered routes without depending on FastAPI internals.

Walking ``app.routes`` looks obvious and is a version trap. FastAPI changed
``include_router`` from eagerly copying a router's routes into the app to
storing a lazy ``_IncludedRouter`` placeholder that is expanded later. Under
the newer behaviour ``app.routes`` holds 55 placeholders and ~10 real routes,
so every test that scanned it for a path silently found nothing and asserted
against an empty set.

That is a genuinely nasty failure mode: the app serves those routes perfectly
well, but the tests that exist to prove the routes are wired report the
opposite. It only appeared once the suite ran somewhere with a newer FastAPI
than the machine the tests were written on (0.128 locally, 0.141 in CI and in
both deployed rings).

``app.openapi()`` forces full resolution whatever the inclusion strategy, and
its schema is the app's own account of what it serves — a more honest source
for "is this route registered?" than any internal attribute. Verified against
both versions: 163 paths on 0.141, the same set on 0.128.
"""
from __future__ import annotations

from typing import Any


def registered_paths(app: Any) -> set[str]:
    """Every path template the app actually serves."""
    return set(app.openapi().get("paths", {}))


def methods_for(app: Any, path: str) -> set[str]:
    """Uppercased HTTP methods registered for exactly ``path``.

    Returns an empty set when the path is not registered at all, so a caller
    can tell "route missing" from "route present, wrong verbs".
    """
    spec = app.openapi().get("paths", {}).get(path)
    if not spec:
        return set()
    return {m.upper() for m in spec if m.lower() != "parameters"}


def paths_containing(app: Any, needle: str) -> set[str]:
    """Registered paths containing ``needle`` — for prefix/suffix assertions."""
    return {p for p in registered_paths(app) if needle in p}
