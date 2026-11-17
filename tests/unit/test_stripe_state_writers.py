"""Guard: every writer of the Stripe connector_state row uses the shared lock.

Review 4bba864c round 4 found two writers (payout reconciliation and the
webhook delivery log) that read the whole state without a lock and wrote the
whole JSON back, erasing a resolve or reopen committed in between. This test
scans app/ and scripts/ for SQL that writes connector_state and fails when a
new one appears outside the reviewed set, and checks that each reviewed Stripe
writer takes lock_stripe_state_row and writes through the jsonb merge.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from app.connectors.stripe import blocked_resolution

ROOT = Path(__file__).resolve().parents[2]
WRITE_SQL = re.compile(
    r"(UPDATE\s+connector_state\s|INSERT\s+INTO\s+connector_state\s)", re.IGNORECASE
)
# Not the Stripe connector: these files only touch connector_name 'ebay_api'.
NON_STRIPE = {
    "app/connectors/ebay/reconcile.py",
    "scripts/ebay_sync.py",
}
# The reviewed Stripe writers and how many write statements each holds.
#   blocked_resolution.py: MERGE_STATE_SQL, the one jsonb merge every writer uses.
#   stripe_sync.py: first-run row creation, an INSERT ... ON CONFLICT jsonb merge
#     inside _record_sync_outcome after lock_stripe_state_row found no row.
#   reconcile.py: creates an EMPTY row when none exists, then locks and merges.
ALLOWED = {
    "app/connectors/stripe/blocked_resolution.py": 1,
    "scripts/stripe_sync.py": 1,
    "app/connectors/stripe/reconcile.py": 1,
}


def _sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for base in ("app", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            out[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return out


def _function(source: str, name: str) -> str:
    start = source.index(f"async def {name}(")
    nxt = re.search(r"\n(async def |def |class |@)", source[start + 1 :])
    return source[start : start + 1 + nxt.start()] if nxt else source[start:]


def test_no_unreviewed_connector_state_writer() -> None:
    found = {
        rel: len(WRITE_SQL.findall(src))
        for rel, src in _sources().items()
        if rel not in NON_STRIPE and WRITE_SQL.search(src)
    }
    assert found == ALLOWED


def test_no_whole_state_replacement_outside_new_rows() -> None:
    pattern = re.compile(r"SET\s+state\s*=\s*CAST\(", re.IGNORECASE)
    offenders = [
        rel
        for rel, src in _sources().items()
        if rel not in NON_STRIPE and pattern.search(src)
    ]
    assert offenders == []


def test_non_stripe_files_never_name_the_stripe_connector() -> None:
    sources = _sources()
    for rel in NON_STRIPE:
        assert "stripe_api" not in sources[rel], rel


@pytest.mark.parametrize(
    ("rel", "function"),
    [
        ("app/connectors/stripe/reconcile.py", "reconcile_one_entity"),
        ("app/api/stripe_oauth.py", "_record_webhook_delivery"),
        ("scripts/stripe_sync.py", "_record_sync_outcome"),
    ],
)
def test_each_stripe_writer_locks_the_exact_row(rel: str, function: str) -> None:
    body = _function(_sources()[rel], function)
    assert "lock_stripe_state_row(" in body
    assert "FOR UPDATE" not in body  # the helper owns the lock statement


@pytest.mark.parametrize(
    ("rel", "function"),
    [
        ("app/connectors/stripe/reconcile.py", "reconcile_one_entity"),
        ("app/api/stripe_oauth.py", "_record_webhook_delivery"),
    ],
)
def test_side_writers_merge_only_their_keys(rel: str, function: str) -> None:
    body = _function(_sources()[rel], function)
    assert "merge_side_state_keys(" in body
    assert "UPDATE connector_state" not in body


def test_side_merge_refuses_block_protocol_keys() -> None:
    class Session:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def execute(self, *args: Any) -> None:
            self.calls.append(args)

    for key in (
        "blocked_item",
        "resolved_blocked_items",
        "reopened_blocked_items",
        "block_revision",
        "_stripe_continuation_cursor",
        "_stripe_failure_counts",
    ):
        session = Session()
        with pytest.raises(ValueError):
            asyncio.run(
                blocked_resolution.merge_side_state_keys(
                    session, "row-1", {"reconciliation": {}, key: None}
                )
            )
        assert session.calls == []

    session = Session()
    asyncio.run(
        blocked_resolution.merge_side_state_keys(
            session, "row-1", {"webhook_log": [], "last_webhook_at_iso": "x"}
        )
    )
    (statement, params) = session.calls[0]
    assert "||" in str(statement)
    assert params["drop_keys"] == []
    assert params["id"] == "row-1"
