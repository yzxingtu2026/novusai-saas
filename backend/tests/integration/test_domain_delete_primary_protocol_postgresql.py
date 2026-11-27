"""中文: 行为测试；域名删除准入的实际 PG 协议边界。

EN: Behavioral: actual platform/tenant domain services, plan entitlement and
repositories with six real FK tables. These are service/database checks, not
HTTP/auth acceptance. Infrastructure is an explicit isolated loopback database;
no business result is mocked and no DNS, hosts write or certificate dispatch runs.
Conflict admission preserves the existing HTTP422/code4090 service contract.
"""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import literal_column, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.base_model import Base
from app.core.config import settings
from app.core.i18n import _ as translate
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException, NotFoundException
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate as Certificate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain as Domain
from app.models.tenant.tenant_plan import TenantPlan
from app.services.system.tenant_domain_service import (
    TenantDomainService,
    TenantDomainTenantService,
)

DSN = os.getenv("DOMAIN_DELETE_PRIMARY_POSTGRES_DSN")
if DSN:
    try:
        fixture_url = make_url(DSN)
    except Exception:
        raise ValueError("Invalid domain fixture DSN; value withheld") from None
    if (
        fixture_url.drivername != "postgresql+asyncpg"
        or fixture_url.host != "127.0.0.1"
        or fixture_url.port is None
        or not fixture_url.database
        or not fixture_url.username
        or fixture_url.query
    ):
        raise ValueError("Only the named disposable domain fixture is allowed")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="Explicit owned domain PostgreSQL required"),
]

TENANT = 1
PRIMARY = 10
TARGET = 11
FOREIGN = 20
CREATED = datetime(2026, 11, 1)
SUFFIX = ".managed.example.invalid"
TABLE_NAMES = (
    "permissions",
    "tenant_plans",
    "tenant_plan_permissions",
    "tenants",
    "tenant_domains",
    "domain_ssl_certificates",
)


async def _ordinary_role(connection):
    """中文: 前后核对身份与五个权限标志。 EN: Verify identity and five role flags."""
    row = (
        await connection.execute(
            text(
                "SELECT current_database(),current_user,rolsuper,rolcreatedb,"
                "rolcreaterole,rolbypassrls,rolreplication FROM pg_roles "
                "WHERE rolname=current_user"
            )
        )
    ).one()
    assert tuple(row) == (
        fixture_url.database,
        fixture_url.username,
        False,
        False,
        False,
        False,
        False,
    )


async def _seed(db):
    """中文: 实际套餐、默认主域名、非主域名与跨租户哨兵。 EN: Real fictional rows."""
    await db.execute(
        TenantPlan.__table__.insert(),
        {
            "id": 1,
            "name": "Fictional domain protocol plan",
            "code": "wave140-protocol",
            "is_active": True,
            "quota": {"allow_custom_domain": True, "max_custom_domains": 0},
        },
    )
    await db.execute(
        Tenant.__table__.insert(),
        [
            {
                "id": identity,
                "name": f"Fictional tenant {identity}",
                "code": code,
                "plan_id": 1,
                "is_active": True,
                "remark": "Fictional unchanged tenant metadata",
            }
            for identity, code in ((TENANT, "tenantone"), (2, "tenanttwo"))
        ],
    )
    await db.execute(
        Domain.__table__.insert(),
        [
            {
                "id": identity,
                "tenant_id": tenant,
                "domain": hostname,
                "is_verified": True,
                "verified_at": CREATED,
                "verification_token": f"fictional-proof-{identity}",
                "is_primary": identity != TARGET,
                "ssl_status": "active",
                "ssl_expires_at": datetime(2099, 1, 1),
                "remark": f"Fictional domain metadata {identity}",
                "created_at": CREATED,
                "updated_at": CREATED,
            }
            for identity, tenant, hostname in (
                (PRIMARY, TENANT, "tenantone" + SUFFIX),
                (TARGET, TENANT, "candidate.example.invalid"),
                (FOREIGN, 2, "foreign.example.invalid"),
            )
        ],
    )
    await db.execute(
        Certificate.__table__.insert(),
        [
            {
                "id": identity + 100,
                "domain_id": identity,
                "tenant_id": tenant,
                "cert_type": "platform",
                "status": "active",
                "auto_renew": True,
                "expires_at": datetime(2099, 1, 1),
                "created_at": CREATED,
                "updated_at": CREATED,
            }
            for identity, tenant in ((PRIMARY, TENANT), (TARGET, TENANT), (FOREIGN, 2))
        ],
    )


