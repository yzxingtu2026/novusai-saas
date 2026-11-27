"""用本地 PostgreSQL 和 Redis 验证租户会话 / Verify tenant sessions with local services.

Test type: behavioral. All identities, schemas, and tokens are disposable fixtures.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from socketio import AsyncNamespace
from socketio.exceptions import ConnectionRefusedError as SocketRefused
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.api.admin.auth import router as admin_auth
from app.api.tenant.auth import router as tenant_auth
from app.api.user.auth import router as user_auth
from app.configs.service import ConfigService
from app.core import database, deps, socketio_server
from app.core.base_model import Base
from app.core.config import settings
from app.core.redis import RedisManager
from app.core.security import (
    ACTIVE_TOKENS_PREFIX,
    REFRESH_TOKEN_FAMILY_CLAIM,
    REFRESH_TOKEN_FAMILY_REVOKED_PREFIX,
    REFRESH_TOKEN_ROTATION_PREFIX,
    TENANT_SESSION_EPOCH_CLAIM,
    TOKEN_BLACKLIST_PREFIX,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
)
from app.exceptions.base import AppException
from app.middleware import tenant as tenant_middleware
from app.models import (
    Admin,
    SystemAgentAssignment,
    Tenant,
    TenantAdmin,
    TenantDomain,
    TenantPlan,
    TenantUser,
)
from app.plugins.sio_auth import PluginAuthNamespaceWrapper
from app.services.common.auth_service import AuthService
from app.services.system.tenant_service import TenantService
from app.sio import tenant_revocation
from app.sio.presence import PresenceManager
from app.sio.session_manager import SessionValidatedRedisManager
from app.sio.socket_trace import (
    register_tenant_socket_session,
    socket_token_is_current,
    tenant_socket_session_is_current,
)
from app.sio.tenant_ns import TenantNamespace
from app.sio.user_ns import UserNamespace

_PASSWORD = "FictionalTenantPassword42!"


def _fixture_tables():
    selected = {
        model.__table__
        for model in (
            Admin,
            SystemAgentAssignment,
            Tenant,
            TenantAdmin,
            TenantDomain,
            TenantPlan,
            TenantUser,
        )
    }
    while True:
        dependencies = {
            foreign_key.column.table
            for table in selected
            for foreign_key in table.foreign_keys
        }
        if dependencies <= selected:
            return list(selected)
        selected |= dependencies


@pytest_asyncio.fixture
async def epoch_fixture(monkeypatch):
    dsn = os.environ.get("PLATFORM_FIXTURE_POSTGRES_DSN")
    redis_url = os.environ.get("PLATFORM_FIXTURE_REDIS_URL")
    if not dsn or not redis_url:
        pytest.skip("Explicit disposable loopback PostgreSQL and Redis are required")
    url = make_url(dsn)
    redis_target = make_url(redis_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or not (url.username and url.database)
        or url.query
        or redis_target.drivername != "redis"
        or redis_target.host != "127.0.0.1"
        or redis_target.query
    ):
        pytest.fail(
            "Tenant epoch tests require disposable loopback fixtures", pytrace=False
        )
    schema = f"tenant_epoch_{uuid4().hex}"
    identity_id = uuid4().int % 1_000_000_000 + 1_000_000
    control = create_async_engine(url)
    async with control.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
            is False
        )
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        async with Redis.from_url(redis_url, decode_responses=True) as redis:
            await redis.ping()
            monkeypatch.setattr(RedisManager, "_client", redis)
            monkeypatch.setattr(database, "async_session_factory", factory)
            monkeypatch.setattr(tenant_middleware, "async_session_factory", factory)
            monkeypatch.setattr(
                settings, "PLATFORM_DOMAINS", "epoch-api.fixture.invalid"
            )
            disconnect = AsyncMock()
            monkeypatch.setattr(
                socketio_server,
                "get_sio",
                lambda: SimpleNamespace(disconnect=disconnect, emit=AsyncMock()),
            )
            try:
                yield engine, factory, redis, identity_id, disconnect
            finally:
                await redis.delete(
                    f"{ACTIVE_TOKENS_PREFIX}admin:{identity_id}",
                    f"{ACTIVE_TOKENS_PREFIX}tenant_admin:{identity_id}",
                    f"{ACTIVE_TOKENS_PREFIX}tenant_user:{identity_id}",
                    f"socketio:tenant-sessions:{identity_id}",
                    f"sio_rate:tenant_admin:{identity_id}",
                    f"sio_rate:tenant_user:{identity_id}",
                    f"sio_rate:plugin_epoch_fixture_tenant_user:{identity_id}",
                )
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()


async def _seed_accounts(engine, factory, identity_id):
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=_fixture_tables())
        )
    password_hash = get_password_hash(_PASSWORD)
    async with factory.begin() as db:
        db.add(Tenant(id=identity_id, name="Epoch Test", code="epoch-test"))
        await db.flush()
        db.add_all(
            (
                Admin(
                    id=identity_id,
                    username="PlatformAdmin",
                    password_hash=password_hash,
                    email="platform@epoch.fixture.invalid",
                ),
                TenantAdmin(
                    id=identity_id,
                    tenant_id=identity_id,
                    username="TenantAdmin",
                    password_hash=password_hash,
                    email="admin@epoch.fixture.invalid",
                ),
                TenantUser(
                    id=identity_id,
                    tenant_id=identity_id,
                    username="TenantUser",
                    password_hash=password_hash,
                    email="user@epoch.fixture.invalid",
                ),
            )
        )


def _application(factory):
    app = FastAPI()
    app.include_router(admin_auth, prefix="/api/admin")
    app.include_router(tenant_auth, prefix="/api/tenant")
    app.include_router(user_auth, prefix="/api/user")

    async def db_for_request():
        async with factory.begin() as db:
            yield db

    app.dependency_overrides[deps.get_db] = db_for_request

    @app.exception_handler(AppException)
    async def app_exception_handler(_request, error):
        return JSONResponse({"code": error.code}, status_code=error.status_code)

    @app.get("/probe/admin")
    async def read_admin(account: deps.ActiveAdmin):
        return {"id": account.id}

    @app.get("/probe/tenant")
    async def read_tenant(account: deps.ActiveTenantAdmin):
        return {"id": account.id}

    @app.get("/probe/user")
    async def read_user(account: deps.ActiveTenantUser):
        return {"id": account.id}

    return app


@pytest.mark.asyncio
@pytest.mark.behavioral
@pytest.mark.parametrize("surface", ["admin", "tenant", "user"])
async def test_refresh_is_single_use_and_logout_revokes_only_its_family(
    epoch_fixture, monkeypatch, surface
):
    """Verify normal session rotation and revocation in a disposable auth stack."""
    engine, factory, redis, identity_id, _disconnect = epoch_fixture
    await _seed_accounts(engine, factory, identity_id)

    async def tenant_config(_self, _tenant, key, default=None):
        if key in {"tenant_captcha_enabled", "user_login_captcha_enabled"}:
            return False
        return default

    async def platform_config(_self, key, default=None):
        return False if key == "admin_captcha_enabled" else default

    monkeypatch.setattr(ConfigService, "get_tenant_config", tenant_config)
    monkeypatch.setattr(ConfigService, "get_platform_config", platform_config)
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 2 * 24 * 60)
    monkeypatch.setattr(settings, "REFRESH_TOKEN_EXPIRE_DAYS", 1)
    minimum_revocation_ttl = 2 * 86400 - 10
    keys = set()

    async def observe_pair(pair):
        access = await decode_token(pair["access_token"])
        refresh = await decode_token(pair["refresh_token"])
        family = access[REFRESH_TOKEN_FAMILY_CLAIM]
        assert refresh[REFRESH_TOKEN_FAMILY_CLAIM] == family
        keys.update(
            {
                f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{family}",
                f"{REFRESH_TOKEN_ROTATION_PREFIX}{refresh['jti']}",
                f"{TOKEN_BLACKLIST_PREFIX}{access['jti']}",
                f"{TOKEN_BLACKLIST_PREFIX}{refresh['jti']}",
            }
        )
        return {
            "token_jti": access["jti"],
            "token_exp": access["exp"],
            REFRESH_TOKEN_FAMILY_CLAIM: family,
        }

    async def login():
        async with factory() as db:
            service = AuthService(db)
            if surface == "admin":
                pair = await service.authenticate_admin("PlatformAdmin", _PASSWORD)
            elif surface == "tenant":
                pair = await service.authenticate_tenant_admin(
                    "TenantAdmin", _PASSWORD, tenant_code="epoch-test"
                )
            else:
                pair = await service.authenticate_tenant_user(
                    "TenantUser", _PASSWORD, tenant_code="epoch-test"
                )
            await db.commit()
            return pair

    def bearer(pair):
        return {"Authorization": f"Bearer {pair['access_token']}"}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=_application(factory)),
            base_url="http://epoch-api.fixture.invalid",
        ) as client:
            refresh_path = f"/api/{surface}/auth/refresh"
            probe_path = f"/probe/{surface}"
            first, second = await login(), await login()
            first_session, second_session = (
                await observe_pair(first),
                await observe_pair(second),
            )
            assert (
                first_session[REFRESH_TOKEN_FAMILY_CLAIM]
                != second_session[REFRESH_TOKEN_FAMILY_CLAIM]
            )
            source = {"refresh_token": first["refresh_token"]}
            exchange = await client.post(refresh_path, json=source)
            assert exchange.status_code == 200
            rotated = exchange.json()["data"]
            child_session = await observe_pair(rotated)
            family = first_session[REFRESH_TOKEN_FAMILY_CLAIM]
            assert child_session[REFRESH_TOKEN_FAMILY_CLAIM] == family
            admitted = await client.get(probe_path, headers=bearer(rotated))
            assert admitted.status_code == 200 and admitted.json() == {
                "id": identity_id
            }
            assert await socket_token_is_current(child_session)
            assert (await client.post(refresh_path, json=source)).status_code == 401
            assert (
                await redis.ttl(f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{family}")
                >= minimum_revocation_ttl
            )
            assert (
                await client.get(probe_path, headers=bearer(rotated))
            ).status_code == 401
            assert not await socket_token_is_current(child_session)

            other = await client.get(probe_path, headers=bearer(second))
            assert other.status_code == 200 and other.json() == {"id": identity_id}
            assert await socket_token_is_current(second_session)
            logout = await client.post(
                f"/api/{surface}/auth/logout", headers=bearer(second)
            )
            assert logout.status_code == 200
            other_family = second_session[REFRESH_TOKEN_FAMILY_CLAIM]
            assert (
                await redis.ttl(f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{other_family}")
                >= minimum_revocation_ttl
            )
            assert not await socket_token_is_current(second_session)
            invalid = await client.post(
                refresh_path, json={"refresh_token": second["refresh_token"]}
            )
            assert invalid.status_code == 401
    finally:
        if keys:
            await redis.delete(*keys)


@pytest.mark.asyncio
async def test_epoch_blocks_old_access_refresh_and_socket_sessions(
    epoch_fixture, monkeypatch
):
    engine, factory, redis, identity_id, disconnect = epoch_fixture
    await _seed_accounts(engine, factory, identity_id)

    async def tenant_config(_self, _tenant, key, default=None):
        if key in {"tenant_captcha_enabled", "user_login_captcha_enabled"}:
            return False
        return default

    async def platform_config(_self, _key, default=None):
        return default

    monkeypatch.setattr(ConfigService, "get_tenant_config", tenant_config)
    monkeypatch.setattr(ConfigService, "get_platform_config", platform_config)
    scopes = {"tenant": "tenant_admin", "user": "tenant_user"}
    credentials = {}
    tenant_claims = {"tenant_id": identity_id, TENANT_SESSION_EPOCH_CLAIM: 0}
    for surface, scope in scopes.items():
        credentials[surface] = {
            "access_token": create_access_token(
                identity_id, scope, extra_claims=tenant_claims
            )[0],
            "refresh_token": create_refresh_token(
                identity_id, scope, extra_claims=tenant_claims
            )[0],
        }
    payload = await decode_token(credentials["user"]["access_token"])
    session = {
        "user_type": "tenant_user",
        "user_id": identity_id,
        "tenant_id": identity_id,
        "token_jti": payload["jti"],
        "token_exp": payload["exp"],
        TENANT_SESSION_EPOCH_CLAIM: 0,
    }
    await register_tenant_socket_session(identity_id, "/user", "old-socket", 0)
    await assert_current_socket(session, expected=True)

    async with AsyncClient(
        transport=ASGITransport(app=_application(factory)),
        base_url="http://epoch-api.fixture.invalid",
    ) as client:
        for surface, pair in credentials.items():
            scope = scopes[surface]
            legacy_access = create_access_token(
                identity_id, scope, extra_claims={"tenant_id": identity_id}
            )[0]
            legacy_refresh = create_refresh_token(
                identity_id, scope, extra_claims={"tenant_id": identity_id}
            )[0]
            missing_epoch = await client.get(
                f"/probe/{surface}",
                headers={"Authorization": f"Bearer {legacy_access}"},
            )
            assert missing_epoch.status_code == 401
            legacy_refresh_denied = await client.post(
                f"/api/{surface}/auth/refresh",
                json={"refresh_token": legacy_refresh},
            )
            assert legacy_refresh_denied.status_code == 401
            response = await client.get(
                f"/probe/{surface}",
                headers={"Authorization": f"Bearer {pair['access_token']}"},
            )
            assert response.status_code == 200 and response.json() == {
                "id": identity_id
            }
            refreshed = await client.post(
                f"/api/{surface}/auth/refresh",
                json={"refresh_token": pair["refresh_token"]},
            )
            assert refreshed.status_code == 200
            rotated = refreshed.json()["data"]
            source = await decode_token(pair["refresh_token"])
            assert REFRESH_TOKEN_FAMILY_CLAIM not in source
            for token_type in ("access_token", "refresh_token"):
                claims = await decode_token(rotated[token_type])
                assert claims[TENANT_SESSION_EPOCH_CLAIM] == 0
                assert claims[REFRESH_TOKEN_FAMILY_CLAIM] == source["jti"]

        async with factory() as db:
            stopped = await TenantService(db).toggle_status(identity_id, False)
            assert stopped.session_revocation_epoch == 1
            await db.rollback()
        disconnect.assert_not_awaited()
        await assert_current_socket(session, expected=True)

        async with factory.begin() as db:
            stopped = await TenantService(db).toggle_status(identity_id, False)
            assert stopped.session_revocation_epoch == 1
        await asyncio.gather(*tuple(tenant_revocation._tasks))
        disconnect.assert_awaited_once_with("old-socket", namespace="/user")
        assert await redis.scard(f"socketio:tenant-sessions:{identity_id}") == 0
        await assert_current_socket(session, expected=False)

        async with factory.begin() as db:
            started = await TenantService(db).toggle_status(identity_id, True)
            assert started.session_revocation_epoch == 2
        await asyncio.gather(*tuple(tenant_revocation._tasks))
        for surface, pair in credentials.items():
            denied = await client.get(
                f"/probe/{surface}",
                headers={"Authorization": f"Bearer {pair['access_token']}"},
            )
            assert denied.status_code == 401
            refresh_denied = await client.post(
                f"/api/{surface}/auth/refresh",
                json={"refresh_token": pair["refresh_token"]},
            )
            assert refresh_denied.status_code == 401

        async with factory() as db:
            service = AuthService(db)
            tenant_pair = await service.authenticate_tenant_admin(
                "TenantAdmin", _PASSWORD, tenant_code="epoch-test"
            )
            user_pair = await service.authenticate_tenant_user(
                "TenantUser", _PASSWORD, tenant_code="epoch-test"
            )
            await db.commit()
        for surface, pair in (("tenant", tenant_pair), ("user", user_pair)):
            access = pair["access_token"]
            claims = await decode_token(access)
            assert claims[TENANT_SESSION_EPOCH_CLAIM] == 2
            current = await client.get(
                f"/probe/{surface}", headers={"Authorization": f"Bearer {access}"}
            )
            assert current.status_code == 200 and current.json()["id"] == identity_id
            refreshed = await client.post(
                f"/api/{surface}/auth/refresh",
                json={"refresh_token": pair["refresh_token"]},
            )
            assert refreshed.status_code == 200
            claims = await decode_token(refreshed.json()["data"]["refresh_token"])
            assert claims[TENANT_SESSION_EPOCH_CLAIM] == 2


async def assert_current_socket(session, *, expected):
    assert await tenant_socket_session_is_current(session) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "user", "plugin"])
async def test_socket_admission_matches_tenant_and_epoch(
    epoch_fixture, monkeypatch, surface
):
    engine, factory, redis, identity_id, _disconnect = epoch_fixture
    await _seed_accounts(engine, factory, identity_id)
    monkeypatch.setattr(
        "app.sio.ws_config.get_ws_configs",
        AsyncMock(return_value={"ws_enabled": True, "ws_max_connections_per_user": 1}),
    )
    delegate = AsyncNamespace("/epoch-fixture-plugin")
    delegate.on_connect = AsyncMock()
    if surface == "tenant":
        namespace = TenantNamespace("/tenant")
        scope, presence_scope = "tenant_admin", "tenant_admin"
    elif surface == "user":
        namespace = UserNamespace("/user")
        scope, presence_scope = "tenant_user", "tenant_user"
    else:
        namespace = PluginAuthNamespaceWrapper(
            delegate, "epoch_fixture", auth_scopes=["tenant_user"]
        )
        scope, presence_scope = "tenant_user", "plugin_epoch_fixture_tenant_user"
    namespace.get_session = AsyncMock(return_value=None)
    namespace.save_session = AsyncMock()
    namespace.enter_room = AsyncMock()
    namespace.emit = AsyncMock()
    epoch_socket_key = f"socketio:tenant-sessions:{identity_id}"
    allowed = {"HTTP_HOST": "epoch-api.fixture.invalid"}
    denied = {"HTTP_HOST": "unknown-tenant.fixture.invalid"}
    legacy_token = create_access_token(
        identity_id, scope, extra_claims={"tenant_id": identity_id}
    )[0]
    initial_token = create_access_token(
        identity_id,
        scope,
        extra_claims={"tenant_id": identity_id, TENANT_SESSION_EPOCH_CLAIM: 0},
    )[0]

    with pytest.raises(SocketRefused):
        await namespace.on_connect("cross-host", denied, {"token": initial_token})
    with pytest.raises(SocketRefused):
        await namespace.on_connect("legacy", allowed, {"token": legacy_token})
    namespace.enter_room.assert_not_awaited()
    delegate.on_connect.assert_not_awaited()
    assert await redis.scard(epoch_socket_key) == 0

    async with factory.begin() as db:
        tenant = await db.get(Tenant, identity_id)
        tenant.session_revocation_epoch = 2
    with pytest.raises(SocketRefused):
        await namespace.on_connect("stale", allowed, {"token": initial_token})
    namespace.enter_room.assert_not_awaited()
    delegate.on_connect.assert_not_awaited()
    assert await redis.scard(epoch_socket_key) == 0

    fresh_token = create_access_token(
        identity_id,
        scope,
        extra_claims={"tenant_id": identity_id, TENANT_SESSION_EPOCH_CLAIM: 2},
    )[0]
    valid_sid = f"valid-{identity_id}"
    await namespace.on_connect(valid_sid, allowed, {"token": fresh_token})
    try:
        assert namespace.enter_room.await_count == 2
        assert await redis.scard(epoch_socket_key) == 1
        assert (
            await PresenceManager.get_user_connection_count(
                presence_scope, identity_id, identity_id
            )
            == 1
        )
        with pytest.raises(SocketRefused):
            await namespace.on_connect("overflow", allowed, {"token": fresh_token})
        assert namespace.enter_room.await_count == 2

        session = namespace.save_session.await_args.args[1]
        manager = SessionValidatedRedisManager(
            os.environ["PLATFORM_FIXTURE_REDIS_URL"], write_only=True
        )
        manager.server = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
            namespace_handlers={namespace.namespace: namespace},
        )
        manager.get_participants = lambda *_args: [(valid_sid, "engineio-fixture")]
        assert (
            await manager._unavailable_recipients(
                "notification:new", namespace.namespace, f"user:{identity_id}", None
            )
            == []
        )
        async with factory.begin() as db:
            tenant = await db.get(Tenant, identity_id)
            tenant.session_revocation_epoch = 3
        assert await manager._unavailable_recipients(
            "notification:new", namespace.namespace, f"user:{identity_id}", None
        ) == [valid_sid]
    finally:
        await namespace.on_disconnect(valid_sid)
    assert await redis.scard(epoch_socket_key) == 0
    assert (
        await PresenceManager.get_user_connection_count(
            presence_scope, identity_id, identity_id
        )
        == 0
    )


@pytest.mark.asyncio
async def test_epoch_migration_preserves_existing_rows_and_zero_default(epoch_fixture):
    engine, _factory, _redis, _identity_id, _disconnect = epoch_fixture
    migration_path = (
        Path(__file__).resolve().parents[2] / "migrations/versions/platform_epoch_v1.py"
    )
    spec = importlib.util.spec_from_file_location(
        "platform_epoch_fixture", migration_path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_revision(connection):
        migration.op = Operations(MigrationContext.configure(connection))
        connection.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO tenants (id) VALUES (1)"))
        migration.upgrade()
        connection.execute(text("INSERT INTO tenants (id) VALUES (2)"))
        rows = connection.execute(
            text("SELECT id, session_revocation_epoch FROM tenants ORDER BY id")
        ).all()
        assert rows == [(1, 0), (2, 0)]
        migration.downgrade()
        assert [
            column["name"] for column in inspect(connection).get_columns("tenants")
        ] == ["id"]
        assert connection.execute(text("SELECT id FROM tenants ORDER BY id")).all() == [
            (1,),
            (2,),
        ]

    async with engine.begin() as connection:
        await connection.run_sync(run_revision)
