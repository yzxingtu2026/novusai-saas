"""Test type: behavioral.

Exercise the real batch task, its post-commit notification choice and the
production WS renderer. Inject item-level task outcomes and persisted cancel
state. This fixture does not execute a dialogue, tool, model or broker.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import sio_bridge
from app.core.i18n import set_locale
from app.services.common.notification_service import NotificationService
from app.sio.notification_seeds import SEED_TEMPLATES
from app.tasks import agent_batch


class _SessionFixture:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_):
        return False


@pytest.mark.parametrize(
    ("outcomes", "state_after", "template_mode", "status", "expected"),
    [
        pytest.param(
            (True,), 0, "stock", "cancelled", None, id="cancel-before-first-item"
        ),
        pytest.param(
            (True, True), 1, "stock", "cancelled", None, id="cancel-after-success"
        ),
        pytest.param(
            (False, True), 1, "stock", "cancelled", None, id="cancel-after-failure"
        ),
        # BUG-2026-11-26-BATCH-CANCEL-FINAL-ITEM
        pytest.param(
            (True,), 1, "stock", "cancelled", None, id="cancel-during-only-item"
        ),
        pytest.param(
            (True, False),
            2,
            "stock",
            "cancelled",
            None,
            id="cancel-during-final-failure",
        ),
        pytest.param(
            (True, True, True),
            2,
            "stock",
            "unknown",
            None,
            id="lease-expiry-during-item",
        ),
        pytest.param(
            (True, True),
            None,
            "stock",
            "completed",
            (
                "ai.batch_complete",
                "批处理已完成",
                "批处理任务已完成，共处理 2 条数据。",
            ),
            id="complete",
        ),
        pytest.param(
            (False, False),
            None,
            "stock",
            "failed",
            (
                "ai.batch_failed",
                "批处理执行失败",
                "批处理任务执行失败，错误信息：fixture item failure",
            ),
            id="full-failure",
        ),
        pytest.param(
            (True, False),
            None,
            "stock",
            "partial_failed",
            (
                "ai.batch_failed",
                "批处理部分失败",
                "批处理任务部分失败。已处理 2 条，成功 1 条，失败 1 条。首个错误：fixture item failure",
            ),
            id="partial-failure",
        ),
        pytest.param(
            (True, False),
            None,
            "custom",
            "partial_failed",
            (
                "ai.batch_failed",
                "Operator batch outcome",
                "State partial_failed: 1 failed",
            ),
            id="preserve-custom-template",
        ),
        pytest.param(
            (True, False),
            None,
            "inbox-only",
            "partial_failed",
            None,
            id="preserve-disabled-ws",
        ),
        pytest.param(
            (True, False),
            None,
            "missing",
            "partial_failed",
            (
                "ai.batch_failed",
                "Batch partially failed",
                "Processed 2 items: 1 succeeded, 1 failed. First error: fixture item failure",
            ),
            id="missing-template-fallback",
        ),
    ],
)
def test_batch_terminal_notice_matches_committed_status_and_counts(
    monkeypatch,
    outcomes,
    state_after,
    template_mode,
    status,
    expected,
):
    monkeypatch.setattr("app.core.redis.RedisManager._pool", object())

    agent = SimpleNamespace(id=77)
    run = SimpleNamespace(
        id=11,
        agent_id=77,
        created_by=21,
        status="pending",
        is_deleted=False,
        started_at=None,
        input_items=[
            {"item_id": f"row-{index}", "input_variables": {"index": index}}
            for index in range(len(outcomes))
        ],
        total_items=len(outcomes),
        completed_items=0,
        failed_items=0,
        completed_at=None,
        results=None,
        errors=None,
    )
    attempted = []
    commits = []
    pushes = []
    stored_status = "pending"
    previous_orm_status = run.status
    holding_row_lock = False
    result_snapshots = []
    db = AsyncMock()

    async def commit():
        nonlocal stored_status, previous_orm_status, holding_row_lock
        if run.status != previous_orm_status:
            stored_status = run.status
            previous_orm_status = run.status
        holding_row_lock = False
        commits.append((stored_status, run.completed_items, run.failed_items))
        result_snapshots.append(deepcopy((run.results, run.errors)))

    async def fetch(statement):
        nonlocal holding_row_lock, stored_status
        if statement.column_descriptions[0]["name"] == "BatchRun":
            return SimpleNamespace(scalar_one_or_none=lambda: run)
        assert statement._for_update_arg is not None
        if state_after == 0:
            stored_status = status
        holding_row_lock = True
        return SimpleNamespace(
            one_or_none=lambda: SimpleNamespace(
                status=stored_status, completed_at=None, lease_active=True
            ),
        )

    async def refresh(record, fields, *, with_for_update=False):
        nonlocal previous_orm_status, holding_row_lock, stored_status
        assert record is run
        assert "status" in fields
        holding_row_lock = with_for_update
        if state_after == 0 and commits:
            stored_status = status
        record.status = stored_status
        previous_orm_status = record.status

    async def execute(actual_agent, request):
        nonlocal stored_status
        assert actual_agent is agent
        assert holding_row_lock is False
        index = request.input_variables["index"]
        attempted.append(index)
        if len(attempted) == state_after:
            stored_status = status
        return SimpleNamespace(
            success=outcomes[index],
            output={"row": index},
            total_tokens=0,
            error=None if outcomes[index] else "fixture item failure",
        )

    async def lookup(_service, code, tenant_id=None):
        assert tenant_id is None
        if code == "ai.batch_failed" and template_mode == "missing":
            return None
        row = next(seed for seed in SEED_TEMPLATES if seed["code"] == code)
        result = SimpleNamespace(
            **row,
            scope="platform",
            tenant_id=None,
            source="core",
            is_system=True,
            override_of=None,
            plugin_name=None,
        )
        if code == "ai.batch_failed" and template_mode == "custom":
            result.title_template = "Operator batch outcome"
            result.body_template = "State {status}: {failed} failed"
        if code == "ai.batch_failed" and template_mode == "inbox-only":
            result.channels = ["inbox"]
        return result

    def capture(user_type, user_id, data):
        pushes.append((user_type, user_id, data, commits[-1]))

    db.commit.side_effect = commit
    db.execute.side_effect = fetch
    db.refresh.side_effect = refresh
    monkeypatch.setattr(agent_batch, "_task_async_session", lambda: _SessionFixture(db))
    monkeypatch.setattr(
        "app.repositories.ai.agent_repository.AgentRepository",
        lambda *_: SimpleNamespace(get_by_id=AsyncMock(return_value=agent)),
    )
    monkeypatch.setattr(
        "app.services.ai.agent_service.AgentService",
        lambda *_: SimpleNamespace(
            build_usage_attribution_context=AsyncMock(
                return_value={"source": "fixture"}
            ),
        ),
    )
    monkeypatch.setattr(
        "app.configs.service.ConfigService",
        lambda *_: SimpleNamespace(
            get_platform_config=AsyncMock(side_effect=lambda _key, default: default),
        ),
    )
    monkeypatch.setattr("app.ai.gateway.AIGateway", lambda *_: object())
    monkeypatch.setattr("app.ai.tools.sandbox.ToolSandbox", lambda **_: object())
    monkeypatch.setattr(
        "app.ai.engine.task.TaskEngine",
        lambda **_: SimpleNamespace(execute=execute),
    )
    monkeypatch.setattr(NotificationService, "_get_template", lookup)
    monkeypatch.setattr(
        NotificationService, "_notifications_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(sio_bridge, "notify_user_sync", capture)
    if template_mode == "missing":
        set_locale("en")

    result = agent_batch.execute_batch_run.run(batch_run_id=11, tenant_id=9)

    processed = len(outcomes) if state_after is None else state_after
    succeeded = sum(outcomes[:processed])
    failed = processed - succeeded
    assert attempted == list(range(processed))
    assert commits[-1] == (status, succeeded, failed)
    if processed == 0:
        assert result_snapshots[-1] == ([], None)
    # [structural] Inspect the first commit of each prefix, before any later
    # terminal write could mask a missing result or error.
    for count in range(1, processed + 1):
        first = next(
            index
            for index, (_, successes, failures) in enumerate(commits)
            if successes + failures == count
        )
        expected_results = [
            {"item_id": f"row-{index}", "output": {"row": index}, "total_tokens": 0}
            for index in range(count)
            if outcomes[index]
        ]
        expected_errors = [
            {"item_id": f"row-{index}", "error": "fixture item failure"}
            for index in range(count)
            if not outcomes[index]
        ]
        assert result_snapshots[first] == (expected_results, expected_errors or None)
        if count == processed and status in {"completed", "partial_failed", "failed"}:
            assert commits[first][0] == status
    assert result == {
        "batch_run_id": 11,
        "total": len(outcomes),
        "succeeded": succeeded,
        "failed": failed,
        "status": status,
    }
    progress = [entry for entry in pushes if entry[2]["type"] == "ai.batch_progress"]
    last_notified_count = processed - (status == "unknown")
    assert [entry[2]["data"]["completed"] for entry in progress] == list(
        range(1, last_notified_count + 1)
    )
    terminal = [entry for entry in pushes if entry[2]["type"] != "ai.batch_progress"]
    if expected is None:
        assert terminal == []
    else:
        event_type, title, body = expected
        assert terminal == [
            (
                "tenant_admin",
                21,
                {
                    "type": event_type,
                    "category": "ai",
                    "title": title,
                    "body": body,
                    "priority": "normal" if status == "completed" else "high",
                    "link": None,
                    "data": {
                        "batch_id": 11,
                        "status": status,
                        "completed": processed,
                        "total": len(outcomes),
                        "progress": 100,
                        "succeeded": succeeded,
                        "failed": failed,
                        "error": "fixture item failure"
                        if failed
                        else "0/2 items failed",
                    },
                },
                (status, succeeded, failed),
            )
        ]
