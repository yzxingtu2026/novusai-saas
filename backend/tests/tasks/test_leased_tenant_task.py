"""Test type: behavioral.

中文: 验证租约生命周期归属、失败可见性及心跳线程清理。
EN: Lease lifecycle ownership, failure visibility and thread cleanup.

DB transport is replaced here; actual CAS/concurrency is covered in PostgreSQL
tests. No business importer outcome is stubbed or counted as end-to-end proof.
"""

from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.tasks import leased_tenant_task as module
from app.tasks.leased_tenant_task import LeasedTenantTask

HANDLER = "fixture.tasks.monitor.first"
CATALOG_HANDLER = "fixture.tasks.monitor.second"


class Monitored(LeasedTenantTask):
    name = HANDLER
    LEASE_HANDLER_PATH = HANDLER


def make_task(headers=None):
    task = Monitored()
    request = SimpleNamespace(headers=headers or {}, retries=0, _monitor_lease=None)
    task.request_stack = SimpleNamespace(top=request)
    return task


def state(task_id="current"):
    return {
        "run_id": 12,
        "lease_token": "owned-token",
        "celery_task_id": task_id,
        "effective_tenant_id": 11,
        "handler_path": HANDLER,
        "stop_event": Event(),
        "thread": None,
    }


def metadata(**overrides):
    values = {
        "task_definition_id": 1,
        "task_code_snapshot": "system.fixture.collect",
        "handler_path_snapshot": HANDLER,
        "effective_tenant_id": 11,
        "trigger_slot": "test-slot",
        "trigger_source": "scheduler",
    }
    return {**values, **overrides}


def test_real_celery_retry_preserves_execution_id_and_increments_attempt():
    """Behavioral: actual Celery retry signature, not a fabricated broker return."""
    from app.tasks.base import get_task_registry, register_task

    @register_task(name=HANDLER, base=Monitored)
    def task(_self, tenant_id):
        return tenant_id

    try:
        from app.repositories.system.task_run_lease_repository import leased_handlers
        from app.tasks.task_scheduling import (
            handler_supports_tenant_dispatch,
            resolve_handler_kwargs,
        )

        assert HANDLER in leased_handlers()
        assert handler_supports_tenant_dispatch(HANDLER)
        assert resolve_handler_kwargs(HANDLER, {}, tenant_id=11) == {"tenant_id": 11}
    finally:
        get_task_registry().pop(HANDLER)
    task.push_request(
        id="same-execution-id",
        retries=0,
        called_directly=False,
        is_eager=True,
        args=(),
        kwargs={"tenant_id": 11},
        delivery_info={"is_eager": True},
    )
    try:
        retry = task.retry(countdown=1, throw=False)
        assert retry.sig.options["task_id"] == "same-execution-id"
        assert retry.sig.options["retries"] == 1
        assert retry.sig.kwargs == {"tenant_id": 11}
    finally:
        task.pop_request()


def test_missing_or_mismatched_metadata_fails_before_registration(monkeypatch):
    def forbidden(**_kwargs):
        pytest.fail("Registration must not be reached for invalid metadata")

    monkeypatch.setattr(module.TaskRunLeaseRepository, "register_active_run", forbidden)
    for headers in (
        {},
        metadata(handler_path_snapshot="other"),
        metadata(effective_tenant_id=None),
        metadata(effective_tenant_id=22),
    ):
        task = make_task(headers)
        with pytest.raises(RuntimeError):
            task._record_task_run_start("current", (), {"tenant_id": 11})
        assert task.request._monitor_lease is None


def test_registration_db_error_propagates_instead_of_ignore(monkeypatch):
    def unavailable(**_kwargs):
        raise OperationalError("register", {}, RuntimeError("offline"))

    monkeypatch.setattr(
        module.TaskRunLeaseRepository, "register_active_run", unavailable
    )
    task = make_task(metadata())
    with pytest.raises(OperationalError):
        task._record_task_run_start("current", (), {"tenant_id": 11})
    assert task.request._monitor_lease is None


def test_transient_heartbeat_retries_but_real_no_match_stops(monkeypatch):
    calls = []

    class ControlledEvent:
        def is_set(self):
            return False

        def wait(self, timeout):
            assert timeout == 20
            assert len(calls) < 3
            return False

    def heartbeat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OperationalError("heartbeat", {}, RuntimeError("partition"))
        return False

    monkeypatch.setattr(module.TaskRunLeaseRepository, "heartbeat", heartbeat)
    module._heartbeat_loop(
        run_id=12,
        lease_token="owned-token",
        celery_task_id="current",
        effective_tenant_id=11,
        handler_path=HANDLER,
        duration_seconds=90,
        heartbeat_seconds=20,
        stop_event=ControlledEvent(),
    )
    assert (
        calls
        == [
            {
                "run_id": 12,
                "lease_token": "owned-token",
                "celery_task_id": "current",
                "effective_tenant_id": 11,
                "handler_path": HANDLER,
                "duration_seconds": 90,
            },
        ]
        * 2
    )


