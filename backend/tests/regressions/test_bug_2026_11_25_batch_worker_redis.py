"""Structural regression for the Redis lifetime of the standalone batch task."""

# bug_id: BUG-2026-11-25-BATCH-WORKER-REDIS

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.redis import RedisManager
from app.tasks.agent_batch import execute_batch_run


@pytest.mark.parametrize(
    "scenario", ("cold", "borrowed", "initialization-fails", "cancelled")
)
def test_batch_worker_redis_stays_within_the_task_event_loop(monkeypatch, scenario):
    """Use the actual task boundary and stop before building any model request."""
    trace = []
    pool, client = object(), object()
    borrowed = scenario == "borrowed"
    cancelled = scenario == "cancelled"
    run = SimpleNamespace(
        id=11,
        tenant_id=3,
        agent_id=7,
        status="cancelled" if cancelled else "pending",
        is_deleted=False,
        created_by=21,
        started_at=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: run)

    @asynccontextmanager
    async def session():
        yield db

    async def init(cls):
        trace.append(("init", asyncio.get_running_loop()))
        cls._pool, cls._client = pool, client
        if scenario == "initialization-fails":
            raise RuntimeError("Fixture Redis ping failed")

    async def close(cls):
        trace.append(("close", asyncio.get_running_loop()))
        assert cls.get_client() is client
        cls._pool = cls._client = None

    async def attribution(**_):
        trace.append(("prepare", asyncio.get_running_loop()))
        assert RedisManager.get_client() is client
        raise RuntimeError("Fixture reached model preparation")

    monkeypatch.setattr(RedisManager, "_pool", pool if borrowed else None)
    monkeypatch.setattr(RedisManager, "_client", client if borrowed else None)
    monkeypatch.setattr(RedisManager, "init", classmethod(init))
    monkeypatch.setattr(RedisManager, "close", classmethod(close))
    monkeypatch.setattr("app.tasks.agent_batch._task_async_session", session)
    monkeypatch.setattr(
        "app.repositories.ai.agent_repository.AgentRepository",
        lambda *_: SimpleNamespace(
            get_by_id=AsyncMock(return_value=SimpleNamespace(id=7))
        ),
    )
    monkeypatch.setattr(
        "app.services.ai.agent_service.AgentService",
        lambda *_: SimpleNamespace(build_usage_attribution_context=attribution),
    )

    result = execute_batch_run.run(batch_run_id=11, tenant_id=3)
    expected = {
        "cold": (("init", "prepare", "close"), "Fixture reached model preparation"),
        "borrowed": (("prepare",), "Fixture reached model preparation"),
        "initialization-fails": (("init", "close"), "Fixture Redis ping failed"),
        "cancelled": ((), None),
    }
    stages, error = expected[scenario]
    assert tuple(label for label, _ in trace) == stages
    if trace:
        loop = trace[0][1]
        assert loop.is_closed() and all(other is loop for _, other in trace)
    if cancelled:
        assert result == {"status": "cancelled"} and run.completed_at is None
        db.commit.assert_not_awaited()
    else:
        assert result == {"error": error}
        assert run.status == "failed" and run.completed_at is not None
        assert db.commit.await_count == (1 if scenario == "initialization-fails" else 2)
    assert RedisManager._pool is (pool if borrowed else None)
    assert RedisManager._client is (client if borrowed else None)
