"""Behavioral contracts for plugin request scope and grants / 插件请求作用域和授权回归。"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from app.configs.service import ConfigService
from app.core.deps import (
    get_current_active_admin,
    get_current_active_tenant_admin,
    get_db,
)
from app.middleware.tenant import TenantMiddleware
from app.models import Admin, Plugin, Tenant, TenantAdmin, TenantDomain, TenantPlan
from app.models.system.resource_tenant_assignment import ResourceTenantAssignment
from app.plugins import api_dispatcher
from app.plugins.context import PluginContext, RequestContext
from app.plugins.exceptions import PluginSecurityError
from app.plugins.loader import PluginLoader
from app.plugins.manifest import PluginManifest
from app.plugins.registry import ExtensionRegistry
from app.rbac.services.permission_service import PermissionService
from app.services.common.storage_config_resolver import StorageConfigResolver
from app.storage import StorageConfig
from app.storage.manager import storage_manager


class _LocalSession:
    """Run real ORM queries in memory / 在本机内存中执行真实 ORM 查询。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)


def _manifest(name: str, scope: str = "admin_and_selected_tenants") -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": name,
            "version": "1.0.0",
            "display_name": {"en": name},
            "scope": scope,
            "extensions": {
                "api": {
                    "public_routes": [
                        {
                            "path": "entries/{entry_id}",
                            "handler": "api.read",
                            "auth": "none",
                        },
                        {"path": "account", "handler": "api.read", "auth": "required"},
                        {
                            "path": "report",
                            "handler": "api.read",
                            "auth": "none",
                            "permission": "catalog:reports:read",
                        },
                    ],
                    "tenant_routes": [
                        {
                            "path": "desk",
                            "handler": "api.read",
                            "permission": "catalog:desk:read",
                        }
                    ],
                }
            },
        }
    )


def _add_plugin(
    session: Session,
    plugin_id: int,
    manifest: PluginManifest,
    config: dict | None = None,
) -> None:
    session.add(
        Plugin(
            id=plugin_id,
            name=manifest.name,
            display_name=manifest.name,
            version=manifest.version,
            scope=manifest.scope,
            status="enabled",
            manifest=manifest.model_dump(),
            config=config or {},
            granted_capabilities=manifest.capabilities,
        )
    )


@pytest.fixture
def local_db():
    engine = create_engine("sqlite://")
    Plugin.metadata.create_all(
        engine,
        tables=[
            Admin.__table__,
            TenantPlan.__table__,
            Tenant.__table__,
            TenantDomain.__table__,
            TenantAdmin.__table__,
            Plugin.__table__,
            ResourceTenantAssignment.__table__,
        ],
    )
    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id=21, code="alpha", name="Alpha"),
                Tenant(id=22, code="beta", name="Beta"),
                Tenant(id=23, code="paused", name="Paused", is_active=False),
                TenantDomain(
                    tenant_id=21, domain="catalog.example.org", is_verified=True
                ),
                Admin(
                    id=61,
                    username="platform",
                    email="platform@example.test",
                    password_hash="",
                ),
                Admin(
                    id=62,
                    username="paused",
                    email="paused@example.test",
                    password_hash="",
                    is_active=False,
                ),
            ]
        )
        for user_id, tenant_id in ((71, 21), (72, 22), (73, 23), (74, 21), (75, 21)):
            session.add(
                TenantAdmin(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"member-{user_id}",
                    email=f"member-{user_id}@example.test",
                    password_hash="",
                    is_active=user_id != 74,
                    is_deleted=user_id == 75,
                )
            )
        _add_plugin(session, 31, _manifest("scoped"), {"title": "Scoped catalog"})
        _add_plugin(session, 32, _manifest("platform-only", "admin_only"))
        _add_plugin(session, 33, _manifest("all-tenant", "all_tenants"))
        session.add_all(
            [
                ResourceTenantAssignment(
                    resource_type="plugin",
                    resource_id=31,
                    tenant_id=21,
                    config={"label": "Alpha"},
                ),
                ResourceTenantAssignment(
                    resource_type="plugin",
                    resource_id=31,
                    tenant_id=22,
                    config={"label": "Beta"},
                    is_active=False,
                ),
                ResourceTenantAssignment(
                    resource_type="plugin", resource_id=31, tenant_id=23
                ),
            ]
        )
        session.commit()
        yield _LocalSession(session)
    engine.dispose()


