"""Test type: behavioral.

Regression for: BUG-2026-11-06-CHAT-ERROR-ENVELOPE.
Scope: Chat Completions transport errors and safe runtime diagnostics.
Mocked dependency: httpx.MockTransport supplies HTTP status and error framing.
The SDK, adapter, exception classification, and log messages execute real code.
The empty and whitespace controls exercise protocol framing only.
The gateway test supplies static models, key, and accounting callbacks instead
of a database. Gateway retry, fallback, and SSE framing execute real code.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from loguru import logger

from app.ai.adapters.openai_adapter import OpenAIAdapter
from app.ai.engine.failure_classifier import FailureClassifier
from app.ai.engine.stream_error_utils import resolve_provider_public_error_message
from app.ai.exceptions import (
    AIGatewayError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    is_retryable,
)
from app.ai.gateway_support.facade_mixins import GatewayFacadeMixin
from app.ai.gateway_support.stream_chat_gateway import execute_stream_chat
from app.core.i18n import _

_MODEL = "fixture-model"
_BASE_URL = "https://provider.fixture.invalid/v1/"
_PRIVATE_TEXT = "synthetic-provider-private-content"
_CHAT_ARGS = {"wire_api": "chat_completions", "messages": [], "model": _MODEL}


@asynccontextmanager
async def _adapter_for(
    receive: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[tuple[OpenAIAdapter, list[str]]]:
    adapter = OpenAIAdapter(api_key="fixture-key", base_url=_BASE_URL)
    original = adapter.client
    requests: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        return receive(request)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(capture), trust_env=False
        ) as client:
            adapter.client = original.with_options(http_client=client)
            yield adapter, requests
    finally:
        await original.close()


@pytest.fixture
def adapter_logs() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)))
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _assert_safe_error(error: AIGatewayError, *, logs: list[str]) -> None:
    kind, details = FailureClassifier.classify_exception(error)
    projected = repr(
        (
            error.to_dict(),
            str(error),
            error.__dict__,
            error.__cause__,
            error.__context__,
            kind,
            details,
            resolve_provider_public_error_message(error),
            logs,
        )
    )
    assert _PRIVATE_TEXT not in projected
    assert error.original_error is None
    assert (error.provider_code, error.model_code) == ("openai", _MODEL)


@pytest.mark.parametrize(
    ("wire_status", "error_body", "error_type", "effective_status", "code", "kind"),
    [
        pytest.param(
            200,
            {"code": "rate_limit_exceeded"},
            ProviderRateLimitError,
            429,
            "provider_rate_limit",
            "provider_rate_limit",
            id="rate-limit",
        ),
        pytest.param(
            201,
            {"type": "authentication_error"},
            ProviderAuthError,
            401,
            "provider_auth_error",
            "provider_bad_response",
            id="auth",
        ),
        pytest.param(
            200,
            {"status": "403", "code": "rate_limit"},
            ProviderAuthError,
            403,
            "permission_denied",
            "provider_bad_response",
            id="permission",
        ),
        pytest.param(
            200,
            {"type": "invalid_request_error"},
            ProviderError,
            400,
            "400",
            "provider_bad_response",
            id="invalid-request",
        ),
        pytest.param(
            200,
            {"type": "server_error", "code": "service_unavailable"},
            ProviderError,
            503,
            "503",
            "provider_bad_response",
            id="server-error",
        ),
        pytest.param(
            200,
            {"status": 429.9, "type": "unrecognized", "code": _PRIVATE_TEXT},
            ProviderError,
            200,
            "provider_error_envelope",
            "provider_bad_response",
            id="unknown",
        ),
    ],
)
async def test_sync_error_envelope_preserves_wire_status_and_safe_classification(
    wire_status: int,
    error_body: dict[str, Any],
    error_type: type[AIGatewayError],
    effective_status: int,
    code: str,
    kind: str,
    adapter_logs: list[str],
) -> None:
    payload = {
        "error": {**error_body, "message": _PRIVATE_TEXT, "detail": [_PRIVATE_TEXT]},
        "choices": [],
    }

    async with _adapter_for(lambda _: httpx.Response(wire_status, json=payload)) as (
        adapter,
        requests,
    ):
        with pytest.raises(AIGatewayError) as caught:
            await adapter.execute_protocol_chat(**_CHAT_ARGS)

    error = caught.value
    assert (type(error), error.status_code, error.error_code) == (
        error_type,
        effective_status,
        code,
    )
    assert error.upstream_status_code == wire_status
    assert is_retryable(error) is (500 <= effective_status < 600)
    classified_kind, details = FailureClassifier.classify_exception(error)
    assert classified_kind == kind
    assert details["status_code"] == wire_status
    assert details.get("effective_status_code", wire_status) == effective_status
    assert requests == ["POST /v1/chat/completions"]
    _assert_safe_error(error, logs=adapter_logs)


class _EventStream(httpx.AsyncByteStream):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.events:
            yield f"data: {json.dumps(event)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("send_whitespace_first", [False, True])
@pytest.mark.parametrize(
    ("error_body", "effective_status", "code"),
    [
        pytest.param(
            {"type": "rate_limit_error", "message": _PRIVATE_TEXT},
            429,
            "provider_rate_limit",
            id="sdk-raised",
        ),
        pytest.param({}, 200, "provider_error_envelope", id="sdk-parsed"),
    ],
)
async def test_stream_error_envelope_keeps_prior_deltas_and_closes_http_response(
    send_whitespace_first: bool,
    error_body: dict[str, Any],
    effective_status: int,
    code: str,
    adapter_logs: list[str],
) -> None:
    events = []
    if send_whitespace_first:
        events.append(
            {
                "choices": [
                    {"index": 0, "delta": {"content": " \n"}, "finish_reason": None}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            }
        )
    events.append({"error": error_body, "choices": []})
    stream = _EventStream(events)

    def receive(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    outputs = []
    async with _adapter_for(receive) as (adapter, requests):
        with pytest.raises(AIGatewayError) as caught:
            async for chunk in adapter.execute_protocol_stream(**_CHAT_ARGS):
                outputs.append((chunk.delta, chunk.total_tokens, chunk.finish_reason))

        assert stream.closed
    error = caught.value
    assert (error.status_code, error.error_code, error.upstream_status_code) == (
        effective_status,
        code,
        200,
    )
    assert outputs == ([(" \n", 4, None)] if send_whitespace_first else [])
    assert requests == ["POST /v1/chat/completions"]
    _assert_safe_error(error, logs=adapter_logs)


async def test_sync_empty_result_with_null_error_keeps_ordinary_empty_contract() -> (
    None
):
    async with _adapter_for(
        lambda _: httpx.Response(200, json={"error": None, "choices": []})
    ) as (adapter, requests):
        response = await adapter.execute_protocol_chat(**_CHAT_ARGS)

    assert response.message.content == ""
    assert response.finish_reason == "stop"
    assert response.metadata["protocol_path"] == "chat_completions"
    assert requests == ["POST /v1/chat/completions"]


async def test_sync_invalid_text_body_is_omitted_from_logs_and_caller_error(
    adapter_logs: list[str],
) -> None:
    async with _adapter_for(lambda _: httpx.Response(200, text=_PRIVATE_TEXT)) as (
        adapter,
        requests,
    ):
        with pytest.raises(ProviderError) as caught:
            await adapter.execute_protocol_chat(**_CHAT_ARGS)

    assert caught.value.message == _("ai.request_failed")
    assert requests == ["POST /v1/chat/completions"]
    _assert_safe_error(caught.value, logs=adapter_logs)


@pytest.mark.parametrize("wire_status", [401, 429, 503])
async def test_non_2xx_sdk_errors_keep_status_and_retry_contract(
    wire_status: int,
) -> None:
    headers = {"retry-after": "4"} if wire_status == 429 else {}
    async with _adapter_for(
        lambda _: httpx.Response(wire_status, json={"error": {}}, headers=headers)
    ) as (adapter, requests):
        with pytest.raises(AIGatewayError) as caught:
            await adapter.execute_protocol_chat(**_CHAT_ARGS)

    error = caught.value
    assert error.status_code == wire_status
    assert error.retry_after == (4 if wire_status == 429 else None)
    assert is_retryable(error) is (wire_status in {429, 503})
    assert (
        FailureClassifier.classify_exception(error)[0]
        == {
            401: "provider_bad_response",
            429: "provider_rate_limit",
            503: "provider_http_5xx",
        }[wire_status]
    )
    assert requests == ["POST /v1/chat/completions"]


@pytest.mark.parametrize(
    ("scenario", "expected_models"),
    [
        ("error-after-delta", [_MODEL]),
        ("retry-before-delta", [_MODEL, _MODEL]),
        ("fallback-before-delta", [_MODEL, "fixture-fallback"]),
        ("wire-503", [_MODEL, _MODEL]),
    ],
)
async def test_gateway_stream_preserves_emitted_chunks_without_replaying_them(
    scenario: str,
    expected_models: list[str],
    adapter_logs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(
        id=10,
        code="fixture",
        name="Fixture",
        type="openai_compatible",
        base_url=_BASE_URL,
        config={},
    )
    api_key = SimpleNamespace(id=20, decrypt_key=lambda: "fixture-key")
    model = SimpleNamespace(id=30, code=_MODEL, name="Fixture Primary")
    fallback = SimpleNamespace(
        id=31,
        code="fixture-fallback",
        name="Fixture Fallback",
        provider=provider,
    )
    gateway = GatewayFacadeMixin()
    gateway.db = object()
    gateway.get_provider_and_key = AsyncMock(return_value=(provider, api_key))
    gateway._get_model = AsyncMock(return_value=model)
    gateway.failover = SimpleNamespace(
        record_provider_runtime_failure=AsyncMock(),
        get_fallback_model=AsyncMock(return_value=fallback),
    )
    gateway.retry_service = SimpleNamespace(
        get_next_api_key=AsyncMock(return_value=None)
    )
    gateway.usage_recorder = SimpleNamespace(
        log_call_failure=AsyncMock(), on_stream_complete=AsyncMock()
    )
    monkeypatch.setattr(
        "app.ai.gateway_support.stream_chat_gateway.RETRY_BASE_DELAY", 0
    )

    requested_models: list[str] = []
    delta = {"choices": [{"index": 0, "delta": {"content": " \n"}}]}

    def receive(request: httpx.Request) -> httpx.Response:
        requested_models.append(json.loads(request.content)["model"])
        if scenario == "wire-503" and len(requested_models) == 1:
            return httpx.Response(503, json={"error": {}})
        if scenario == "error-after-delta" and requested_models[-1] == _MODEL:
            events = [
                delta,
                {"error": {"code": "service_unavailable", "message": _PRIVATE_TEXT}},
            ]
        elif len(requested_models) == 1:
            code = (
                "invalid_api_key"
                if scenario == "fallback-before-delta"
                else "service_unavailable"
            )
            events = [{"error": {"code": code, "message": _PRIVATE_TEXT}}]
        else:
            events = [delta]
        return httpx.Response(
            200,
            stream=_EventStream(events),
            headers={"content-type": "text/event-stream"},
        )

    async with _adapter_for(receive) as (adapter, _requests):
        response = await execute_stream_chat(
            gateway=gateway,
            provider_code=provider.code,
            model=model.code,
            messages=[],
            adapter_registry=SimpleNamespace(create_adapter=lambda **_: adapter),
            token_counter=object(),
            cost_calculator=SimpleNamespace(calculate_cost=lambda *_: 0),
        )
        frames = [
            frame.removeprefix("data: ").strip()
            async for frame in response.body_iterator
        ]

    assert requested_models == expected_models
    assert frames[-1] == "[DONE]"
    events = [json.loads(frame) for frame in frames[:-1]]
    assert [event["delta"] for event in events if "delta" in event] == [" \n"]
    errors = [event for event in events if event.get("error")]
    if scenario == "error-after-delta":
        assert [event["code"] for event in errors] == ["STREAM_ERROR"]
        assert events[-1] == errors[0]
        assert gateway.usage_recorder.on_stream_complete.await_count == 0
        failed = gateway.usage_recorder.log_call_failure.await_args.kwargs["error"]
        assert (failed.status_code, failed.upstream_status_code) == (503, 200)
        _assert_safe_error(failed, logs=adapter_logs)
    else:
        assert errors == []
        assert gateway.usage_recorder.on_stream_complete.await_count == 1
        assert gateway.usage_recorder.log_call_failure.await_count == 0
    assert _PRIVATE_TEXT not in repr((frames, adapter_logs))
