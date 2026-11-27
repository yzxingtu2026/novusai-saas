"""Persist destructive recovery ownership without adopting historical work.

Revision ID: sm_003_recovery_owner
Revises: sm_002_cleanup_meta
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "sm_003_recovery_owner"
down_revision = "sm_002_cleanup_meta"
branch_labels = None

_TASK_TABLE = "px_storage_migration_tasks"


def _has_token_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(_TASK_TABLE) and any(
        column["name"] == "recovery_token"
        for column in inspector.get_columns(_TASK_TABLE)
    )


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(_TASK_TABLE) or _has_token_column():
        return
    # NULL is intentional: an upgrade does not grant ownership of prior work.
    op.add_column(
        _TASK_TABLE,
        sa.Column(
            "recovery_token",
            sa.String(32),
            nullable=True,
            comment="Current destructive recovery claim; NULL grants no ownership",
        ),
    )


def downgrade() -> None:
    if not _has_token_column():
        return
    bind = op.get_bind()
    # Keep admission and removal in one transaction. A separate SELECT before
    # acquiring the DDL lock would allow a new owner to appear after the check.
    bind.execute(
        sa.text("LOCK TABLE px_storage_migration_tasks IN ACCESS EXCLUSIVE MODE")
    )
    blocked = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM px_storage_migration_tasks "
            "WHERE recovery_token IS NOT NULL OR status IN "
            "('source_cleaning', 'cleanup_interrupted', 'rolling_back', "
            "'rollback_failed', 'rollback_interrupted'))"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError("storage_recovery_ownership_downgrade_blocked")
    op.drop_column(_TASK_TABLE, "recovery_token")
