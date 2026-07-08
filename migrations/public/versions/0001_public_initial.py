"""public schema: workspace registry + users + memberships + global enums

Revision ID: 0001_public_initial
Revises:
Create Date: 2026-05-21

Three-level isolation model (see docs/SCHEMA-PER-TENANT.md):
  workspace = customer/org (billing + isolation unit)
  entity    = legal entity within workspace (per-entity table in workspace schema)
  user      = human identity (joined to workspaces via workspace_membership)

Plus agent_identity entries for non-human callers (Brigade agents) which
authenticate via RFC 8693 act-claim on top of a delegating user.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_public_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    for name, values in (
        ("sovereignty_mode", ("standard", "strict")),
        ("isolation_tier", ("shared", "premium", "enterprise")),
        ("workspace_status", ("provisioning", "active", "suspended", "archived")),
        ("workspace_role", ("owner", "admin", "accountant", "viewer")),
        ("agent_kind", ("bookkeeper", "reconciler", "tax_reporting", "compliance", "cpa_handoff", "cash_forecaster", "runway_watcher", "ap_scheduler", "receipt_ingestor", "payment_initiator", "orchestrator", "other")),
    ):
        sa.Enum(*values, name=name).create(bind, checkfirst=True)

    sovereignty_mode = postgresql.ENUM(name="sovereignty_mode", create_type=False)
    isolation_tier = postgresql.ENUM(name="isolation_tier", create_type=False)
    workspace_status = postgresql.ENUM(name="workspace_status", create_type=False)
    workspace_role = postgresql.ENUM(name="workspace_role", create_type=False)
    agent_kind = postgresql.ENUM(name="agent_kind", create_type=False)

    op.create_table(
        "workspace",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("postgres_schema", sa.String(length=128), nullable=False),
        sa.Column("falkordb_subgraph", sa.String(length=128), nullable=False),
        sa.Column("garage_bucket", sa.String(length=128), nullable=False),
        sa.Column(
            "isolation_tier",
            isolation_tier,
            server_default="shared",
            nullable=False,
        ),
        sa.Column(
            "hipaa_protected",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "sovereignty_mode",
            sovereignty_mode,
            server_default="standard",
            nullable=False,
        ),
        sa.Column(
            "status",
            workspace_status,
            server_default="provisioning",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.UniqueConstraint("postgres_schema"),
        schema="public",
    )
    op.create_index("ix_workspace_slug", "workspace", ["slug"], schema="public")
    op.create_index("ix_workspace_status", "workspace", ["status"], schema="public")

    op.create_table(
        "user_account",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("uc_uid", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uc_uid"),
        schema="public",
    )
    op.create_index("ix_user_account_uc_uid", "user_account", ["uc_uid"], schema="public")

    op.create_table(
        "workspace_membership",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", workspace_role, nullable=False),
        sa.Column(
            "entity_scope",
            postgresql.JSONB(),
            nullable=True,
            comment="Optional. If null, role applies to all entities in workspace. "
            "If set, list of entity UUIDs the role applies to (CPA scoped to one entity, e.g.)",
        ),
        sa.Column(
            "invited_by_uc_uid",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["public.workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["public.user_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
        schema="public",
    )
    op.create_index(
        "ix_workspace_membership_workspace_id",
        "workspace_membership",
        ["workspace_id"],
        schema="public",
    )
    op.create_index(
        "ix_workspace_membership_user_id",
        "workspace_membership",
        ["user_id"],
        schema="public",
    )

    # Agent identities (non-human callers). An agent acts on behalf of a delegating
    # human user (via RFC 8693 act claim). Each agent identity has its own scoped
    # OAuth client + limited tools. Multiple agents can share a kind.
    op.create_table(
        "agent_identity",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "agent_id",
            sa.String(length=256),
            nullable=False,
            comment="Stable identifier used in OAuth act claim, e.g. 'brigade:bookkeeper:v1.2'",
        ),
        sa.Column("kind", agent_kind, nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column(
            "owning_workspace_id",
            sa.UUID(),
            nullable=True,
            comment="If set, this agent belongs to one workspace. If null, it's a "
            "platform-wide agent (e.g. shared Brigade orchestrator).",
        ),
        sa.Column(
            "default_runtime_tier",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="1=local Qwen, 2=cheap hosted open-weight, 3=frontier Claude/Gemini, 4=long-context",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owning_workspace_id"], ["public.workspace.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id"),
        schema="public",
    )
    op.create_index(
        "ix_agent_identity_kind",
        "agent_identity",
        ["kind"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_table("agent_identity", schema="public")
    op.drop_table("workspace_membership", schema="public")
    op.drop_table("user_account", schema="public")
    op.drop_table("workspace", schema="public")
    for name in (
        "agent_kind",
        "workspace_role",
        "workspace_status",
        "isolation_tier",
        "sovereignty_mode",
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