@pytest_asyncio.fixture
async def protocol_pg(monkeypatch, record_property):
    """中文: 独立六表 fixture，不导入原 RED 测试。 EN: Independent six-table fixture."""
    schema = "domain_delete_protocol_" + uuid4().hex
    limits = {
        "statement_timeout": "12000",
        "lock_timeout": "9000",
        "idle_in_transaction_session_timeout": "20000",
    }
    control = create_async_engine(
        DSN,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": 5, "server_settings": limits},
    )
    engine = None
    created = False
    try:
        async with control.begin() as connection:
            await _ordinary_role(connection)
            await connection.execute(CreateSchema(schema))
            created = True
        engine = create_async_engine(
            DSN,
            poolclass=NullPool,
            hide_parameters=True,
            isolation_level="READ COMMITTED",
            connect_args={
                "timeout": 5,
                "server_settings": {**limits, "search_path": schema},
            },
        )
        tables = [Base.metadata.tables[name] for name in TABLE_NAMES]
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            assert all(table.schema is None for table in tables)
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=tables)
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", SUFFIX)
        monkeypatch.setattr(settings, "DEBUG", False)
        async with sessions() as db, db.begin():
            await _seed(db)
        async with sessions() as db:
            service = TenantDomainTenantService(db, TENANT)
            target = await service.get_by_id(TARGET)
            await service.ensure_custom_domain_entitled(TENANT, target)
        record_property("classification", "behavioral")
        yield SimpleNamespace(sessions=sessions, tables=tables)
    finally:
        if engine is not None:
            await engine.dispose()
        try:
            if created:
                async with control.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_namespace "
                                "WHERE nspname=:schema"
                            ),
                            {"schema": schema},
                        )
                        == 0
                    )
                    await _ordinary_role(connection)
                record_property("owned_schema_cleanup", "verified_absent")
                record_property("ordinary_role_after", "five_privileged_flags_false")
        finally:
            await control.dispose()


async def _snapshot(fixture):
    """中文: 新物理连接读取全部六表与 xmin。 EN: Fresh complete physical row images."""
    async with fixture.sessions() as db:
        return {
            table.name: [
                dict(row)
                for row in (
                    await db.execute(
                        select(table)
                        .add_columns(literal_column("xmin::text").label("_xmin"))
                        .order_by(*table.primary_key.columns)
                    )
                ).mappings()
            ]
            for table in fixture.tables
        }


def _service(surface, db):
    return (
        TenantDomainTenantService(db, TENANT)
        if surface == "tenant"
        else TenantDomainService(db)
    )


def _assert_safe_admission(error, reason):
    """中文: 保留现有 422/4090 及安全原因。 EN: Preserve existing 422/4090 protocol."""
    key = f"tenant_domain.primary_switch_{reason}"
    assert (error.status_code, error.code) == (422, ErrorCode.CONFLICT)
    assert error.data == {"reason": f"domain_primary_{reason}"}
    assert error.message == translate(key)
    assert error.message != key
    assert error.debug is None
    assert error.__cause__ is None


@pytest.mark.behavioral
async def test_admin_retained_nonprimary_cannot_remove_committed_primary(protocol_pg):
    """中文: 行为；平台服务刷新旧身份后拒绝主域名。 EN: Behavioral; platform stale state."""
    async with protocol_pg.sessions() as remover:
        service = TenantDomainService(remover)
        retained = await service.get_by_id(TARGET)
        assert (retained.id, retained.is_primary) == (TARGET, False)
        async with protocol_pg.sessions() as promoter:
            promoted = await TenantDomainTenantService(
                promoter, TENANT
            ).set_primary_domain(TENANT, TARGET)
            assert (promoted.id, promoted.is_primary) == (TARGET, True)
            await promoter.commit()
        committed = await _snapshot(protocol_pg)
        assert [
            row["id"]
            for row in committed[Domain.__tablename__]
            if row["tenant_id"] == TENANT
            and row["is_primary"]
            and not row["is_deleted"]
        ] == [TARGET]
        assert retained.is_primary is False
        assert await remover.get(Domain, TARGET) is retained
        denial = None
        try:
            await service.remove_domain(TARGET)
        except BusinessException as error:
            denial = (error.status_code, error.code, error.message, error.data)
        await remover.commit()
    assert await _snapshot(protocol_pg) == committed
    assert denial == (
        422,
        ErrorCode.FORBIDDEN,
        translate("tenant_domain.cannot_delete_primary"),
        None,
    )


