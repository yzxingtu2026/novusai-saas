"""Test type: behavioral. Real PostgreSQL lease, cancellation and 0115 DDL."""

# bug_id: BUG-2026-11-26-BATCH-EXECUTION-LOSS

import asyncio
import importlib.util
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import raiseload
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, CreateTable, DropSchema

from app.celery_app import celery_app
from app.core.data_permission import data_permission_ctx
from app.exceptions import BusinessException, NotFoundException
from app.models.ai.batch_run import BatchRun
from app.repositories.ai import batch_run_repository as repo
from app.services.ai.batch_run_service import BatchRunService

_DSN = os.environ.get("AI_BATCH_EXECUTION_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.behavioral,
    pytest.mark.asyncio,
    pytest.mark.skipif(not _DSN, reason="Disposable loopback PostgreSQL required"),
]


@pytest.fixture
async def lease_db():
    url = make_url(_DSN)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host in {"127.0.0.1", "localhost", "::1"}
    assert re.fullmatch(r"platform_ai|batch_execution_[a-f0-9]{12}", url.database or "")
    schema = "execution_lease_" + uuid4().hex
    host = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "5000",
                "timezone": "Asia/Tokyo",
            }
        },
    )
    reader = sa.create_engine(
        url.set(drivername="postgresql+psycopg2"),
        poolclass=NullPool,
        connect_args={"options": f"-c search_path={schema} -c statement_timeout=3000"},
    )
    try:
        async with host.begin() as conn:
            await conn.execute(CreateSchema(schema))
        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE tenants (id integer PRIMARY KEY)"))
            await conn.execute(sa.text("CREATE TABLE agents (id integer PRIMARY KEY)"))
            await conn.execute(sa.text("INSERT INTO tenants (id) VALUES (11), (12)"))
            await conn.execute(sa.text("INSERT INTO agents (id) VALUES (21), (22)"))
            await conn.execute(CreateTable(BatchRun.__table__))
        yield (
            engine,
            async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
            reader,
        )
    finally:
        reader.dispose()
        await engine.dispose()
        async with host.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        await host.dispose()


def _disable_unneeded_relationship_reads(db):
    # 中文: 夹具只需真实批次行与外键，不加载无关智能体关系。
    # EN: This fixture uses real batch rows and foreign keys without loading unrelated agent relations.
    @sa.event.listens_for(db.sync_session, "do_orm_execute")
    def narrow_to_batch(statement):
        if statement.is_select:
            statement.statement = statement.statement.options(raiseload("*"))


async def _seed(db, *, status="running", **fields):
    seeded = BatchRun(
        **{
            "tenant_id": 11,
            "agent_id": 21,
            "created_by": 71,
            "status": status,
            "started_at": await db.scalar(
                sa.select(sa.func.timezone("UTC", sa.func.statement_timestamp()))
            ),
            "celery_task_id": str(uuid4()),
            "total_items": 4,
            "completed_items": 1,
            "failed_items": 1,
            "results": [{"item_id": "0"}],
            "errors": [{"item_id": "1", "error": "fixture input"}],
            **fields,
        }
    )
    db.add(seeded)
    await db.flush()
    return seeded


async def _snapshot(sessions):
    async with sessions() as reader:
        rows = await reader.execute(
            sa.text("SELECT id, to_jsonb(batch_runs), xmin::text FROM batch_runs")
        )
        return {identifier: (data, xmin) for identifier, data, xmin in rows}


