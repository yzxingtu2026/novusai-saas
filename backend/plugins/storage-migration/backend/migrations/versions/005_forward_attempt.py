"""Persist forward migration attempts without adopting historical receipts.

Revision ID: sm_005_forward_attempt
Revises: sm_004_rollback_journal
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "sm_005_forward_attempt"
down_revision = "sm_004_rollback_journal"
branch_labels = None

_TASK_TABLE = "px_storage_migration_tasks"
_LOG_TABLE = "px_storage_migration_logs"
_ATTEMPT_TABLE = "px_storage_migration_attempts"
_CONSTRAINT = "ck_px_sm_logs_forward_attempt"
_ATTEMPT_INDEX = "ix_px_sm_attempts_task_log"


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns(_LOG_TABLE)
    if not columns:
        return
    if "forward_token" not in columns:
        op.add_column(
            _LOG_TABLE,
            sa.Column(
                "forward_token",
                sa.String(32),
                nullable=True,
                comment="Forward attempt identity; historical rows remain unclaimed",
            ),
        )
    if "forward_snapshot" not in columns:
        op.add_column(
            _LOG_TABLE,
            sa.Column(
                "forward_snapshot",
                sa.JSON(),
                nullable=True,
                comment="Frozen forward attempt receipt retained after settlement",
            ),
        )
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(_LOG_TABLE)
    }
    if _CONSTRAINT not in constraints:
        # A settled success/failure still needs its attempt receipt. Runtime
        # validates receipt content; this constraint enforces SQL NULL pairing.
        op.create_check_constraint(
            _CONSTRAINT,
            _LOG_TABLE,
            "(forward_token IS NULL AND forward_snapshot IS NULL) OR "
            "(forward_token IS NOT NULL AND forward_snapshot IS NOT NULL)",
        )
    if not sa.inspect(op.get_bind()).has_table(_ATTEMPT_TABLE):
        # The log points at the latest attempt; this separate ledger retains
        # every older destination receipt across explicit retries.
        op.create_table(
            _ATTEMPT_TABLE,
            sa.Column("token", sa.String(32), primary_key=True),
            sa.Column(
                "task_id",
                sa.BigInteger(),
                sa.ForeignKey("px_storage_migration_tasks.id"),
                nullable=False,
            ),
            sa.Column(
                "log_id",
                sa.BigInteger(),
                sa.ForeignKey("px_storage_migration_logs.id"),
                nullable=False,
            ),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "state IN ('prepared', 'uploaded', 'succeeded', 'failed', 'unknown')",
                name="ck_px_sm_attempts_state",
            ),
        )
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_ATTEMPT_TABLE)
    }
    if _ATTEMPT_INDEX not in indexes:
        op.create_index(_ATTEMPT_INDEX, _ATTEMPT_TABLE, ["task_id", "log_id"])


def downgrade() -> None:
    columns = _columns(_LOG_TABLE)
    forward_columns = {"forward_token", "forward_snapshot"}
    has_attempt_table = sa.inspect(op.get_bind()).has_table(_ATTEMPT_TABLE)
    if not columns.intersection(forward_columns) and not has_attempt_table:
        return
    if not forward_columns | {
        "status",
        "rollback_state",
        "rollback_snapshot",
    } <= columns or not {"status", "recovery_token"} <= _columns(_TASK_TABLE):
        raise RuntimeError("storage_forward_attempt_downgrade_schema_unavailable")
    bind = op.get_bind()
    # Match the existing recovery migration's lock order. Keep both locks until
    # the migration transaction ends, including every admission check and DROP.
    lock_statement = (
        "LOCK TABLE px_storage_migration_tasks, px_storage_migration_logs, "
        "px_storage_migration_attempts IN ACCESS EXCLUSIVE MODE"
        if has_attempt_table
        else (
            "LOCK TABLE px_storage_migration_tasks, px_storage_migration_logs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    bind.execute(sa.text(lock_statement))
    blocked = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM px_storage_migration_logs "
            "WHERE forward_token IS NOT NULL OR forward_snapshot IS NOT NULL "
            "OR status IN ('transferring', 'commit_unknown') "
            "OR rollback_state <> 'none' OR rollback_snapshot IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM px_storage_migration_tasks "
            "WHERE recovery_token IS NOT NULL OR status IN "
            "('migration_uncertain', 'source_cleaning', 'cleanup_interrupted', "
            "'rolling_back', 'rollback_failed', 'rollback_interrupted'))"
        )
    ).scalar_one()
    if has_attempt_table:
        blocked = (
            bind.execute(
                sa.text("SELECT EXISTS (SELECT 1 FROM px_storage_migration_attempts)")
            ).scalar_one()
            or blocked
        )
    if blocked:
        raise RuntimeError("storage_forward_attempt_downgrade_blocked")
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints(_LOG_TABLE)
    }
    if _CONSTRAINT in constraints:
        op.drop_constraint(_CONSTRAINT, _LOG_TABLE, type_="check")
    if has_attempt_table:
        op.drop_table(_ATTEMPT_TABLE)
    op.drop_column(_LOG_TABLE, "forward_snapshot")
    op.drop_column(_LOG_TABLE, "forward_token")
