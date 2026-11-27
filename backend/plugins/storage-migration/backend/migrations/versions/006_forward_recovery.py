"""Track forward migrations covered by PostgreSQL ownership recovery."""

import sqlalchemy as sa
from alembic import op

revision = "sm_006_forward_recovery"
down_revision = "sm_005_forward_attempt"
branch_labels = None

_TABLE = "px_storage_migration_tasks"
_INDEX = "ix_px_sm_forward_recovery"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("forward_run_id", sa.String(32), nullable=True))
    op.create_index(
        _INDEX, _TABLE, ["id"], postgresql_where=sa.text("forward_run_id IS NOT NULL")
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {_TABLE} WHERE forward_run_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("storage_forward_recovery_downgrade_blocked")
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "forward_run_id")
