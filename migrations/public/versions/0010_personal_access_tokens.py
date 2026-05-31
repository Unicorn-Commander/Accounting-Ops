"""public schema: personal_access_token — user-minted long-lived MCP tokens

Revision ID: 0010_personal_access_tokens
Revises: 0005_tax_knowledge_base
Create Date: 2026-05-31

ARCHITECTURE NOTE
-----------------
Personal Access Tokens (PATs) let a human user mint a long-lived bearer that
external AI clients (Claude Desktop, Cursor, etc.) present when calling our
MCP surface. The flow is:

  1. User signs into the web app via Keycloak SSO (uchub).
  2. User visits /settings/pats and clicks "Generate new token".
  3. Backend returns `aops_pat_<43 url-safe chars>` ONCE — never retrievable.
  4. User pastes that secret into the external client's MCP config.
  5. Every subsequent request bearing that token validates fast (sha256 hash
     lookup on indexed prefix) and resolves to AuthContext(uc_uid=owner, ...).

Why a separate table (not a service token in env):
  * Per-user, self-served lifecycle (mint, list, revoke) without env edits.
  * Per-token scope constraints (workspaces, scopes) survive restarts.
  * Audit: last_used_at + immediate revocation.
  * Defense-in-depth: PATs cannot mint more PATs.

Storage shape (one row per token):
  * `secret_hash`   = raw sha256(token).digest()    -- 32 bytes BYTEA
  * `secret_prefix` = first 12 chars of token       -- e.g. 'aops_pat_a3f'

The prefix is INDEXED so a request's bearer can be resolved in O(1):
look up by prefix → constant-time compare the full sha256 hash. The prefix
alone is non-secret (it identifies the token, not the bearer) so showing it
in the UI + audit log is safe.

Runtime grants (accounting_ops_app role):
  SELECT, INSERT, UPDATE, DELETE on personal_access_token
  (last_used_at updates + revoke flips + owner-initiated delete).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_personal_access_tokens"
down_revision: str | None = "0009_regulatory_feed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUNTIME_ROLE = "accounting_ops_app"


def upgrade() -> None:
    op.create_table(
        "personal_access_token",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_user_id",
            sa.UUID(),
            nullable=False,
            comment="FK -> public.user_account.id. Token impersonates this user.",
        ),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="User-supplied label, e.g. 'Claude Desktop — laptop'.",
        ),
        sa.Column(
            "secret_hash",
            postgresql.BYTEA(),
            nullable=False,
            comment="Raw sha256(token) bytes. 32 bytes. Compared constant-time.",
        ),
        sa.Column(
            "secret_prefix",
            sa.Text(),
            nullable=False,
            comment="First 12 chars of the token (e.g. 'aops_pat_a3f'). "
            "Indexed for fast lookup; non-secret — safe to display + log.",
        ),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
            comment="Subset of the owner's role-granted scopes "
            "(e.g. accounting:read, accounting:write).",
        ),
        sa.Column(
            "workspace_scope",
            postgresql.ARRAY(postgresql.UUID()),
            nullable=True,
            comment="Optional workspace UUIDs the PAT may access. "
            "NULL = all workspaces the owner is a member of.",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="NULL = never expires. Default 90 days from creation "
            "(enforced at the API layer, not server_default).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Updated on each successful auth (fire-and-forget).",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft delete. Revoked tokens fail auth immediately.",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["public.user_account.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="public",
    )

    # Hot path: every request with a `aops_pat_*` bearer looks up by prefix.
    op.create_index(
        "ix_personal_access_token_secret_prefix",
        "personal_access_token",
        ["secret_prefix"],
        schema="public",
    )
    # Listing a user's own tokens.
    op.create_index(
        "ix_personal_access_token_owner_user_id",
        "personal_access_token",
        ["owner_user_id"],
        schema="public",
    )

    # Runtime grants (mirrors 0003_rls_and_runtime_grants pattern).
    # The runtime role needs SELECT (lookup), INSERT (mint), UPDATE
    # (last_used_at + revoke flip), DELETE (admin hard-delete).
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.personal_access_token "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE ALL ON public.personal_access_token FROM {RUNTIME_ROLE}"
    )
    op.drop_index(
        "ix_personal_access_token_owner_user_id",
        table_name="personal_access_token",
        schema="public",
    )
    op.drop_index(
        "ix_personal_access_token_secret_prefix",
        table_name="personal_access_token",
        schema="public",
    )
    op.drop_table("personal_access_token", schema="public")
