"""中文: 为 task_runs 增加监控租约字段；注册租约回收周期任务。

EN: Add monitor-lease columns to task_runs; seed the lease reaper periodic task.

严格校验：task_runs / task_definitions 必须存在且具备预期列；缺失则失败，不静默跳过。
Strict validation: task_runs / task_definitions must exist with expected columns; missing -> fail.

Revision ID: platform_task_lease_v1
Revises: platform_notifications_v1
Create Date: 2026-11-26
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "platform_task_lease_v1"
down_revision: str | Sequence[str] | None = "platform_notifications_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONITOR_TASK_CODE = "system.task_run_monitor"
MONITOR_TASK_HANDLER = "app.tasks.task_run_monitor.reconcile_expired_task_runs"

# task_runs 必填列（添加租约字段前必须存在）。
# task_runs mandatory columns (must exist before adding lease fields).
_TASK_RUNS_REQUIRED_COLUMNS: tuple[str, ...] = (
    "id",
    "celery_task_id",
    "status",
    "handler_path_snapshot",
    "run_key",
    "effective_tenant_id",
)

# task_definitions 必填列（种子任务定义前必须存在）。
# task_definitions mandatory columns (must exist before seeding monitor task).
_TASK_DEFINITIONS_REQUIRED_COLUMNS: tuple[str, ...] = (
    "id",
    "code",
    "handler_path",
    "owner_tenant_id",
    "is_system_builtin",
    "is_enabled",
    "name",
    "definition_type",
    "scope",
)


def _require_table(bind: sa.Connection, table_name: str) -> None:
    if not sa.inspect(bind).has_table(table_name):
        raise RuntimeError(
            f"Migration {revision} requires table {table_name!r} but it does not exist"
        )


def _require_columns(
    bind: sa.Connection, table_name: str, required: tuple[str, ...]
) -> set[str]:
    _require_table(bind, table_name)
    actual = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
    missing = set(required) - actual
    if missing:
        raise RuntimeError(
            f"Migration {revision} requires columns {sorted(missing)} "
            f"on table {table_name!r} but they are missing"
        )
    return actual


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}


_LEASE_INDEX_NAME = "ix_task_runs_lease_reap"
_LEASE_INDEX_COLUMNS: tuple[str, ...] = (
    "status",
    "handler_path_snapshot",
    "lease_token",
    "lease_expires_at",
)

# 期望列规格：(类型, 可空性)。
# Expected column spec: (type, nullable).
_LEASE_COLUMN_SPECS: dict[str, tuple[sa.TypeEngine, bool]] = {
    "lease_token": (sa.String(length=64), True),
    "lease_expires_at": (sa.DateTime(timezone=True), True),
    "monitor_lost_at": (sa.DateTime(timezone=True), True),
}


def _validate_existing_column(
    col: dict[str, object],
    expected_type: sa.TypeEngine,
    expected_nullable: bool,
    dialect: str,
) -> None:
    """校验已有列的类型、长度、时区、可空性；不兼容则抛错。
    Validate existing column type/length/timezone/nullable; raise on mismatch."""
    col_type = col.get("type")
    if col_type is None:
        raise RuntimeError(
            f"Migration {revision}: column {col.get('name')!r} has no type info"
        )

    if isinstance(expected_type, sa.String):
        if not isinstance(col_type, sa.String):
            raise RuntimeError(
                f"Migration {revision}: column {col.get('name')!r} type "
                f"{col_type!r} is not String; expected String(64)"
            )
        actual_length = getattr(col_type, "length", None)
        if actual_length != 64:
            raise RuntimeError(
                f"Migration {revision}: column {col.get('name')!r} has "
                f"String length {actual_length}, expected 64"
            )
    elif isinstance(expected_type, sa.DateTime):
        if not isinstance(col_type, sa.DateTime):
            raise RuntimeError(
                f"Migration {revision}: column {col.get('name')!r} type "
                f"{col_type!r} is not DateTime; expected DateTime(timezone=True)"
            )
        # SQLite 反射丢失 timezone 标志；仅 PG 严格校验。
        # SQLite reflection loses the tz flag; only enforce on PG.
        if dialect == "postgresql" and not bool(getattr(col_type, "timezone", False)):
            raise RuntimeError(
                f"Migration {revision}: column {col.get('name')!r} DateTime "
                "is not timezone-aware; expected DateTime(timezone=True)"
            )
    else:
        if type(col_type) is not type(expected_type):
            raise RuntimeError(
                f"Migration {revision}: column {col.get('name')!r} type "
                f"{col_type!r} incompatible with {expected_type!r}"
            )

    if bool(col.get("nullable")) != expected_nullable:
        raise RuntimeError(
            f"Migration {revision}: column {col.get('name')!r} nullable="
            f"{col.get('nullable')!r}, expected {expected_nullable}"
        )


def _add_lease_columns(bind: sa.Connection) -> None:
    """添加租约字段（幂等，类型/长度/tz/nullable 不兼容则失败）。
    Add lease columns (idempotent; fail on any incompatible attribute)."""
    columns_info = _require_columns(bind, "task_runs", _TASK_RUNS_REQUIRED_COLUMNS)
    dialect = bind.dialect.name
    existing_columns = {c["name"]: c for c in sa.inspect(bind).get_columns("task_runs")}

    for name, (expected_type, expected_nullable) in _LEASE_COLUMN_SPECS.items():
        if name in columns_info and name in existing_columns:
            _validate_existing_column(
                existing_columns[name],
                expected_type,
                expected_nullable,
                dialect,
            )

    if "lease_token" not in columns_info:
        op.add_column(
            "task_runs",
            sa.Column(
                "lease_token",
                sa.String(length=64),
                nullable=True,
                comment="本次执行租约令牌 / Lease token for current run",
            ),
        )
    if "lease_expires_at" not in columns_info:
        op.add_column(
            "task_runs",
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="租约过期时间（带时区）/ Lease expiry (tz-aware)",
            ),
        )
    if "monitor_lost_at" not in columns_info:
        op.add_column(
            "task_runs",
            sa.Column(
                "monitor_lost_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="监控失联观察时间 / Monitor-observed lost timestamp",
            ),
        )


def _find_index(bind: sa.Connection, table_name: str, index_name: str):
    for index in sa.inspect(bind).get_indexes(table_name):
        if index["name"] == index_name:
            return index
    return None


def _predicate_equivalent(actual_where: str | None) -> bool:
    """Accept only the expected IN predicate and its PostgreSQL ANY rewrite."""
    if not actual_where:
        return False
    value = str(actual_where)
    # Keep literal case significant; never accept an extra predicate or OR branch.
    if re.findall(r"'[^']*'", value) != ["'running'", "'unknown'"]:
        return False
    value = re.sub(r"::(?:character varying|text)(?:\[\])?", "", value, flags=re.I)
    normalized = re.sub(r'[\s()"]', "", value).lower()
    return normalized in {
        "lease_tokenisnotnullandstatusin'running','unknown'",
        "lease_tokenisnotnullandstatus=anyarray['running','unknown']",
    }


def _add_lease_index(bind: sa.Connection) -> None:
    """添加租约回收部分索引；已存在时严格校验列/唯一性/谓词。
    Add lease reaping partial index; on existing, strictly verify columns,
    uniqueness, and predicate (accept PG IN -> ANY rewrites)."""
    _require_columns(bind, "task_runs", _TASK_RUNS_REQUIRED_COLUMNS + ("lease_token",))
    existing = _find_index(bind, "task_runs", _LEASE_INDEX_NAME)

    if existing is not None:
        actual_columns = list(existing.get("column_names") or [])
        if tuple(actual_columns) != _LEASE_INDEX_COLUMNS:
            raise RuntimeError(
                f"Migration {revision}: index {_LEASE_INDEX_NAME!r} has columns "
                f"{actual_columns!r}, expected {list(_LEASE_INDEX_COLUMNS)!r}"
            )
        if bool(existing.get("unique")):
            raise RuntimeError(
                f"Migration {revision}: index {_LEASE_INDEX_NAME!r} is unique; "
                "expected non-unique"
            )
        # PG 将 postgresql_where 以改写后的 SQL 形式返回。
        # PG returns postgresql_where as rewritten SQL.
        actual_where = (existing.get("dialect_options") or {}).get("postgresql_where")
        if bind.dialect.name == "postgresql" and not _predicate_equivalent(
            actual_where
        ):
            raise RuntimeError(
                f"Migration {revision}: index {_LEASE_INDEX_NAME!r} predicate "
                f"{actual_where!r} does not match expected "
                "'lease_token IS NOT NULL AND status IN (running, unknown)'"
            )
        return

    op.create_index(
        _LEASE_INDEX_NAME,
        "task_runs",
        list(_LEASE_INDEX_COLUMNS),
        unique=False,
        postgresql_where=sa.text(
            "lease_token IS NOT NULL AND status IN ('running', 'unknown')"
        ),
    )


def _task_definitions_table(columns: set[str]) -> sa.TableClause:
    column_types: dict[str, sa.TypeEngine] = {
        "id": sa.Integer(),
        "created_at": sa.DateTime(),
        "updated_at": sa.DateTime(),
        "is_deleted": sa.Boolean(),
        "deleted_at": sa.DateTime(),
        "delete_level": sa.String(length=20),
        "recycle_stage": sa.String(length=20),
        "promoted_to_global_at": sa.DateTime(),
        "owner_tenant_id": sa.Integer(),
        "code": sa.String(length=100),
        "name": sa.String(length=200),
        "definition_type": sa.String(length=30),
        "handler_path": sa.String(length=255),
        "category": sa.String(length=50),
        "description": sa.Text(),
        "scope": sa.String(length=32),
        "default_schedule_type": sa.String(length=20),
        "default_cron_expression": sa.String(length=100),
        "default_interval_seconds": sa.Integer(),
        "default_queue": sa.String(length=50),
        "default_priority": sa.Integer(),
        "default_args": sa.JSON(),
        "default_kwargs": sa.JSON(),
        "config_schema": sa.JSON(),
        "required_feature_codes": sa.JSON(),
        "required_plugin_names": sa.JSON(),
        "is_system_builtin": sa.Boolean(),
        "max_retries": sa.Integer(),
        "retry_delay": sa.Integer(),
        "timeout": sa.Integer(),
        "notify_on_failure": sa.Boolean(),
        "notify_emails": sa.Text(),
        "is_enabled": sa.Boolean(),
        "is_editable": sa.Boolean(),
        "is_deletable": sa.Boolean(),
    }
    return sa.table(
        "task_definitions",
        *(
            sa.column(name, column_types[name])
            for name in column_types
            if name in columns
        ),
    )


def _monitor_task_row(columns: set[str]) -> dict[str, object]:
    values: dict[str, object] = {
        "owner_tenant_id": None,
        "code": MONITOR_TASK_CODE,
        "name": "TaskRun 监控租约回收",
        "definition_type": "system",
        "handler_path": MONITOR_TASK_HANDLER,
        "category": "maintenance",
        "description": "每分钟回收过期的 TaskRun 监控租约",
        # ResourceScopeEnum.ADMIN_ONLY; "platform" is a run kind, not a scope.
        "scope": "admin_only",
        "default_schedule_type": "cron",
        "default_cron_expression": "* * * * *",
        "default_interval_seconds": None,
        "default_queue": "scheduled",
        "default_priority": None,
        "default_args": [],
        "default_kwargs": {},
        "config_schema": {},
        "required_feature_codes": [],
        "required_plugin_names": [],
        "is_system_builtin": True,
        "max_retries": 0,
        "retry_delay": 60,
        "timeout": 60,
        "notify_on_failure": False,
        "notify_emails": None,
        "is_enabled": True,
        "is_editable": False,
        "is_deletable": False,
        "is_deleted": False,
        "deleted_at": None,
        "delete_level": None,
        "recycle_stage": None,
        "promoted_to_global_at": None,
        "updated_at": sa.func.now(),
    }
    values["created_at"] = sa.func.now()
    return {key: value for key, value in values.items() if key in columns}


def _ensure_monitor_task(bind: sa.Connection) -> None:
    """Seed the monitor without rewriting its existing operator settings / 保留既有设置并补齐目录。"""
    _require_columns(bind, "task_definitions", _TASK_DEFINITIONS_REQUIRED_COLUMNS)
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("task_definitions")
    }
    task_definitions = _task_definitions_table(columns)

    existing = (
        bind.execute(
            sa.select(
                task_definitions.c.id,
                task_definitions.c.handler_path,
                task_definitions.c.owner_tenant_id,
                task_definitions.c.is_system_builtin,
            ).where(task_definitions.c.code == MONITOR_TASK_CODE)
        )
        .mappings()
        .first()
    )

    if existing:
        if (
            existing["handler_path"] != MONITOR_TASK_HANDLER
            or existing["owner_tenant_id"] is not None
            or existing["is_system_builtin"] is not True
        ):
            raise ValueError(
                f"Task code {MONITOR_TASK_CODE!r} is occupied by handler "
                f"{existing['handler_path']!r}, expected {MONITOR_TASK_HANDLER!r}"
            )
        return

    bind.execute(sa.insert(task_definitions).values(_monitor_task_row(columns)))


def upgrade() -> None:
    bind = op.get_bind()
    _add_lease_columns(bind)
    _add_lease_index(bind)
    _ensure_monitor_task(bind)


# 未完结租约状态：downgrade 必须拒绝，避免丢弃进行中/未知的结果不确定性。
# Unresolved lease statuses: downgrade must refuse to avoid discarding
# in-flight / unknown outcome uncertainty.
_UNRESOLVED_STATUSES: tuple[str, ...] = ("running", "retrying", "unknown")


def _assert_no_unresolved_leases(bind: sa.Connection) -> None:
    """只阻止未完结状态（running/retrying/unknown），保留 success/failed 历史。
    Block only unresolved statuses; preserve success/failed historical leases."""
    if not sa.inspect(bind).has_table("task_runs"):
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns("task_runs")}
    if "status" not in columns:
        return
    task_runs = sa.table(
        "task_runs",
        sa.column("status", sa.String()),
        sa.column("lease_token", sa.String()),
    )
    guard = task_runs.c.status == "unknown"
    if "lease_token" in columns:
        guard = sa.or_(
            guard,
            sa.and_(
                task_runs.c.lease_token.is_not(None),
                task_runs.c.status.in_(("running", "retrying")),
            ),
        )
    unresolved = bind.execute(
        sa.select(sa.func.count()).select_from(task_runs).where(guard)
    ).scalar_one()
    if unresolved:
        raise RuntimeError(
            f"Downgrade {revision} refused: {unresolved} task_run row(s) still "
            f"in unresolved statuses {_UNRESOLVED_STATUSES}. "
            "Resolve running/retrying/unknown before reverting. "
            "Terminal success/failed rows are preserved."
        )


def downgrade() -> None:
    bind = op.get_bind()
    _assert_no_unresolved_leases(bind)

    if sa.inspect(bind).has_table("task_definitions"):
        columns = {
            column["name"]
            for column in sa.inspect(bind).get_columns("task_definitions")
        }
        if {"code", "handler_path", "owner_tenant_id", "is_system_builtin"}.issubset(
            columns
        ):
            task_definitions = _task_definitions_table(columns)
            bind.execute(
                sa.delete(task_definitions).where(
                    task_definitions.c.code == MONITOR_TASK_CODE,
                    task_definitions.c.handler_path == MONITOR_TASK_HANDLER,
                    task_definitions.c.owner_tenant_id.is_(None),
                    task_definitions.c.is_system_builtin.is_(True),
                )
            )

    if sa.inspect(bind).has_table("task_runs"):
        indexes = _index_names(bind, "task_runs")
        if _LEASE_INDEX_NAME in indexes:
            op.drop_index(_LEASE_INDEX_NAME, table_name="task_runs")

        columns = {
            column["name"] for column in sa.inspect(bind).get_columns("task_runs")
        }
        if "monitor_lost_at" in columns:
            op.drop_column("task_runs", "monitor_lost_at")
        if "lease_expires_at" in columns:
            op.drop_column("task_runs", "lease_expires_at")
        if "lease_token" in columns:
            op.drop_column("task_runs", "lease_token")
