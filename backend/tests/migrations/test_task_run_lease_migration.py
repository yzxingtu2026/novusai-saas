"""Test type: structural
Scope: task_run_lease migration (0055) — lease columns, partial index,
monitor task seed, downgrade safety.
Mock strategy: no mocks. Each test binds a real Alembic
``Operations(MigrationContext.configure(conn))`` to ``module.op`` so
``add_column`` / ``create_index`` / ``drop_index`` / ``drop_column``
execute against the in-memory SQLite connection exactly as they would
under Alembic. Real PG clone execution (partial index predicate
canonicalisation, TIMESTAMPTZ reflection) is a separate root-owned gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "platform_task_lease_v1.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "test_task_run_lease_migration_module",
        MIGRATION,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_real_ops(module, conn) -> Operations:
    """Replace ``module.op`` with a real Alembic Operations bound to conn.

    Returns the Operations object so callers can assert on it if needed.
    Using real Operations (not stubbed add/drop methods) is the whole point
    — the migration's ``op.*`` calls must flow through the same code path
    Alembic uses against a live connection.
    """
    ctx = MigrationContext.configure(conn)
    real_ops = Operations(ctx)
    module.op = real_ops
    return real_ops


def _create_task_runs(conn, *, with_lease: bool = False) -> sa.Table:
    metadata = sa.MetaData()
    columns = [
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("handler_path_snapshot", sa.String(255), nullable=True),
        sa.Column("run_key", sa.String(128), nullable=True),
        sa.Column("effective_tenant_id", sa.Integer, nullable=True),
    ]
    if with_lease:
        columns += [
            sa.Column("lease_token", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("monitor_lost_at", sa.DateTime(timezone=True), nullable=True),
        ]
    task_runs = sa.Table("task_runs", metadata, *columns)
    metadata.create_all(conn)
    return task_runs


def _create_task_definitions(conn) -> sa.Table:
    metadata = sa.MetaData()
    task_definitions = sa.Table(
        "task_definitions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, default=False),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("delete_level", sa.String(20), nullable=True),
        sa.Column("recycle_stage", sa.String(20), nullable=True),
        sa.Column("promoted_to_global_at", sa.DateTime, nullable=True),
        sa.Column("owner_tenant_id", sa.Integer, nullable=True),
        sa.Column("code", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("definition_type", sa.String(30), nullable=False),
        sa.Column("handler_path", sa.String(255), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("default_schedule_type", sa.String(20), nullable=True),
        sa.Column("default_cron_expression", sa.String(100), nullable=True),
        sa.Column("default_interval_seconds", sa.Integer, nullable=True),
        sa.Column("default_queue", sa.String(50), nullable=False),
        sa.Column("default_priority", sa.Integer, nullable=True),
        sa.Column("default_args", sa.JSON, nullable=True),
        sa.Column("default_kwargs", sa.JSON, nullable=True),
        sa.Column("config_schema", sa.JSON, nullable=True),
        sa.Column("required_feature_codes", sa.JSON, nullable=True),
        sa.Column("required_plugin_names", sa.JSON, nullable=True),
        sa.Column("is_system_builtin", sa.Boolean, nullable=False),
        sa.Column("max_retries", sa.Integer, nullable=False),
        sa.Column("retry_delay", sa.Integer, nullable=False),
        sa.Column("timeout", sa.Integer, nullable=False),
        sa.Column("notify_on_failure", sa.Boolean, nullable=False),
        sa.Column("notify_emails", sa.Text, nullable=True),
        sa.Column("is_enabled", sa.Boolean, nullable=False),
        sa.Column("is_editable", sa.Boolean, nullable=False),
        sa.Column("is_deletable", sa.Boolean, nullable=False),
    )
    metadata.create_all(conn)
    return task_definitions


def test_add_lease_columns_creates_nullable() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_task_runs(conn)
        _bind_real_ops(module, conn)
        module._add_lease_columns(conn)
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("task_runs")}

    assert "lease_token" in cols
    assert "lease_expires_at" in cols
    assert "monitor_lost_at" in cols
    assert cols["lease_token"]["nullable"] is True
    assert isinstance(cols["lease_token"]["type"], sa.String)
    assert cols["lease_expires_at"]["nullable"] is True
    assert cols["monitor_lost_at"]["nullable"] is True
    # SQLite 反射丢失 tz 标志；这里只断言 DateTime 类型本身。
    # SQLite reflection loses tz; only assert DateTime shape.
    assert isinstance(cols["lease_expires_at"]["type"], sa.DateTime)
    assert isinstance(cols["monitor_lost_at"]["type"], sa.DateTime)


def test_add_lease_columns_idempotent() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_task_runs(conn)
        _bind_real_ops(module, conn)
        module._add_lease_columns(conn)
        # Re-bind ops for each upgrade pass, as Operations keeps state.
        _bind_real_ops(module, conn)
        module._add_lease_columns(conn)
        _bind_real_ops(module, conn)
        module._add_lease_columns(conn)
        cols = [c["name"] for c in sa.inspect(conn).get_columns("task_runs")]

    assert cols.count("lease_token") == 1
    assert cols.count("lease_expires_at") == 1
    assert cols.count("monitor_lost_at") == 1


def test_add_lease_columns_rejects_incompatible_type() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    # Pre-create lease_token as Integer — incompatible with String(64).
    metadata = sa.MetaData()
    sa.Table(
        "task_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("handler_path_snapshot", sa.String(255), nullable=True),
        sa.Column("run_key", sa.String(128), nullable=True),
        sa.Column("effective_tenant_id", sa.Integer, nullable=True),
        sa.Column("lease_token", sa.Integer, nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="is not String"):
            module._add_lease_columns(conn)


def test_add_lease_columns_rejects_wrong_string_length() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    metadata = sa.MetaData()
    sa.Table(
        "task_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("handler_path_snapshot", sa.String(255), nullable=True),
        sa.Column("run_key", sa.String(128), nullable=True),
        sa.Column("effective_tenant_id", sa.Integer, nullable=True),
        sa.Column("lease_token", sa.String(32), nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="String length"):
            module._add_lease_columns(conn)


def test_add_lease_columns_rejects_non_nullable() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    metadata = sa.MetaData()
    sa.Table(
        "task_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("handler_path_snapshot", sa.String(255), nullable=True),
        sa.Column("run_key", sa.String(128), nullable=True),
        sa.Column("effective_tenant_id", sa.Integer, nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="nullable"):
            module._add_lease_columns(conn)


def test_missing_task_runs_table_fails() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="requires table"):
            module._add_lease_columns(conn)


def test_missing_required_column_fails() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    metadata = sa.MetaData()
    sa.Table(
        "task_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        # Missing celery_task_id, status, etc.
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="requires columns"):
            module._add_lease_columns(conn)


def test_lease_index_created() -> None:
    """Verify _add_lease_index creates the index on a fresh schema."""
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_task_runs(conn, with_lease=True)
        _bind_real_ops(module, conn)
        module._add_lease_index(conn)

        indexes = {
            idx["name"]: idx for idx in sa.inspect(conn).get_indexes("task_runs")
        }
        idx = indexes[module._LEASE_INDEX_NAME]

    assert list(idx["column_names"]) == list(module._LEASE_INDEX_COLUMNS)
    assert not bool(idx["unique"])


def test_lease_index_rejects_incompatible_columns() -> None:
    """Existing index with wrong columns must fail, not silently pass."""
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()

    sa.Table(
        "task_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("handler_path_snapshot", sa.String(255), nullable=True),
        sa.Column("run_key", sa.String(128), nullable=True),
        sa.Column("effective_tenant_id", sa.Integer, nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monitor_lost_at", sa.DateTime(timezone=True), nullable=True),
        # Wrong-column index with the same name.
        sa.Index(
            module._LEASE_INDEX_NAME,
            "status",
            "celery_task_id",
        ),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="columns"):
            module._add_lease_index(conn)


def test_lease_index_rejects_unique() -> None:
    """Existing unique index with correct columns must fail (expected non-unique)."""
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()

    sa.Table(
        "task_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("handler_path_snapshot", sa.String(255), nullable=True),
        sa.Column("run_key", sa.String(128), nullable=True),
        sa.Column("effective_tenant_id", sa.Integer, nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monitor_lost_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index(
            module._LEASE_INDEX_NAME,
            *module._LEASE_INDEX_COLUMNS,
            unique=True,
        ),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        _bind_real_ops(module, conn)
        with pytest.raises(RuntimeError, match="unique"):
            module._add_lease_index(conn)


def test_lease_index_idempotent() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_task_runs(conn, with_lease=True)
        _bind_real_ops(module, conn)
        module._add_lease_index(conn)
        _bind_real_ops(module, conn)
        module._add_lease_index(conn)
        _bind_real_ops(module, conn)
        module._add_lease_index(conn)

        indexes = [idx["name"] for idx in sa.inspect(conn).get_indexes("task_runs")]

    assert indexes.count(module._LEASE_INDEX_NAME) == 1


def test_predicate_equivalent_covers_pg_rewrites() -> None:
    """Predicate equivalence must accept both IN and PG's ANY(ARRAY[...]) forms."""
    module = _load_migration_module()

    # Original IN form.
    assert module._predicate_equivalent(
        "lease_token IS NOT NULL AND status IN ('running', 'unknown')"
    )
    # PG canonical rewrite without type casts.
    assert module._predicate_equivalent(
        "lease_token IS NOT NULL AND status = ANY (ARRAY['running', 'unknown'])"
    )
    # PG canonical rewrite with text casts.
    assert module._predicate_equivalent(
        "lease_token IS NOT NULL AND status = ANY (ARRAY['running'::text, 'unknown'::text])"
    )
    # PG canonical with varchar cast wrapping.
    assert module._predicate_equivalent(
        "((lease_token IS NOT NULL) AND ((status)::text = ANY "
        "(ARRAY[('running'::character varying)::text, "
        "('unknown'::character varying)::text])))"
    )
    # Wrong predicate — different statuses.
    assert not module._predicate_equivalent(
        "lease_token IS NOT NULL AND status IN ('running', 'failed')"
    )
    # Wrong predicate — missing lease_token guard.
    assert not module._predicate_equivalent("status IN ('running', 'unknown')")
    # Empty / None.
    assert not module._predicate_equivalent(None)
    assert not module._predicate_equivalent("")
    assert not module._predicate_equivalent(
        "lease_token IS NOT NULL OR status IN ('running', 'unknown')"
    )
    assert not module._predicate_equivalent(
        "lease_token IS NOT NULL AND status IN ('running', 'unknown') AND false"
    )
    assert not module._predicate_equivalent(
        "lease_token IS NOT NULL AND status IN ('RUNNING', 'unknown')"
    )


