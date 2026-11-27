"""Behavioral: real user-role permission writes and reads in owned PostgreSQL.

Fictional identities and permissions live in one fresh private schema per case.
No authentication, external provider or production database is exercised.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

import app.models  # noqa: F401 -- register actual relationship targets.
from app.core.base_model import Base
from app.core.i18n import set_locale
from app.enums import ErrorCode
from app.exceptions import BusinessException, NotFoundException
from app.models.auth.admin_role import AdminRole
from app.models.auth.permission import Permission
from app.models.auth.tenant_admin_role import TenantAdminRole
from app.models.auth.tenant_user_role import TenantUserRole
from app.models.org.admin_org_node import (
    AdminOrgNode,
    AdminOrgScopePolicy,
    AdminOrgScopeTarget,
)
from app.models.org.tenant_org_node import (
    TenantOrgNode,
    TenantOrgScopePolicy,
    TenantOrgScopeTarget,
)
from app.models.system.admin import Admin
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_admin import TenantAdmin
from app.models.tenant.tenant_user import TenantUser
from app.rbac.services.permission_domains.aggregation import PermissionAggregationDomain
from app.services.tenant.tenant_user_role_service import TenantUserRoleService

pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("ROLE_PERMISSION_POSTGRES_DSN")
_INVALID_PERMISSIONS = (201, 202, 203, 204, 301, 302, 999)


def _tables():
    selected = {
        model.__table__
        for model in (
            Admin,
            AdminRole,
            AdminOrgNode,
            AdminOrgScopePolicy,
            AdminOrgScopeTarget,
            Permission,
            Tenant,
            TenantAdmin,
            TenantUser,
            TenantAdminRole,
            TenantUserRole,
            TenantOrgNode,
            TenantOrgScopePolicy,
            TenantOrgScopeTarget,
        )
    }
    selected |= {
        table
        for table in Base.metadata.tables.values()
        if table.name
        in {
            "admin_org_node_permissions",
            "admin_role_permissions",
            "tenant_org_node_permissions",
            "tenant_admin_role_permissions",
            "tenant_user_role_permissions",
        }
    }
    while True:
        dependencies = {
            key.column.table for table in selected for key in table.foreign_keys
        }
        if dependencies <= selected:
            return list(selected)
        selected |= dependencies


@pytest_asyncio.fixture
async def database():
    if not _DSN:
        pytest.skip("Explicit owned ROLE_PERMISSION_POSTGRES_DSN is required")
    url = make_url(_DSN)
    assert (url.drivername, url.host, url.database, url.username) == (
        "postgresql+asyncpg",
        "127.0.0.1",
        "org_tree_audit",
        "audit_fixture",
    )
    schema = "role_permissions_" + uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as connection:
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=_tables())
            )
        async with factory() as db:
            db.add(
                Tenant(
                    id=77,
                    name="Fictional enterprise",
                    code="fictional-77",
                    is_active=True,
                )
            )
            permissions = {}
            for permission_id, scope, enabled, deleted in (
                (101, "user", True, False),
                (102, "user", True, False),
                (201, "admin", True, False),
                (202, "tenant", True, False),
                (203, "both", True, False),
                (204, "unknown", True, False),
                (301, "user", False, False),
                (302, "user", True, True),
            ):
                permission = Permission(
                    id=permission_id,
                    name=f"Fictional permission {permission_id}",
                    code=f"fictional:{permission_id}",
                    scope=scope,
                    type="operation",
                    resource="fictional",
                    action=f"action-{permission_id}",
                    is_enabled=enabled,
                    is_deleted=deleted,
                )
                db.add(permission)
                permissions[permission_id] = permission
            for role_id, tenant_id in ((10, 77), (20, 88)):
                db.add(
                    TenantUserRole(
                        id=role_id,
                        tenant_id=tenant_id,
                        name=f"Existing role {role_id}",
                        code=f"existing-role-{role_id}",
                        permissions=[permissions[101]],
                    )
                )
            db.add(
                TenantUser(
                    id=500,
                    tenant_id=77,
                    username="fictional-role-user",
                    email="role-user@example.invalid",
                    password_hash="fictional-test-value",
                    role_id=10,
                )
            )
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()


async def _write(service, action, permission_ids):
    if action == "create":
        return await service.create_role(
            "New role", code="new-role", permission_ids=permission_ids
        )
    if action == "update":
        return await service.update_role(
            10, {"name": "Changed role", "permission_ids": permission_ids}
        )
    return await service.assign_permissions(10, permission_ids)


@pytest.mark.parametrize("action", ("create", "update", "assign"))
@pytest.mark.parametrize("invalid_id", _INVALID_PERMISSIONS)
async def test_invalid_permission_rejects_before_any_role_write(
    database, action, invalid_id
):
    async with database() as db:
        with pytest.raises(BusinessException) as caught:
            await _write(TenantUserRoleService(db, 77), action, [102, invalid_id])
        assert caught.value.code == ErrorCode.VALIDATION_ERROR
        assert caught.value.data == {"reason": "invalid_user_role_permissions"}
        # Deliberately commit the caller's still-valid transaction. A business
        # validation failure must not stage a new/partly updated role.
        await db.commit()
    async with database() as db:
        roles = list((await db.scalars(select(TenantUserRole))).all())
        assert {(role.id, role.name) for role in roles} == {
            (10, "Existing role 10"),
            (20, "Existing role 20"),
        }
        assert {role.id: [p.id for p in role.permissions] for role in roles} == {
            10: [101],
            20: [101],
        }


@pytest.mark.parametrize("action", ("create", "update", "assign"))
async def test_valid_user_permissions_are_persisted_once(database, action):
    async with database() as db:
        role = await _write(TenantUserRoleService(db, 77), action, [102, 101, 102])
        role_id = role.id
        assert sorted(permission.id for permission in role.permissions) == [101, 102]
        await db.commit()
    async with database() as db:
        role = await TenantUserRoleService(db, 77).get_by_id(role_id)
        assert role.tenant_id == 77
        assert sorted(permission.id for permission in role.permissions) == [101, 102]


@pytest.mark.parametrize("action", ("update", "assign"))
async def test_explicit_empty_list_clears_permissions(database, action):
    async with database() as db:
        await _write(TenantUserRoleService(db, 77), action, [])
        await db.commit()
    async with database() as db:
        role = await TenantUserRoleService(db, 77).get_by_id(10)
        assert role.permissions == []


async def test_omitted_permissions_preserve_existing_assignment(database):
    async with database() as db:
        role = await TenantUserRoleService(db, 77).update_role(
            10, {"description": "Changed description"}
        )
        assert [permission.id for permission in role.permissions] == [101]
        await db.commit()
    async with database() as db:
        role = await TenantUserRoleService(db, 77).get_by_id(10)
        assert role.description == "Changed description"
        assert [permission.id for permission in role.permissions] == [101]


@pytest.mark.parametrize("action", ("update", "assign"))
async def test_foreign_tenant_role_cannot_be_assigned(database, action):
    async with database() as db:
        service = TenantUserRoleService(db, 77)
        with pytest.raises(NotFoundException):
            if action == "update":
                await service.update_role(20, {"permission_ids": [102]})
            else:
                await service.assign_permissions(20, [102])
        await db.commit()
    async with database() as db:
        role = await TenantUserRoleService(db, 88).get_by_id(20)
        assert [permission.id for permission in role.permissions] == [101]


async def test_runtime_ignores_historical_wrong_scope_assignments(database):
    async with database() as db:
        role = await TenantUserRoleService(db, 77).get_by_id(10)
        role.permissions = list((await db.scalars(select(Permission))).all())
        await db.commit()
    async with database() as db:
        user = await db.get(TenantUser, 500)
        domain = PermissionAggregationDomain(db)
        assert await domain.get_tenant_user_permissions(user) == {
            "fictional:101",
            "fictional:102",
        }
        assert await domain.get_tenant_user_effective_permission_ids(user) == {
            101,
            102,
        }


@pytest.mark.parametrize("field", ("is_active", "is_deleted"))
async def test_runtime_retains_role_lifecycle_checks(database, field):
    async with database() as db:
        role = await TenantUserRoleService(db, 77).get_by_id(10)
        setattr(role, field, field == "is_deleted")
        await db.commit()
    async with database() as db:
        user = await db.get(TenantUser, 500)
        domain = PermissionAggregationDomain(db)
        assert await domain.get_tenant_user_permissions(user) == set()
        assert await domain.get_tenant_user_effective_permission_ids(user) == set()


async def test_runtime_foreign_tenant_role_remains_empty(database):
    async with database() as db:
        user = await db.get(TenantUser, 500)
        user.role_id = 20
        await db.commit()
    async with database() as db:
        user = await db.get(TenantUser, 500)
        domain = PermissionAggregationDomain(db)
        assert await domain.get_tenant_user_permissions(user) == set()
        assert await domain.get_tenant_user_effective_permission_ids(user) == set()


@pytest.mark.parametrize(
    ("locale", "message"),
    (
        (
            "zh_CN",
            "部分所选权限已失效或不适用于用户端，请刷新权限列表后重新选择。",
        ),
        (
            "en",
            "Some selected permissions are unavailable or do not belong to the user app. "
            "Refresh the permission list and select again.",
        ),
    ),
)
async def test_invalid_selection_has_localized_safe_reason(database, locale, message):
    set_locale(locale)
    async with database() as db:
        with pytest.raises(BusinessException) as caught:
            await TenantUserRoleService(db, 77).assign_permissions(10, [999])
        assert caught.value.message == message
        assert caught.value.data == {"reason": "invalid_user_role_permissions"}
        assert caught.value.debug is None
        assert caught.value.extra == {}


async def _role_detail_request(database, grants, *, include_options):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.tenant.user_roles import router
    from app.core.deps import get_current_active_tenant_admin, get_db

    app = FastAPI()

    @app.middleware("http")
    async def effective_grants(request, call_next):
        # The effective grants supplied to the real route wrapper are fixture input.
        request.state.user_permissions = grants
        return await call_next(request)

    async def local_db():
        async with database() as db:
            yield db

    async def local_admin():
        return TenantAdmin(id=7, tenant_id=77, username="fictional-role-admin")

    app.dependency_overrides[get_db] = local_db
    app.dependency_overrides[get_current_active_tenant_admin] = local_admin
    app.include_router(router, prefix="/api/tenant")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as client:
        return await client.get(
            "/api/tenant/user-roles/10",
            params={"include_permission_options": True} if include_options else None,
        )


async def test_assignment_options_include_only_live_user_permissions(database):
    response = await _role_detail_request(
        database,
        {"tenant_user_role:detail", "tenant_user_role:assign_permissions"},
        include_options=True,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["permission_ids"] == [101]
    assert {(p["id"], p["scope"]) for p in data["permission_options"]} == {
        (101, "user"),
        (102, "user"),
    }


@pytest.mark.parametrize(
    ("grants", "include_options", "expected_status"),
    [
        ({"tenant_user_role:detail"}, True, 403),
        ({"tenant_user_role:assign_permissions"}, True, 403),
        ({"tenant_user_role:detail"}, False, 200),
    ],
    ids=["missing-assign", "missing-detail", "ordinary-detail"],
)
async def test_assignment_options_preserve_existing_role_grant_boundaries(
    database, grants, include_options, expected_status
):
    response = await _role_detail_request(
        database, grants, include_options=include_options
    )

    assert response.status_code == expected_status
    if expected_status == 200:
        data = response.json()["data"]
        assert data["permission_ids"] == [101]
        assert "permission_options" not in data
