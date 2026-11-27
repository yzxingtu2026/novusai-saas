"""平台企业写请求 / Platform tenant write requests.

Test type: behavioral.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin import tenants
from app.core.config import settings
from app.core.deps import get_current_active_admin, get_db

pytestmark = pytest.mark.asyncio

TENANTS_PATH = "/admin/tenants"
TENANT_ID = 7
UPDATE_PATH = f"{TENANTS_PATH}/{TENANT_ID}"
CREATE_PAYLOAD = {
    "name": "Fixture tenant",
    "admin_username": "fictional-owner",
    "admin_email": "owner@fixture.invalid",
    "admin_password": "FictionalPassword42!",
}
NULLABLE_FIELDS = (
    "contact_name",
    "contact_phone",
    "contact_email",
    "plan_id",
    "quota",
    "expires_at",
    "remark",
)


@pytest.fixture
async def tenant_write_client(monkeypatch):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = SimpleNamespace(
        id=TENANT_ID,
        code="fictional-tenant",
        name=CREATE_PAYLOAD["name"],
        is_active=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    service = SimpleNamespace(
        create_tenant=AsyncMock(return_value=result),
        update_tenant=AsyncMock(return_value=result),
    )
    db = SimpleNamespace(commit=AsyncMock())
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(tenants, "TenantService", service_factory)

    app = FastAPI()
    app.include_router(tenants.AdminTenantController.get_router(), prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_active_admin] = lambda: SimpleNamespace(id=1)

    @app.middleware("http")
    async def bind_permissions(request, call_next):
        request.state.user_permissions = {"tenant:create", "tenant:update"}
        return await call_next(request)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, db, service_factory


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"name": "Renamed tenant", "code": "read-only"}, {"name": "Renamed tenant"}),
        (dict.fromkeys(NULLABLE_FIELDS), dict.fromkeys(NULLABLE_FIELDS)),
        ({"remark": None, "quota": {}}, {"remark": None, "quota": {}}),
    ],
    ids=("omissions-and-read-only-code", "explicit-null-fields", "empty-quota"),
)
async def test_update_projects_only_supplied_mutable_fields(
    tenant_write_client, body, expected
) -> None:
    client, db, service_factory = tenant_write_client
    response = await client.put(UPDATE_PATH, json=body)

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    service_factory.assert_called_once_with(db)
    service_factory.return_value.update_tenant.assert_awaited_once_with(
        TENANT_ID, expected
    )
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("name", (None, "", "   "), ids=("null", "empty", "blank"))
async def test_update_rejects_invalid_name_before_writing(
    tenant_write_client, name
) -> None:
    client, db, service_factory = tenant_write_client
    response = await client.put(UPDATE_PATH, json={"name": name, "remark": None})

    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "name"]]
    service_factory.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("method", ("POST", "PUT"))
@pytest.mark.parametrize(
    ("wire_value", "platform_timezone", "offset_minutes"),
    [
        ("2030-12-21 13:14:15", "Asia/Shanghai", 480),
        ("2030-12-21 13:14:15", "UTC", 0),
        ("2030-12-21T13:14:15", "Asia/Shanghai", 480),
        ("2030-12-21", "Asia/Shanghai", 480),
        ("2030-12-21T13:14:15+05:30", "Asia/Shanghai", 330),
        ("2030-12-21T13:14:15-04:00", "Asia/Shanghai", -240),
    ],
)
async def test_tenant_write_passes_native_expiry_with_offset(
    tenant_write_client,
    monkeypatch,
    method,
    wire_value,
    platform_timezone,
    offset_minutes,
) -> None:
    client, db, service_factory = tenant_write_client
    monkeypatch.setattr(settings, "TIMEZONE", platform_timezone)
    service = service_factory.return_value

    if method == "POST":
        response = await client.post(
            TENANTS_PATH, json={**CREATE_PAYLOAD, "expires_at": wire_value}
        )
        assert response.status_code == 200, response.text
        service.create_tenant.assert_awaited_once()
        expiry = service.create_tenant.await_args.kwargs["expires_at"]
    else:
        response = await client.put(UPDATE_PATH, json={"expires_at": wire_value})
        assert response.status_code == 200, response.text
        service.update_tenant.assert_awaited_once()
        expiry = service.update_tenant.await_args.args[1]["expires_at"]

    assert isinstance(expiry, datetime)
    assert expiry.utcoffset() == timedelta(minutes=offset_minutes)
    assert expiry.replace(tzinfo=None) == datetime.fromisoformat(wire_value).replace(
        tzinfo=None
    )
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("expiry", ({}, {"expires_at": None}), ids=("omitted", "null"))
async def test_create_accepts_optional_expiry(tenant_write_client, expiry) -> None:
    client, db, service_factory = tenant_write_client
    response = await client.post(TENANTS_PATH, json={**CREATE_PAYLOAD, **expiry})

    assert response.status_code == 200, response.text
    create_tenant = service_factory.return_value.create_tenant
    create_tenant.assert_awaited_once()
    assert create_tenant.await_args.kwargs["expires_at"] is None
    db.commit.assert_awaited_once()
