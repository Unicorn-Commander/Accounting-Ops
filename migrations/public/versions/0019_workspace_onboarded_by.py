"""public schema: workspace += onboarded_by_user_id (console-onboarded client workspaces)

``POST /api/me/clients`` (Team → Clients → "Add client") provisions a workspace
for a client of the caller's practice. The practitioner who does that is seated
as the workspace's ``owner`` so the practice can run the books end to end —
connect the bank (admin floor), invite the client-owner and their own team
(owner floor), close periods. That also means "holds a ``cpa`` seat" no longer
identifies a practitioner, so the registry records who onboarded each
workspace and the persona router counts those.

Nullable, no backfill: existing workspaces were provisioned by operators or by
self-serve first-workspace onboarding, never through the console.

Also widens ``ck_membership_audit_action`` (0017) with ``console_onboard``: the
console's self-seat is written to the membership audit trail like any other
membership mutation, and the CHECK must admit that action.

Revision ID: 0019_workspace_onboarded_by
Revises: 0018_invoice_pay_token
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_workspace_onboarded_by"
down_revision: str | None = "0018_invoice_pay_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "onboarded_by_user_id"
FK_NAME = "fk_workspace_onboarded_by_user"
INDEX_NAME = "ix_workspace_onboarded_by_user_id"

AUDIT_CHECK = "ck_membership_audit_action"
# 0017's list + console_onboard (app/api/clients.py CONSOLE_ONBOARD_AUDIT_ACTION).
MEMBERSHIP_AUDIT_ACTIONS = (
    "invitation_create",
    "invitation_cancel",
    "invitation_accept",
    "role_change",
    "entity_scope_change",
    "removal",
    "console_onboard",
)
_PREVIOUS_AUDIT_ACTIONS = MEMBERSHIP_AUDIT_ACTIONS[:-1]


def _action_check_sql(actions: tuple[str, ...]) -> str:
    return "action IN (" + ", ".join(f"'{a}'" for a in actions) + ")"


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
        ).first()
    )


def _constraint_exists(schema: str, table: str, name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_schema = :schema AND table_name = :table
                  AND constraint_name = :name
                """
            ),
            {"schema": schema, "table": table, "name": name},
        ).first()
    )


def _index_exists(schema: str, name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes WHERE schemaname = :schema AND indexname = :name"
            ),
            {"schema": schema, "name": name},
        ).first()
    )


def upgrade() -> None:
    if not _column_exists("public", "workspace", COLUMN):
        op.add_column(
            "workspace",
            sa.Column(
                COLUMN,
                sa.UUID(),
                nullable=True,
                comment=(
                    "user_account.id of the practitioner who provisioned this "
                    "workspace through POST /api/me/clients (the CPA console). "
                    "NULL for operator- or self-provisioned workspaces."
                ),
            ),
            schema="public",
        )
    if not _constraint_exists("public", "workspace", FK_NAME):
        op.create_foreign_key(
            FK_NAME,
            "workspace",
            "user_account",
            [COLUMN],
            ["id"],
            source_schema="public",
            referent_schema="public",
            ondelete="SET NULL",
        )
    if not _index_exists("public", INDEX_NAME):
        op.create_index(
            INDEX_NAME,
            "workspace",
            [COLUMN],
            schema="public",
            postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        )

    # Membership audit: admit the console's self-seat action. The constraint is
    # recreated (not altered in place — Postgres has no ALTER CHECK); the table
    # stays append-only and RLS-isolated exactly as 0017 left it.
    if _constraint_exists("public", "membership_audit", AUDIT_CHECK):
        op.drop_constraint(AUDIT_CHECK, "membership_audit", schema="public", type_="check")
    op.create_check_constraint(
        AUDIT_CHECK,
        "membership_audit",
        _action_check_sql(MEMBERSHIP_AUDIT_ACTIONS),
        schema="public",
    )


def downgrade() -> None:
    # Restores 0017's action list. Refuses (loudly) if console_onboard rows
    # exist: audit rows are append-only and must not be dropped to downgrade.
    if _constraint_exists("public", "membership_audit", AUDIT_CHECK):
        op.drop_constraint(AUDIT_CHECK, "membership_audit", schema="public", type_="check")
    op.create_check_constraint(
        AUDIT_CHECK,
        "membership_audit",
        _action_check_sql(_PREVIOUS_AUDIT_ACTIONS),
        schema="public",
    )
    if _index_exists("public", INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name="workspace", schema="public")
    if _constraint_exists("public", "workspace", FK_NAME):
        op.drop_constraint(FK_NAME, "workspace", schema="public", type_="foreignkey")
    if _column_exists("public", "workspace", COLUMN):
        op.drop_column("workspace", COLUMN, schema="public")
