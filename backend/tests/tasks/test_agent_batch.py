"""Test type: structural. Exercise batch Worker admission and task boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.enums.agent import AgentExecutionModeEnum, BatchRunStatusEnum
from app.tasks import agent_batch


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.parametrize("heartbeat_fails", [False, True])
def test_execute_batch_run_does_not_eager_resolve_agent_inventory(
    monkeypatch, heartbeat_fails
) -> None:
    import app.ai.engine.task as task_engine_module
    import app.ai.gateway as gateway_module
    import app.ai.skills.resolver as resolver_module
    import app.ai.tools.sandbox as sandbox_module
    import app.configs.service as config_service_module
    import app.repositories.ai.agent_repository as agent_repository_module
    import app.services.ai.agent_service as agent_service_module
    from app.core.redis import RedisManager

    redis_events = []

    async def init_redis(_cls):
        redis_events.append("open")

    async def close_redis(_cls):
        redis_events.append("closed")

    monkeypatch.setattr(RedisManager, "_pool", None)
    monkeypatch.setattr(RedisManager, "init", classmethod(init_redis))
    monkeypatch.setattr(RedisManager, "close", classmethod(close_redis))

    batch_run = SimpleNamespace(
        id=11,
        agent_id=77,
        status="pending",
        is_deleted=False,
        started_at=None,
        completed_at=None,
        completed_items=0,
        failed_items=0,
        results=None,
        errors=None,
        created_by=None,
        input_items=[
            {
                "item_id": "row-1",
                "input_variables": {"customer_name": "Acme"},
            }
        ],
    )
    agent = SimpleNamespace(id=77)
    db = AsyncMock()
    db.execute.side_effect = [
        _ScalarResult(batch_run),
        SimpleNamespace(
            one_or_none=lambda: SimpleNamespace(
                status="running", completed_at=None, lease_active=True
            ),
        ),
        SimpleNamespace(
            one_or_none=lambda: SimpleNamespace(status="running", completed_at=None),
        ),
    ]
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    monkeypatch.setattr(
        agent_batch,
        "_task_async_session",
        lambda: _AsyncSessionContext(db),
    )

    class _FakeAgentRepository:
        def __init__(self, db, tenant_id):
            self.db = db
            self.tenant_id = tenant_id

        async def get_by_id(self, agent_id):
            _ = agent_id
            return agent

    class _FakeAgentService:
        def __init__(self, db, tenant_id):
            self.db = db
            self.tenant_id = tenant_id

        async def build_usage_attribution_context(
            self,
            *,
            agent,
            user_id,
            user_role,
            user_role_id,
        ):
            _ = (agent, user_id, user_role, user_role_id)
            return {"source": "batch"}

    class _FakeConfigService:
        def __init__(self, db):
            self.db = db

        async def get_platform_config(self, key, default=None):
            _ = key
            return default

    class _FakeGateway:
        def __init__(self, db):
            self.db = db

    class _FakeSandbox:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    execute_calls: list[dict[str, object]] = []
    heartbeat_events = []

    async def heartbeat(**claim):
        heartbeat_events.append(("open", claim))
        try:
            if heartbeat_fails:
                raise RuntimeError("fixture heartbeat stopped")
            await asyncio.Event().wait()
        finally:
            heartbeat_events.append(("closed", claim))

    monkeypatch.setattr(
        agent_batch, "_heartbeat_batch_execution", heartbeat, raising=False
    )

    class _FakeTaskEngine:
        def __init__(self, *, db, gateway, sandbox):
            self.db = db
            self.gateway = gateway
            self.sandbox = sandbox

        async def execute(self, agent_arg, request_arg, skill_result=None):
            await asyncio.sleep(0)
            execute_calls.append(
                {
                    "agent": agent_arg,
                    "request": request_arg,
                    "skill_result": skill_result,
                }
            )
            return SimpleNamespace(
                success=True,
                output="done",
                total_tokens=5,
            )

    resolve_mock = AsyncMock(
        side_effect=AssertionError(
            "agent_batch should not eagerly resolve the full agent inventory"
        )
    )

    monkeypatch.setattr(
        agent_repository_module,
        "AgentRepository",
        _FakeAgentRepository,
    )
    monkeypatch.setattr(
        agent_service_module,
        "AgentService",
        _FakeAgentService,
    )
    monkeypatch.setattr(
        config_service_module,
        "ConfigService",
        _FakeConfigService,
    )
    monkeypatch.setattr(gateway_module, "AIGateway", _FakeGateway)
    monkeypatch.setattr(sandbox_module, "ToolSandbox", _FakeSandbox)
    monkeypatch.setattr(task_engine_module, "TaskEngine", _FakeTaskEngine)
    monkeypatch.setattr(resolver_module, "resolve_for_agent", resolve_mock)

    result = agent_batch.execute_batch_run.run(batch_run_id=11, tenant_id=9)

    assert result == {
        "batch_run_id": 11,
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "status": BatchRunStatusEnum.COMPLETED.value,
    }
    assert batch_run.status == BatchRunStatusEnum.COMPLETED.value
    assert batch_run.results == [
        {
            "item_id": "row-1",
            "output": "done",
            "total_tokens": 5,
        }
    ]
    assert batch_run.errors is None
    assert len(execute_calls) == 1
    assert execute_calls[0]["agent"] is agent
    assert execute_calls[0]["skill_result"] is None

    request = execute_calls[0]["request"]
    assert request.tenant_id == 9
    assert request.execution_mode == AgentExecutionModeEnum.TASK.value
    assert request.billing_context == {"source": "batch"}
    assert request.skip_quota is True
    assert request.skip_persistence is True
    resolve_mock.assert_not_awaited()
    claim = {
        "batch_run_id": 11,
        "tenant_id": 9,
        "started_at": batch_run.started_at,
    }
    assert heartbeat_events == [("open", claim), ("closed", claim)]

    for status in ("running", "unknown", "completed", "cancelled"):
        batch_run.status = status
        db.execute.side_effect = [_ScalarResult(batch_run)]
        assert agent_batch.execute_batch_run.run(batch_run_id=11, tenant_id=9) == {
            "status": status,
        }
    assert len(execute_calls) == 1
    assert heartbeat_events == [("open", claim), ("closed", claim)]
    assert redis_events == ["open", "closed"]


def test_batch_execution_heartbeat_commits_each_renewal_and_stops_at_owner_loss(
    monkeypatch,
):
    import app.repositories.ai.batch_run_repository as repo

    events = []
    started_at = datetime(2026, 11, 26, 10, 20)
    renewals = iter((True, False))

    @asynccontextmanager
    async def begin():
        events.append("begin")
        yield
        events.append("commit")

    db = SimpleNamespace(begin=begin)

    @asynccontextmanager
    async def session():
        events.append("open")
        yield db
        events.append("closed")

    async def renew(actual_db, *, batch_run_id, tenant_id, started_at, duration):
        assert actual_db is db
        events.append(("renew", batch_run_id, tenant_id, started_at, duration))
        return next(renewals)

    monkeypatch.setattr(agent_batch, "_task_async_session", session)
    monkeypatch.setattr(agent_batch, "EXECUTION_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(repo, "renew_batch_execution_lease", renew)
    asyncio.run(
        agent_batch._heartbeat_batch_execution(
            batch_run_id=11, tenant_id=9, started_at=started_at
        )
    )

    attempt = [
        "open",
        "begin",
        ("renew", 11, 9, started_at, agent_batch.EXECUTION_LEASE_DURATION),
        "commit",
        "closed",
    ]
    assert events == attempt + attempt


def test_batch_execution_expiry_scan_retains_dispatch_counts(monkeypatch):
    import app.repositories.ai.batch_run_repository as repo

    events = []

    @asynccontextmanager
    async def begin():
        events.append("begin")
        yield
        events.append("commit")

    db = SimpleNamespace(begin=begin)

    async def no_pending(actual_db, _now):
        assert actual_db is db
        events.append("pending-empty")

    async def expire(actual_db, *, limit):
        assert actual_db is db
        events.append(("expired", limit))
        return 2

    monkeypatch.setattr(
        agent_batch, "_task_async_session", lambda: _AsyncSessionContext(db)
    )
    monkeypatch.setattr(repo, "lock_next_batch_dispatch", no_pending)
    monkeypatch.setattr(repo, "mark_expired_batch_executions_unknown", expire)
    assert asyncio.run(agent_batch._recover_batch_dispatches()) == {
        "claimed": 0,
        "confirmed": 0,
        "unconfirmed": 0,
    }
    assert events == [
        "begin",
        "pending-empty",
        "commit",
        "begin",
        ("expired", agent_batch.DISPATCH_RECOVERY_BATCH_SIZE),
        "commit",
    ]
