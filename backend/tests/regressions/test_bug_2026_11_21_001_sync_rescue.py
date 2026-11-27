"""Test type: behavioral.

Regression for: BUG-2026-11-21-001 (reasoning effort on sync rescue).
Scope: ConversationQueryEngine through the real adapter and SDK.
Only HTTP transport is replaced. Both calls receive HTTP errors.
"""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.ai.adapters.openai_adapter import OpenAIAdapter
from app.ai.exceptions import ProviderError
from app.ai.runtime.query_engine import ConversationQueryEngine
from app.ai.types import ChatMessage

pytestmark = pytest.mark.behavioral

_BASE_URL = "https://reasoning-rescue.example.test/v1"
_API_KEY = "fictional-rescue-fixture"
_MODEL = "gpt-6.2-sol"


def _reasoning_fields(wire_api: str, effort: str | None) -> dict:
    if effort is None:
        return {}
    if wire_api == "responses":
        return {"reasoning": {"effort": effort}}
    return {"reasoning_effort": effort}


@pytest.mark.parametrize(
    ("wire_api", "endpoint"),
    [
        ("responses", "/v1/responses"),
        ("chat_completions", "/v1/chat/completions"),
    ],
)
@pytest.mark.parametrize(
    ("configured_effort", "explicit_override", "stream_effort", "rescue_effort"),
    [
        ("max", None, "max", "max"),
        ("none", None, "none", "none"),
        (None, None, None, "low"),
        ("unknown", None, None, "low"),
        ("max", "none", "none", "none"),
    ],
    ids=["configured-max", "configured-none", "missing", "invalid", "caller-none"],
)
async def test_sync_rescue_only_defaults_when_no_valid_effort(
    wire_api: str,
    endpoint: str,
    configured_effort: str | None,
    explicit_override: str | None,
    stream_effort: str | None,
    rescue_effort: str,
) -> None:
    model_config = {
        "runtime_overrides": {
            "openai_compatible": {
                wire_api: _reasoning_fields(wire_api, configured_effort),
            },
        },
    }
    extra_kwargs: dict = {"model_config": model_config}
    if explicit_override is not None:
        extra_kwargs["_runtime_reasoning_effort_override"] = explicit_override

    requests: list[tuple[str, str, dict]] = []

    def reject_after_capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        message = (
            "stream fixture rejection"
            if body.get("stream")
            else "rescue fixture rejection"
        )
        return httpx.Response(
            400,
            json={"error": {"code": "fixture_negative_control", "message": message}},
        )

    adapter = OpenAIAdapter(
        api_key=_API_KEY,
        base_url=_BASE_URL,
        provider_config={
            "protocol_capabilities": {
                "primary_wire_api": wire_api,
                "allowed_wire_apis": ["responses", "chat_completions"],
                "allow_adapter_cross_protocol_fallback": False,
            },
        },
    )
    await adapter.client.close()
    transport = httpx.MockTransport(reject_after_capture)
    async with AsyncOpenAI(
        api_key=_API_KEY,
        base_url=_BASE_URL,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport, trust_env=False),
    ) as client:
        adapter.client = client
        engine = ConversationQueryEngine(adapter=adapter, strict_contract=True)
        with pytest.raises(ProviderError, match="rescue fixture rejection") as captured:
            await engine.run_stream_turn(
                messages=[ChatMessage(role="user", content="Sync rescue fixture")],
                model=_MODEL,
                temperature=0.0,
                max_tokens=None,
                top_p=1.0,
                tools=None,
                tool_choice=None,
                supports_vision=False,
                supports_audio=False,
                supports_video=False,
                extra_kwargs=extra_kwargs,
            )
    assert captured.value.status_code == 400
    stream_error = captured.value.__cause__
    assert isinstance(stream_error, ProviderError)
    assert str(stream_error) == "stream fixture rejection"
    assert stream_error.status_code == 400

    assert [(method, path) for method, path, _ in requests] == [("POST", endpoint)] * 2
    assert [body.get("stream", False) for _, _, body in requests] == [True, False]
    assert [body["model"] for _, _, body in requests] == [_MODEL, _MODEL]
    for _, _, body in requests:
        assert not any(
            key.startswith("_runtime_") or key == "model_config" for key in body
        )
    record = engine.turn_record
    assert record.protocol_path == wire_api
    assert record.fallback_history == []
    assert record.turn_outcome == "failed"
    assert record.metadata["sync_rescue_attempted"] is True
    assert record.metadata["sync_rescue_attempt_count"] == 1
    assert record.metadata["sync_rescue_retry_count"] == 0
    assert [
        {key: body[key] for key in ("reasoning", "reasoning_effort") if key in body}
        for _, _, body in requests
    ] == [
        _reasoning_fields(wire_api, stream_effort),
        _reasoning_fields(wire_api, rescue_effort),
    ]
