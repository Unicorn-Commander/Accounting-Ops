"""Per-workspace data-retention configuration.

Storage decision (read this before "fixing" it)
-----------------------------------------------
This module persists its state inside the existing ``public.workspace``
``setup_progress`` JSONB column, under a namespaced top-level key
(``DATA_RIGHTS_KEY``), rather than in a table of its own.

That is a deliberate trade-off, not laziness:

  * A new table needs an Alembic migration. At the time this was written, other
    agents had migrations in flight on sibling branches (tenant 0027, public
    0014), so adding another head would create a merge conflict over revision
    ordering for a feature that does not need one.
  * ``setup_progress`` is the only per-workspace mutable JSONB column that the
    runtime role can already write. The grant is column-scoped —
    ``GRANT UPDATE (setup_progress) ON public.workspace`` (public/0011) — so no
    new grant is needed either.
  * The Setup-Assistant PATCH handler that owns that column merges by mutating
    only the keys it knows about (``version``, ``skipped``, ``completed_at``,
    ``steps``) and writes the whole document back. A sibling top-level key
    therefore survives its writes. This was verified against
    ``app/api/setup.py`` before choosing this approach — it is a real property
    of that code, not an assumption.
  * ``public.workspace`` lives in the public schema, which is NOT covered by the
    tenant FORCE-RLS sweep, so reads here do not need the workspace GUC set.

Known cost, stated plainly: we are a guest in a column another feature owns. If
the Setup Assistant is ever rewritten to replace the document wholesale instead
of merging, this data is silently lost. The follow-up is a dedicated
``workspace_data_rights`` table once the in-flight migrations land; see
``docs/DATA-RIGHTS.md``. A test pins the sibling-key-survives-a-setup-PATCH
behaviour so the regression is caught rather than discovered.

The retention default
---------------------
``DEFAULT_LEDGER_RETENTION_DAYS`` is a PLACEHOLDER pending a decision by
counsel. It is currently 7 years, which is the conservative end of common US
guidance for retaining business tax records. It is deliberately conservative:
erring long risks a storage-limitation criticism, erring short risks destroying
a customer's books before they need them for an audit, and the second failure is
much harder to apologise for. Both ``docs/legal/PRIVACY-POLICY.draft.md`` §7 and
``docs/legal/DPA.draft.md`` §8.4 carry a matching ``[COUNSEL: ...]`` marker.
Whoever changes this number must change it in all three places.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, asdict
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Namespaced key inside public.workspace.setup_progress. Never collide with the
# Setup Assistant's own keys (version / skipped / completed_at / steps).
DATA_RIGHTS_KEY = "data_rights"
DATA_RIGHTS_SCHEMA_VERSION = 1

# --- The documented default. See module docstring. -------------------------- #
# [COUNSEL] 7 years. Placeholder pending legal review.
DEFAULT_LEDGER_RETENTION_DAYS = 2555

# Bounds on what a workspace may configure for itself.
#   0    => purge the ledger immediately on an erasure request (no waiting
#           period). Offered because the privacy policy promises a customer can
#           instruct immediate purge, and a policy promise with no code path
#           behind it is a lie.
#   3650 => 10 years. An upper bound exists so "retention" cannot be used to
#           mean "forever", which would defeat the storage-limitation principle.
MIN_LEDGER_RETENTION_DAYS = 0
MAX_LEDGER_RETENTION_DAYS = 3650


class RetentionConfigError(ValueError):
    """Invalid retention configuration supplied by a caller."""


@dataclass(frozen=True)
class RetentionConfig:
    """Effective retention settings for one workspace."""

    ledger_retention_days: int
    is_default: bool          # True when no per-workspace override is stored
    set_by_uc_uid: str | None = None
    set_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["default_ledger_retention_days"] = DEFAULT_LEDGER_RETENTION_DAYS
        d["min_ledger_retention_days"] = MIN_LEDGER_RETENTION_DAYS
        d["max_ledger_retention_days"] = MAX_LEDGER_RETENTION_DAYS
        return d


def validate_retention_days(days: Any) -> int:
    """Coerce + range-check a retention value. Raises RetentionConfigError."""
    if isinstance(days, bool) or not isinstance(days, int):
        raise RetentionConfigError("ledger_retention_days must be an integer")
    if days < MIN_LEDGER_RETENTION_DAYS or days > MAX_LEDGER_RETENTION_DAYS:
        raise RetentionConfigError(
            f"ledger_retention_days must be between {MIN_LEDGER_RETENTION_DAYS} "
            f"and {MAX_LEDGER_RETENTION_DAYS} (got {days})"
        )
    return days


def _empty_data_rights() -> dict[str, Any]:
    return {"version": DATA_RIGHTS_SCHEMA_VERSION}


def parse_retention(data_rights: dict[str, Any] | None) -> RetentionConfig:
    """Pure: derive the effective RetentionConfig from a data_rights document.

    Split out from the DB read so it can be unit-tested without a database, and
    so a malformed stored document degrades to the documented default instead of
    raising in the middle of an erasure.
    """
    node = (data_rights or {}).get("retention")
    if not isinstance(node, dict):
        return RetentionConfig(
            ledger_retention_days=DEFAULT_LEDGER_RETENTION_DAYS, is_default=True
        )
    raw = node.get("ledger_retention_days")
    try:
        days = validate_retention_days(raw)
    except RetentionConfigError:
        # A corrupted stored value must not brick erasure. Fall back to the
        # documented default rather than failing the request.
        return RetentionConfig(
            ledger_retention_days=DEFAULT_LEDGER_RETENTION_DAYS, is_default=True
        )
    return RetentionConfig(
        ledger_retention_days=days,
        is_default=False,
        set_by_uc_uid=node.get("set_by_uc_uid"),
        set_at=node.get("set_at"),
    )


def purge_due_at(requested_at: dt.datetime, config: RetentionConfig) -> dt.datetime:
    """When the ledger purge becomes due for an erasure requested at a time."""
    return requested_at + dt.timedelta(days=config.ledger_retention_days)


# --------------------------------------------------------------------------- #
# Persistence. public.workspace is NOT under tenant RLS, so these run on a
# plain session — but every statement is still explicitly filtered by
# workspace_id. Defence in depth: never rely on the caller having scoped us.
# --------------------------------------------------------------------------- #
async def read_data_rights_doc(
    session: AsyncSession, workspace_id: UUID
) -> dict[str, Any]:
    """Read the namespaced data_rights sub-document. {} if absent."""
    row = (
        await session.execute(
            text("SELECT setup_progress FROM public.workspace WHERE id = :id"),
            {"id": str(workspace_id)},
        )
    ).first()
    if row is None:
        return {}
    progress = row.setup_progress or {}
    node = progress.get(DATA_RIGHTS_KEY)
    return node if isinstance(node, dict) else {}


async def _write_data_rights_doc(
    session: AsyncSession, workspace_id: UUID, doc: dict[str, Any]
) -> None:
    """Merge the data_rights sub-document back, preserving sibling keys.

    Uses jsonb_set with create_missing=true so the Setup Assistant's own keys are
    untouched by construction — we never read-modify-write the whole document,
    which removes the lost-update race against a concurrent setup PATCH.
    COALESCE handles the column being NULL for a workspace that never ran setup.
    """
    await session.execute(
        text(
            """
            UPDATE public.workspace
               SET setup_progress = jsonb_set(
                     COALESCE(setup_progress, '{}'::jsonb),
                     ARRAY[:key],
                     CAST(:doc AS jsonb),
                     true
                   )
             WHERE id = :id
            """
        ),
        {"key": DATA_RIGHTS_KEY, "doc": json.dumps(doc), "id": str(workspace_id)},
    )


async def get_retention(session: AsyncSession, workspace_id: UUID) -> RetentionConfig:
    return parse_retention(await read_data_rights_doc(session, workspace_id))


async def set_retention(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    ledger_retention_days: int,
    actor_uc_uid: str,
) -> RetentionConfig:
    """Set the per-workspace override. Raises RetentionConfigError if invalid."""
    days = validate_retention_days(ledger_retention_days)
    doc = await read_data_rights_doc(session, workspace_id)
    doc.setdefault("version", DATA_RIGHTS_SCHEMA_VERSION)
    doc["retention"] = {
        "ledger_retention_days": days,
        "set_by_uc_uid": actor_uc_uid,
        "set_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    await _write_data_rights_doc(session, workspace_id, doc)
    return RetentionConfig(
        ledger_retention_days=days,
        is_default=False,
        set_by_uc_uid=actor_uc_uid,
        set_at=doc["retention"]["set_at"],
    )


async def record_job(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    bucket: str,
    record: dict[str, Any],
    cap: int = 10,
) -> None:
    """Upsert a job record into a capped list under data_rights[bucket].

    Used for export-job status (bucket='exports'). Records are keyed by 'id';
    an existing record with the same id is replaced in place, so a background
    task can transition running -> completed without duplicating the row. The
    list is capped so the JSONB document cannot grow without bound.
    """
    doc = await read_data_rights_doc(session, workspace_id)
    doc.setdefault("version", DATA_RIGHTS_SCHEMA_VERSION)
    existing = doc.get(bucket)
    items: list[dict[str, Any]] = list(existing) if isinstance(existing, list) else []
    items = [i for i in items if isinstance(i, dict) and i.get("id") != record.get("id")]
    items.insert(0, record)
    doc[bucket] = items[:cap]
    await _write_data_rights_doc(session, workspace_id, doc)


async def read_jobs(
    session: AsyncSession, workspace_id: UUID, *, bucket: str
) -> list[dict[str, Any]]:
    doc = await read_data_rights_doc(session, workspace_id)
    items = doc.get(bucket)
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


__all__ = (
    "DATA_RIGHTS_KEY",
    "DEFAULT_LEDGER_RETENTION_DAYS",
    "MAX_LEDGER_RETENTION_DAYS",
    "MIN_LEDGER_RETENTION_DAYS",
    "RetentionConfig",
    "RetentionConfigError",
    "get_retention",
    "parse_retention",
    "purge_due_at",
    "read_data_rights_doc",
    "read_jobs",
    "record_job",
    "set_retention",
    "validate_retention_days",
)
