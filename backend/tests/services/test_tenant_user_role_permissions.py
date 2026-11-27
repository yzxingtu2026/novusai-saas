"""Behavioral coverage for tenant-user permission aggregation."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.base_model import Base
from app.enums.rbac import PermissionScope, PermissionType
from app.models import Permission, Tenant, TenantUser
from app.models.auth.tenant_user_role import TenantUserRole
from app.rbac.services.permission_service import PermissionService


@pytest.fixture
def role_db():
    engine = create_engine("sqlite://")
    table_names = (
        "permissions",
        "tenant_plans",
        "tenants",
        "tenant_org_nodes",
        "tenant_user_roles",
        "tenant_user_role_permissions",
        "tenant_users",
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            Base.metadata.create_all(
                connection, tables=[Base.metadata.tables[name] for name in table_names]
            )
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role_fields, expected_codes, expected_ids",
    [
        pytest.param({}, {"fixture:read"}, {21}, id="active-local-role"),
        pytest.param({"tenant_id": 12}, set(), set(), id="other-tenant-role"),
        pytest.param({"is_deleted": True}, set(), set(), id="deleted-role"),
        pytest.param({"is_active": False}, set(), set(), id="inactive-role"),
        pytest.param(None, set(), set(), id="no-role"),
    ],
)
async def test_tenant_user_permission_codes_and_ids_share_role_scope(
    role_db, role_fields, expected_codes, expected_ids
):
    role_db.add_all(
        [
            Tenant(id=11, name="Local fixture", code="local"),
            Tenant(id=12, name="Other fixture", code="other"),
        ]
    )
    if role_fields is not None:
        grants = [
            Permission(
                id=grant_id,
                name=action,
                code=f"fixture:{action}",
                type=PermissionType.OPERATION.value,
                resource="fixture",
                action=action,
                **fields,
            )
            for grant_id, action, fields in (
                (21, "read", {"scope": PermissionScope.USER.value}),
                (22, "admin", {"scope": PermissionScope.ADMIN.value}),
                (23, "tenant", {"scope": PermissionScope.TENANT.value}),
                (24, "both", {"scope": PermissionScope.BOTH.value}),
                (
                    25,
                    "disabled",
                    {"scope": PermissionScope.USER.value, "is_enabled": False},
                ),
                (
                    26,
                    "deleted",
                    {"scope": PermissionScope.USER.value, "is_deleted": True},
                ),
            )
        ]
        role_db.add(
            TenantUserRole(
                id=31,
                name="Fixture role",
                code="fixture",
                permissions=grants,
                **({"tenant_id": 11} | role_fields),
            )
        )
    role_db.commit()

    async def execute(statement):
        return role_db.execute(statement)

    service = PermissionService(SimpleNamespace(execute=execute))
    user = TenantUser(tenant_id=11, role_id=31 if role_fields is not None else None)
    assert await service.get_tenant_user_permissions(user) == expected_codes
    assert await service.get_tenant_user_effective_permission_ids(user) == expected_ids
