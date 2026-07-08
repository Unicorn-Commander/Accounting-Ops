"""Personal Access Token (PAT) — mint, verify, revoke.

A PAT is a long-lived bearer token a human user mints from /settings/pats so
external MCP clients (Claude Desktop, Cursor, ...) can call our API on the
user's behalf without holding a Keycloak refresh token. Wire format::

    aops_pat_<43 url-safe base64 chars>      # 52 chars total

Properties:
  * High entropy: secrets.token_urlsafe(32) → 256 bits, base64-url encoded.
  * Storage: ONLY sha256(token).digest() (raw 32 bytes BYTEA) + first 12 chars
    as a non-secret prefix (the prefix identifies the token row; it does NOT
    grant access on its own).
  * Comparison: constant-time via ``secrets.compare_digest`` on the full hash.
  * Revocation: ``revoked_at IS NOT NULL`` short-circuits auth. Effective
    immediately (no cache).

Why not bcrypt/argon2?  PATs are 256-bit random tokens, not user-typed
passwords. There is nothing to protect against an offline guess attack — the
keyspace is already cryptographically large. SHA-256 is the right choice for
authenticated-token storage (constant work, no dependency on a slow KDF) and
matches how GitHub PATs, Stripe API keys, etc. are stored.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PAT_PREFIX = "aops_pat_"
"""Wire-format prefix. MUST stay in sync with .well-known/mcps.json."""

_RAW_TOKEN_LEN = 32  # bytes → 43 base64-url chars
PREFIX_DISPLAY_LEN = 12
"""How many chars of the raw token we store as the displayable prefix.

12 chars = 9 chars of the PAT_PREFIX literal + 3 chars of entropy. The 3 chars
of entropy are enough to distinguish dozens of a user's tokens at a glance
without exposing enough material to be brute-forceable (3 base64 chars = 18
bits of entropy, still useless without the rest of the token).
"""

DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365
MAX_ACTIVE_PATS_PER_USER = 20


@dataclass(frozen=True)
class MintedPAT:
    """Result of minting. ``secret`` is shown to the user ONCE then forgotten."""

    id: UUID
    name: str
    secret: str  # raw token — ONLY in the create response, never re-derivable
    prefix: str
    scopes: tuple[str, ...]
    workspace_scope: tuple[UUID, ...] | None
    expires_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class PATRow:
    """Server-side view of a stored PAT (no secret material)."""

    id: UUID
    owner_user_id: UUID
    name: str
    secret_prefix: str
    scopes: tuple[str, ...]
    workspace_scope: tuple[UUID, ...] | None
    expires_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(timezone.utc)

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired


# ---------------------------------------------------------------------------
# Secret generation + hashing primitives
# ---------------------------------------------------------------------------


def generate_secret() -> str:
    """Return a fresh raw PAT in wire format: ``aops_pat_<43 url-safe chars>``."""
    return f"{PAT_PREFIX}{secrets.token_urlsafe(_RAW_TOKEN_LEN)}"


def hash_secret(raw: str) -> bytes:
    """sha256 of the raw token. 32 raw bytes (NOT hex). Stored in BYTEA."""
    return hashlib.sha256(raw.encode("utf-8")).digest()


def secret_prefix(raw: str) -> str:
    """Non-secret display prefix (first 12 chars). Safe to show + log."""
    return raw[:PREFIX_DISPLAY_LEN]


def looks_like_pat(raw: str) -> bool:
    """Cheap pre-check: does this bearer look like one of our PATs?

    Used by the auth chain to skip JWT validation for obvious PATs. Returns
    False on anything that can't be ours, so callers fall through to JWT.
    """
    if not raw or not isinstance(raw, str):
        return False
    if not raw.startswith(PAT_PREFIX):
        return False
    # PAT_PREFIX + 43 url-safe chars = 52 total. Be generous on the upper bound
    # (token_urlsafe might round to 43 reliably, but tolerate near-misses).
    return 44 <= len(raw) <= 80


def constant_time_eq(a: bytes, b: bytes) -> bool:
    """Constant-time bytes equality. Thin wrapper over secrets.compare_digest."""
    return secrets.compare_digest(a, b)


# ---------------------------------------------------------------------------
# Validation helpers (scope intersection, TTL, count cap)
# ---------------------------------------------------------------------------


def validate_scopes(
    requested: list[str] | None, owner_allowed: set[str]
) -> tuple[str, ...]:
    """A PAT may only grant scopes the OWNER has via their workspace membership.

    Returns the requested subset that overlaps with ``owner_allowed``. If
    ``requested`` is None, returns the full owner_allowed set (a fresh PAT
    inherits everything the user can do). If the request asks for a scope the
    user lacks, raises ``ValueError`` — fail loud rather than silently widen.
    """
    if requested is None:
        return tuple(sorted(owner_allowed))
    extras = [s for s in requested if s not in owner_allowed]
    if extras:
        raise ValueError(
            f"Cannot grant scopes the owner lacks: {sorted(extras)!r}"
        )
    return tuple(sorted({s for s in requested if s}))


def resolve_ttl(ttl_days: int | None) -> datetime | None:
    """Map a TTL request (in days) to a UTC expires_at.

    ``None`` or 0 → ``None`` (never expires; reserved for power users who
    accept the staleness risk). Negative → ValueError. Capped at MAX_TTL_DAYS.
    """
    if ttl_days is None or ttl_days == 0:
        return None
    if ttl_days < 0:
        raise ValueError("ttl_days must be >= 0 (0 = never expires)")
    if ttl_days > MAX_TTL_DAYS:
        raise ValueError(f"ttl_days exceeds maximum ({MAX_TTL_DAYS})")
    return datetime.now(timezone.utc) + timedelta(days=ttl_days)


# ---------------------------------------------------------------------------
# Storage helpers (raw SQL — schema is in the public schema migration)
# ---------------------------------------------------------------------------


def _row_to_pat(row: Any) -> PATRow:
    workspace_scope: tuple[UUID, ...] | None = None
    if row.workspace_scope is not None:
        workspace_scope = tuple(
            v if isinstance(v, UUID) else UUID(str(v)) for v in row.workspace_scope
        )
    return PATRow(
        id=row.id if isinstance(row.id, UUID) else UUID(str(row.id)),
        owner_user_id=(
            row.owner_user_id
            if isinstance(row.owner_user_id, UUID)
            else UUID(str(row.owner_user_id))
        ),
        name=row.name,
        secret_prefix=row.secret_prefix,
        scopes=tuple(row.scopes or ()),
        workspace_scope=workspace_scope,
        expires_at=row.expires_at,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


async def count_active_pats(session: AsyncSession, owner_user_id: UUID) -> int:
    """Return how many non-revoked, non-expired PATs the user has."""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS n
                FROM public.personal_access_token
                WHERE owner_user_id = :uid
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                """
            ),
            {"uid": str(owner_user_id)},
        )
    ).first()
    return int(row.n) if row else 0


