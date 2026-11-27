"""Behavioral tests for the host-only storage billing bridge / 存储账单宿主调用回归。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.plugins import storage_billing_execution as billing
from app.plugins.exceptions import PluginSecurityError
from app.plugins.host_read_facade import HostReadFacade
from app.storage import StorageConfig
from tests.integration.storage_migration_test_support import create_model_tables
from tests.integration.storage_migration_test_support import (
    owned_storage as storage_fixture,
)

owned_storage = storage_fixture



def _request():
    day = date(2026, 1, 8)
    return {
        "driver_code": "aliyun-oss",
        "billing_date": day,
        "period_type": "daily",
        "period_start": day,
        "period_end": day,
    }


@pytest.mark.asyncio
async def test_registered_billing_adapter_only_receives_host_credentials(monkeypatch):
    """Exercise the granted host call with a capture adapter / 用捕获适配器校验宿主授权。"""
    request = _request()
    provider = request["driver_code"]
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                one_or_none=lambda: ({"providers": {provider: {"enabled": True}}}, {})
            )
        )
    )
    host = HostReadFacade(
        db,
        plugin_name="storage-billing",
        granted_capabilities=("platform:read", "storage:credential_use"),
    )
    monkeypatch.setattr(
        host,
        "get_plugin_runtime_summary",
        AsyncMock(
            side_effect=lambda driver_codes: [
                {"name": code, "enabled": code == provider} for code in driver_codes
            ]
        ),
    )
    monkeypatch.setattr(
        billing.StorageConfigResolver,
        "resolve_platform_config",
        AsyncMock(
            return_value=StorageConfig(
                driver=provider,
                root_path="synthetic",
                options={
                    "region": "synthetic-region",
                    "access_key_id": "synthetic-key-id",
                    "access_key_secret": "synthetic-secret",
                },
            )
        ),
    )
    providers = billing.load_plugin_module("storage-billing", "providers")

    async def fetch(supplied):
        assert supplied.billing_date == request["billing_date"]
        assert supplied.profile["access_key_secret"] == "synthetic-secret"
        return providers.BillingFetchResult(
            provider_code=provider,
            driver_code=provider,
            billing_date=supplied.billing_date,
            source_status="empty",
            raw_payload_json={
                "access_key_secret": "synthetic-secret",
                "row_count": 0,
            },
        )

    spy = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(
        providers,
        "get_provider_adapter",
        lambda _: SimpleNamespace(fetch_official_bill=spy),
    )
    result = await host.execute_storage_billing_fetch(request)
    assert result.source_status == "empty"
    assert result.raw_payload_json == {"access_key_secret": "***", "row_count": 0}
    spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_billing_host_requires_fixed_plugin_grant():
    """Use the ordinary capability gate / 核对普通插件能力门。"""
    host = HostReadFacade(
        None,
        plugin_name="storage-billing",
        granted_capabilities=("platform:read",),
    )
    with pytest.raises(PluginSecurityError, match="explicit plugin capability grants"):
        await host.execute_storage_billing_fetch(_request())


@pytest.mark.behavioral
@pytest.mark.asyncio
async def test_factory_preserves_authenticated_tenant_billing_without_platform_access(
    owned_storage, monkeypatch
):
    """Use the production context factory, binding and ORM with a synthetic storage config."""
    from app.models import Plugin, Tenant, TenantDomain
    from app.plugins.context import RequestContext
    from app.plugins.context_factory import create_plugin_context
    from app.plugins.loader import PluginLoader

    module = billing.load_plugin_module("storage-billing", "services.binding_service")
    assert module is not None
    manifest = PluginLoader().load_manifest("storage-billing")
    await create_model_tables(
        owned_storage, Tenant, TenantDomain, Plugin, module.StorageTenantBinding
    )
    storage = StorageConfig(
        driver="local",
        root_path="fictional",
        options={"secret_key": "testing-private-value"},
    )
    monkeypatch.setattr(
        billing.StorageConfigResolver,
        "resolve_context",
        AsyncMock(return_value=("platform", storage, True)),
    )
    monkeypatch.setattr(
        billing.StorageConfigResolver,
        "resolve_platform_config",
        AsyncMock(return_value=storage),
    )
    async with owned_storage.factory() as db:
        db.add_all([
            Tenant(id=21, code="alpha", name="Alpha"),
            Tenant(id=22, code="beta", name="Beta"),
            Plugin(
                name=manifest.name,
                display_name="Fixture",
                scope=manifest.scope,
                version=manifest.version,
                manifest=manifest.model_dump(),
            ),
        ])
        await db.commit()
        ctx = create_plugin_context(
            manifest.name,
            manifest,
            db,
            manifest.capabilities,
            RequestContext(tenant_id=21, user_id=31, user_role="tenant_admin"),
        )
        with pytest.raises(PluginSecurityError, match="platform administrator"):
            await ctx.host.get_platform_storage_context()
        service = module.StorageBillingBindingService.from_context(ctx)
        with pytest.raises(PluginSecurityError, match="current tenant"):
            await service.get_tenant_prerequisites(22)
        result = await service.get_tenant_prerequisites(21)
        assert result["tenant_id"] == 21
        assert result["bindings"]["total"] == 0
        assert result["prerequisites"]["missing_reasons"] == [
            "plan_feature_disabled",
            "current_driver_not_billable",
        ]
        assert result["platform_storage_context"]["storage_config"]["options"] == {
            "secret_key": "***"
        }