async def _migrate(engine, action):
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/platform_ai_batch_exec_v1.py"
    )
    spec = importlib.util.spec_from_file_location("batch_execution_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(bind):
        with Operations.context(MigrationContext.configure(bind)):
            getattr(migration, action)()

    async with engine.begin() as conn:
        await conn.run_sync(run)


async def test_0115_preserves_legacy_and_downgrade_keeps_unknown(lease_db):
    engine, sessions, _ = lease_db
    async with sessions.begin() as db:
        row_id = (await _seed(db)).id
        await db.execute(
            sa.text(
                "ALTER TABLE batch_runs DROP COLUMN IF EXISTS execution_lease_expires_at"
            )
        )
    before = await _snapshot(sessions)

    await _migrate(engine, "upgrade")
    await _migrate(engine, "upgrade")
    after = await _snapshot(sessions)
    assert after[row_id] == (
        {**before[row_id][0], "execution_lease_expires_at": None},
        before[row_id][1],
    )
    async with sessions.begin() as db:
        definition = await db.scalar(
            sa.text(
                "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                "WHERE indexrelid = to_regclass('ix_batch_runs_execution_lease_due')"
            )
        )
        assert "(execution_lease_expires_at, id)" in definition
        assert "is_deleted IS FALSE" in definition and "'running'" in definition
        assert "execution_lease_expires_at IS NOT NULL" in definition
        await db.execute(
            sa.update(BatchRun)
            .where(BatchRun.id == row_id)
            .values(
                execution_lease_expires_at=repo.execution_lease_deadline(
                    timedelta(minutes=-1)
                )
            )
        )
    running = await _snapshot(sessions)
    assert running[row_id][0]["status"] == "running"
    with pytest.raises(RuntimeError, match="batch_execution_lease_has_running_runs"):
        await _migrate(engine, "downgrade")
    assert await _snapshot(sessions) == running

    async with sessions.begin() as db:
        await db.execute(
            sa.text("UPDATE batch_runs SET status = 'unknown' WHERE id = :id"),
            {"id": row_id},
        )
        deleted_id = (
            await _seed(
                db,
                is_deleted=True,
                execution_lease_expires_at=repo.execution_lease_deadline(
                    timedelta(minutes=1)
                ),
            )
        ).id
    before_downgrade = await _snapshot(sessions)
    assert before_downgrade[row_id][0]["status"] == "unknown"
    assert before_downgrade[row_id][0]["execution_lease_expires_at"] is not None
    deleted_run = before_downgrade[deleted_id][0]
    assert deleted_run["status"] == "running"
    assert deleted_run["is_deleted"] is True
    assert deleted_run["execution_lease_expires_at"] is not None

    await _migrate(engine, "downgrade")
    await _migrate(engine, "downgrade")
    assert await _snapshot(sessions) == {
        run_id: (
            {
                key: value
                for key, value in data.items()
                if key != "execution_lease_expires_at"
            },
            xmin,
        )
        for run_id, (data, xmin) in before_downgrade.items()
    }


async def test_expiry_skips_locked_rows_and_preserves_business_data(lease_db):
    _, sessions, _ = lease_db
    async with sessions.begin() as seed:
        now = await seed.scalar(
            sa.select(sa.func.timezone("UTC", sa.func.statement_timestamp()))
        )
        expired = now - timedelta(minutes=5)
        ids = [
            (await _seed(seed, **values)).id
            for values in (
                {"execution_lease_expires_at": expired},
                {"execution_lease_expires_at": expired, "tenant_id": 12},
                {"execution_lease_expires_at": expired + timedelta(seconds=1)},
                {"execution_lease_expires_at": now + timedelta(hours=1)},
                {"execution_lease_expires_at": None},
                {"execution_lease_expires_at": expired, "is_deleted": True},
                {"execution_lease_expires_at": expired, "status": "pending"},
                {"execution_lease_expires_at": expired, "status": "cancelled"},
                {"execution_lease_expires_at": expired, "status": "unknown"},
            )
        ]
    initial = await _snapshot(sessions)
    async with sessions.begin() as locked:
        await locked.execute(
            sa.select(BatchRun.id).where(BatchRun.id == ids[0]).with_for_update()
        )
        async with sessions.begin() as sweep:
            assert await repo.mark_expired_batch_executions_unknown(sweep, limit=1) == 1
        interim = await _snapshot(sessions)
        assert set(interim) == set(initial)
        assert {row_id for row_id in ids if interim[row_id] != initial[row_id]} == {
            ids[1]
        }
        assert interim[ids[1]][0] == {**initial[ids[1]][0], "status": "unknown"}
    async with sessions.begin() as sweep:
        assert await repo.mark_expired_batch_executions_unknown(sweep, limit=1) == 1
    middle = await _snapshot(sessions)
    assert {row_id for row_id in ids if middle[row_id] != interim[row_id]} == {ids[0]}
    async with sessions.begin() as sweep:
        assert await repo.mark_expired_batch_executions_unknown(sweep, limit=5) == 1
        assert await repo.mark_expired_batch_executions_unknown(sweep, limit=5) == 0
    final = await _snapshot(sessions)
    for row_id in ids[:3]:
        assert final[row_id][0] == {**initial[row_id][0], "status": "unknown"}
        assert final[row_id][1] != initial[row_id][1]
    assert {key: final[key] for key in ids[3:]} == {
        key: initial[key] for key in ids[3:]
    }


async def test_renewal_uses_database_utc_and_only_the_exact_live_claim(lease_db):
    _, sessions, _ = lease_db
    async with sessions.begin() as seed:
        row = await _seed(seed)
        row.execution_lease_expires_at = repo.execution_lease_deadline(
            timedelta(minutes=-2)
        )
        await seed.flush()
        batch_id, started_at = row.id, row.started_at
        others = [
            (await _seed(seed, status=status, **extras)).id
            for status, extras in (
                ("unknown", {"execution_lease_expires_at": started_at}),
                ("running", {}),
                (
                    "running",
                    {"is_deleted": True, "execution_lease_expires_at": started_at},
                ),
                ("cancelled", {"execution_lease_expires_at": started_at}),
            )
        ]
    before = await _snapshot(sessions)
    async with sessions.begin() as db:
        utc = await db.scalar(
            sa.select(sa.func.timezone("UTC", sa.func.statement_timestamp()))
        )
        assert await db.scalar(sa.text("SHOW timezone")) == "Asia/Tokyo"
        assert before[batch_id][0]["execution_lease_expires_at"] < utc.isoformat()
        for target, tenant, started in [
            (batch_id, 12, started_at),
            (batch_id, 11, started_at + timedelta(microseconds=1)),
            *(
                (other, 11, datetime.fromisoformat(before[other][0]["started_at"]))
                for other in others
            ),
        ]:
            assert (
                await repo.renew_batch_execution_lease(
                    db,
                    batch_run_id=target,
                    tenant_id=tenant,
                    started_at=started,
                    duration=timedelta(minutes=2),
                )
                is False
            )
        assert (
            await repo.renew_batch_execution_lease(
                db,
                batch_run_id=batch_id,
                tenant_id=11,
                started_at=started_at,
                duration=timedelta(minutes=2),
            )
            is True
        )
    after = await _snapshot(sessions)
    assert (utc + timedelta(minutes=2)).isoformat() <= after[batch_id][0][
        "execution_lease_expires_at"
    ]
    assert after[batch_id][0] == {
        **before[batch_id][0],
        "execution_lease_expires_at": after[batch_id][0]["execution_lease_expires_at"],
    }
    assert all(after[key] == before[key] for key in others)
    async with sessions.begin() as sweep:
        assert await repo.mark_expired_batch_executions_unknown(sweep, limit=10) == 0
    assert await _snapshot(sessions) == after


async def test_cancel_commits_before_broker_revoke_and_keeps_access_scope(
    lease_db, monkeypatch
):
    _, sessions, reader = lease_db
    async with sessions.begin() as seed:
        row = await _seed(seed, status="pending")
        run_id, task_id = row.id, row.celery_task_id
        foreign = (await _seed(seed, tenant_id=12)).id
        other_creator = (await _seed(seed, created_by=72)).id
        deleted = (await _seed(seed, is_deleted=True)).id
    observed = []

    def revoke(identifier, *, terminate):
        with reader.connect() as connection:
            status = connection.scalar(
                sa.select(BatchRun.__table__.c.status).where(
                    BatchRun.__table__.c.id == run_id
                )
            )
        observed.append((identifier, terminate, status))

    monkeypatch.setattr(celery_app.control, "revoke", revoke)
    token = data_permission_ctx.set({"scope_mode": "self", "current_user_id": 71})
    try:
        async with sessions() as db:
            _disable_unneeded_relationship_reads(db)
            service = BatchRunService(db, 11)
            for agent, candidate in (
                (22, run_id),
                (21, foreign),
                (21, other_creator),
                (21, deleted),
            ):
                with pytest.raises(NotFoundException):
                    await service.cancel_batch_run(agent, candidate)
            assert observed == []
            cancelled = await service.cancel_batch_run(21, run_id)
            assert cancelled.id == run_id and cancelled.status == "cancelled"
            # 中文: 父版由路由提交。候选服务先提交再通知。
            # EN: The old route commits here; the candidate service commits before notifying.
            await db.commit()
    finally:
        data_permission_ctx.reset(token)
    assert observed == [(task_id, False, "cancelled")]
    final = await _snapshot(sessions)
    assert [
        final[key][0]["status"] for key in (run_id, foreign, other_creator, deleted)
    ] == [
        "cancelled",
        "running",
        "running",
        "running",
    ]


async def test_cancel_cannot_overwrite_concurrent_unknown(lease_db, monkeypatch):
    _, sessions, _ = lease_db
    async with sessions.begin() as seed:
        run_id = (await _seed(seed)).id
    revoked = []
    monkeypatch.setattr(
        celery_app.control, "revoke", lambda task_id, **_: revoked.append(task_id)
    )
    async with sessions() as sweep, sessions() as cancel, sessions() as watcher:
        _disable_unneeded_relationship_reads(cancel)
        blocker = await sweep.scalar(sa.text("SELECT pg_backend_pid()"))
        blocked = await cancel.scalar(sa.text("SELECT pg_backend_pid()"))
        await sweep.execute(
            sa.update(BatchRun).where(BatchRun.id == run_id).values(status="unknown")
        )

        async def call_cancel():
            row = await BatchRunService(cancel, 11).cancel_batch_run(21, run_id)
            await cancel.commit()
            return row.status

        request = asyncio.create_task(call_cancel())
        try:
            async with asyncio.timeout(3):
                while blocker not in await watcher.scalar(
                    sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked}
                ):
                    await asyncio.sleep(0.01)
            await sweep.commit()
            with pytest.raises(BusinessException):
                await request
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    assert revoked == []
    assert (await _snapshot(sessions))[run_id][0]["status"] == "unknown"
