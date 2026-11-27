"""中文: 有界提交提示和任务清理。 EN: Bounded commit hints and task cleanup.

Test type: behavioral / structural. No broker connection is permitted.
"""

from concurrent.futures import Future
from contextlib import contextmanager
from threading import BoundedSemaphore
from types import SimpleNamespace

import pytest

from app.services.common import notification_delivery_dispatch as dispatch
from tests.integration.notification_postgres_support import (
    deny_unfaked_notification_network,
)


@pytest.fixture(autouse=True)
def network_denied(monkeypatch):
    blocked = deny_unfaked_notification_network(monkeypatch)
    yield
    assert blocked == []


def session_with_hints(*ids):
    return SimpleNamespace(
        info={dispatch._KEY: set(ids)}, in_nested_transaction=lambda: False
    )


def test_hint_capacity_is_bounded_and_completion_releases_capacity(monkeypatch):
    pending = []

    class Publisher:
        def submit(self, fn, delivery_id):
            assert fn is dispatch.publish_notification_delivery
            future = Future()
            pending.append((delivery_id, future))
            return future

    monkeypatch.setattr(dispatch, "_publisher_slots", BoundedSemaphore(2))
    monkeypatch.setattr(dispatch, "_publisher", Publisher())
    session = session_with_hints(1, 2, 3, 4)
    dispatch._committed(session)
    assert len(pending) == 2 and dispatch._KEY not in session.info
    pending[0][1].set_result(None)
    dispatch._committed(session_with_hints(5))
    assert len(pending) == 3 and pending[2][0] == 5
    pending[1][1].set_exception(RuntimeError("fictional publish failure"))
    pending[2][1].set_result(None)
    assert dispatch._publisher_slots.acquire(blocking=False) is True
    assert dispatch._publisher_slots.acquire(blocking=False) is True
    assert dispatch._publisher_slots.acquire(blocking=False) is False


def test_stopped_executor_never_fails_a_business_commit_or_leaks_capacity(monkeypatch):
    class Publisher:
        def submit(self, *_args):
            raise RuntimeError("fictional stopped executor")

    monkeypatch.setattr(dispatch, "_publisher_slots", BoundedSemaphore(1))
    monkeypatch.setattr(dispatch, "_publisher", Publisher())
    dispatch._committed(session_with_hints(1, 2, 3))
    assert dispatch._publisher_slots.acquire(blocking=False) is True
    assert dispatch._publisher_slots.acquire(blocking=False) is False


def test_publisher_has_owned_bounded_connection_and_id_only_task(monkeypatch):
    from app.celery_app import celery_app

    events = []
    connection = object()

    @contextmanager
    def bounded_connection(**kwargs):
        events.append(("connect", kwargs))
        try:
            yield connection
        finally:
            events.append(("close",))

    monkeypatch.setattr(celery_app, "connection_for_write", bounded_connection)
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda *args, **kwargs: events.append(("send", args, kwargs)),
    )
    dispatch.publish_notification_delivery(42)
    assert events == [
        (
            "connect",
            {
                "connect_timeout": 2,
                "transport_options": {
                    "socket_connect_timeout": 2,
                    "socket_timeout": 2,
                    "retry_on_timeout": False,
                    "max_retries": 0,
                },
            },
        ),
        (
            "send",
            ("app.tasks.notification.dispatch_notification_delivery",),
            {
                "kwargs": {"delivery_id": 42},
                "queue": "notification",
                "connection": connection,
                "retry": False,
                "ignore_result": True,
            },
        ),
        ("close",),
    ]


def test_broker_failure_drops_hint_and_closes_owned_connection(monkeypatch):
    from app.celery_app import celery_app

    closed = []

    @contextmanager
    def connection(**_kwargs):
        try:
            yield object()
        finally:
            closed.append(True)

    def fail(*_args, **_kwargs):
        raise TimeoutError("fictional unavailable broker")

    monkeypatch.setattr(celery_app, "connection_for_write", connection)
    monkeypatch.setattr(celery_app, "send_task", fail)
    assert dispatch.publish_notification_delivery(42) is None
    assert closed == [True]


def test_task_engine_cleanup_failure_preserves_primary_error(monkeypatch):
    from app.services.common import notification_task_runtime as runtime

    errors = []
    original = LookupError("fictional business failure")

    class Engine:
        async def dispose(self):
            errors.append("disposed")
            raise RuntimeError("fictional broken cleanup")

    monkeypatch.setattr(
        runtime, "create_async_engine", lambda *_args, **_kwargs: Engine()
    )
    monkeypatch.setattr(
        runtime, "async_sessionmaker", lambda *_args, **_kwargs: object()
    )

    async def fail(_sessions):
        raise original

    with pytest.raises(LookupError) as raised:
        runtime.run_notification_task(fail)
    assert raised.value is original and errors == ["disposed"]


@pytest.mark.parametrize("positional", [False, True])
def test_legacy_task_run_audit_cannot_capture_raw_mail_arguments(
    monkeypatch, positional
):
    from app.tasks.base import BaseTask
    from app.tasks.notification import LegacyNotificationTask

    captured = []
    monkeypatch.setattr(
        BaseTask, "_record_task_run_start", lambda _self, *args: captured.append(args)
    )
    raw = (
        "fixture@example.test",
        "Fictional subject",
        "Fictional token",
        "Fictional token",
        "fixture.notice",
        77,
        42,
    )
    kwargs = (
        {}
        if positional
        else {"to": [raw[0]], "subject": raw[1], "html_body": raw[2], "delivery_id": 42}
    )
    LegacyNotificationTask()._record_task_run_start(
        "fictional-task", raw if positional else (), kwargs
    )
    assert captured == [("fictional-task", (), {"delivery_id": 42})]