def test_monitor_task_seed_exact_fields() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_definitions = _create_task_definitions(conn)
        module._ensure_monitor_task(conn)

        row = (
            conn.execute(
                sa.select(task_definitions).where(
                    task_definitions.c.code == module.MONITOR_TASK_CODE
                )
            )
            .mappings()
            .one()
        )

    assert row["code"] == "system.task_run_monitor"
    assert row["handler_path"] == module.MONITOR_TASK_HANDLER
    assert (
        row["handler_path"] == "app.tasks.task_run_monitor.reconcile_expired_task_runs"
    )
    assert row["definition_type"] == "system"
    assert row["scope"] == "admin_only"
    from app.tasks.scheduler import PLATFORM_SCHEDULE_SCOPES

    assert row["scope"] in PLATFORM_SCHEDULE_SCOPES
    assert row["default_schedule_type"] == "cron"
    assert row["default_cron_expression"] == "* * * * *"
    assert row["default_queue"] == "scheduled"
    assert row["is_system_builtin"] is True
    assert row["is_enabled"] is True
    assert row["is_editable"] is False
    assert row["is_deletable"] is False
    assert row["category"] == "maintenance"
    assert row["max_retries"] == 0


def test_monitor_task_repeated_upgrade_no_duplicates() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_definitions = _create_task_definitions(conn)
        module._ensure_monitor_task(conn)
        module._ensure_monitor_task(conn)
        module._ensure_monitor_task(conn)

        rows = (
            conn.execute(
                sa.select(task_definitions).where(
                    task_definitions.c.code == module.MONITOR_TASK_CODE
                )
            )
            .mappings()
            .all()
        )

    assert len(rows) == 1