async def list_pats(session: AsyncSession, owner_user_id: UUID) -> list[PATRow]:
    """Return all PATs (active + revoked + expired) belonging to the user."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, owner_user_id, name, secret_prefix, scopes,
                       workspace_scope, expires_at, created_at,
                       last_used_at, revoked_at
                FROM public.personal_access_token
                WHERE owner_user_id = :uid
                ORDER BY created_at DESC
                """
            ),
            {"uid": str(owner_user_id)},
        )
    ).all()
    return [_row_to_pat(r) for r in rows]


async def create_pat(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    name: str,
    scopes: tuple[str, ...],
    workspace_scope: tuple[UUID, ...] | None,
    expires_at: datetime | None,
) -> MintedPAT:
    """Mint a PAT and persist its hash. Returns the raw secret ONCE."""
    raw = generate_secret()
    digest = hash_secret(raw)
    prefix = secret_prefix(raw)
    workspace_scope_param = (
        [str(w) for w in workspace_scope] if workspace_scope is not None else None
    )
    row = (
        await session.execute(
            text(
                """
                INSERT INTO public.personal_access_token (
                    id, owner_user_id, name, secret_hash, secret_prefix,
                    scopes, workspace_scope, expires_at, created_at
                ) VALUES (
                    gen_random_uuid(), :uid, :name, :hash, :prefix,
                    :scopes, :wscope, :expires_at, now()
                )
                RETURNING id, created_at
                """
            ),
            {
                "uid": str(owner_user_id),
                "name": name,
                "hash": digest,
                "prefix": prefix,
                "scopes": list(scopes),
                "wscope": workspace_scope_param,
                "expires_at": expires_at,
            },
        )
    ).one()
    return MintedPAT(
        id=row.id if isinstance(row.id, UUID) else UUID(str(row.id)),
        name=name,
        secret=raw,
        prefix=prefix,
        scopes=scopes,
        workspace_scope=workspace_scope,
        expires_at=expires_at,
        created_at=row.created_at,
    )


