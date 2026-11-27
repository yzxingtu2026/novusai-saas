"""Test types: structural and behavioral (marked per test).

Regression for: BUG-2026-11-21-001.
Scope: configured reasoning effort and fast-turn policy at the SDK wire boundary.
The in-memory HTTP transport captures the request and returns a rejection.
"""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.ai.adapters.openai_adapter import OpenAIAdapter
from app.ai.engine.conversation_runtime_context_builder import (
    _build_request_extra_kwargs,
)
from app.ai.engine.model_policy import build_model_request_overrides
from app.ai.exceptions import ProviderError
from app.ai.runtime.contracts import ProtocolGuardContract
from app.ai.types import ChatMessage

_BASE_URL = "https://reasoning.example.test/v1"
_API_KEY = "fictional-reasoning-fixture"
_MODEL = "gpt-6.2-sol"
_WIRE_APIS = ("responses", "chat_completions")
_REASONING_KEYS = {"reasoning", "reasoning_effort"}


async def _send_request(
    *,
    wire_api: str,
    model: str,
    effort: str | None,
    stream: bool = False,
    **runtime_kwargs,
) -> dict:
    other_wire_api = next(wire for wire in _WIRE_APIS if wire != wire_api)
    protocol_config = {
        "responses": {"reasoning": {"effort": "medium"}},
        "chat_completions": {"reasoning_effort": "medium"},
    }
    if effort is None:
        protocol_config.pop(wire_api)
    elif wire_api == "responses":
        protocol_config[wire_api] = {"reasoning": {"effort": effort}}
    else:
        protocol_config[wire_api] = {"reasoning_effort": effort}
    config = {"runtime_overrides": {"openai_compatible": protocol_config}}
    requests: list[httpx.Request] = []

    def reject_after_capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "fixture stops after serialization",
                }
            },
        )

    adapter = OpenAIAdapter(
        api_key=_API_KEY,
        base_url=_BASE_URL,
        provider_config={
            "protocol_capabilities": {
                "primary_wire_api": other_wire_api,
                "allowed_wire_apis": list(_WIRE_APIS),
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
        request_args = {
            "messages": [ChatMessage(role="user", content="Reasoning fixture")],
            "model": model,
            "model_config": config,
            "_runtime_force_wire_api": wire_api,
            ProtocolGuardContract.RUNTIME_DISABLE_CROSS_PROTOCOL_FALLBACK: True,
            ProtocolGuardContract.RUNTIME_DISABLE_SYNC_RESCUE: True,
            **runtime_kwargs,
        }
        with pytest.raises(ProviderError) as captured:
            if stream:
                async for _ in adapter.stream_chat(**request_args):
                    pass
            else:
                await adapter.chat(**request_args)
        assert captured.value.status_code == 400

    expected_path = (
        "/v1/responses" if wire_api == "responses" else "/v1/chat/completions"
    )
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", expected_path)
    ]
    request_body = json.loads(requests[0].content)
    assert request_body["model"] == model
    assert request_body.get("stream", False) is stream
    assert "model_config" not in request_body
    assert not any(key.startswith("_runtime_") for key in request_body)
    return {key: request_body[key] for key in _REASONING_KEYS if key in request_body}


@pytest.mark.structural
@pytest.mark.parametrize(
    ("wire_api", "expected"),
    [
        ("responses", {"reasoning": {"effort": "max"}}),
        ("chat_completions", {"reasoning_effort": "max"}),
    ],
)
async def test_gpt_6_2_sol_sends_max_in_active_protocol(wire_api, expected) -> None:
    assert (
        await _send_request(wire_api=wire_api, model=_MODEL, effort=" MAX ") == expected
    )


@pytest.mark.structural
@pytest.mark.parametrize(
    ("wire_api", "model", "effort", "expected"),
    [
        ("responses", _MODEL, "unknown", {}),
        ("chat_completions", _MODEL, "unknown", {}),
        ("responses", "custom-chat", "max", {}),
        ("chat_completions", "custom-chat", "max", {}),
        (
            "responses",
            "gpt-5.4",
            "xhigh",
            {"reasoning": {"effort": "xhigh", "summary": "auto"}},
        ),
        ("chat_completions", "gpt-5.4", "xhigh", {"reasoning_effort": "xhigh"}),
        ("responses", "o4-mini", "unknown", {"reasoning": {"summary": "auto"}}),
        ("chat_completions", "o4-mini", "unknown", {}),
    ],
)
async def test_reasoning_effort_preserves_protocol_and_model_guards(
    wire_api, model, effort, expected
) -> None:
    assert (
        await _send_request(wire_api=wire_api, model=model, effort=effort) == expected
    )


def _fast_turn_kwargs(**explicit_overrides) -> dict:
    return _build_request_extra_kwargs(
        execution_path="fast",
        tools=None,
        tool_use_policy=None,
        runtime_context=None,
        extra_kwargs=explicit_overrides,
        model_request_override_builder=build_model_request_overrides,
    )


@pytest.mark.behavioral
@pytest.mark.parametrize("wire_api", _WIRE_APIS)
@pytest.mark.parametrize("stream", [False, True], ids=["sync", "stream"])
@pytest.mark.parametrize(
    ("configured_effort", "expected_effort"),
    [("max", "max"), ("none", "none"), (None, "low"), ("unknown", "low")],
    ids=["configured-max", "configured-none", "missing", "invalid"],
)
async def test_fast_text_turn_defaults_only_without_valid_active_protocol_effort(
    wire_api, stream, configured_effort, expected_effort
) -> None:
    actual = await _send_request(
        wire_api=wire_api,
        model=_MODEL,
        effort=configured_effort,
        stream=stream,
        **_fast_turn_kwargs(),
    )
    if wire_api == "responses":
        assert actual == {"reasoning": {"effort": expected_effort}}
    else:
        assert actual == {"reasoning_effort": expected_effort}


@pytest.mark.behavioral
@pytest.mark.parametrize(
    ("wire_api", "expected"),
    [
        ("responses", {"reasoning": {"effort": "high"}}),
        ("chat_completions", {"reasoning_effort": "high"}),
    ],
)
async def test_explicit_call_override_still_wins_over_configuration_and_fast_default(
    wire_api, expected
) -> None:
    actual = await _send_request(
        wire_api=wire_api,
        model=_MODEL,
        effort="max",
        **_fast_turn_kwargs(_runtime_reasoning_effort_override="high"),
    )
    assert actual == expected