def test_monitor_task_preserves_explicit_disabled() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_definitions = _create_task_definitions(conn)
        conn.execute(
            task_definitions.insert(),
            {
                "id": 7,
                "code": module.MONITOR_TASK_CODE,
                "name": "old",
                "definition_type": "system",
                "handler_path": module.MONITOR_TASK_HANDLER,
                "category": "maintenance",
                "scope": "admin_only",
                "default_schedule_type": "interval",
                "default_queue": "default",
                "default_args": [],
                "default_kwargs": {},
                "config_schema": {},
                "required_feature_codes": [],
                "required_plugin_names": [],
                "is_system_builtin": True,
                "max_retries": 1,
                "retry_delay": 30,
                "timeout": 30,
                "notify_on_failure": True,
                "notify_emails": "ops@example.test",
                "is_enabled": False,
                "is_editable": True,
                "is_deletable": True,
                "is_deleted": False,
            },
        )

        module._ensure_monitor_task(conn)

        row = (
            conn.execute(sa.select(task_definitions).where(task_definitions.c.id == 7))
            .mappings()
            .one()
        )

    assert row["is_enabled"] is False
    assert row["scope"] == "admin_only"
    assert row["handler_path"] == module.MONITOR_TASK_HANDLER

    assert row["default_schedule_type"] == "interval"
    assert row["default_queue"] == "default"


