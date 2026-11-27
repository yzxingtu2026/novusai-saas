"""Exercise tenant identity and browser-origin checks against persisted domains."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
import socketio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core import cors
from app.core.config import settings
from app.middleware import tenant
from app.middleware.socketio_origin import SocketIOOriginMiddleware
from app.models import Tenant, TenantDomain
from app.models.tenant.tenant_plan import TenantPlan

_DSN = os.getenv("TENANT_INGRESS_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="Disposable PostgreSQL required")


@pytest_asyncio.fixture
async def registered_domains(monkeypatch):
    url = make_url(_DSN)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "127.0.0.1"
    assert url.database == "platform_storage"
    schema = "tenant_ingress_" + uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as connection:
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            for table in (
                TenantPlan.__table__,
                Tenant.__table__,
                TenantDomain.__table__,
            ):
                await connection.run_sync(table.create)
        async with sessions.begin() as db:
            db.add_all(
                [
                    Tenant(id=11, name="Active", code="active"),
                    Tenant(id=12, name="Other", code="other"),
                    Tenant(id=13, name="Inactive", code="inactive", is_active=False),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    TenantDomain(
                        tenant_id=11,
                        domain="Custom.Fixture.Test.",
                        is_verified=True,
                    ),
                    TenantDomain(
                        tenant_id=11,
                        domain="not-verified.fixture.test",
                    ),
                    TenantDomain(
                        tenant_id=13,
                        domain="disabled.fixture.test",
                        is_verified=True,
                    ),
                ]
            )
        monkeypatch.setattr(settings, "APP_ENV", "production")
        monkeypatch.setattr(settings, "PLATFORM_DOMAINS", "api.app.fixture.test")
        monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".app.fixture.test")
        monkeypatch.setattr(settings, "CORS_ORIGINS", "")
        monkeypatch.setattr(tenant, "async_session_factory", sessions)
        monkeypatch.setattr(cors, "async_session_factory", sessions)
        yield
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()


@pytest.mark.asyncio
async def test_tenant_resolution_through_mounted_asgi_boundary(registered_domains):
    async def route(request: Request):
        context = tenant.get_tenant_context(request)
        return JSONResponse({"tenant": context.tenant_id})

    application = Starlette(routes=[Route("/tenant/fixture", route)])
    application.add_middleware(tenant.TenantMiddleware)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://fixture.test"
    ) as client:
        for host, origin, expected in (
            ("Active.App.Fixture.Test.:443", None, 11),
            ("custom.fixture.test", None, 11),
            ("api.app.fixture.test", "https://custom.fixture.test", 11),
            ("api.app.fixture.test", "https://inactive.app.fixture.test", None),
            ("active.app.fixture.test", "https://other.app.fixture.test", None),
            ("unregistered.fixture.test", "https://active.app.fixture.test", None),
        ):
            headers = {"host": host}
            if origin is not None:
                headers["origin"] = origin
            response = await client.get("/tenant/fixture", headers=headers)
            assert (response.status_code, response.json()) == (
                200,
                {"tenant": expected},
            )

        response = await client.get(
            "/tenant/fixture",
            headers={
                "host": "api.app.fixture.test",
                "origin": "https://active.app.fixture.test/path",
            },
        )
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_registered_origins_control_http_and_socket_tenant_identity(
    registered_domains,
):
    for origin, allowed in (
        ("https://active.app.fixture.test", True),
        ("https://custom.fixture.test", True),
        ("https://inactive.app.fixture.test", False),
        ("https://unknown.app.fixture.test", False),
        ("https://not-verified.fixture.test", False),
        ("https://disabled.fixture.test", False),
        ("http://active.app.fixture.test", False),
        ("https://active.app.fixture.test:8080", False),
        ("https://active.app.fixture.test/", False),
    ):
        assert await cors.is_origin_allowed(origin) is allowed, origin

    from app.sio.tenant_boundary import socket_matches_authenticated_tenant

    for host, origin, expected in (
        ("api.app.fixture.test", None, True),
        ("api.app.fixture.test", "https://custom.fixture.test", True),
        ("active.app.fixture.test", "https://other.app.fixture.test", False),
        ("unregistered.fixture.test", "https://active.app.fixture.test", False),
    ):
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        environ = {"asgi.scope": {"headers": headers}}
        assert await socket_matches_authenticated_tenant(environ, 11) is expected


@pytest.mark.asyncio
async def test_engineio_handshake_uses_this_requests_persisted_origin(
    registered_domains,
):
    server = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=cors.is_origin_allowed_sync,
    )
    application = SocketIOOriginMiddleware(
        socketio.ASGIApp(server, socketio_path="/sio")
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="https://api.app.fixture.test",
        ) as client:
            valid = await client.get(
                "/sio/?EIO=4&transport=polling",
                headers={"Origin": "https://active.app.fixture.test"},
            )
            assert valid.status_code == 200
            assert valid.text.startswith('0{"sid":')
            assert (
                valid.headers["access-control-allow-origin"]
                == "https://active.app.fixture.test"
            )

            unknown = await client.get(
                "/sio/?EIO=4&transport=polling",
                headers={"Origin": "https://unknown.app.fixture.test"},
            )
            assert unknown.status_code == 400
            assert unknown.json() == {"detail": "Origin is not allowed"}
    finally:
        await server.shutdown()
