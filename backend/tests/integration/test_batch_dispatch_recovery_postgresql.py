"""Test type: behavioral. PostgreSQL dispatch leases and catalog migration."""

# bug_id: BUG-2026-11-26-BATCH-DISPATCH-RECOVERY

import importlib.util
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, CreateTable, DropSchema

from app.core.base_model import utc_now
from app.models.ai.batch_run import BatchRun
from app.models.system.task_definition import TaskDefinition
from app.repositories.ai.batch_run_repository import BatchRunRepository
from app.tasks import agent_batch
from app.tasks.scheduler import _build_task_definition_schedule
from app.tasks.task_scheduling import is_handler_registered

_DSN = os.environ.get("AI_BATCH_RECOVERY_POSTGRES_DSN")
pytestmark = [
    pytest.mark.behavioral,
    pytest.mark.asyncio,
    pytest.mark.skipif(not _DSN, reason="Disposable loopback PostgreSQL required"),
]


@pytest.fixture
async def recovery_db():
    url = make_url(_DSN)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host in {"127.0.0.1", "localhost", "::1"}
    assert re.fullmatch(r"platform_ai|batch_recovery_[a-f0-9]{12}", url.database or "")

    host = create_async_engine(url, poolclass=NullPool)
    schema = "batch_dispatch_" + uuid4().hex
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {"search_path": schema, "statement_timeout": "10000"}
        },
    )
    try:
        async with host.begin() as conn:
            await conn.execute(CreateSchema(schema))
        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE tenants (id integer PRIMARY KEY)"))
            await conn.execute(sa.text("CREATE TABLE agents (id integer PRIMARY KEY)"))
            await conn.execute(sa.text("INSERT INTO tenants (id) VALUES (11), (12)"))
            await conn.execute(sa.text("INSERT INTO agents (id) VALUES (21)"))
            await conn.execute(CreateTable(BatchRun.__table__))
            await conn.execute(CreateTable(TaskDefinition.__table__))
            await conn.execute(
                sa.text(
                    "ALTER TABLE batch_runs DROP COLUMN dispatch_recovery_due_at, "
                    "DROP COLUMN dispatch_recovery_attempts"
                )
            )
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with host.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        await host.dispose()


