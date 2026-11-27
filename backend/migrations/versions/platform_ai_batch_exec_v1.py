"""Add an opt-in execution lease without rewriting historical batch runs.

Revision ID: platform_ai_batch_exec_v1
Revises: platform_ai_batch_v1
"""

import sqlalchemy as sa
from alembic import op

revision = "platform_ai_batch_exec_v1"
down_revision = "platform_ai_batch_v1"
branch_labels = None
depends_on = None

TABLE = "batch_runs"
LEASE = "execution_lease_expires_at"
INDEX = "ix_batch_runs_execution_lease_due"
PREDICATE = "is_deleted IS FALSE AND status = 'running' AND execution_lease_expires_at IS NOT NULL"


def _normalize_predicate(value: object) -> str:
    text = str(value).replace("(", "").replace(")", "").replace("::text", "")
    return " ".join(text.split())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column = next(
        (col for col in inspector.get_columns(TABLE) if col["name"] == LEASE), None
    )
    if column is None:
        op.add_column(TABLE, sa.Column(LEASE, sa.DateTime(), nullable=True))
    elif (
        not isinstance(column["type"], sa.DateTime)
        or column["type"].timezone
        or not column["nullable"]
        or column["default"] is not None
    ):
        raise RuntimeError("batch_execution_lease_column_incompatible")

    index = next(
        (item for item in inspector.get_indexes(TABLE) if item["name"] == INDEX), None
    )
    if index is None:
        op.create_index(
            INDEX, TABLE, [LEASE, "id"], postgresql_where=sa.text(PREDICATE)
        )
    elif (
        index["column_names"] != [LEASE, "id"]
        or index["unique"]
        or _normalize_predicate(index["dialect_options"].get("postgresql_where"))
        != _normalize_predicate(PREDICATE)
    ):
        raise RuntimeError("batch_execution_lease_index_incompatible")


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE batch_runs IN ACCESS EXCLUSIVE MODE"))
    inspector = sa.inspect(bind)
    if not any(col["name"] == LEASE for col in inspector.get_columns(TABLE)):
        return

    runs = sa.table(
        TABLE, sa.column("is_deleted"), sa.column("status"), sa.column(LEASE)
    )
    has_running_lease = sa.exists().where(
        runs.c.is_deleted.is_(False),
        runs.c.status == "running",
        runs.c[LEASE].is_not(None),
    )
    if bind.scalar(sa.select(has_running_lease)):
        raise RuntimeError("batch_execution_lease_has_running_runs")

    if any(item["name"] == INDEX for item in inspector.get_indexes(TABLE)):
        op.drop_index(INDEX, table_name=TABLE)
    # 中文: 已判为待确认的运行保留原状态。应用回退可保留全部增量结构。
    # EN: Keep recorded unknown outcomes. An application rollback may leave the additive schema intact.
    op.drop_column(TABLE, LEASE)
