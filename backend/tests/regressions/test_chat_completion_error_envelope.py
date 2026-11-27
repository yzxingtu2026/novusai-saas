"""Test type: behavioral.

Regression for: BUG-2026-11-06-CHAT-ERROR-ENVELOPE.
Scope: Chat Completions transport errors and safe runtime diagnostics.
Mocked dependency: httpx.MockTransport supplies HTTP status and error framing.
The SDK, adapter, exception classification, and log messages execute real code.
The empty and whitespace controls exercise protocol framing only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

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
