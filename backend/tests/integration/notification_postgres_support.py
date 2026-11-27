"""中文: 虚构通知测试的独占 PostgreSQL schema。 EN: Isolated fictional notification schema."""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.common.notification import Notification
from app.models.common.notification_delivery import NotificationDelivery
from app.models.common.notification_preference import NotificationPreference
from app.models.common.notification_template import NotificationTemplate
from app.models.system.admin import Admin
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_admin import TenantAdmin
from app.models.tenant.tenant_user import TenantUser

POSTGRES_DSN = os.getenv("PLATFORM_FIXTURE_POSTGRES_DSN")
if POSTGRES_DSN:
    fixture_url = make_url(POSTGRES_DSN)
    if (
        fixture_url.host != "127.0.0.1"
        or fixture_url.port is None
        or fixture_url.database != "platform_tasks"
        or fixture_url.drivername != "postgresql+asyncpg"
        or fixture_url.query
    ):
        raise ValueError(
            "Only the disposable platform_tasks PostgreSQL fixture is allowed"
        )


@asynccontextmanager
async def notification_database(*, additional_models=()):
    """中文: 仅允许已校验的回环临时数据库。 EN: Use the validated loopback fixture only."""
    schema = "notification_fixture_" + uuid4().hex
    async_engine = create_async_engine(POSTGRES_DSN)
    sync_engine = create_engine(POSTGRES_DSN.replace("+asyncpg", "+psycopg2"))

    def set_path(connection, _record):
        previous = connection.autocommit
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}", public')
        cursor.close()
        connection.autocommit = previous

    event.listen(async_engine.sync_engine, "connect", set_path)
    event.listen(sync_engine, "connect", set_path)
    try:
        async with async_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            for model in (
                Admin,
                Tenant,
                TenantAdmin,
                TenantUser,
                Notification,
                NotificationDelivery,
                NotificationTemplate,
                NotificationPreference,
                *additional_models,
            ):
                table = model.__table__
                await connection.execute(
                    text(
                        str(
                            CreateTable(
                                table, include_foreign_key_constraints=[]
                            ).compile(dialect=connection.dialect)
                        )
                    )
                )
                for index in table.indexes:
                    await connection.execute(
                        text(
                            str(CreateIndex(index).compile(dialect=connection.dialect))
                        )
                    )
        yield SimpleNamespace(
            sessions=async_sessionmaker(
                async_engine, expire_on_commit=False, autoflush=False
            ),
            sync_sessions=sessionmaker(
                sync_engine,
                class_=type("FixtureSession", (Session,), {}),
                expire_on_commit=False,
                autoflush=False,
            ),
            dsn=POSTGRES_DSN,
            schema=schema,
        )
    finally:
        async with async_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await async_engine.dispose()
        sync_engine.dispose()


