"""Recover opted-in AI batch dispatches after loss of the publisher.

Revision ID: platform_ai_batch_v1
Revises: 20260612_0051_context_tools
"""

import sqlalchemy as sa
from alembic import op

revision = "platform_ai_batch_v1"
down_revision = "20260612_0051_context_tools"
branch_labels = None
depends_on = None

TABLE = "batch_runs"
DUE = "dispatch_recovery_due_at"
ATTEMPTS = "dispatch_recovery_attempts"
INDEX = "ix_batch_runs_dispatch_recovery_due"
INDEX_COLUMNS = ["status", DUE, "id"]
INDEX_PREDICATE = "is_deleted IS FALSE AND dispatch_recovery_due_at IS NOT NULL"
TASK_CODE = "ai.batch_dispatch.recovery"
TASK_HANDLER = "app.tasks.agent_batch.recover_batch_dispatches"


def _task_definitions(bind):
    return sa.Table("task_definitions", sa.MetaData(), autoload_with=bind)


def _seed_recovery_task(bind) -> None:
    definitions = _task_definitions(bind)
    found = bind.execute(
        sa.select(
            definitions.c.code,
            definitions.c.handler_path,
            definitions.c.owner_tenant_id,
            definitions.c.is_system_builtin,
            definitions.c.scope,
        ).where(
            sa.or_(
                definitions.c.code == TASK_CODE,
                definitions.c.handler_path == TASK_HANDLER,
            )
        )
    ).all()
    if found:
        if found != [(TASK_CODE, TASK_HANDLER, None, True, "admin_only")]:
            raise RuntimeError("batch_dispatch_recovery_catalog_conflict")
        return

    bind.execute(
        sa.insert(definitions).values(
            code=TASK_CODE,
            name="AI 批处理投递恢复 / AI batch dispatch recovery",
            description="恢复已提交、投递未确认的 AI 批处理 / Recover committed AI batch dispatches",
            definition_type="system",
            handler_path=TASK_HANDLER,
            category="ai",
            scope="admin_only",
            default_schedule_type="interval",
            default_interval_seconds=30,
            default_queue="scheduled",
            default_args=[],
            default_kwargs={},
            config_schema={},
            required_feature_codes=[],
            required_plugin_names=[],
            is_system_builtin=True,
            max_retries=0,
            retry_delay=30,
            timeout=60,
            notify_on_failure=False,
            is_enabled=True,
            is_editable=False,
            is_deletable=False,
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
            is_deleted=False,
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"]: col for col in sa.inspect(bind).get_columns(TABLE)}
    if DUE not in columns:
        op.add_column(TABLE, sa.Column(DUE, sa.DateTime(), nullable=True))
    else:
        due = columns[DUE]
        if (
            not isinstance(due["type"], sa.DateTime)
            or due["type"].timezone
            or not due["nullable"]
            or due["default"] is not None
        ):
            raise RuntimeError("batch_dispatch_recovery_due_column_incompatible")
    if ATTEMPTS not in columns:
        op.add_column(
            TABLE,
            sa.Column(
                ATTEMPTS, sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
        )
    else:
        attempts = columns[ATTEMPTS]
        if (
            type(attempts["type"]) is not sa.INTEGER
            or attempts["nullable"]
            or attempts["default"] != "0"
        ):
            raise RuntimeError("batch_dispatch_recovery_attempts_column_incompatible")

    indexes = {idx["name"]: idx for idx in sa.inspect(bind).get_indexes(TABLE)}
    if INDEX not in indexes:
        op.create_index(
            INDEX, TABLE, INDEX_COLUMNS, postgresql_where=sa.text(INDEX_PREDICATE)
        )
    else:
        index = indexes[INDEX]
        predicate = str(index["dialect_options"].get("postgresql_where"))
        normalized = " ".join(predicate.replace("(", "").replace(")", "").split())
        if (
            index["column_names"] != INDEX_COLUMNS
            or index["unique"]
            or normalized.casefold() != INDEX_PREDICATE.casefold()
        ):
            raise RuntimeError("batch_dispatch_recovery_index_incompatible")
    _seed_recovery_task(bind)


def downgrade() -> None:
    bind = op.get_bind()
    # Preserve work that has committed but has no confirmed publication.
    bind.execute(sa.text("LOCK TABLE batch_runs IN ACCESS EXCLUSIVE MODE"))
    pending = sa.table(
        TABLE,
        sa.column("id"),
        sa.column("status"),
        sa.column("is_deleted"),
        sa.column(DUE),
    )
    if (
        bind.scalar(
            sa.select(pending.c.id)
            .where(
                pending.c.status == "pending",
                pending.c.is_deleted.is_(False),
                pending.c[DUE].is_not(None),
            )
            .limit(1)
        )
        is not None
    ):
        raise RuntimeError("batch_dispatch_recovery_has_pending_work")

    definitions = _task_definitions(bind)
    bind.execute(
        sa.delete(definitions).where(
            definitions.c.code == TASK_CODE,
            definitions.c.handler_path == TASK_HANDLER,
            definitions.c.owner_tenant_id.is_(None),
            definitions.c.is_system_builtin.is_(True),
        )
    )
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, ATTEMPTS)
    op.drop_column(TABLE, DUE)
