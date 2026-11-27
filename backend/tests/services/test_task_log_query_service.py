"""中文: 任务日志读模型合同测试。

EN: Contract tests for task-log read models.

Test type: behavioral
"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.i18n import _
from app.services.system.task_log_query_service import TaskLogRelationService


def _task_run() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        celery_task_id="celery-1",
        run_key="task:1:slot",
        task_name_snapshot="Example task",
        handler_path_snapshot="app.tasks.example",
        task_definition_id=2,
        binding_id=3,
        owner_tenant_id=None,
        effective_tenant_id=42,
        queue="scheduled",
        status="success",
        args_summary={"args": [1], "kwargs": {"tenant_id": 42}},
        result_summary={"cleaned": 1},
        error_message_public=None,
        error_message_internal=None,
        trigger_source="scheduler",
        run_kind="tenant_binding",
        trace_id="trace-1",
        started_at=datetime(2026, 5, 10, tzinfo=UTC),
        finished_at=datetime(2026, 5, 10, tzinfo=UTC),
        duration_ms=12,
        retry_count=0,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
        traceback_internal="stack",
    )


def test_task_log_response_uses_effective_tenant_id_without_tenant_alias() -> None:
    service = TaskLogRelationService(db=None)

    payload = service.serialize_task_run(_task_run())
    detail = service.serialize_task_run_detail(_task_run())

    assert payload["effective_tenant_id"] == 42
    assert "tenant_id" not in payload
    assert detail["effective_tenant_id"] == 42
    assert "tenant_id" not in detail


@pytest.mark.parametrize(
    "handler_path",
    [
        "app.tasks.email.send_email_task",
        "app.tasks.notification.send_notification_email",
    ],
)
def test_task_log_projects_mail_payloads_without_changing_retry_inputs(
    handler_path: str,
) -> None:
    service = TaskLogRelationService(db=None)
    run = _task_run()
    run.handler_path_snapshot = handler_path
    run.args_summary = {
        "args": [
            ["recipient@example.invalid"],
            "fixture mail subject",
            "<p>fixture mail body</p>",
            "fixture mail text",
        ],
        "kwargs": {
            "subject": "fixture subject keyword",
            "requestHeaders": [
                ["Authorization", "fixture auth header"],
                ["X-Trace-ID", "trace-1"],
            ],
            "idempotencyKey": "idempotency-9",
            "run_key": "run-9",
        },
    }
    run.result_summary = {"delivery_id": 19, "nested": {"signingKey": "fixture-key"}}
    original_args, original_result = deepcopy((run.args_summary, run.result_summary))
    relation_maps = {
        "definition_map": {2: {"name": "Example delivery", "scope": "all_tenants"}},
        "tenant_map": {42: "Example tenant"},
    }

    listing = service.serialize_task_run(run, **relation_maps)
    detail = service.serialize_task_run_detail(run, **relation_maps)

    assert listing["args"] == [["recipient@example.invalid"], "***", "***", "***"]
    assert listing["kwargs"] == {
        "subject": "***",
        "requestHeaders": [["Authorization", "***"], ["X-Trace-ID", "trace-1"]],
        "idempotencyKey": "idempotency-9",
        "run_key": "run-9",
    }
    assert listing["result"] == {"delivery_id": 19, "nested": {"signingKey": "***"}}
    assert listing["handler_path"] == handler_path
    assert listing["task_definition_name"] == "Example delivery"
    assert listing["task_scope"] == "all_tenants"
    assert listing["effective_tenant_name"] == "Example tenant"
    assert listing["status"] == "success"
    assert listing["trace_id"] == "trace-1"
    assert listing["run_key"] == "task:1:slot"
    assert listing["error_message"] is None
    assert detail == {**listing, "traceback": None}
    assert run.args_summary == original_args
    assert run.result_summary == original_result
    assert service.unpack_args_kwargs(run.args_summary) == (
        original_args["args"],
        original_args["kwargs"],
    )


def test_task_log_redacts_credentials_and_otp_in_historical_results() -> None:
    service = TaskLogRelationService(db=None)
    run = _task_run()
    run.args_summary = {
        "args": [10, "visible position"],
        "kwargs": {"subject": "visible business label", "retryCount": 1},
    }
    run.result_summary = {
        "encoded": '{"apiKey":"fixture-token","businessCode":"task.sync"}',
        "authorization": "Basic fixture-authorization-value",
        "message": "Verification code is 123456; attempt 2.",
        "details": "Open https://fixture-user:fixture-pass@example.invalid/status",
        "run_key": "run-9",
    }

    listing = service.serialize_task_run(run)

    assert listing["args"] == [10, "visible position"]
    assert listing["kwargs"] == {"subject": "visible business label", "retryCount": 1}
    result = listing["result"]
    assert json.loads(result["encoded"]) == {
        "apiKey": "***",
        "businessCode": "task.sync",
    }
    assert result["authorization"] == "***"
    assert result["message"] == "Verification code is ***; attempt 2."
    assert result["details"] == "Open https://***@example.invalid/status"
    assert result["run_key"] == "run-9"
    assert service.serialize_task_run_detail(run) == {**listing, "traceback": None}


@pytest.mark.parametrize(
    "error_field", ["error_message_public", "error_message_internal"]
)
def test_task_log_projects_generic_error_without_traceback(error_field: str) -> None:
    service = TaskLogRelationService(db=None)
    run = _task_run()
    run.status = "failed"
    setattr(run, error_field, "fixture exception with auth token")
    run.traceback_internal = "fixture private traceback"

    listing = service.serialize_task_run(run)

    assert listing["status"] == "failed"
    assert listing["error_message"] == _("common.server_error")
    assert listing["result"] == {"cleaned": 1}
    assert "traceback" not in listing
    assert service.serialize_task_run_detail(run) == {**listing, "traceback": None}
