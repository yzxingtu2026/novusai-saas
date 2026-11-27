"""Test type: structural. Batch startup must read the persisted state under a row lock."""

# bug_id: BUG-2026-11-25-BATCH-CANCEL-DURING-REDIS-INIT

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.redis import RedisManager
from app.tasks.agent_batch import execute_batch_run


@pytest.mark.parametrize(
    ("initial", "after_ping", "ping_fails"),
    [
        pytest.param("pending", "cancelled", False, id="cancel-during-ping"),
        pytest.param("pending", "cancelled", True, id="cancel-during-failed-ping"),
        pytest.param("pending", "running", False, id="duplicate-claimed-during-ping"),
        pytest.param("completed", None, False, id="duplicate-after-completion"),
    ],
)
def test_batch_does_not_replace_another_committed_state(
    monkeypatch, initial, after_ping, ping_fails
):
    run = SimpleNamespace(
        id=11,
        agent_id=7,
        created_by=21,
        status=initial,
        is_deleted=False,
        started_at=None,
        completed_at=None,
    )
    stored = {"status": initial}
    locked_reads = []
    writes = []
    redis = []
    model_preparation = []
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: run)

    @asynccontextmanager
    async def session():
        yield db

    async def init(_cls):
        redis.append("init")
        if after_ping:
            stored["status"] = after_ping
        if ping_fails:
            raise ConnectionError("fixture Redis ping")

    async def close(_cls):
        redis.append("close")

    async def refresh(row, fields, *, with_for_update=False):
        assert (row, fields, with_for_update) == (run, ["status", "is_deleted"], True)
        row.status = stored["status"]
        locked_reads.append(row.status)

    async def commit():
        writes.append(run.status)
        stored["status"] = run.status

    async def prepare(**_):
        model_preparation.append("unexpected")
        raise RuntimeError("fixture detected model preparation")

    db.refresh.side_effect = refresh
    db.commit.side_effect = commit
    monkeypatch.setattr(RedisManager, "_pool", None)
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
        lambda *_: SimpleNamespace(build_usage_attribution_context=prepare),
    )

    result = execute_batch_run.run(batch_run_id=11, tenant_id=3)

    observed = after_ping or initial
    assert not writes, f"worker replaced a committed {observed} row: {writes}"
    assert result == {"status": observed}
    assert stored["status"] == observed and run.started_at is None
    assert writes == model_preparation == []
    assert locked_reads == ([observed] if after_ping else [])
    assert redis == (["init", "close"] if after_ping else [])
