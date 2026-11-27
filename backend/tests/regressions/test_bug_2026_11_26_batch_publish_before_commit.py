"""Test type: structural. Batch submission and broker outcomes use a committed run."""

# bug_id: BUG-2026-11-26-BATCH-PUBLISH-BEFORE-COMMIT

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.ai.engine import dispatcher
from app.ai.engine.types import BatchItem, ExecutionRequest
from app.core.base_model import utc_now
from app.exceptions import ServiceUnavailableException
from app.tasks.agent_batch import execute_batch_run


class _PublisherProcessExit(BaseException):
    """Stop before the broker call, without entering the live Exception handler."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery",
    (
        "ok",
        "rejected",
        "lost-before-claim",
        "lost-after-claim",
        "commit-error",
        # bug_id: BUG-2026-11-26-BATCH-DISPATCH-RECOVERY
        "process-exit-before-publish",
    ),
)
async def test_batch_publishes_one_committed_task_identity(monkeypatch, delivery):
    db = AsyncMock()
    batch = SimpleNamespace(id=17, completed_at=None)
    durable = None
    publications = []
    lock_reads = []
    now = utc_now()

    async def create(data):
        for name, value in data.items():
            setattr(batch, name, value)
        return batch

    async def update(run_id, data):
        assert run_id == batch.id
        for name, value in data.items():
            setattr(batch, name, value)

    async def confirm_dispatch(batch_id, task_id):
        assert (batch_id, task_id) == (17, batch.celery_task_id)
        batch.dispatch_recovery_due_at = None

    async def commit():
        nonlocal durable
        if delivery == "commit-error":
            raise RuntimeError("fixture PostgreSQL commit rejected")
        durable = vars(batch).copy()

    async def refresh(row, fields, *, with_for_update):
        assert row is batch and durable is not None and with_for_update is True
        assert fields == ["status", "celery_task_id"]
        for field in fields:
            setattr(row, field, durable[field])
        lock_reads.append(row.status)

    def publish(*args, **options):
        if delivery == "process-exit-before-publish":
            raise _PublisherProcessExit
        publications.append((args, options, None if durable is None else dict(durable)))
        if delivery == "lost-after-claim" and durable is not None:
            durable["status"] = "running"
        if delivery not in {"ok", "commit-error"}:
            raise ConnectionError("fixture broker did not confirm")
        return SimpleNamespace(id=options.get("task_id") or "legacy-publish-id")

    db.commit.side_effect = commit
    db.refresh.side_effect = refresh
    monkeypatch.setattr(
        "app.repositories.ai.batch_run_repository.BatchRunRepository",
        lambda *_: SimpleNamespace(
            create=create, update=update, confirm_dispatch=confirm_dispatch
        ),
    )
    monkeypatch.setattr(
        dispatcher,
        "AgentRepository",
        lambda *_: SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(id=23, status="published", quota_config={})
            )
        ),
    )
    monkeypatch.setattr(dispatcher.AgentQuotaManager, "check_quota", AsyncMock())
    monkeypatch.setattr(execute_batch_run, "apply_async", publish)
    request = ExecutionRequest(agent_id=23, tenant_id=31, execution_mode="batch")
    submit = dispatcher.ExecutionDispatcher(db).dispatch_batch(
        request,
        [BatchItem(item_id="0", input_variables={"line": "synthetic"})],
        created_by=9,
    )
    if delivery == "process-exit-before-publish":
        with pytest.raises(_PublisherProcessExit):
            await submit
        assert durable is not None and durable["status"] == "pending"
        assert str(UUID(durable["celery_task_id"])) == durable["celery_task_id"]
        assert durable["input_items"] == [
            {"item_id": "0", "input_variables": {"line": "synthetic"}}
        ]
        due_at = durable.get("dispatch_recovery_due_at")
        assert isinstance(due_at, datetime) and due_at > now, (
            "worker recovery must be eligible after the committed publisher exits"
        )
        assert publications == lock_reads == []
        assert db.commit.await_count == 1
        return
    if delivery == "commit-error":
        with pytest.raises(RuntimeError, match="PostgreSQL commit rejected"):
            await submit
        assert durable is None and publications == [] and lock_reads == []
        return
    if delivery in {"rejected", "lost-before-claim"}:
        with pytest.raises(ServiceUnavailableException) as error:
            await submit
        assert (error.value.status_code, error.value.code, error.value.data) == (
            503,
            5030,
            {"batch_run_id": 17},
        )
    else:
        accepted = await submit
        assert accepted.batch_run_id == 17 and accepted.total == 1

    ((args, options, as_published),) = publications
    assert args == ()
    assert as_published is not None, (
        "worker can consume before PostgreSQL sees the batch"
    )
    task_id = as_published["celery_task_id"]
    assert str(UUID(task_id)) == task_id
    assert as_published["status"] == "pending" and as_published["completed_at"] is None
    assert as_published["created_by"] == 9 and as_published["agent_id"] == 23
    assert options == {
        "kwargs": {"batch_run_id": 17, "tenant_id": 31},
        "task_id": task_id,
        "retry": False,
    }

    assert durable["celery_task_id"] == task_id
    assert durable["completed_items"] == durable["failed_items"] == 0
    if delivery == "ok":
        assert durable == as_published | {"dispatch_recovery_due_at": None}
        assert lock_reads == [] and db.commit.await_count == 2
    elif delivery == "lost-after-claim":
        assert durable == as_published | {"status": "running"}
        assert lock_reads == ["running"] and db.commit.await_count == 2
    else:
        assert durable["status"] == "failed" and durable["completed_at"] is not None
        assert durable["dispatch_recovery_due_at"] is None
        assert lock_reads == ["pending"] and db.commit.await_count == 2