@pytest.fixture
def router_app(local_db, monkeypatch):
    @asynccontextmanager
    async def session_factory():
        yield local_db

    async def request_db():
        return local_db

    monkeypatch.setattr("app.middleware.tenant.async_session_factory", session_factory)
    monkeypatch.setattr(
        "app.middleware.tenant.settings.TENANT_DOMAIN_SUFFIX", ".example.test"
    )
    monkeypatch.setattr(api_dispatcher.settings, "DEBUG", False)
    app = FastAPI()
    app.add_middleware(TenantMiddleware)
    app.include_router(api_dispatcher.plugin_api_router, prefix="/admin")
    app.include_router(api_dispatcher.plugin_public_api_router, prefix="/api/public")
    app.include_router(api_dispatcher.plugin_tenant_api_router, prefix="/tenant")
    app.dependency_overrides[get_db] = request_db
    return app


async def test_public_routes_use_resolved_scope_and_recheck_assignment(
    router_app, local_db, monkeypatch
) -> None:
    calls = []

    async def read(request, ctx):
        data = {
            "entry": request.path_params.get("entry_id"),
            "tenant": ctx.get_current_tenant_id(),
            "actor": ctx.get_current_user_id(),
        }
        calls.append(data)
        return data

    monkeypatch.setattr(api_dispatcher, "load_plugin_handler", lambda *_: read)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router_app)
    ) as client:
        alpha = "http://alpha.example.test"
        platform = "http://example.test"
        base = "/api/public/plugins"
        path = f"{base}/scoped/api/entries/édition-7"

        bound = await client.get(f"{alpha}{path}")
        assert bound.status_code == 200
        assert bound.json()["data"] == {
            "entry": "édition-7",
            "tenant": 21,
            "actor": None,
        }
        custom = await client.get(f"http://catalog.example.org{path}")
        assert custom.status_code == 200
        assert custom.json()["data"] == bound.json()["data"]

        for url in (
            f"http://beta.example.test{path}",
            f"{platform}{path}",
            f"{platform}{base}/all-tenant/api/entries/7",
            f"{alpha}{base}/scoped/api/account",
            f"{alpha}{base}/scoped/api/report",
            f"{alpha}{base}/platform-only/api/entries/7",
        ):
            denied = await client.get(url)
            assert (denied.status_code, denied.json()["code"]) == (404, 4040)

        unbound = await client.get(f"{platform}{base}/platform-only/api/entries/7")
        assert unbound.status_code == 200
        assert unbound.json()["data"] == {"entry": "7", "tenant": None, "actor": None}

        await local_db.execute(
            update(ResourceTenantAssignment)
            .where(
                ResourceTenantAssignment.resource_id == 31,
                ResourceTenantAssignment.tenant_id == 21,
            )
            .values(is_active=False)
        )
        denied = await client.get(f"{alpha}{path}")
        assert (denied.status_code, denied.json()["code"]) == (404, 4040)

        await local_db.execute(
            update(Plugin).where(Plugin.id == 32).values(status="disabled")
        )
        disabled = await client.get(f"{platform}{base}/platform-only/api/entries/7")
        assert (disabled.status_code, disabled.json()["code"]) == (404, 4040)

    assert calls == [
        bound.json()["data"],
        custom.json()["data"],
        unbound.json()["data"],
    ]


async def test_tenant_router_keeps_compound_permissions_and_weather_config(
    router_app, local_db, monkeypatch
) -> None:
    async def active_admin():
        return local_db.session.get(TenantAdmin, 71)

    router_app.dependency_overrides[get_current_active_tenant_admin] = active_admin
    registry = ExtensionRegistry()
    registry.register_permission(
        "scoped", "catalog:desk", scope="tenant", actions=["read"]
    )
    weather = PluginLoader().load_manifest("weather-widget")
    _add_plugin(local_db.session, 34, weather, {"default_city": "Chengdu"})
    local_db.session.add(
        ResourceTenantAssignment(resource_type="plugin", resource_id=34, tenant_id=21)
    )
    local_db.session.flush()
    for permission in weather.extensions.permissions:
        registry.register_permission(
            weather.name,
            permission.code,
            scope=permission.scope,
            actions=permission.actions,
        )
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)
    grants = {"plugin.scoped.catalog:desk:read", "plugin.weather-widget.weather:view"}
    monkeypatch.setattr(
        PermissionService,
        "get_tenant_admin_permissions",
        AsyncMock(return_value=grants),
    )
    real_loader = api_dispatcher.load_plugin_handler

    async def read(ctx):
        return {
            "tenant": ctx.get_current_tenant_id(),
            "actor": ctx.get_current_user_id(),
        }

    def load(name, target):
        return read if name == "scoped" else real_loader(name, target)

    monkeypatch.setattr(api_dispatcher, "load_plugin_handler", load)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router_app),
        base_url="http://alpha.example.test",
    ) as client:
        desk = await client.get("/tenant/plugins/scoped/api/desk")
        assert desk.status_code == 200
        assert desk.json()["data"] == {"tenant": 21, "actor": 71}
        weather_response = await client.get("/tenant/plugins/weather-widget/api/config")
        assert weather_response.status_code == 200
        assert weather_response.json()["data"] == {
            "config": {"default_city": "Chengdu"}
        }

        grants.remove("plugin.scoped.catalog:desk:read")
        denied = await client.get("/tenant/plugins/scoped/api/desk")
        assert (denied.status_code, denied.json()["code"]) == (403, 4030)


