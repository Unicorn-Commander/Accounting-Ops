"""public schema: 3-persona RBAC expansion + workspace_invitation table

Revision ID: 0008_rbac_expansion
Revises: 0007_tax_chunking
Create Date: 2026-05-30

Three-persona customer model (platform admin, customer/owner, external CPA):

  * Adds new role enum values WITHOUT removing existing ones (additive).
    Final ``workspace_role`` enum:
      platform_admin, owner, client_owner, admin, cpa, accountant,
      bookkeeper, client_user, viewer
    Existing rows (owner/admin/accountant/viewer) remain untouched.

  * Creates ``workspace_invitation`` — pending email invites with a
    cryptographically random token, 7-day default TTL, and audit columns.
    INSERT/SELECT/UPDATE for the runtime role; DELETE allowed for cleanup of
    cancelled invites by the inviter.

  * Creates ``email_outbox`` — fallback queue for outbound mail when no SMTP
    relay is configured. The mailer attempts SMTP first; on failure or when
    no relay is set, it queues here for a follow-up integration.

Idempotent on re-run: enum ADDs are guarded with IF NOT EXISTS-style DO blocks.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_rbac_expansion"
down_revision: str | None = "0007_tax_chunking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Order matches the hierarchy in app/tenancy.py _ROLE_RANK (highest = most
# privileged). Values are ADDED ONLY — never reorder or drop existing ones.
NEW_ROLE_VALUES = (
    "platform_admin",
    "client_owner",
    "cpa",
    "bookkeeper",
    "client_user",
)

RUNTIME_ROLE = "accounting_ops_app"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Additive enum expansion. Each ADD VALUE IF NOT EXISTS is its own
    #    DO block so a partial re-run survives (and so Postgres can commit
    #    each value before the table referencing it is touched).
    # ------------------------------------------------------------------
    for value in NEW_ROLE_VALUES:
        op.execute(
            f"ALTER TYPE workspace_role ADD VALUE IF NOT EXISTS '{value}'"
        )

    # ------------------------------------------------------------------
    # 2. workspace_invitation — magic-link pending invites.
    # ------------------------------------------------------------------
    op.create_table(
        "workspace_invitation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column(
            "email",
            sa.String(length=256),
            nullable=False,
            comment="Invitee email — lower-cased on write.",
        ),
        sa.Column(
            "role",
            postgresql.ENUM(name="workspace_role", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "entity_scope",
            postgresql.JSONB(),
            nullable=True,
            comment="Optional restriction list of entity UUIDs. Null = whole workspace.",
        ),
        sa.Column(
            "token_hash",
            sa.String(length=128),
            nullable=False,
            comment="SHA-256 hex of the 32-byte url-safe token. The raw token is "
            "returned to the inviter ONCE and never stored.",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Hard expiry. Default 7 days from creation; configurable per-invite.",
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "accepted_by_user_id",
            sa.UUID(),
            nullable=True,
            comment="Set when accept lands on a known user_account.",
        ),
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_by_uc_uid",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "resent_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_resent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["public.workspace.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"], ["public.user_account.id"], ondelete="SET NULL"
        ),
        schema="public",
    )
    op.create_index(
        "ix_workspace_invitation_token_hash",
        "workspace_invitation",
        ["token_hash"],
        unique=True,
        schema="public",
    )
    op.create_index(
        "ix_workspace_invitation_workspace_id",
        "workspace_invitation",
        ["workspace_id"],
        schema="public",
    )
    op.create_index(
        "ix_workspace_invitation_email_lower",
        "workspace_invitation",
        [sa.text("lower(email)")],
        schema="public",
    )

    # ------------------------------------------------------------------
    # 3. email_outbox — best-effort outbound queue for invite + system mail.
    #    Used when SMTP is unreachable OR when SMTP is unconfigured (so the
    #    invite is still tracked and a follow-up integration can drain the
    #    queue). The app NEVER fails an invite creation just because the
    #    relay is down — the queue absorbs the message.
    # ------------------------------------------------------------------
    op.create_table(
        "email_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("to_email", sa.String(length=256), nullable=False),
        sa.Column("from_email", sa.String(length=256), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
            comment="pending | sent | failed",
        ),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "related_invitation_id",
            sa.UUID(),
            nullable=True,
            comment="If set, links back to a workspace_invitation for tracing.",
        ),
        sa.ForeignKeyConstraint(
            ["related_invitation_id"],
            ["public.workspace_invitation.id"],
            ondelete="SET NULL",
        ),
        schema="public",
    )
    op.create_index(
        "ix_email_outbox_status_queued",
        "email_outbox",
        ["status", "queued_at"],
        schema="public",
    )

    # ------------------------------------------------------------------
    # 4. Runtime grants — least-privilege for the app role.
    #    Invitation lifecycle: app inserts, selects, updates (accept/cancel/
    #    resend). DELETE is intentionally NOT granted — cancellation flips
    #    cancelled_at instead, preserving the audit trail.
    # ------------------------------------------------------------------
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON public.workspace_invitation TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON public.email_outbox TO {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON public.email_outbox FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON public.workspace_invitation FROM {RUNTIME_ROLE}")
    op.drop_index("ix_email_outbox_status_queued", table_name="email_outbox", schema="public")
    op.drop_table("email_outbox", schema="public")
    op.drop_index(
        "ix_workspace_invitation_email_lower",
        table_name="workspace_invitation",
        schema="public",
    )
    op.drop_index(
        "ix_workspace_invitation_workspace_id",
        table_name="workspace_invitation",
        schema="public",
    )
    op.drop_index(
        "ix_workspace_invitation_token_hash",
        table_name="workspace_invitation",
        schema="public",
    )
    op.drop_table("workspace_invitation", schema="public")
    # NOTE: We do NOT drop the new enum values. Postgres does not support
    # removing an enum value without a destructive table rewrite, and existing
    # rows may legitimately reference them by the time a downgrade runs.
