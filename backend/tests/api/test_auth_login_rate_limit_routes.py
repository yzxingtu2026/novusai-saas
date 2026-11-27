"""登录限流在认证之前检查 / Login limits run before authentication.

Test type: behavioral.
"""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.admin import auth as admin_auth
from app.api.tenant import auth as tenant_auth
from app.api.user import auth as user_auth
from app.core import rate_limit
from app.core.config import settings
from app.core.deps import get_db
from app.exceptions import AppException

pytestmark = pytest.mark.asyncio

LOGIN = {"username": "fixture", "password": "FictionalPassword42!"}
EMAIL = {"channel": "email", "email": "member@fixture.invalid"}
CLIENT_IP = "192.0.2.33"


@pytest.fixture
async def login_client(monkeypatch):
    counter = AsyncMock(return_value=10)
    monkeypatch.setattr(rate_limit, "cache_increment", counter)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.42.0.0/16")
    auth_call = AsyncMock(
        return_value={
            "access_token": "fictional-access",
            "refresh_token": "fictional-refresh",
            "token_type": "bearer",
        }
    )
    send_code = AsyncMock(return_value={"sent": True})
    factory = Mock(
        return_value=SimpleNamespace(
            authenticate_admin=auth_call,
            tenant_admin_auth=SimpleNamespace(authenticate=auth_call),
            authenticate_tenant_user=auth_call,
            authenticate_tenant_user_by_code=auth_call,
            send_tenant_user_login_code=send_code,
        )
    )
    db = SimpleNamespace(commit=AsyncMock())
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    for prefix, module in (
        ("/admin", admin_auth),
        ("/tenant", tenant_auth),
        ("/api/user", user_auth),
    ):
        monkeypatch.setattr(module, "AuthService", factory)
        app.include_router(module.router, prefix=prefix)

    @app.exception_handler(AppException)
    async def app_exception_handler(_request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("10.42.0.8", 43210)),
        base_url="http://test",
        headers={"X-Forwarded-For": f"198.51.100.56, {CLIENT_IP}, 10.42.0.11"},
    ) as client:
        yield client, db, factory, auth_call, send_code, counter


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/admin/auth/login", {"json": LOGIN}),
        ("/tenant/auth/login", {"json": LOGIN}),
        ("/api/user/auth/login", {"data": LOGIN}),
        ("/api/user/auth/login/json", {"json": LOGIN}),
        ("/api/user/auth/login-code/send", {"json": EMAIL}),
        ("/api/user/auth/login-code/login", {"json": {**EMAIL, "code": "123456"}}),
    ],
    ids=("admin", "tenant", "user-form", "user-json", "user-send-code", "user-code"),
)
async def test_login_routes_check_shared_limit_before_authenticating(
    login_client, path, body
) -> None:
    client, db, factory, auth_call, send_code, counter = login_client

    allowed = await client.post(path, **body)
    counter.return_value = 11
    limited = await client.post(path, **body)
    counter.side_effect = RedisConnectionError("isolated fixture")
    unavailable = await client.post(path, **body)

    assert allowed.status_code == 200, allowed.text
    assert limited.status_code == 429, limited.text
    assert limited.headers["retry-after"] == "60"
    assert (limited.json()["code"], limited.json()["retry_after"]) == (4290, 60)
    assert unavailable.status_code == 503, unavailable.text
    assert unavailable.json()["code"] == 5030

    digest = sha256(CLIENT_IP.encode()).hexdigest()
    assert counter.await_args_list == [call(f"auth:login_rate:{digest}", ttl=60)] * 3
    factory.assert_called_once_with(db)
    if path.endswith("/send"):
        send_code.assert_awaited_once()
        assert send_code.await_args.kwargs["client_ip"] == CLIENT_IP
        auth_call.assert_not_awaited()
        db.commit.assert_not_awaited()
    else:
        auth_call.assert_awaited_once()
        assert auth_call.await_args.kwargs["client_ip"] == CLIENT_IP
        send_code.assert_not_awaited()
        db.commit.assert_awaited_once()
