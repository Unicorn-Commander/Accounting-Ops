"""Object-level authorization helpers (OWASP API1:2023 — BOLA).

Workspace isolation is enforced at the Postgres-schema boundary (see
app/workspace_session). Entity isolation is a *row-level* concern: a
workspace_membership row may carry an ``entity_scope`` (a set of entity UUIDs)
that restricts the member to those entities only. Owners/admins typically have
``entity_scope is None`` (full workspace access); a scoped accountant (e.g. an
outside CPA hired for one subsidiary) has ``entity_scope = (entity_A,)``.

This module centralizes the entity-scope check so every endpoint that takes an
``entity_id`` enforces it identically. It mirrors the guards already present in
app/tenancy.resolve_entity and app/api/tools.py (the consolidated P&L guard).

Two shapes of access:

  1. A concrete entity_id (UUID or its string form): if the membership is
     scoped and the id is NOT in scope -> 403. In-scope or unscoped -> allowed.

  2. A "consolidated" request (entity_id is None / "consolidated" / "all"):
     an unscoped member sees everything (return None -> no restriction). A
     scoped member MUST NOT get a cross-entity consolidated view; instead of
     403'ing them out entirely we RESTRICT the consolidated query to their
     allowed entity set (return the tuple of allowed UUIDs). The caller passes
     that restriction into its query builder.

Both functions are pure (no I/O) so they're cheap to call on the hot path and
trivially unit-testable.
"""
from __future__ import annotations

from typing import Iterable
from uuid import UUID

from fastapi import HTTPException, status

from app.tenancy import WorkspaceContext

# Sentinel strings that mean "all entities / consolidated" rather than a
# specific entity id. Matches app/api/reports.py:_entity_filter.
_CONSOLIDATED_TOKENS = frozenset({"consolidated", "all"})


def assert_entity_in_scope(
    workspace: WorkspaceContext, entity_id: UUID | str | None
) -> tuple[UUID, ...] | None:
    """Enforce a membership's entity_scope for a single-entity request.

    Returns the tuple of entity UUIDs the query must be restricted to, or None
    if no row-level restriction applies. Semantics:

      * Unscoped membership (entity_scope is None): full access. Always returns
        None (no restriction) regardless of the requested entity.
      * Scoped membership + concrete entity_id IN scope: allowed; returns None
        (the concrete id already restricts the query — nothing extra to add).
      * Scoped membership + concrete entity_id NOT in scope: raises 403.
      * Scoped membership + consolidated/None request: returns the allowed
        entity tuple so the caller restricts the consolidated query to it
        (never 403 — the member legitimately sees their own entities).

    `entity_id` accepts a UUID, the string form of one, or a consolidated
    sentinel ("consolidated"/"all"/None). A malformed non-sentinel string is
    rejected with 400 (mirrors reports._entity_filter).
    """
    scope = workspace.entity_scope
    if scope is None:
        # Owner/admin/unscoped member — full access, no restriction.
        return None

    # Scoped member. Distinguish a concrete entity request from consolidated.
    if entity_id is None:
        return scope
    if isinstance(entity_id, str):
        token = entity_id.strip().lower()
        if token in _CONSOLIDATED_TOKENS or token == "":
            return scope
        try:
            entity_uuid = UUID(entity_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="entity_id must be a UUID or 'consolidated'/'all'",
            ) from exc
    else:
        entity_uuid = entity_id

    if entity_uuid not in scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entity outside this membership's scope",
        )
    # In scope: the concrete id already narrows the query; no extra restriction.
    return None


def scoped_entity_ids(
    workspace: WorkspaceContext, entity_id: UUID | str | None
) -> Iterable[UUID] | None:
    """Convenience alias returning the restriction set for a request.

    Identical to assert_entity_in_scope but named for the call-site that only
    wants "which entity ids may this consolidated query touch?". Returns None
    when no restriction applies (unscoped member, or in-scope concrete id).
    """
    return assert_entity_in_scope(workspace, entity_id)


__all__ = ("assert_entity_in_scope", "scoped_entity_ids")
