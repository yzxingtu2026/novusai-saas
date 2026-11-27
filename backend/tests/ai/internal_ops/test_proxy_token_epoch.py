"""Test type: behavioral. Check signed proxy claims over an isolated HTTP transport."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.ai.internal_ops.proxy_token import issue_ai_proxy_token
from app.ai.tools.executors import internal_api_executor
from app.ai.tools.executors.internal_api_executor import InternalApiExecutor
from app.ai.tools.types import ExecutionContext
from app.core.security import TENANT_SESSION_EPOCH_CLAIM, _decode_token_no_blacklist
from app.enums.common import UserRoleEnum

pytestmark = pytest.mark.behavioral
_TENANT_ROLES = (UserRoleEnum.TENANT_ADMIN.value, UserRoleEnum.TENANT_USER.value)


@pytest.mark.parametrize("user_role", _TENANT_ROLES)
def test_tenant_proxy_token_requires_a_current_epoch(user_role):
    with pytest.raises(ValueError, match="tenant_session_epoch"):
        issue_ai_proxy_token(
            user_id=19,
            user_role=user_role,
            tenant_id=7,
            agent_id=11,
            conversation_id=13,
        )


@pytest.mark.parametrize("user_role", _TENANT_ROLES)
@pytest.mark.parametrize("epoch", [0, 5, None])
async def test_internal_request_carries_the_queried_tenant_epoch(
    monkeypatch, user_role, epoch
):
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: epoch)
    claims_seen = []
    real_client = httpx.AsyncClient

    def endpoint(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/fixture"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = _decode_token_no_blacklist(token)
        claims_seen.append(
            (
                claims["sub"],
                claims["scope"],
                claims["tenant_id"],
                claims.get(TENANT_SESSION_EPOCH_CLAIM),
                claims["ai_proxy"]["on_behalf_of"],
            )
        )
        return httpx.Response(200, json={"success": True})

    def client_for_fixture(**_kwargs):
        return real_client(
            base_url="http://self.fixture.invalid",
            transport=httpx.MockTransport(endpoint),
            trust_env=False,
        )

    monkeypatch.setattr(internal_api_executor.httpx, "AsyncClient", client_for_fixture)
    result = await InternalApiExecutor()._perform_http_call(
        tool_call_id="fixture-call",
        context=ExecutionContext(
            tenant_id=7, agent_id=11, user_id=19, user_role=user_role, db=db
        ),
        op=SimpleNamespace(method="GET", operation_id="fixture.read"),
        resolved_path="/fixture",
        query_params={},
        body=None,
    )

    if epoch is None:
        assert result.success is False
        assert (
            result.error == "Internal API call denied: tenant session validation failed"
        )
        assert claims_seen == []
    else:
        assert result.success is True
        assert json.loads(result.output)["body"] == {"success": True}
        assert claims_seen == [("19", user_role, 7, epoch, 19)]
