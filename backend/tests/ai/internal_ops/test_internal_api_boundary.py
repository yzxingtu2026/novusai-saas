"""Test type: behavioral. Exercise internal operations with isolated transports."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.ai.engine.tool_processor_messages import check_confirmation_output
from app.ai.internal_ops.catalog import InternalOperation
from app.ai.internal_ops.proxy_token import issue_ai_proxy_token
from app.ai.internal_ops.tools import (
    TOOL_INVOKE_OPERATION,
    build_internal_ops_tool_definitions,
)
from app.ai.tools.executors import internal_api_executor
from app.ai.tools.types import ExecutionContext
from app.core import redis as redis_manager

pytestmark = pytest.mark.behavioral

_OPERATION_PATH = "/admin/_internal_api_fixture/items/{item_id}"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _ConfirmationStore:
    """In-memory sentinel store with the single-key Redis delete contract."""

    def __init__(self):
        self.values: dict[str, str] = {}

    async def setex(self, key, _seconds, value):
        self.values[key] = value

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = []
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded.append(len(chunk))
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def invoke_fixture(monkeypatch):
    def make(handler, *, method="GET", user_id=19):
        operation = InternalOperation(
            operation_id=f"{method}:{_OPERATION_PATH}",
            method=method,
            path=_OPERATION_PATH,
            scope="admin",
            module="fixture",
            action="list" if method == "GET" else "update",
            permission_code="fixture:access",
        )
        monkeypatch.setattr(
            internal_api_executor,
            "get_operation",
            lambda operation_id: (
                operation if operation_id == operation.operation_id else None
            ),
        )
        client_settings = []
        real_client = httpx.AsyncClient

        def client_for_fixture(**kwargs):
            client_settings.append(
                (kwargs.get("trust_env"), kwargs["follow_redirects"])
            )
            return real_client(
                **{**kwargs, "base_url": "http://self.fixture.invalid"},
                transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr(
            internal_api_executor.httpx, "AsyncClient", client_for_fixture
        )
        definition = next(
            tool
            for tool in build_internal_ops_tool_definitions()
            if tool.name == TOOL_INVOKE_OPERATION
        )
        context = ExecutionContext(
            tenant_id=0,
            agent_id=11,
            user_id=user_id,
            user_role="platform_admin",
            permissions={"fixture:access"},
            conversation_id=13,
        )
        executor = internal_api_executor.InternalApiExecutor()

        async def invoke(**arguments):
            return await executor.execute(
                definition,
                "fixture-call",
                {
                    "operation_id": operation.operation_id,
                    "path_params": {"item_id": 7},
                    **arguments,
                },
                context,
            )

        return invoke, client_settings

    return make


@pytest.mark.parametrize(
    ("path_params", "message"),
    [
        ({"item_id": None}, "must be a scalar"),
        ({"item_id": []}, "must be a scalar"),
        ({"item_id": "x" * 2049}, "invalid"),
        ({"item_id": "7", "extra": "value"}, "Unknown path params"),
        ({"extra": "value"}, "Missing path params"),
    ],
)
async def test_invalid_path_arguments_stop_before_transport(
    invoke_fixture, path_params, message
):
    def endpoint(_request):
        pytest.fail("Invalid route fixture was forwarded")

    invoke, clients = invoke_fixture(endpoint)
    result = await invoke(path_params=path_params)

    assert result.success is False
    assert message in result.error
    assert clients == []


async def test_valid_path_and_query_use_the_configured_transport_policy(invoke_fixture):
    def endpoint(request):
        assert request.url.path == "/admin/_internal_api_fixture/items/示例 7"
        assert request.url.params["view"] == "detail"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"record": 7})

    invoke, clients = invoke_fixture(endpoint)
    result = await invoke(
        path_params={"item_id": "示例 7"}, query_params={"view": "detail"}
    )

    assert result.success is True
    assert json.loads(result.output)["body"] == {"record": 7}
    assert clients == [(False, False)]


async def test_only_an_explicit_preview_opens_a_single_confirmation(
    monkeypatch, invoke_fixture
):
    store = _ConfirmationStore()
    monkeypatch.setattr(redis_manager, "get_redis_client", lambda: store)
    submitted = []

    def endpoint(request):
        submitted.append((request.method, json.loads(request.content)))
        return httpx.Response(200, json={"updated": 7})

    invoke, _ = invoke_fixture(endpoint, method="POST")
    args = {"body": {"label": "sample"}}

    missing = await invoke(confirmed=True, **args)
    assert missing.success is False
    assert (
        missing.error
        == "Internal API confirmation is missing, expired, or already used. Request a new preview."
    )
    assert check_confirmation_output(missing) is None
    assert store.values == {}
    assert submitted == []

    preview = await invoke(**args)
    assert check_confirmation_output(preview)["preview"]["body"] == args["body"]
    attempts = await asyncio.gather(*(invoke(confirmed=True, **args) for _ in range(3)))
    assert sum(result.success for result in attempts) == 1
    assert [result.error for result in attempts if not result.success] == [
        missing.error
    ] * 2
    assert all(check_confirmation_output(result) is None for result in attempts)
    assert submitted == [("POST", {"label": "sample"})]
    assert store.values == {}

    fresh = await invoke(**args)
    assert check_confirmation_output(fresh)["preview"]["body"] == args["body"]
    result = await invoke(confirmed=True, **args)
    assert json.loads(result.output)["body"] == {"updated": 7}
    assert submitted == [("POST", {"label": "sample"})] * 2


@pytest.mark.parametrize("announced_size", [False, True])
async def test_large_response_stops_and_closes_http_stream(
    invoke_fixture, announced_size
):
    half = b"x" * (_MAX_RESPONSE_BYTES // 2)
    body = _ResponseStream([half, half, b"x", b"unread"])

    def endpoint(_request):
        headers = {}
        if announced_size:
            headers["content-length"] = str(_MAX_RESPONSE_BYTES + 1)
        return httpx.Response(200, headers=headers, stream=body)

    invoke, _ = invoke_fixture(endpoint)
    result = await invoke()

    assert result.success is False
    assert result.error == "Internal API response too large"
    assert body.yielded == ([] if announced_size else [len(half), len(half), 1])
    assert body.closed is True


async def test_text_response_retains_decoding_and_display_limit(invoke_fixture):
    def endpoint(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=iso-8859-1"},
            content=b"\xe9" * 2002,
        )

    invoke, _ = invoke_fixture(endpoint)
    result = await invoke()

    assert result.success is True
    assert json.loads(result.output)["body"] == "é" * 2000


@pytest.mark.parametrize("user_id", [0, -1, True, 1.5])
def test_proxy_signer_requires_a_positive_integer_subject(user_id):
    with pytest.raises(ValueError, match="user_id must be a positive integer"):
        issue_ai_proxy_token(
            user_id=user_id,
            user_role="platform_admin",
            tenant_id=None,
            agent_id=11,
            conversation_id=13,
        )