@pytest.mark.parametrize(
    ("actor", "tenant_id", "expected"),
    [
        (RequestContext(21, 71, "tenant_admin"), 21, {"label": "Alpha"}),
        (RequestContext(21, 71, "tenant_admin"), 22, None),
        (RequestContext(21), 21, None),
        (RequestContext(21, 71, "tenant_user"), 21, None),
        (RequestContext(None, 61, "admin"), 21, {"label": "Alpha"}),
        (RequestContext(21, 61, "admin"), 22, None),
        (None, 21, {"label": "Alpha"}),
        (None, 22, {}),
    ],
)
async def test_config_grant_and_tenant_binding(
    local_db, actor, tenant_id, expected
) -> None:
    manifest = _manifest("scoped")
    no_grant = PluginContext("scoped", manifest, local_db, request_context=actor)
    with pytest.raises(PluginSecurityError, match="config:read"):
        await no_grant.get_config()
    with pytest.raises(PluginSecurityError, match="config:read"):
        await no_grant.get_tenant_config(tenant_id)

    context = PluginContext("scoped", manifest, local_db, ["config:read"], actor)
    assert await context.get_config() == {"title": "Scoped catalog"}
    if expected is None:
        with pytest.raises(PluginSecurityError):
            await context.get_tenant_config(tenant_id)
    else:
        assert await context.get_tenant_config(tenant_id) == expected


async def test_host_reads_remain_authorized_after_public_tenant_binding(
    local_db,
) -> None:
    manifest = PluginLoader().load_manifest("storage-billing")
    assert {"config:read", "platform:read", "storage:credential_use"} <= set(
        manifest.capabilities
    )
    context = PluginContext(
        manifest.name,
        manifest,
        local_db,
        manifest.capabilities,
        RequestContext(21, 71, "tenant_admin"),
    )
    snapshot = await context.host.get_tenant_plan_snapshot(21)
    assert snapshot == {
        "tenant_id": 21,
        "tenant_code": "alpha",
        "tenant_name": "Alpha",
        "plan_id": None,
        "plan": None,
    }
    with pytest.raises(PluginSecurityError, match="current tenant"):
        await context.host.get_tenant_plan_snapshot(22)
    with pytest.raises(PluginSecurityError, match="platform administrator"):
        await context.host.get_platform_storage_context()

    public = PluginContext(
        manifest.name,
        manifest,
        local_db,
        [*manifest.capabilities, "notifications:send"],
        RequestContext(21),
    )
    with pytest.raises(PluginSecurityError, match="authenticated user"):
        await public.host.get_tenant_plan_snapshot(21)
    with pytest.raises(PluginSecurityError, match="authenticated user"):
        await public.send_notification(21, [71], "plugin.storage-billing.ready")


async def test_platform_billing_profile_handler_preserves_own_config(
    router_app, local_db, monkeypatch
) -> None:
    async def active_admin():
        return local_db.session.get(Admin, 61)

    router_app.dependency_overrides[get_current_active_admin] = active_admin
    manifest = PluginLoader().load_manifest("storage-billing")
    profile_code = "billing-fixture"
    _add_plugin(
        local_db.session,
        35,
        manifest,
        {"providers": {"qiniu-kodo": {"enabled": True, "profile_code": profile_code}}},
    )
    local_db.session.flush()
    registry = ExtensionRegistry()
    registry.register_permission(
        "storage-billing", "billing_admin", scope="admin", actions=["view"]
    )
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)
    monkeypatch.setattr(
        PermissionService,
        "get_admin_permissions",
        AsyncMock(return_value={"plugin.storage-billing.billing_admin:view"}),
    )
    monkeypatch.setattr(
        StorageConfigResolver,
        "resolve_platform_config",
        AsyncMock(return_value=StorageConfig(driver="local", root_path="fixture")),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router_app), base_url="http://example.test"
    ) as client:
        response = await client.get(
            "/admin/plugins/storage-billing/api/provider-profiles"
        )

    assert response.status_code == 200
    profile = response.json()["data"]["providers"]["qiniu-kodo"]
    assert profile["profile_code"] == profile_code
    assert profile["storage_context"]["current_driver"] == "local"


