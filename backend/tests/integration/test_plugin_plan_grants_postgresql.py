"""Behavioral: the plugin disable lifecycle persists the intended plan state."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import Permission, Plugin, TenantPlan
from app.models.tenant.tenant_plan import tenant_plan_permissions
from app.plugins.lifecycle import PluginLifecycle
from tests.integration.storage_migration_test_support import create_model_tables
from tests.integration.storage_migration_test_support import (
    owned_storage as storage_fixture,
)

owned_storage = storage_fixture
pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]


@pytest.mark.parametrize("preserve", [False, True])
async def test_disable_lifecycle_keeps_plan_grants_during_archive_mutations(
    owned_storage, monkeypatch, preserve
):
    await create_model_tables(owned_storage, Plugin, Permission, TenantPlan)
    async with owned_storage.engine.begin() as connection:
        await connection.run_sync(tenant_plan_permissions.create)

    async with owned_storage.factory() as db:
        db.add_all(
            [
                Plugin(
                    id=11,
                    name="demo-plugin",
                    display_name="Demo",
                    version="1.0.0",
                    scope="all_tenants",
                    status="enabled",
                ),
                TenantPlan(id=21, code="fixture", name="Fixture plan"),
                Permission(
                    id=31,
                    code="menu:tenant.plugin_demo_plugin_usage",
                    name="Usage",
                    type="menu",
                    scope="tenant",
                    resource="menu",
                    action="usage",
                ),
            ]
        )
        await db.flush()
        await db.execute(
            tenant_plan_permissions.insert().values(plan_id=21, permission_id=31)
        )
        await db.commit()

        lifecycle = PluginLifecycle(db)
        monkeypatch.setattr(lifecycle, "_deactivate_plugin_skill_records", AsyncMock())
        monkeypatch.setattr(lifecycle, "_deactivate_plugin_task_definitions", AsyncMock())
        await lifecycle._disable_impl(11, preserve_plan_grants=preserve)
        await db.commit()

    async with owned_storage.factory() as db:
        assert await db.scalar(select(Plugin.status).where(Plugin.id == 11)) == "disabled"
        assert await db.scalar(
            select(Permission.is_enabled).where(Permission.id == 31)
        ) is False
        grants = (
            await db.execute(
                select(tenant_plan_permissions.c.plan_id, tenant_plan_permissions.c.permission_id)
            )
        ).all()
        assert grants == ([(21, 31)] if preserve else [])