@pytest.mark.behavioral
async def test_tenant_foreign_domain_remains_not_found_without_mutation(protocol_pg):
    """中文: 行为；外租户仍在 SQL 可见性层拒绝。 EN: Behavioral; foreign visibility."""
    before = await _snapshot(protocol_pg)
    async with protocol_pg.sessions() as db:
        with pytest.raises(NotFoundException) as denied:
            await TenantDomainTenantService(db, TENANT).remove_domain(FOREIGN)
        assert (denied.value.status_code, denied.value.code) == (404, 4040)
        assert denied.value.message == translate("tenant_domain.not_found")
        await db.commit()
    assert await _snapshot(protocol_pg) == before


@pytest.mark.behavioral
async def test_default_primary_domain_keeps_default_rejection_priority(protocol_pg):
    """中文: 行为；真实默认域名优先使用默认拒绝。 EN: Behavioral; default precedence."""
    before = await _snapshot(protocol_pg)
    async with protocol_pg.sessions() as db:
        service = TenantDomainTenantService(db, TENANT)
        default = await service.get_by_id(PRIMARY)
        assert (default.domain, default.is_primary) == ("tenantone" + SUFFIX, True)
        with pytest.raises(BusinessException) as denied:
            await service.remove_domain(PRIMARY)
        assert (denied.value.status_code, denied.value.code) == (
            422,
            ErrorCode.FORBIDDEN,
        )
        assert denied.value.message == translate("tenant_domain.cannot_delete_default")
        await db.commit()
    assert await _snapshot(protocol_pg) == before


@pytest.mark.behavioral
@pytest.mark.parametrize("surface", ["tenant", "admin"])
async def test_delete_lock_timeout_preserves_safe_reason_and_caller_rollback(
    protocol_pg, surface
):
    """中文: 行为；实际锁超时保留失败事务给调用方。 EN: Behavioral; real lock timeout."""
    before = await _snapshot(protocol_pg)
    async with protocol_pg.sessions() as holder, protocol_pg.sessions() as waiter:
        await holder.execute(
            select(Tenant.id).where(Tenant.id == TENANT).with_for_update(key_share=True)
        )
        service = _service(surface, waiter)
        pending = await service.get_by_id(TARGET)
        pending.remark = "Fictional pending field must roll back"
        await waiter.execute(text("SET LOCAL lock_timeout='100ms'"))
        await waiter.execute(text("SET LOCAL statement_timeout='600ms'"))
        try:
            with pytest.raises(BusinessException) as denied:
                await service.remove_domain(TARGET)
            _assert_safe_admission(denied.value, "busy")
            assert waiter.in_transaction()
            with pytest.raises(DBAPIError) as aborted:
                await waiter.execute(text("SELECT 1"))
            assert getattr(aborted.value.orig, "sqlstate", None) == "25P02"
            await waiter.rollback()
            assert await waiter.scalar(text("SELECT 1")) == 1
        finally:
            await waiter.rollback()
            await holder.rollback()
    assert await _snapshot(protocol_pg) == before


@pytest.mark.behavioral
@pytest.mark.parametrize("surface", ["tenant", "admin"])
@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "AUTOCOMMIT"])
async def test_delete_unsupported_transaction_keeps_safe_reason_and_pending_fields(
    protocol_pg, surface, isolation
):
    """中文: 行为；不支持事务不得刷新待写字段。 EN: Behavioral; unsupported modes."""
    before = await _snapshot(protocol_pg)
    async with protocol_pg.sessions() as db:
        await db.connection(execution_options={"isolation_level": isolation})
        service = _service(surface, db)
        pending = await service.get_by_id(TARGET)
        pending.remark = "Fictional unsupported transaction must not flush"
        try:
            with pytest.raises(BusinessException) as denied:
                await service.remove_domain(TARGET)
            _assert_safe_admission(denied.value, "transaction_unsupported")
            assert db.in_transaction()
            await db.rollback()
            assert await db.scalar(text("SELECT 1")) == 1
        finally:
            await db.rollback()
    assert await _snapshot(protocol_pg) == before
