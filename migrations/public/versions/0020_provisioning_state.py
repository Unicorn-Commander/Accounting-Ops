# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Control-plane state, bounded replay storage, and removal receipts (public only)."""

import sqlalchemy as sa
from alembic import op

revision = "0020_provisioning_state"
down_revision = "0019_workspace_onboarded_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column("control_plane_state", sa.Text(), nullable=False, server_default="active"),
        schema="public",
    )
    op.create_check_constraint(
        "ck_workspace_control_plane_state",
        "workspace",
        "control_plane_state IN ('active', 'suspended', 'archived')",
        schema="public",
    )
    # Existing runtime grants intentionally do not allow updating registry status.
    op.execute(
        "GRANT UPDATE (control_plane_state, uc_org_id) ON public.workspace TO accounting_ops_app"
    )
    for name in ("revoked_pat_count", "static_service_token_count"):
        op.add_column(
            "membership_audit",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
            schema="public",
        )
    op.create_table(
        "provisioning_replay",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        schema="public",
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON public.provisioning_replay TO accounting_ops_app")


def downgrade() -> None:
    op.drop_table("provisioning_replay", schema="public")
    for name in ("revoked_pat_count", "static_service_token_count"):
        op.drop_column("membership_audit", name, schema="public")
    op.execute(
        "REVOKE UPDATE (control_plane_state, uc_org_id) ON public.workspace FROM accounting_ops_app"
    )
    op.drop_constraint("ck_workspace_control_plane_state", "workspace", schema="public")
    op.drop_column("workspace", "control_plane_state", schema="public")