@pytest.mark.parametrize("callback", ["success", "retry", "failure"])
def test_callbacks_always_stop_thread_on_database_failure(monkeypatch, callback):
    task = make_task()
    owned = state()
    stopped = owned["stop_event"]
    task.request._monitor_lease = owned
    seen = []

    def unavailable(**kwargs):
        seen.append(kwargs)
        raise OperationalError("settle", {}, RuntimeError("offline"))

    method = {
        "success": "settle_success",
        "retry": "settle_retrying",
        "failure": "settle_failure",
    }[callback]
    monkeypatch.setattr(module.TaskRunLeaseRepository, method, unavailable)
    if callback == "success":
        task._record_task_run_success("current", {"actual": True}, 1.25)
    elif callback == "retry":
        task._record_task_run_retry("current", RuntimeError("retry"))
    else:
        task._record_task_run_failure("current", RuntimeError("failure"), None, 1.25)
    assert stopped.is_set() and task.request._monitor_lease is None
    assert len(seen) == 1
    assert (
        seen[0]["lease_token"] == "owned-token" and seen[0]["effective_tenant_id"] == 11
    )
    assert seen[0]["celery_task_id"] == "current"


def test_wrong_callback_id_does_not_touch_current_request(monkeypatch):
    task = make_task()
    owned = state()
    task.request._monitor_lease = owned

    def forbidden(**_kwargs):
        pytest.fail("Wrong callback must not write current execution")

    monkeypatch.setattr(module.TaskRunLeaseRepository, "settle_success", forbidden)
    task._record_task_run_success("old-request", {}, 10)
    assert task.request._monitor_lease is owned
    assert not owned["stop_event"].is_set()


def test_catalog_callback_settles_catalog_handler_identity(monkeypatch):
    from app.repositories.system.task_run_lease_repository import LeaseRegistration

    class CatalogMonitored(Monitored):
        name = CATALOG_HANDLER
        LEASE_HANDLER_PATH = CATALOG_HANDLER

    task = CatalogMonitored()
    task.request_stack = SimpleNamespace(
        top=SimpleNamespace(
            headers=metadata(handler_path_snapshot=CATALOG_HANDLER),
            retries=0,
            _monitor_lease=None,
        )
    )
    registration = LeaseRegistration(
        run_id=99,
        lease_token="catalog-token",
        celery_task_id="catalog-task",
        effective_tenant_id=11,
        handler_path=CATALOG_HANDLER,
    )
    monkeypatch.setattr(
        module.TaskRunLeaseRepository, "register_active_run", lambda **_: registration
    )
    monkeypatch.setattr(module.threading.Thread, "start", lambda _self: None)
    seen = []

    def settle(**kwargs):
        seen.append(kwargs)
        return True

    monkeypatch.setattr(module.TaskRunLeaseRepository, "settle_success", settle)
    task._record_task_run_start("catalog-task", (), {"tenant_id": 11})
    task._record_task_run_success("catalog-task", {"created_count": 3}, 1.0)

    assert len(seen) == 1
    assert seen[0]["run_id"] == registration.run_id
    assert seen[0]["lease_token"] == registration.lease_token
    assert seen[0]["handler_path"] == CATALOG_HANDLER


def test_platform_reaper_propagates_database_failure(monkeypatch):
    from app.tasks import task_run_monitor

    monkeypatch.setattr(task_run_monitor, "leased_handlers", lambda: (HANDLER,))

    def unavailable(**_kwargs):
        raise OperationalError("reap", {}, RuntimeError("offline"))

    monkeypatch.setattr(module.TaskRunLeaseRepository, "reap_lost_runs", unavailable)
    with pytest.raises(OperationalError):
        task_run_monitor.reconcile_expired_task_runs.run()


def test_platform_reaper_scans_every_opted_in_handler(monkeypatch):
    from app.tasks import task_run_monitor

    monkeypatch.setattr(
        task_run_monitor, "leased_handlers", lambda: (HANDLER, CATALOG_HANDLER)
    )

    seen = []

    def reap(*, handler_path, batch_limit):
        seen.append((handler_path, batch_limit))
        return [len(seen)]

    monkeypatch.setattr(module.TaskRunLeaseRepository, "reap_lost_runs", reap)
    result = task_run_monitor.reconcile_expired_task_runs.run()
    assert seen == [
        (handler_path, task_run_monitor.REAP_BATCH_LIMIT)
        for handler_path in (HANDLER, CATALOG_HANDLER)
    ]
    assert result["total_lost"] == 2
    assert [entry["handler"] for entry in result["handlers_reaped"]] == [
        HANDLER,
        CATALOG_HANDLER,
    ]
