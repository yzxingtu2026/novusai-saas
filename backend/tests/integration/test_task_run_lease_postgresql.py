"""Test type: behavioral.

中文: 真实 PostgreSQL 验证租约身份、结算、回收及会话竞争。
EN: Actual PostgreSQL monitor-lease transitions and competing sessions.

Requires PLATFORM_FIXTURE_POSTGRES_DSN for the disposable platform_tasks database.
Uses a committed UUID schema with all TaskRun columns and unique keys, but no
foreign keys to public fixtures. This tests CAS/query behavior, not full FK DDL.
Synthetic handler names are opted in by this fixture.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models.system.task_run import TaskRun
from app.repositories.system import task_run_lease_repository as lease_module
from app.repositories.system.task_run_lease_repository import TaskRunLeaseRepository

HANDLER = "fixture.tasks.monitor.first"
CATALOG_HANDLER = "fixture.tasks.monitor.second"


@pytest.fixture
def lease_db(monkeypatch):
    dsn = os.getenv("PLATFORM_FIXTURE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable platform_tasks PostgreSQL fixture required")
    url = make_url(dsn)
    assert url.host == "127.0.0.1" and url.port is not None
    assert url.database == "platform_tasks"
    assert url.get_backend_name() == "postgresql" and not url.query
    url = url.set(drivername="postgresql+psycopg2")
    engine = create_engine(url, poolclass=NullPool)
    schema = "task_lease_" + uuid4().hex
    metadata = MetaData(schema=schema)
    table = Table(
        "task_runs",
        metadata,
        *(
            Column(
                column.name,
                column.type.copy(),
                primary_key=column.primary_key,
                nullable=column.nullable,
                unique=column.name in {"celery_task_id", "run_key"},
            )
            for column in TaskRun.__table__.columns
        ),
    )
    with engine.begin() as conn:
        assert (
            conn.execute(text("SELECT current_database()")).scalar_one() == url.database
        )
        conn.execute(CreateSchema(schema))
        table.create(conn)
    scoped = engine.execution_options(schema_translate_map={None: schema})

    def factory():
        return Session(scoped, expire_on_commit=False)

    monkeypatch.setattr(lease_module, "sync_session_factory", factory)
    monkeypatch.setattr(
        lease_module, "leased_handlers", lambda: (HANDLER, CATALOG_HANDLER)
    )
    try:
        yield factory
    finally:
        scoped.dispose()
        with engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        engine.dispose()


def register(*, key=None, tenant=11, handler=HANDLER, celery_id=None, retry_attempt=0):
    return TaskRunLeaseRepository.register_active_run(
        celery_task_id=celery_id or uuid4().hex,
        effective_tenant_id=tenant,
        owner_tenant_id=None,
        task_definition_id=1,
        binding_id=None,
        task_code_snapshot="system.fixture.collect",
        task_name_snapshot="Synthetic monitor task",
        handler_path_snapshot=handler,
        trigger_source="scheduler",
        run_kind="tenant_binding",
        queue="scheduled",
        priority=None,
        run_key=key or uuid4().hex,
        trigger_slot="test-slot",
        trigger_id=None,
        retry_of_run_id=None,
        retry_of_task_id=None,
        args_summary={"kwargs": {"tenant_id": tenant}},
        trace_id="test-trace",
        retry_attempt=retry_attempt,
    )


def identity(registration, **overrides):
    values = {
        "run_id": registration.run_id,
        "lease_token": registration.lease_token,
        "celery_task_id": registration.celery_task_id,
        "effective_tenant_id": registration.effective_tenant_id,
        "handler_path": registration.handler_path,
    }
    values.update(overrides)
    return values


def alter(factory, registration, **values):
    with factory() as db, db.begin():
        db.execute(
            update(TaskRun).where(TaskRun.id == registration.run_id).values(**values)
        )


def expire(factory, registration):
    alter(
        factory,
        registration,
        lease_expires_at=text("clock_timestamp() - interval '1 second'"),
    )


def row(factory, registration):
    with factory() as db:
        return db.get(TaskRun, registration.run_id)


def settle(registration, **overrides):
    return TaskRunLeaseRepository.settle_success(
        **identity(registration, **overrides),
        result_summary={"imported_count": 2},
        summary="actual completion callback",
        duration_ms=1234,
    )


def race(*callbacks):
    gate = Barrier(len(callbacks))

    def execute(callback):
        gate.wait(timeout=10)
        return callback()

    with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:
        futures = [pool.submit(execute, callback) for callback in callbacks]
        return [future.result(timeout=20) for future in futures]


def test_expiry_excludes_live_legacy_deleted_and_other_handlers(lease_db):
    lost = register()
    live = register()
    legacy = register()
    deleted = register()
    catalog = register(handler=CATALOG_HANDLER)
    for registration in (lost, legacy, deleted, catalog):
        expire(lease_db, registration)
    alter(lease_db, legacy, lease_token=None)
    alter(lease_db, deleted, is_deleted=True)
    before = row(lease_db, lost)
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == [lost.run_id]
    after = row(lease_db, lost)
    assert after.status == "unknown"
    assert after.monitor_lost_at is not None
    assert after.finished_at is None and after.duration_ms is None
    assert after.run_key == before.run_key and after.lease_token == before.lease_token
    assert after.result_summary is None
    assert [row(lease_db, r).status for r in (live, legacy, deleted, catalog)] == [
        "running"
    ] * 4
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == []
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=CATALOG_HANDLER) == [
        catalog.run_id
    ]
    assert row(lease_db, catalog).status == "unknown"


def test_heartbeat_and_callbacks_require_exact_token_tenant_and_active_row(lease_db):
    active = register()
    original = row(lease_db, active)
    for overrides in (
        {"effective_tenant_id": 22},
        {"effective_tenant_id": None},
        {"lease_token": uuid4().hex},
        {"celery_task_id": uuid4().hex},
    ):
        assert not TaskRunLeaseRepository.heartbeat(**identity(active, **overrides))
        assert not settle(active, **overrides)
    assert row(lease_db, active).lease_expires_at == original.lease_expires_at
    assert TaskRunLeaseRepository.heartbeat(**identity(active))
    assert row(lease_db, active).lease_expires_at > original.lease_expires_at
    alter(lease_db, active, is_deleted=True)
    assert not TaskRunLeaseRepository.heartbeat(**identity(active))
    assert not settle(active)


def test_late_callback_resolves_unknown_without_reexecuting_business(lease_db):
    active = register()
    expire(lease_db, active)
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == [
        active.run_id
    ]
    assert settle(active)
    result = row(lease_db, active)
    assert result.status == "success"
    assert result.result_summary == {"imported_count": 2}
    assert result.finished_at is not None and result.duration_ms == 1234
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == []
    assert not TaskRunLeaseRepository.heartbeat(**identity(active))
    assert not settle(active)


def test_multiple_reapers_change_each_row_once(lease_db):
    registrations = [register(tenant=tenant) for tenant in (11, 22, 33)]
    for registration in registrations:
        expire(lease_db, registration)
    batches = race(
        *[
            lambda: TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER)
            for _ in range(3)
        ]
    )
    ids = [run_id for batch in batches for run_id in batch]
    assert sorted(ids) == sorted(r.run_id for r in registrations)
    assert all(row(lease_db, r).status == "unknown" for r in registrations)


def test_concurrent_heartbeat_wins_or_recovers_expiry(lease_db):
    active = register()
    expire(lease_db, active)
    results = race(
        lambda: TaskRunLeaseRepository.heartbeat(**identity(active)),
        lambda: TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER),
    )
    assert results[0] is True
    assert row(lease_db, active).status == "running"
    assert row(lease_db, active).finished_at is None
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == []


def test_callback_and_reaper_concurrency_preserves_completion(lease_db):
    active = register()
    expire(lease_db, active)
    results = race(
        lambda: settle(active),
        lambda: TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER),
    )
    assert results[0] is True
    result = row(lease_db, active)
    assert result.status == "success" and result.result_summary == {"imported_count": 2}
    assert result.finished_at is not None


def test_parallel_registration_keeps_run_key_unique(lease_db):
    key = uuid4().hex
    results = race(lambda: register(key=key), lambda: register(key=key))
    assert sum(result is not None for result in results) == 1
    with lease_db() as db:
        runs = list(db.scalars(select(TaskRun).where(TaskRun.run_key == key)))
    assert len(runs) == 1
    assert runs[0].status == "running" and runs[0].lease_token


def test_reaper_errors_propagate_instead_of_false_success(lease_db, monkeypatch):
    def unavailable():
        raise OperationalError(
            "probe", {}, RuntimeError("synthetic connection unavailable")
        )

    monkeypatch.setattr(lease_module, "sync_session_factory", unavailable)
    with pytest.raises(OperationalError, match="synthetic connection unavailable"):
        TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER)


def test_retry_rotates_once_and_stale_callback_never_owns_new_attempt(lease_db):
    key, celery_id = uuid4().hex, uuid4().hex
    original = register(key=key, celery_id=celery_id)
    assert register(key=key, celery_id=celery_id, retry_attempt=1) is None
    assert TaskRunLeaseRepository.settle_retrying(
        **identity(original),
        retry_count=0,
        error_message_public="retry",
        summary="retry",
    )
    resumed = race(
        lambda: register(key=key, celery_id=celery_id, retry_attempt=1),
        lambda: register(key=key, celery_id=celery_id, retry_attempt=1),
    )
    owners = [result for result in resumed if result is not None]
    assert len(owners) == 1
    current = owners[0]
    assert (
        current.run_id == original.run_id
        and current.lease_token != original.lease_token
    )
    assert not settle(original)
    assert not TaskRunLeaseRepository.heartbeat(**identity(original))
    assert row(lease_db, current).status == "running"
    assert settle(current)
    assert row(lease_db, current).status == "success"
    assert row(lease_db, current).retry_count == 1


def test_unknown_heartbeat_restores_same_owner_without_finishing(lease_db):
    active = register()
    expire(lease_db, active)
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == [
        active.run_id
    ]
    assert TaskRunLeaseRepository.heartbeat(**identity(active))
    restored = row(lease_db, active)
    assert restored.status == "running" and restored.finished_at is None
    assert restored.monitor_lost_at is not None  # keep observation history
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == []


def test_monitor_batch_boundary_does_not_silently_scan_everything(lease_db):
    active = register()
    expire(lease_db, active)
    for invalid in (0, -1, 1001, True):
        with pytest.raises(ValueError):
            TaskRunLeaseRepository.reap_lost_runs(
                handler_path=HANDLER, batch_limit=invalid
            )
    with pytest.raises(ValueError):
        TaskRunLeaseRepository.reap_lost_runs(handler_path="not.opted.in")
    assert row(lease_db, active).status == "running"


def test_failed_callback_settles_unknown_not_inferred_by_reaper(lease_db):
    active = register()
    expire(lease_db, active)
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == [
        active.run_id
    ]
    assert TaskRunLeaseRepository.settle_failure(
        **identity(active),
        error_message_public="actual failure",
        error_message_internal="trace",
        traceback_internal=None,
        summary="actual callback",
        duration_ms=2500,
    )
    final = row(lease_db, active)
    assert final.status == "failed" and final.finished_at is not None
    assert final.error_message_public == "actual failure" and final.duration_ms == 2500


def test_catalog_handler_heartbeat_success_failure_retry_and_reaping(lease_db):
    """Behavioral: catalog task transitions update only its registered TaskRun."""
    succeeded = register(handler=CATALOG_HANDLER)
    failed = register(handler=CATALOG_HANDLER)
    retrying = register(handler=CATALOG_HANDLER)
    lost_catalog = register(handler=CATALOG_HANDLER)
    live_order = register(handler=HANDLER)

    assert TaskRunLeaseRepository.heartbeat(**identity(succeeded))
    assert settle(succeeded)
    assert row(lease_db, succeeded).status == "success"

    assert TaskRunLeaseRepository.settle_failure(
        **identity(failed),
        error_message_public="catalog failed",
        error_message_internal="fictional failure",
        traceback_internal=None,
        summary="catalog callback failed",
        duration_ms=2500,
    )
    assert row(lease_db, failed).status == "failed"

    assert TaskRunLeaseRepository.settle_retrying(
        **identity(retrying),
        retry_count=1,
        error_message_public="retrying",
        summary="catalog retry",
    )
    assert row(lease_db, retrying).status == "retrying"

    expire(lease_db, lost_catalog)
    expire(lease_db, live_order)
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=CATALOG_HANDLER) == [
        lost_catalog.run_id
    ]
    assert row(lease_db, lost_catalog).status == "unknown"
    assert row(lease_db, live_order).status == "running"
    assert TaskRunLeaseRepository.reap_lost_runs(handler_path=HANDLER) == [
        live_order.run_id
    ]


def test_unapproved_handler_cannot_register_or_reap(lease_db):
    """Behavioral: monitor lease opt-in remains an explicit repository boundary."""
    with pytest.raises(ValueError, match="not opted in"):
        register(handler="not.opted.in")
    with pytest.raises(ValueError, match="not opted in"):
        TaskRunLeaseRepository.reap_lost_runs(handler_path="not.opted.in")