async def test_storage_operations_enforce_their_own_grants_and_preserve_options(
    monkeypatch,
) -> None:
    driver = SimpleNamespace(
        put=AsyncMock(return_value="stored"),
        get=AsyncMock(return_value=b"render"),
        delete=AsyncMock(return_value=True),
        exists=AsyncMock(return_value=True),
        get_info=AsyncMock(return_value={"size": 6}),
        get_url=AsyncMock(return_value="https://assets.example.test/render.png"),
    )
    monkeypatch.setattr(
        ConfigService,
        "get_platform_config",
        AsyncMock(side_effect=lambda _key, default: default),
    )
    monkeypatch.setattr(storage_manager, "get_driver", lambda _: driver)
    manifest = _manifest("scoped")
    reader = await PluginContext(
        "scoped", manifest, None, ["storage:read"]
    ).get_storage()
    writer = await PluginContext(
        "scoped", manifest, None, ["storage:write"]
    ).get_storage()
    path = "renders/card.png"
    scoped_path = f"plugins/scoped/{path}"

    with pytest.raises(PluginSecurityError, match="storage:write"):
        await reader.put(path, b"render")
    with pytest.raises(PluginSecurityError, match="storage:write"):
        await reader.delete(path)
    for method in (writer.get, writer.exists, writer.get_info, writer.get_url):
        with pytest.raises(PluginSecurityError, match="storage:read"):
            await method(path)
    for operation in vars(driver).values():
        operation.assert_not_awaited()

    assert await reader.get(path) == b"render"
    assert await reader.exists(path) is True
    assert await reader.get_info(path) == {"size": 6}
    assert await reader.get_url(
        path, expires=80, response_content_type="image/png"
    ) == ("https://assets.example.test/render.png")
    assert (
        await writer.put(
            path, b"render", mime_type="image/png", cache_control="private"
        )
        == "stored"
    )
    assert await writer.delete(path) is True
    driver.get.assert_awaited_once_with(scoped_path)
    driver.get_url.assert_awaited_once_with(
        scoped_path, expires=80, response_content_type="image/png"
    )
    driver.put.assert_awaited_once_with(
        scoped_path, b"render", mime_type="image/png", cache_control="private"
    )
    driver.delete.assert_awaited_once_with(scoped_path)


@pytest.mark.parametrize(
    ("actor", "user_id", "side", "allowed"),
    [
        (RequestContext(21, 71, "tenant_admin"), 71, "tenant", True),
        (RequestContext(21, 71, "tenant_admin"), 72, "tenant", False),
        (RequestContext(21, 71, "tenant_admin"), 61, "admin", False),
        (RequestContext(None, 61, "admin"), 61, "admin", True),
        (RequestContext(None, 61, "admin"), 62, "admin", False),
        (RequestContext(None, 61, "admin"), 71, "tenant", True),
        (RequestContext(None, 61, "admin"), 72, "tenant", False),
        (RequestContext(None, 61, "admin"), 73, "tenant", False),
        (RequestContext(None, 61, "admin"), 74, "tenant", False),
        (RequestContext(None, 61, "admin"), 75, "tenant", False),
        (RequestContext(21), 71, "tenant", False),
        (None, 71, "tenant", True),
        (None, 72, "tenant", False),
        (None, 61, "admin", True),
        (None, 71, "user", False),
    ],
)
async def test_push_uses_active_recipient_and_its_runtime_gate(
    local_db, monkeypatch, actor, user_id, side, allowed
) -> None:
    emit = AsyncMock()
    monkeypatch.setitem(
        sys.modules,
        "app.core.socketio_server",
        SimpleNamespace(get_sio=lambda: SimpleNamespace(emit=emit)),
    )
    context = PluginContext(
        "scoped", _manifest("scoped"), local_db, ["notifications:send"], actor
    )
    if allowed:
        assert (
            await context.push_to_user(user_id, "ready", {"state": "done"}, side=side)
            is True
        )
        emit.assert_awaited_once_with(
            "plugin.scoped.ready",
            {"state": "done", "plugin": "scoped", "event": "ready"},
            room=f"user:{user_id}",
            namespace=f"/{side}",
        )
    else:
        with pytest.raises(PluginSecurityError):
            await context.push_to_user(user_id, "ready", side=side)
        emit.assert_not_awaited()
