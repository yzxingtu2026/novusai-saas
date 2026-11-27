"""中文: 用实际应用观测异常响应与操作日志。

EN: Exercise exception responses and operation logs in the mounted application.

Test type: behavioral
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.types import ASGIApp, Message, Scope

from app.core.config import settings
from app.exceptions import BusinessException
from app.rbac.decorators import public

PREFIX = "/admin/_audit-fixture"
TRACE = "fixture-trace-" + "x" * 50
PRIVATE_ERROR = "fictional private runtime error"
STREAM_CHUNK = b"data: fixture\n\n"


@pytest.fixture
def create_app(monkeypatch, tmp_path) -> Callable[[], Any]:
    from app import storage

    monkeypatch.setattr(storage, "LOCAL_STORAGE_ROOT", tmp_path / "uploads")
    from app.main import create_application

    return create_application


@pytest.fixture
def mounted_app(monkeypatch, create_app) -> tuple[ASGIApp, list[dict[str, Any]]]:
    from app.services.system import operation_log_service

    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", False)
    records: list[dict[str, Any]] = []

    def record_operation(**values: Any) -> None:
        records.append(values)

    monkeypatch.setattr(operation_log_service, "create_log_async", record_operation)
    asgi_app = create_app()
    app: FastAPI = asgi_app.other_asgi_app

    @app.get(f"{PREFIX}/ok")
    @public
    async def normal(value: int) -> dict:
        return {"code": 0, "message": "Fixture accepted", "data": value}

    @app.get(f"{PREFIX}/business-error")
    @public
    async def rejected() -> None:
        raise BusinessException(message="Fixture rule rejected")

    @app.get(f"{PREFIX}/unhandled")
    @public
    async def unhandled() -> None:
        raise RuntimeError(PRIVATE_ERROR)

    @app.get(f"{PREFIX}/array")
    @public
    async def array_response() -> list[int]:
        return [1, 2]

    @app.get(f"{PREFIX}/stream")
    @public
    async def stream() -> StreamingResponse:
        async def chunks():
            yield STREAM_CHUNK
            raise RuntimeError(PRIVATE_ERROR)

        return StreamingResponse(
            chunks(), media_type="text/event-stream", status_code=202
        )

    @app.get(f"{PREFIX}/cancel")
    @public
    async def cancelled() -> None:
        raise asyncio.CancelledError

    return asgi_app, records


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("ok?value=3", 200, 0),
        ("ok?value=invalid", 422, 4220),
        ("business-error", 422, 4220),
        ("unhandled", 500, 5000),
        ("array", 200, None),
    ],
)
async def test_mounted_response_has_one_correlated_audit(
    mounted_app, path: str, status: int, code: int | None
) -> None:
    app, records = mounted_app
    async with AsyncClient(
        base_url="http://fixture.test",
        transport=ASGITransport(app=app, raise_app_exceptions=False),
    ) as client:
        response = await client.get(
            f"{PREFIX}/{path}",
            headers={"X-Trace-ID": f" {TRACE}suffix ", "X-Language": "en"},
        )

    assert response.status_code == status
    payload = response.json()
    assert response.headers.get_list("x-trace-id") == [TRACE]
    if code is None:
        assert payload == [1, 2]
        message = None
    else:
        assert payload["code"] == code
        message = payload["message"]
    if status == 500:
        assert payload == {
            "code": 5000,
            "message": "Internal server error",
            "data": None,
            "trace_id": TRACE,
        }
    [record] = records
    assert (record["trace_id"], record["status_code"]) == (TRACE, status)
    assert (record["response_code"], record["response_message"]) == (code, message)
    assert record["path"] == f"{PREFIX}/{path.split('?')[0]}"


@pytest.mark.parametrize("path", ["stream", "cancel"])
async def test_interrupted_response_keeps_observed_outcome(mounted_app, path) -> None:
    app, records = mounted_app
    messages: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": f"{PREFIX}/{path}",
        "query_string": b"",
        "headers": [(b"host", b"fixture.test"), (b"x-trace-id", TRACE.encode())],
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    if path == "stream":
        with pytest.raises(RuntimeError, match=PRIVATE_ERROR):
            await app(scope, receive, send)
        starts = [
            message for message in messages if message["type"] == "http.response.start"
        ]
        assert [message["status"] for message in starts] == [202]
        assert dict(starts[0]["headers"])[b"x-trace-id"] == TRACE.encode()
        assert messages[-1]["body"] == STREAM_CHUNK
        expected_status = 202
    else:
        await app(scope, receive, send)
        assert messages == []
        expected_status = None

    [record] = records
    assert (record["trace_id"], record["status_code"], record["response_code"]) == (
        TRACE,
        expected_status,
        None,
    )


@pytest.mark.parametrize(
    ("environment", "paths"),
    [
        ("  PrOdUcTiOn ", (None, None, None)),
        ("development", ("/docs", "/redoc", "/openapi.json")),
    ],
)
def test_debug_documentation_routes_follow_response_policy(
    monkeypatch, create_app, environment, paths
) -> None:
    monkeypatch.setattr(settings, "APP_ENV", environment)
    monkeypatch.setattr(settings, "DEBUG", True)

    app = create_app().other_asgi_app

    assert (app.docs_url, app.redoc_url, app.openapi_url) == paths
