"""Persist per-object rollback recovery without inventing historical proofs.

Revision ID: sm_004_rollback_journal
Revises: sm_003_recovery_owner
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "sm_004_rollback_journal"
down_revision = "sm_003_recovery_owner"
branch_labels = None

_TASK_TABLE = "px_storage_migration_tasks"
_LOG_TABLE = "px_storage_migration_logs"
_CONSTRAINT = "ck_px_sm_logs_rollback_journal"


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns(_LOG_TABLE)
    if not columns:
        return
    if "rollback_state" not in columns:
        op.add_column(
            _LOG_TABLE,
            sa.Column(
                "rollback_state",
                sa.String(20),
                nullable=False,
                server_default=sa.text("'none'"),
                comment="Durable rollback phase; historical work remains none",
            ),
        )
    if "rollback_snapshot" not in columns:
        op.add_column(
            _LOG_TABLE,
            sa.Column(
                "rollback_snapshot",
                sa.JSON(),
                nullable=True,
                comment="Frozen target recovery material; no historical proof adoption",
            ),
        )
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(_LOG_TABLE)
    }
    if _CONSTRAINT not in constraints:
        op.create_check_constraint(
            _CONSTRAINT,
            _LOG_TABLE,
            "rollback_state IN ('none', 'source_restored', 'target_deleting', "
            "'target_deleted') AND ((rollback_state = 'none' AND "
            "rollback_snapshot IS NULL) OR (rollback_state <> 'none' AND "
            "rollback_snapshot IS NOT NULL))",
        )


def downgrade() -> None:
    columns = _columns(_LOG_TABLE)
    journal_columns = {"rollback_state", "rollback_snapshot"}
    if not columns.intersection(journal_columns):
        return
    if not journal_columns <= columns or not {
        "status",
        "recovery_token",
    } <= _columns(_TASK_TABLE):
        raise RuntimeError("storage_rollback_journal_downgrade_schema_unavailable")
    bind = op.get_bind()
    # Retain both locks through the caller-owned migration transaction, so
    # recovery cannot acquire ownership between the safety check and DROP.
    bind.execute(
        sa.text(
            "LOCK TABLE px_storage_migration_tasks, px_storage_migration_logs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    blocked = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM px_storage_migration_logs "
            "WHERE rollback_state <> 'none' OR rollback_snapshot IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM px_storage_migration_tasks "
            "WHERE recovery_token IS NOT NULL OR status IN "
            "('source_cleaning', 'cleanup_interrupted', 'rolling_back', "
            "'rollback_failed', 'rollback_interrupted'))"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError("storage_rollback_journal_downgrade_blocked")
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints(_LOG_TABLE)
    }
    if _CONSTRAINT in constraints:
        op.drop_constraint(_CONSTRAINT, _LOG_TABLE, type_="check")
    op.drop_column(_LOG_TABLE, "rollback_snapshot")
    op.drop_column(_LOG_TABLE, "rollback_state")