async def _migrate(engine, action="upgrade"):
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/platform_ai_batch_v1.py"
    )
    spec = importlib.util.spec_from_file_location("batch_dispatch_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(bind):
        with Operations.context(MigrationContext.configure(bind)):
            getattr(migration, action)()

    async with engine.begin() as conn:
        await conn.run_sync(run)


async def _seed(session, *, due, status="pending", deleted=False, tenant_id=11):
    row = BatchRun(
        tenant_id=tenant_id,
        agent_id=21,
        status=status,
        is_deleted=deleted,
        total_items=1,
        celery_task_id=str(uuid4()),
        input_items=[{"item_id": "0", "input_variables": {"body": "local"}}],
        dispatch_recovery_due_at=due,
    )
    session.add(row)
    await session.flush()
    return row.id, tenant_id, row.celery_task_id


async def test_migration_and_db_beat_keep_legacy_rows_and_operator_settings(
    recovery_db,
):
    engine, sessions = recovery_db
    async with sessions.begin() as db:
        await db.execute(
            sa.text(
                "INSERT INTO batch_runs "
                "(tenant_id, agent_id, status, celery_task_id, created_at, updated_at, "
                "is_deleted, total_items, completed_items, failed_items, max_workers) "
                "VALUES (11,21,'pending','legacy',now(),now(),false,1,0,0,1)"
            )
        )
        snapshot = (
            await db.execute(
                sa.text("SELECT id, celery_task_id, status, xmin::text FROM batch_runs")
            )
        ).one()

    await _migrate(engine)
    async with sessions.begin() as db:
        row = (
            await db.execute(
                sa.text(
                    "SELECT id, celery_task_id, status, xmin::text, "
                    "dispatch_recovery_due_at, dispatch_recovery_attempts FROM batch_runs"
                )
            )
        ).one()
        assert row == (*snapshot, None, 0)
        definition = (await db.scalars(sa.select(TaskDefinition))).one()
        assert (
            definition.code,
            definition.handler_path,
            definition.scope,
            definition.is_enabled,
            definition.owner_tenant_id,
        ) == (
            "ai.batch_dispatch.recovery",
            "app.tasks.agent_batch.recover_batch_dispatches",
            "admin_only",
            True,
            None,
        )
        assert is_handler_registered(definition.handler_path)
        schedule = _build_task_definition_schedule([definition])
        assert schedule == {
            f"task_definition:{definition.id}:{definition.code}": {
                "task": "app.tasks.task_scheduling.run_task_definition",
                "args": (definition.id,),
                "kwargs": {},
                "options": {"queue": "scheduled"},
                "schedule": 30.0,
            },
        }
        definition.is_enabled = False
        definition.default_interval_seconds = 90
        due_id, _, _ = await _seed(db, due=utc_now())

    await _migrate(engine)
    async with sessions() as db:
        definition = (await db.scalars(sa.select(TaskDefinition))).one()
        assert not definition.is_enabled and definition.default_interval_seconds == 90
    with pytest.raises(RuntimeError, match="batch_dispatch_recovery_has_pending_work"):
        await _migrate(engine, "downgrade")
    async with sessions.begin() as db:
        await db.execute(
            sa.update(BatchRun).where(BatchRun.id == due_id).values(status="cancelled")
        )
    await _migrate(engine, "downgrade")
    async with sessions() as db:
        assert (
            await db.execute(sa.text("SELECT count(*) FROM task_definitions"))
        ).scalar_one() == 0
        assert (
            await db.execute(
                sa.text(
                    "SELECT id, celery_task_id, status, xmin::text FROM batch_runs "
                    "WHERE id = :id"
                ),
                {"id": snapshot.id},
            )
        ).one() == snapshot


async def test_skip_locked_scan_only_sends_eligible_rows_once(recovery_db, monkeypatch):
    engine, sessions = recovery_db
    await _migrate(engine)
    now = utc_now()
    due = now - timedelta(seconds=5)
    protected = [(None, "pending", False), (now + timedelta(hours=1), "pending", False)]
    protected += [
        (due, state, False)
        for state in ("running", "completed", "cancelled", "failed", "partial_failed")
    ]
    protected.append((due, "pending", True))
    async with sessions.begin() as db:
        first = await _seed(db, due=due)
        second = await _seed(db, due=due, tenant_id=12)
        for when, state, deleted in protected:
            await _seed(db, due=when, status=state, deleted=deleted)
    writes = []
    current = None

    @asynccontextmanager
    async def session():
        nonlocal current
        async with sessions() as db:
            current = db
            yield db

    def publish(*identity):
        assert not current.in_transaction()
        writes.append(identity)

    monkeypatch.setattr(agent_batch, "_task_async_session", session)
    monkeypatch.setattr(agent_batch, "publish_batch_run", publish)

    async with sessions.begin() as lock:
        await lock.execute(
            sa.select(BatchRun.id).where(BatchRun.id == first[0]).with_for_update()
        )
        assert await agent_batch._recover_batch_dispatches() == {
            "claimed": 1,
            "confirmed": 1,
            "unconfirmed": 0,
        }
    assert writes == [second]
    assert await agent_batch._recover_batch_dispatches() == {
        "claimed": 1,
        "confirmed": 1,
        "unconfirmed": 0,
    }
    assert await agent_batch._recover_batch_dispatches() == {
        "claimed": 0,
        "confirmed": 0,
        "unconfirmed": 0,
    }
    assert writes == [second, first]
    async with sessions() as db:
        rows = (
            await db.execute(
                sa.select(
                    BatchRun.status,
                    BatchRun.is_deleted,
                    BatchRun.dispatch_recovery_due_at,
                    BatchRun.dispatch_recovery_attempts,
                ).order_by(BatchRun.id)
            )
        ).all()
    assert rows[:2] == [("pending", False, None, 1)] * 2
    assert rows[2:] == [(state, deleted, when, 0) for when, state, deleted in protected]


class _ScannerProcessExit(BaseException):
    """Stop the scanner after its real lease commit, before reconciliation."""


async def test_expired_scanner_lease_reuses_id_and_rejects_stale_confirmation(
    recovery_db, monkeypatch
):
    engine, sessions = recovery_db
    await _migrate(engine)
    original_due = utc_now() - timedelta(seconds=5)
    async with sessions.begin() as db:
        identity = await _seed(db, due=original_due)

    monkeypatch.setattr(agent_batch, "_task_async_session", sessions)
    writes = []

    def publish(*args):
        writes.append(args)
        if len(writes) == 1:
            raise _ScannerProcessExit
        if len(writes) == 2:
            raise ConnectionError("fixture broker reply lost")

    async def read():
        async with sessions() as db:
            return (
                await db.execute(
                    sa.select(
                        BatchRun.celery_task_id,
                        BatchRun.status,
                        BatchRun.dispatch_recovery_attempts,
                        BatchRun.dispatch_recovery_due_at,
                    ).where(BatchRun.id == identity[0])
                )
            ).one()

    async def expire():
        async with sessions.begin() as db:
            await db.execute(
                sa.update(BatchRun)
                .where(BatchRun.id == identity[0])
                .values(dispatch_recovery_due_at=original_due)
            )

    monkeypatch.setattr(agent_batch, "publish_batch_run", publish)
    with pytest.raises(_ScannerProcessExit):
        await agent_batch._recover_batch_dispatches()
    first = await read()
    assert first[:3] == (identity[2], "pending", 1)
    assert first[3] > utc_now()
    assert (await agent_batch._recover_batch_dispatches())["claimed"] == 0

    await expire()
    assert await agent_batch._recover_batch_dispatches() == {
        "claimed": 1,
        "confirmed": 0,
        "unconfirmed": 1,
    }
    second = await read()
    assert second[:3] == (identity[2], "pending", 2)
    assert second[3] - first[3] >= timedelta(minutes=2)
    async with sessions.begin() as db:
        await BatchRunRepository(db, identity[1]).confirm_dispatch(
            identity[0], identity[2], recovery_attempt=1
        )
    assert await read() == second

    await expire()
    assert await agent_batch._recover_batch_dispatches() == {
        "claimed": 1,
        "confirmed": 1,
        "unconfirmed": 0,
    }
    assert await read() == (identity[2], "pending", 3, None)
    assert writes == [identity] * 3
