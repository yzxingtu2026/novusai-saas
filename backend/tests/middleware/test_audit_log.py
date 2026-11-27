from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from starlette.types import Message, Receive, Scope, Send

from app.core.config import settings
from app.middleware.audit_log import (
    AuditLogMiddleware,
    get_client_ip,
    sanitize_body,
    should_log_request,
)


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("203.0.113.8", "198.51.100.50", "203.0.113.8"),
        ("10.0.0.1", "198.51.100.50, 203.0.113.8, 10.0.0.2", "203.0.113.8"),
        (None, "198.51.100.50", None),
    ],
)
def test_audit_client_ip_uses_login_proxy_policy(
    monkeypatch, peer: str | None, forwarded: str, expected: str | None
) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.0/8")
    scope = {
        "headers": [(b"x-forwarded-for", forwarded.encode())],
        "client": (peer, 12345) if peer else None,
    }
    assert get_client_ip(scope) == expected


@pytest.mark.asyncio
async def test_audit_log_collects_chunked_json_response_metadata() -> None:
    captured: dict[str, object] = {}

    async def chunked_app(scope, receive, send):  # noqa: ANN001
        _ = scope
        await receive()
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b'{"code":5000,',
                "more_body": True,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'"message":"failed"}',
                "more_body": False,
            }
        )

    middleware = AuditLogMiddleware(chunked_app)
    middleware._write_log = AsyncMock(
        side_effect=lambda **kwargs: captured.update(kwargs["response_info"])
    )

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/admin/test",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }

    received = False

    async def receive():  # noqa: ANN202
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    sent_messages: list[dict] = []

    async def send(message):  # noqa: ANN202, ANN001
        sent_messages.append(message)

    await middleware(scope, receive, send)

    assert sent_messages[0]["type"] == "http.response.start"
    assert captured["status_code"] == 500
    assert captured["response_code"] == 5000
    assert captured["response_message"] == "failed"


def test_should_log_request_skips_root_document_path() -> None:
    assert should_log_request("/", "GET") is False


def test_sanitize_body_redacts_normalized_fields_recursively() -> None:
    original = {
        "apiKey": "fixture-api-key",
        "safe": "visible",
        "nested": [
            {
                "Authorization": "Bearer fixture-token",
                "webhookURL": "https://example.test/fixture-signature",
                "otpCode": "fixture-otp",
                "business_id": 42,
            }
        ],
        "requestHeaders": [
            ["Cookie", "fixture-cookie"],
            ["Proxy-Authorization", "fixture-proxy-auth"],
            ["X-Trace-ID", "trace-1"],
        ],
    }

    assert sanitize_body(original) == {
        "apiKey": "***",
        "safe": "visible",
        "nested": [
            {
                "Authorization": "***",
                "webhookURL": "***",
                "otpCode": "***",
                "business_id": 42,
            }
        ],
        "requestHeaders": [
            ["Cookie", "***"],
            ["Proxy-Authorization", "***"],
            ["X-Trace-ID", "trace-1"],
        ],
    }
    assert original["apiKey"] == "fixture-api-key"
    assert original["nested"][0]["Authorization"] == "Bearer fixture-token"
    assert original["requestHeaders"][0] == ["Cookie", "fixture-cookie"]


@pytest.mark.asyncio
async def test_audit_log_redacts_before_write_and_preserves_login_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.system import operation_log_service

    raw_body = (
        b'{"account":{"refreshToken":"body-token",'
        b'"safe":"visible"},"items":[{"private_key":"body-key"}]}'
    )
    raw_query = b"apiKey=query-key&limit=20&limit=30&signature=query-signature"
    delivered_body = bytearray()
    create_log = Mock()
    monkeypatch.setattr(operation_log_service, "create_log_async", create_log)

    async def fixture_app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["query_string"] == raw_query
        while True:
            message = await receive()
            delivered_body.extend(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b'{"code":0,"message":"ok"}',
                "more_body": False,
            }
        )

    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/auth/login",
        "query_string": raw_query,
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    async def send(_message: Message) -> None:
        return None

    await AuditLogMiddleware(fixture_app)(scope, receive, send)

    create_log.assert_called_once()
    captured = create_log.call_args.kwargs
    assert captured["query_params"] == {
        "apiKey": "***",
        "limit": ["20", "30"],
        "signature": "***",
    }
    assert captured["request_body"] == {
        "account": {"refreshToken": "***", "safe": "visible"},
        "items": [{"private_key": "***"}],
    }
    assert captured["action"] == "login"
    assert captured["path"] == "/auth/login"
    assert captured["status_code"] == 200
    assert captured["response_code"] == 0
    assert captured["response_message"] == "ok"
    assert bytes(delivered_body) == raw_body
