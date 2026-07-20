"""Workspace erasure — the deletion half of the data-rights surface.

===========================================================================
THE DESIGN DECISION, AND WHY
===========================================================================

**Erasure here is workspace-level. We do not delete individual rows out of a
live ledger. This is a deliberate choice, and the privacy policy and DPA both
describe it to customers, so changing it is changing a representation we have
made to them.**

The constraint
--------------
The general ledger is append-only and hash-chained:

  * ``journal_entry`` and ``event_log`` carry ``BEFORE UPDATE OR DELETE`` row
    triggers that raise unconditionally — for EVERY database role, including
    the table owner (triggers are not role-aware). Statement-level triggers
    additionally block TRUNCATE. See tenant migrations 0001 and 0005.
  * Each entry's SHA-256 covers its own contents — including ``payee`` and
    ``description``, which are exactly the fields most likely to carry personal
    data — and links to the previous entry's hash **for the same entity**.

Three options were considered:

  (a) Redact the personal fields in place.
      REJECTED. Doubly impossible: the trigger rejects the UPDATE, and even
      with the trigger bypassed the chain verifier recomputes each row's hash
      and would flag every redacted row as tampered, permanently. We would be
      converting the customer's tamper-evidence feature into a permanent false
      alarm. An integrity check that always cries wolf is worse than none.

  (b) Delete individual rows mid-chain.
      REJECTED. The next surviving entry's ``prev_hash`` would no longer match
      its predecessor, so verification reports the customer's books as tampered
      — correctly, because they would have been. We would be destroying the
      integrity of the records the customer keeps, to honour a request about
      records they asked us to remove. That is a bad trade and the customer
      would not thank us for it.

  (c) Erase the whole workspace as a unit.  <-- CHOSEN
      Every entry for the workspace goes together, so no chain is left with a
      hole in it. Nothing is left inconsistent.

Why (c) is safe for everyone else — the load-bearing fact
---------------------------------------------------------
**The hash chain is keyed on ``entity_id``, not globally and not on
``workspace_id``.** ``get_last_chain_hash`` and ``verify_chain`` both filter
``WHERE entity_id = :eid``, and each chain starts from a genesis hash of 32
zero bytes.

Since every ``entity`` belongs to exactly one workspace, no hash in workspace
B's chain can ever reference a row belonging to workspace A. Erasing workspace
A's rows therefore CANNOT affect the verifiability of workspace B's ledger —
even though, since tenant migration 0026, all workspaces' rows physically share
one ``books.journal_entry`` table.

This is a property worth stating precisely because it is what makes (c)
defensible rather than merely convenient, and it is asserted by an automated
test rather than assumed.

A future option, if counsel requires row-level erasure
------------------------------------------------------
Crypto-shredding: store personal fields by reference to a per-subject key and
destroy the key, leaving the chain intact over opaque tokens. It preserves
historical verifiability, and it is the only one of the options that does. It
also requires a ledger format change plus a chain-version bump and
re-verification of all historical entries — months of work, not days. See the
``[COUNSEL: ...]`` marker at DPA §6.3, which asks for this determination early
rather than late.

===========================================================================
THE TWO TIERS, AND WHY THE SPLIT IS WHERE IT IS
===========================================================================

Tier 1 — immediate, runs on the ordinary runtime role, always available:
  1. Destroy connector credentials (bank access tokens, Stripe/eBay keys).
  2. Destroy stored documents and receipts in object storage.
  3. Destroy tax identifiers (``entity.tax_id_encrypted``).
  4. Write the audit trail.

Tier 1 is ordered by risk, not convenience. A live bank access token is the
single most dangerous item we hold, so it goes first. After Tier 1 the workspace
still has a ledger, but it no longer has anything that grants access to the
customer's money or reveals their identity documents.

Tier 2 — requires the table-owning (admin) database role:
  5. Revoke access (``public.workspace.status = 'suspended'``).
  6. Remove memberships.
  7. Purge the ledger and all remaining tenant rows.

The split is not arbitrary — it follows the grants. The runtime role
``accounting_ops_app`` has DELETE on ``public.tenant_credentials`` but only
``SELECT`` + column-scoped ``UPDATE (setup_progress)`` on ``public.workspace``,
no DELETE on ``public.workspace_membership``, and no UPDATE/DELETE at all on the
append-only tables. So steps 5-7 are structurally impossible without the admin
connection. That is a real finding about this system, not a limitation of this
module.

**If the admin connection is not configured, Tier 2 steps report ``unavailable``
with a reason. They never report success.** Every step returns its own status
and the receipt lists all of them. A partially-completed erasure that reported
"done" would be precisely the silent-degradation failure this codebase has been
bitten by before — and here it would also make the privacy policy a lie.

Why the ledger purge is deferred by default
--------------------------------------------
Steps 1-6 happen immediately. Step 7 waits for the workspace's retention window
(see ``retention.py``) unless the caller explicitly asks for immediate purge.

These are accounting records. Customers routinely need them back after an
unexpected audit notice, and destroying them the instant someone clicks a button
has burned real businesses. The customer can still demand immediate destruction
and we honour it — ``immediate=True`` — which is what keeps the retention window
a service rather than an excuse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
)
from app.config import Settings, get_admin_database_url, get_settings
from app.data_rights import retention as retention_mod
from app.workspace_session import SHARED_SCHEMA

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Step outcome vocabulary. Anything other than DONE means the thing did not
# happen — callers and the receipt must never conflate them.
STATUS_DONE = "done"
STATUS_DEFERRED = "deferred"        # scheduled, not yet due
STATUS_UNAVAILABLE = "unavailable"  # cannot run in this deployment
STATUS_FAILED = "failed"

ERASURE_STATE_KEY = "erasure"


class ErasureConfirmationError(ValueError):
    """Confirmation token missing or does not match. Never auto-resolvable."""


@dataclass
class ErasureStep:
    name: str
    status: str
    detail: str = ""
    count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.count is not None:
            d["count"] = self.count
        return d


@dataclass
class ErasureReceipt:
    """The durable record that an erasure happened.

    Deliberately stored in ``public.workspace`` — OUTSIDE the tenant data — so it
    survives the very purge it describes. An erasure record that got erased along
    with everything else would leave us unable to show a regulator, or the
    customer, that we honoured the request. Contains no financial records and no
    document contents.
    """

    workspace_id: str
    requested_by_uc_uid: str
    requested_at: str
    steps: list[ErasureStep] = field(default_factory=list)
    ledger_purge_due_at: str | None = None
    retention_days: int | None = None

    @property
    def overall_status(self) -> str:
        statuses = {s.status for s in self.steps}
        if STATUS_FAILED in statuses:
            return "partial_failed"
        if STATUS_UNAVAILABLE in statuses:
            return "partial_unavailable"
        if STATUS_DEFERRED in statuses:
            return "deferred"
        return "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "requested_by_uc_uid": self.requested_by_uc_uid,
            "requested_at": self.requested_at,
            "overall_status": self.overall_status,
            "steps": [s.to_dict() for s in self.steps],
            "ledger_purge_due_at": self.ledger_purge_due_at,
            "retention_days": self.retention_days,
        }


def verify_confirmation(supplied: str | None, workspace_slug: str) -> None:
    """Require the caller to type the workspace slug exactly.

    Erasure is irreversible, so it must not be reachable by a stray click or a
    replayed request. Matching the slug proves the caller knows which workspace
    they are destroying — a plain boolean flag would not.
    """
    if not supplied or supplied.strip() != workspace_slug:
        raise ErasureConfirmationError(
            "Erasure requires the `confirm` field to exactly match the workspace "
            f"slug ('{workspace_slug}'). Nothing was deleted."
        )


# --------------------------------------------------------------------------- #
# Tier 1 — runtime role
# --------------------------------------------------------------------------- #
async def purge_credentials(session: AsyncSession, workspace_id: UUID) -> ErasureStep:
    """Destroy every stored connector credential. Highest-risk item, so first."""
    try:
        await session.execute(
            text("SELECT set_config('app.current_workspace', :wid, true)"),
            {"wid": str(workspace_id)},
        )
        result = await session.execute(
            text("DELETE FROM public.tenant_credentials WHERE workspace_id = :wid"),
            {"wid": str(workspace_id)},
        )
        return ErasureStep(
            "credentials_purged",
            STATUS_DONE,
            "Connector credentials destroyed (bank, Stripe, eBay access tokens).",
            count=result.rowcount or 0,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in the receipt, never swallowed
        return ErasureStep("credentials_purged", STATUS_FAILED, f"{type(exc).__name__}: {exc}")


async def purge_documents(
    session: AsyncSession, workspace_id: UUID, *, settings: Settings | None = None
) -> ErasureStep:
    """Delete every stored receipt/document object from object storage."""
    settings = settings or get_settings()
    s3 = GarageS3Client(settings)
    if not s3.is_configured():
        return ErasureStep(
            "documents_purged",
            STATUS_UNAVAILABLE,
            "Object store not configured; stored documents were NOT deleted.",
        )
    try:
        rows = (
            await session.execute(
                text(
                    f'SELECT id, garage_bucket, object_key FROM {SHARED_SCHEMA}."receipt" '  # noqa: S608
                    "WHERE workspace_id = :wid"
                ),
                {"wid": str(workspace_id)},
            )
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        return ErasureStep("documents_purged", STATUS_FAILED, f"{type(exc).__name__}: {exc}")

    deleted, failed = 0, 0
    for r in rows:
        try:
            await s3.delete_object(bucket=r["garage_bucket"], object_key=r["object_key"])
            deleted += 1
        except (GarageNotConfigured, GarageUploadError):
            failed += 1
    if failed:
        return ErasureStep(
            "documents_purged",
            STATUS_FAILED,
            f"{deleted} object(s) deleted, {failed} failed. Re-run erasure to retry; "
            "object deletion is idempotent.",
            count=deleted,
        )
    return ErasureStep(
        "documents_purged", STATUS_DONE, "Stored documents and receipts deleted.", count=deleted
    )


async def purge_tax_identifiers(session: AsyncSession, workspace_id: UUID) -> ErasureStep:
    """Erase encrypted tax identifiers (EIN/SSN) from entity rows.

    ``entity`` is UPDATE-able but not DELETE-able by the runtime role, so this
    runs in Tier 1 while whole-row removal waits for Tier 2. Nulling the
    ciphertext destroys the identifier immediately — we do not wait for the
    retention window for a TIN.
    """
    try:
        result = await session.execute(
            text(
                f'UPDATE {SHARED_SCHEMA}."entity" SET tax_id_encrypted = NULL '  # noqa: S608
                "WHERE workspace_id = :wid AND tax_id_encrypted IS NOT NULL"
            ),
            {"wid": str(workspace_id)},
        )
        return ErasureStep(
            "tax_identifiers_purged",
            STATUS_DONE,
            "Encrypted tax identifiers (EIN/SSN) erased.",
            count=result.rowcount or 0,
        )
    except Exception as exc:  # noqa: BLE001
        return ErasureStep(
            "tax_identifiers_purged", STATUS_FAILED, f"{type(exc).__name__}: {exc}"
        )


# --------------------------------------------------------------------------- #
# Tier 2 — admin/table-owner role
# --------------------------------------------------------------------------- #
def admin_connection_available() -> bool:
    """True when a distinct admin DB URL is configured.

    ``get_admin_database_url()`` falls back to the runtime URL when
    ADMIN_DATABASE_URL is unset. In that single-role case the "admin" connection
    is really the restricted runtime role, and Tier 2 will fail on grants — so we
    report unavailable up front instead of attempting and half-succeeding.
    """
    return bool(get_settings().admin_database_url)


def _admin_url() -> str:
    url = get_admin_database_url()
    # Scripts in this repo pass the admin URL straight to create_async_engine,
    # so it is expected in asyncpg form. Normalise the sync form defensively.
    if "+psycopg2" in url:
        url = url.replace("+psycopg2", "+asyncpg")
    elif "://" in url and "+" not in url.split("://", 1)[0]:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def revoke_access(workspace_id: UUID) -> list[ErasureStep]:
    """Suspend the workspace and remove memberships. Requires admin role.

    Suspension is the hard access kill: ``_resolve_membership`` in app/tenancy.py
    requires ``w.status = 'active'``, so every request for this workspace — user
    or agent — starts returning 403 immediately. ``'suspended'`` is an existing
    value of the ``workspace_status`` enum, so no migration is needed.
    """
    if not admin_connection_available():
        detail = (
            "ADMIN_DATABASE_URL is not configured. The runtime role has no UPDATE "
            "grant on public.workspace.status and no DELETE on "
            "public.workspace_membership, so access could NOT be revoked."
        )
        return [
            ErasureStep("access_revoked", STATUS_UNAVAILABLE, detail),
            ErasureStep("memberships_removed", STATUS_UNAVAILABLE, detail),
        ]

    engine = create_async_engine(_admin_url(), pool_pre_ping=True)
    steps: list[ErasureStep] = []
    try:
        async with engine.begin() as conn:
            try:
                await conn.execute(
                    text("UPDATE public.workspace SET status = 'suspended' WHERE id = :wid"),
                    {"wid": str(workspace_id)},
                )
                steps.append(
                    ErasureStep(
                        "access_revoked",
                        STATUS_DONE,
                        "Workspace suspended; all user and agent access now denied.",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                steps.append(
                    ErasureStep("access_revoked", STATUS_FAILED, f"{type(exc).__name__}: {exc}")
                )
            try:
                result = await conn.execute(
                    text("DELETE FROM public.workspace_membership WHERE workspace_id = :wid"),
                    {"wid": str(workspace_id)},
                )
                steps.append(
                    ErasureStep(
                        "memberships_removed",
                        STATUS_DONE,
                        "Workspace memberships removed.",
                        count=result.rowcount or 0,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                steps.append(
                    ErasureStep(
                        "memberships_removed", STATUS_FAILED, f"{type(exc).__name__}: {exc}"
                    )
                )
    finally:
        await engine.dispose()
    return steps


async def purge_workspace_ledger(workspace_id: UUID) -> ErasureStep:
    """Delete every tenant row for this workspace, as a unit. Requires admin role.

    Mechanism, and why each piece is the way it is:

      * ``SET LOCAL app.current_workspace`` — FORCE RLS applies to the table
        owner too, so without the GUC this deletes NOTHING and would silently
        report success. Set first, always.

      * ``SET LOCAL session_replication_role = 'replica'`` — bypasses the
        append-only triggers for THIS SESSION ONLY. The alternative,
        ``ALTER TABLE ... DISABLE TRIGGER``, is DDL: it takes an ACCESS
        EXCLUSIVE lock on a table shared by every tenant, so it would block all
        other customers for the duration of one customer's erasure. Session-local
        replica mode has no such blast radius. It also disables FK constraint
        triggers, which makes deletion order a non-issue — though we still delete
        ``entity`` last, so the order is correct either way.

        If the role may not set this parameter, the DELETE fails on the
        append-only trigger and is reported as FAILED. It cannot silently
        no-op — a purge that quietly deleted nothing while telling the customer
        their data was gone is the worst possible outcome here.

      * Table discovery is dynamic, so a table added next month is purged too.
        An erasure that missed a table added after it was written would leave
        personal data behind while reporting success.
    """
    if not admin_connection_available():
        return ErasureStep(
            "ledger_purged",
            STATUS_UNAVAILABLE,
            "ADMIN_DATABASE_URL is not configured. The runtime role cannot DELETE "
            "from append-only tables, so the ledger was NOT purged. An operator "
            "must complete this step; see docs/DATA-RIGHTS.md.",
        )

    engine = create_async_engine(_admin_url(), pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_workspace', :wid, true)"),
                {"wid": str(workspace_id)},
            )
            try:
                await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
            except Exception as exc:  # noqa: BLE001
                return ErasureStep(
                    "ledger_purged",
                    STATUS_FAILED,
                    "Could not enter replica mode to bypass the append-only triggers "
                    f"({type(exc).__name__}). The database role needs superuser, or "
                    "GRANT SET ON PARAMETER session_replication_role. Nothing deleted.",
                )

            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT c.relname AS name
                          FROM pg_class c
                          JOIN pg_namespace n ON n.oid = c.relnamespace
                          JOIN pg_attribute a ON a.attrelid = c.oid
                         WHERE n.nspname = :schema
                           AND c.relkind = 'r'
                           AND a.attname = 'workspace_id'
                           AND NOT a.attisdropped
                         ORDER BY c.relname
                        """
                    ),
                    {"schema": SHARED_SCHEMA},
                )
            ).mappings().all()
            tables = [r["name"] for r in rows if _IDENT_RE.match(r["name"])]
            # entity last: every entity-scoped FK is ON DELETE RESTRICT.
            ordered = [t for t in tables if t != "entity"] + [t for t in tables if t == "entity"]

            total = 0
            per_table: dict[str, int] = {}
            for table in ordered:
                result = await conn.execute(
                    text(
                        f'DELETE FROM {SHARED_SCHEMA}."{table}" WHERE workspace_id = :wid'  # noqa: S608
                    ),
                    {"wid": str(workspace_id)},
                )
                deleted = result.rowcount or 0
                per_table[table] = deleted
                total += deleted
        return ErasureStep(
            "ledger_purged",
            STATUS_DONE,
            "All tenant records purged as a unit across "
            f"{len(ordered)} table(s). Other workspaces' hash chains are unaffected "
            "(chains are keyed on entity_id, and every entity belongs to exactly "
            "one workspace).",
            count=total,
        )
    except Exception as exc:  # noqa: BLE001
        return ErasureStep("ledger_purged", STATUS_FAILED, f"{type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Durable record
# --------------------------------------------------------------------------- #
async def record_erasure(
    session: AsyncSession, workspace_id: UUID, receipt: ErasureReceipt
) -> None:
    """Persist the receipt outside the tenant data, so it survives the purge."""
    doc = await retention_mod.read_data_rights_doc(session, workspace_id)
    doc.setdefault("version", retention_mod.DATA_RIGHTS_SCHEMA_VERSION)
    doc[ERASURE_STATE_KEY] = receipt.to_dict()
    await retention_mod._write_data_rights_doc(session, workspace_id, doc)


async def read_erasure_record(
    session: AsyncSession, workspace_id: UUID
) -> dict[str, Any] | None:
    doc = await retention_mod.read_data_rights_doc(session, workspace_id)
    rec = doc.get(ERASURE_STATE_KEY)
    return rec if isinstance(rec, dict) else None


__all__ = (
    "ERASURE_STATE_KEY",
    "STATUS_DEFERRED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_UNAVAILABLE",
    "ErasureConfirmationError",
    "ErasureReceipt",
    "ErasureStep",
    "admin_connection_available",
    "purge_credentials",
    "purge_documents",
    "purge_tax_identifiers",
    "purge_workspace_ledger",
    "read_erasure_record",
    "record_erasure",
    "revoke_access",
    "verify_confirmation",
)
