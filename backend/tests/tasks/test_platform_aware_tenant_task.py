"""Structural and behavioral tests for the RAG Worker's platform task context."""

from types import SimpleNamespace

import pytest
from celery.exceptions import Ignore


def test_worker_imports_default_modules_and_registers_rag_task() -> None:
    """Test type: structural. Use the same module loader as Celery startup."""
    from app.celery_app import celery_app
    from app.tasks.base import PlatformAwareTenantTask, get_task_registry

    celery_app.loader.import_default_modules()

    name = "app.ai.rag.processor.process_document"
    assert celery_app.tasks[name].queue == "ai_gateway"
    assert isinstance(celery_app.tasks[name], PlatformAwareTenantTask)
    entry = get_task_registry()[name]
    assert entry["queue"] == "ai_gateway"


def test_platform_run_clears_previous_tenant_context() -> None:
    """Test type: behavioral. A platform KB job supplies an explicit null."""
    from app.tasks.base import PlatformAwareTenantTask

    task = PlatformAwareTenantTask()
    task.request_stack = SimpleNamespace(top=SimpleNamespace(headers={}))
    task._tenant_id = 56

    task._before_task_run_start("platform-kb", (), {"tenant_id": None})

    assert task.tenant_id is None


@pytest.mark.parametrize(
    ("kwargs", "headers"),
    [
        ({}, {}),
        ({"tenant_id": -1}, {}),
        ({"tenant_id": None}, {"effective_tenant_id": 57}),
        ({"tenant_id": None}, []),
    ],
    ids=[
        "missing-tenant",
        "invalid-tenant",
        "tenant-dispatch-header",
        "invalid-headers",
    ],
)
def test_platform_task_rejects_undeclared_or_mismatched_context(
    kwargs: dict,
    headers: object,
) -> None:
    """Test type: behavioral. Only an explicitly platform dispatch enters the platform path."""
    from app.tasks.base import PlatformAwareTenantTask

    task = PlatformAwareTenantTask()
    task.request_stack = SimpleNamespace(top=SimpleNamespace(headers=headers))

    with pytest.raises(Ignore):
        task._before_task_run_start("bad-kb-context", (), kwargs)


def test_ordinary_task_still_requires_tenant_context() -> None:
    """Test type: behavioral. A null tenant cannot enter ordinary tasks."""
    from app.tasks.base import TenantTask

    task = TenantTask()
    task.request_stack = SimpleNamespace(top=SimpleNamespace(headers={}))

    with pytest.raises(Ignore):
        task._before_task_run_start("ordinary-tenant-task", (), {"tenant_id": None})