def fake_notification_configuration(monkeypatch):
    """中文: 仅模拟配置和 broker，真实持久化不替换。 EN: Fake configuration and broker only, preserving real persistence."""
    from app.configs.service import ConfigService
    from app.services.common import notification_delivery_dispatch as dispatch

    deny_unfaked_notification_network(monkeypatch)

    values = {
        "notification_enabled": True,
        "notification_max_per_user": 0,
        "email_enabled": True,
        "email_smtp_host": "smtp.example.test",
        "email_smtp_port": 25,
        "email_smtp_encryption": "none",
        "email_from_address": "fixture-sender@example.test",
        "webhook_enabled": True,
        "webhook_url": "https://hooks.example.test/fixture",
    }

    async def get_platform(self, key, default=None):
        return values.get(key, default)

    async def get_tenant(self, tenant_id, key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(ConfigService, "get_platform_config", get_platform)
    monkeypatch.setattr(ConfigService, "get_tenant_config", get_tenant)
    published = []

    class ImmediatePublisher:
        def submit(self, fn, delivery_id):
            from concurrent.futures import Future

            fn(delivery_id)
            future = Future()
            future.set_result(None)
            return future

    monkeypatch.setattr(dispatch, "_publisher", ImmediatePublisher())
    monkeypatch.setattr(dispatch, "publish_notification_delivery", published.append)
    return values, published


def deny_unfaked_notification_network(monkeypatch):
    """中文: 只允许已校验的临时 PG 回环端口。 EN: Permit only the validated disposable PostgreSQL loopback port."""
    import asyncio
    import socket
    from threading import local
    from urllib.parse import urlsplit

    previous = getattr(monkeypatch, "notification_network_blocked", None)
    if previous is not None:
        return previous
    parsed = urlsplit(POSTGRES_DSN or "postgresql://127.0.0.1:1/fixture")
    allowed = (parsed.hostname, parsed.port or 5432)
    blocked = []
    monkeypatch.notification_network_blocked = blocked

    def require(host, port):
        if (host, int(port or 0)) != allowed:
            blocked.append((host, port))
            raise AssertionError("notification_fixture_forbids_unfaked_network")

    dns = socket.getaddrinfo
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    create_connection = asyncio.BaseEventLoop.create_connection
    socketpair = socket.socketpair
    internal_pair = local()

    def guarded_socketpair(*args, **kwargs):
        # 中文: Windows 用专用回环连接创建事件循环自唤醒管道。
        # EN: Windows creates the event-loop self-pipe through its own loopback socket pair.
        internal_pair.active = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            internal_pair.active = False

    def guarded_dns(host, port, *args, **kwargs):
        require(host, port)
        return dns(host, port, *args, **kwargs)

    def guarded_connect(sock, address):
        if not getattr(internal_pair, "active", False):
            require(*address[:2])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        require(*address[:2])
        return connect_ex(sock, address)

    async def guarded_async_connection(loop, factory, host=None, port=None, **kwargs):
        require(host, port)
        return await create_connection(loop, factory, host, port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(
        asyncio.BaseEventLoop, "create_connection", guarded_async_connection
    )
    return blocked


async def seed_notification_fixture(fixture, channels=None):
    """中文: 虚构平台与租户收件人。 EN: Fictional platform and tenant recipients."""
    async with fixture.sessions() as db, db.begin():
        db.add_all(
            [
                Admin(
                    id=42,
                    username="fixture-admin",
                    password_hash="fictional",
                    email="fixture@example.test",
                ),
                Tenant(id=77, name="Fixture tenant", code="fixture-77"),
                Tenant(id=88, name="Other fixture", code="fixture-88"),
                TenantAdmin(
                    id=42,
                    tenant_id=77,
                    username="fixture-tenant-admin",
                    password_hash="fictional",
                    email="tenant-fixture@example.test",
                ),
                TenantAdmin(
                    id=84,
                    tenant_id=88,
                    username="foreign-fixture",
                    password_hash="fictional",
                    email="foreign-fixture@example.test",
                ),
            ]
        )
        for code in ("fixture.notice",):
            db.add(
                NotificationTemplate(
                    code=code,
                    category="system",
                    scope="platform",
                    title_template="Fictional notice",
                    body_template="Fictional {token}",
                    channels=channels or ["email"],
                )
            )
        for user_type, tenant_id in (("admin", 0), ("tenant_admin", 77)):
            db.add(
                NotificationPreference(
                    user_type=user_type,
                    user_id=42,
                    tenant_id=tenant_id,
                    category="system",
                    channel_email=True,
                    channel_ws=True,
                    channel_inbox=True,
                )
            )


async def plan_notification(
    fixture,
    *,
    tenant_id=None,
    recipient_type="admin",
    recipient_id=42,
    dedupe_key="fixture-once",
    force=False,
    **kwargs,
):
    """中文: 通过真实统一规划器提交通知。 EN: Commit through the real unified planner."""
    from app.services.common.notification_service import NotificationService

    async with fixture.sessions() as db, db.begin():
        await NotificationService(db).send(
            "fixture.notice",
            [(recipient_type, recipient_id)],
            tenant_id=tenant_id,
            dedupe_key=dedupe_key,
            data={"token": "fictional-token"},
            force_all_channels=force,
            **kwargs,
        )
        await db.flush()
        return list(
            (
                await db.scalars(
                    select(NotificationDelivery.id)
                    .where(NotificationDelivery.dedupe_key == dedupe_key)
                    .order_by(NotificationDelivery.id)
                )
            ).all()
        )