async def revoke_pat(
    session: AsyncSession, *, owner_user_id: UUID, pat_id: UUID
) -> bool:
    """Flip revoked_at = now() (idempotent — already-revoked stays). Returns
    True if a row matched, False if the PAT doesn't exist or isn't this user's.
    """
    result = await session.execute(
        text(
            """
            UPDATE public.personal_access_token
            SET revoked_at = COALESCE(revoked_at, now())
            WHERE id = :pat_id AND owner_user_id = :uid
            RETURNING id
            """
        ),
        {"pat_id": str(pat_id), "uid": str(owner_user_id)},
    )
    return result.first() is not None


async def delete_pat(
    session: AsyncSession, *, owner_user_id: UUID, pat_id: UUID
) -> bool:
    """Hard-delete a PAT row. Caller must enforce admin authorization."""
    result = await session.execute(
        text(
            """
            DELETE FROM public.personal_access_token
            WHERE id = :pat_id AND owner_user_id = :uid
            RETURNING id
            """
        ),
        {"pat_id": str(pat_id), "uid": str(owner_user_id)},
    )
    return result.first() is not None


async def find_active_pat_by_token(
    session: AsyncSession, raw_token: str
) -> tuple[PATRow, UUID, str] | None:
    """Look up the PAT row matching ``raw_token`` and return (pat, owner_uc_uid).

    Returns None on any failure (no prefix match, hash mismatch, revoked,
    expired). NEVER raises (the auth chain falls through on None).

    Returns the tuple ``(pat_row, owner_user_id, owner_uc_uid)`` on success.
    """
    if not looks_like_pat(raw_token):
        return None
    prefix = secret_prefix(raw_token)
    presented_hash = hash_secret(raw_token)

    # Look up by indexed prefix. There may be ≥ 1 row sharing this prefix on
    # an astronomical collision; we constant-time-compare the full hash on
    # each candidate so timing leaks the count, not the secret. With 18 bits
    # of entropy in the displayable prefix tail there will almost never be
    # more than one candidate, but the loop is correct regardless.
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    p.id, p.owner_user_id, p.name, p.secret_hash,
                    p.secret_prefix, p.scopes, p.workspace_scope,
                    p.expires_at, p.created_at, p.last_used_at, p.revoked_at,
                    u.uc_uid AS owner_uc_uid
                FROM public.personal_access_token p
                JOIN public.user_account u ON u.id = p.owner_user_id
                WHERE p.secret_prefix = :prefix
                """
            ),
            {"prefix": prefix},
        )
    ).all()
    if not rows:
        return None

    # Constant-time compare every candidate so a timing oracle can't single
    # out the winning row.
    matched = None
    for row in rows:
        if constant_time_eq(bytes(row.secret_hash), presented_hash):
            matched = row
            # Don't break — keep doing the compare for consistent timing.
    if matched is None:
        return None

    pat = _row_to_pat(matched)
    if pat.is_revoked or pat.is_expired:
        return None
    return pat, pat.owner_user_id, matched.owner_uc_uid


async def touch_last_used(session: AsyncSession, pat_id: UUID) -> None:
    """Update last_used_at. Best-effort: never raises (caller logs + continues)."""
    try:
        await session.execute(
            text(
                """
                UPDATE public.personal_access_token
                SET last_used_at = now()
                WHERE id = :pat_id
                """
            ),
            {"pat_id": str(pat_id)},
        )
    except Exception:  # pragma: no cover - never block auth on telemetry
        pass


__all__ = (
    "PAT_PREFIX",
    "PREFIX_DISPLAY_LEN",
    "DEFAULT_TTL_DAYS",
    "MAX_TTL_DAYS",
    "MAX_ACTIVE_PATS_PER_USER",
    "MintedPAT",
    "PATRow",
    "generate_secret",
    "hash_secret",
    "secret_prefix",
    "looks_like_pat",
    "constant_time_eq",
    "validate_scopes",
    "resolve_ttl",
    "count_active_pats",
    "list_pats",
    "create_pat",
    "revoke_pat",
    "delete_pat",
    "find_active_pat_by_token",
    "touch_last_used",
)
