"""中文: 持久通知租约和补扫目录。 EN: Durable notification leases and recovery catalog.

Revision ID: platform_notifications_v1
Revises: 20260612_0051_context_tools
"""

import sqlalchemy as sa
from alembic import op

revision = "platform_notifications_v1"
down_revision = "20260612_0051_context_tools"
branch_labels = None
depends_on = None

TABLE = "notification_deliveries"
INDEX = "ix_notification_deliveries_outbox_due"
TASK_CODE = "notification.deliveries.recovery"
TASK_HANDLER = "app.tasks.notification.recover_notification_deliveries"
TASK_QUEUE = "notification"
TASK_INTERVAL_SECONDS = 30
COLUMNS = {
    "payload_ciphertext": sa.Text(),
    "available_at": sa.DateTime(timezone=True),
    "lease_token": sa.String(36),
    "lease_expires_at": sa.DateTime(timezone=True),
    "submission_started_at": sa.DateTime(timezone=True),
}


def _seed_recovery_task(bind):
    """中文: 重跑不覆盖运营停用或定时设置。 EN: Repeat runs preserve operator disablement and scheduling overrides."""
    if not sa.inspect(bind).has_table("task_definitions"):
        return
    definitions = sa.Table("task_definitions", sa.MetaData(), autoload_with=bind)
    required = {"id", "code", "handler_path", "owner_tenant_id", "is_system_builtin"}
    if not required.issubset(definitions.c.keys()):
        raise RuntimeError("notification_recovery_catalog_incomplete")
    existing = bind.execute(
        sa.select(
            definitions.c.id,
            definitions.c.handler_path,
            definitions.c.owner_tenant_id,
            definitions.c.is_system_builtin,
        ).where(definitions.c.code == TASK_CODE)
    ).first()
    if existing:
        if (
            existing.handler_path != TASK_HANDLER
            or existing.owner_tenant_id is not None
            or existing.is_system_builtin is not True
        ):
            raise RuntimeError("notification_recovery_task_code_conflict")
        return
    values = {
        "owner_tenant_id": None,
        "code": TASK_CODE,
        "name": "通知持久投递补扫 / Durable notification recovery",
        "definition_type": "system",
        "handler_path": TASK_HANDLER,
        "category": "maintenance",
        "description": "仅补派持久通知ID；未知外发不重试 / Recover durable IDs without replaying uncertain writes",
        "scope": "admin_only",
        "default_schedule_type": "interval",
        "default_cron_expression": None,
        "default_interval_seconds": TASK_INTERVAL_SECONDS,
        "default_queue": TASK_QUEUE,
        "default_priority": None,
        "default_args": [],
        "default_kwargs": {"limit": 20},
        "config_schema": {},
        "required_feature_codes": [],
        "required_plugin_names": [],
        "is_system_builtin": True,
        "max_retries": 0,
        "retry_delay": 30,
        "timeout": 300,
        "notify_on_failure": False,
        "notify_emails": None,
        "is_enabled": True,
        "is_editable": False,
        "is_deletable": False,
        "created_at": sa.func.now(),
        "updated_at": sa.func.now(),
        "is_deleted": False,
    }
    bind.execute(
        sa.insert(definitions).values(
            {key: value for key, value in values.items() if key in definitions.c}
        )
    )


def _ensure_dedupe(bind):
    for table, channels in (
        ("notifications", ()),
        ("notification_deliveries", ("channel",)),
    ):
        inspection = sa.inspect(bind)
        if "dedupe_key" not in {
            column["name"] for column in inspection.get_columns(table)
        }:
            op.add_column(table, sa.Column("dedupe_key", sa.String(160), nullable=True))
        indexes = {index["name"] for index in inspection.get_indexes(table)}
        columns = [
            "recipient_type",
            "recipient_id",
            "template_code",
            *channels,
            "dedupe_key",
        ]
        for scope, tenant_columns, tenant_filter in (
            ("tenant", ["tenant_id"], "tenant_id IS NOT NULL"),
            ("platform", [], "tenant_id IS NULL"),
        ):
            name = f"uq_{table}_{scope}_dedupe"
            if name not in indexes:
                op.create_index(
                    name,
                    table,
                    tenant_columns + columns,
                    unique=True,
                    postgresql_where=sa.text(
                        f"is_deleted = false AND {tenant_filter} AND dedupe_key IS NOT NULL"
                    ),
                )
        lookup = f"idx_{table}_dedupe"
        if channels and lookup not in indexes:
            op.create_index(lookup, table, ["dedupe_key"])


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    for name, column_type in COLUMNS.items():
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, column_type, nullable=True))
    if INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(
            INDEX,
            TABLE,
            ["status", "available_at", "id"],
            postgresql_where=sa.text(
                "is_deleted = false AND payload_ciphertext IS NOT NULL"
            ),
        )
    _ensure_dedupe(bind)
    _seed_recovery_task(bind)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    if "payload_ciphertext" in existing:
        table = sa.Table(TABLE, sa.MetaData(), autoload_with=bind)
        if bind.scalar(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.payload_ciphertext.is_not(None))
        ):
            raise RuntimeError(
                "notification_payloads_require_explicit_retention_before_downgrade"
            )
    if inspector.has_table("task_definitions"):
        definitions = sa.Table("task_definitions", sa.MetaData(), autoload_with=bind)
        bind.execute(
            sa.delete(definitions).where(
                definitions.c.code == TASK_CODE,
                definitions.c.handler_path == TASK_HANDLER,
                definitions.c.owner_tenant_id.is_(None),
                definitions.c.is_system_builtin.is_(True),
            )
        )
    if INDEX in {index["name"] for index in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    for name in reversed(COLUMNS):
        if name in existing:
            op.drop_column(TABLE, name)