def test_monitor_task_handler_collision_fails() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_definitions = _create_task_definitions(conn)
        conn.execute(
            task_definitions.insert(),
            {
                "id": 50,
                "code": module.MONITOR_TASK_CODE,
                "name": "hijacked",
                "definition_type": "system",
                "handler_path": "other.module.hijacked_handler",
                "category": "maintenance",
                "scope": "platform",
                "default_schedule_type": "cron",
                "default_queue": "scheduled",
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
            },
        )

        with pytest.raises(ValueError, match="is occupied by handler"):
            module._ensure_monitor_task(conn)


def test_downgrade_blocks_running() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_runs = _create_task_runs(conn, with_lease=True)
        conn.execute(
            task_runs.insert(),
            {"id": 1, "status": "running", "lease_token": "abc"},
        )

        with pytest.raises(RuntimeError, match="unresolved"):
            module._assert_no_unresolved_leases(conn)


def test_downgrade_blocks_retrying() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_runs = _create_task_runs(conn, with_lease=True)
        conn.execute(
            task_runs.insert(),
            {"id": 2, "status": "retrying", "lease_token": "retry-token"},
        )

        with pytest.raises(RuntimeError, match="unresolved"):
            module._assert_no_unresolved_leases(conn)


def test_downgrade_blocks_unknown_without_token() -> None:
    """unknown status blocked even without lease_token (outcome uncertainty)."""
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_runs = _create_task_runs(conn, with_lease=True)
        conn.execute(
            task_runs.insert(),
            {"id": 3, "status": "unknown", "lease_token": None},
        )

        with pytest.raises(RuntimeError, match="unresolved"):
            module._assert_no_unresolved_leases(conn)


def test_downgrade_allows_terminal_success_with_lease() -> None:
    """Historical success rows with residual lease_token must not block."""
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        task_runs = _create_task_runs(conn, with_lease=True)
        conn.execute(
            task_runs.insert(),
            {"id": 10, "status": "success", "lease_token": "old-token"},
        )
        conn.execute(
            task_runs.insert(),
            {"id": 11, "status": "failed", "lease_token": "old-token-2"},
        )

        # No raise — terminal rows preserved.
        module._assert_no_unresolved_leases(conn)


def test_downgrade_does_not_reclassify_legacy_unleased_tasks() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        runs = _create_task_runs(conn, with_lease=True)
        conn.execute(
            runs.insert(),
            [
                {"id": 20, "status": "running", "lease_token": None},
                {"id": 21, "status": "retrying", "lease_token": None},
            ],
        )
        module._assert_no_unresolved_leases(conn)
        assert conn.execute(
            sa.select(runs.c.status).order_by(runs.c.id)
        ).scalars().all() == ["running", "retrying"]


def test_downgrade_removes_only_seeded_task() -> None:
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_task_runs(conn, with_lease=True)
        task_definitions = _create_task_definitions(conn)
        conn.execute(
            task_definitions.insert(),
            [
                {
                    "id": 1,
                    "code": "unrelated.task",
                    "name": "Unrelated",
                    "definition_type": "business",
                    "handler_path": "app.tasks.unrelated",
                    "category": "maintenance",
                    "scope": "all_tenants",
                    "default_schedule_type": "cron",
                    "default_queue": "scheduled",
                    "default_args": [],
                    "default_kwargs": {},
                    "config_schema": {},
                    "required_feature_codes": [],
                    "required_plugin_names": [],
                    "is_system_builtin": False,
                    "max_retries": 1,
                    "retry_delay": 60,
                    "timeout": 120,
                    "notify_on_failure": False,
                    "notify_emails": None,
                    "is_enabled": True,
                    "is_editable": False,
                    "is_deletable": False,
                    "is_deleted": False,
                },
                {
                    "id": 2,
                    "code": module.MONITOR_TASK_CODE,
                    "name": "Monitor",
                    "definition_type": "system",
                    "handler_path": module.MONITOR_TASK_HANDLER,
                    "category": "maintenance",
                    "scope": "platform",
                    "default_schedule_type": "cron",
                    "default_queue": "scheduled",
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
                },
            ],
        )

        _bind_real_ops(module, conn)
        module.downgrade()

        remaining = (
            conn.execute(
                sa.select(task_definitions.c.code).order_by(task_definitions.c.id)
            )
            .scalars()
            .all()
        )

    assert remaining == ["unrelated.task"]
    assert module.MONITOR_TASK_CODE not in remaining
