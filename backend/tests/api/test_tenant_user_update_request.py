"""企业用户部分更新校验 / Tenant user partial update validation.

Test type: behavioral.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.tenant import users
from app.core.deps import get_current_active_tenant_admin, get_db
from app.models.tenant.tenant_user import TenantUser

pytestmark = pytest.mark.asyncio

USER_PATH = "/tenant/users/7"


@pytest.fixture
async def user_update_client(monkeypatch):
    db = Mock()
    update_user = AsyncMock(
        return_value=TenantUser(
            id=7,
            tenant_id=5,
            username="fictional-member",
            approval_status="approved",
            is_active=True,
            gender=2,
        )
    )
    service_factory = Mock(return_value=SimpleNamespace(update_user=update_user))
    monkeypatch.setattr(users, "TenantUserService", service_factory)

    app = FastAPI()
    app.include_router(users.router, prefix="/tenant")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_active_tenant_admin] = lambda: SimpleNamespace(
        tenant_id=5
    )

    @app.middleware("http")
    async def bind_permissions(request, call_next):
        request.state.user_permissions = {"tenant_user:update"}
        return await call_next(request)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, db, service_factory, update_user


@pytest.mark.parametrize("field", ("is_active", "gender"))
async def test_managed_user_update_rejects_explicit_null_before_service(
    user_update_client, field
) -> None:
    client, _db, service_factory, update_user = user_update_client
    response = await client.put(
        USER_PATH, json={field: None, "nickname": "Rejected update"}
    )

    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["detail"]] == [["body", field]]
    service_factory.assert_not_called()
    update_user.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nickname": "Partial update"},
        {"is_active": False, "gender": 0},
        dict.fromkeys(
            ("email", "phone", "nickname", "avatar", "role_id", "org_node_id")
        ),
    ],
    ids=("empty-update", "omit-required", "falsy-values", "clear-nullable-fields"),
)
async def test_managed_user_update_passes_only_supplied_fields(
    user_update_client, payload
) -> None:
    client, db, service_factory, update_user = user_update_client
    response = await client.put(USER_PATH, json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    service_factory.assert_called_once_with(db, 5)
    update_user.assert_awaited_once_with(7, payload)
