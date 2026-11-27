"""中文: 行为；删除持锁与真实域名验证的 PostgreSQL 准入交错。

EN: Behavioral: actual tenant services, repositories, PostgreSQL transactions,
configuration tables and a fixture-owned loopback TXT DNS server. No business,
permission, quota, verification or repository return is mocked. These cases
describe a candidate boundary; authoring them does not establish product RED.
No HTTP/auth, CA, Celery, hosts mutation or upstream/provider endpoint is used.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from copy import deepcopy
from datetime import datetime
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.rrset
import pytest
import pytest_asyncio
from sqlalchemy import literal_column, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import app.configs.service as config_service_module
import app.core.cors as cors_module
from app.core.base_model import Base
from app.core.config import settings
from app.core.i18n import _ as translate
from app.exceptions import AppException
from app.models.system.config import SystemConfig, SystemConfigGroup, SystemConfigValue
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate as Certificate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain as Domain
from app.models.tenant.tenant_plan import TenantPlan
from app.services.system.tenant_domain_service import TenantDomainTenantService

DSN = os.getenv("DOMAIN_DELETED_WRITE_POSTGRES_DSN")
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
        raise ValueError("Only the named disposable deleted-domain fixture is allowed")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="Explicit owned domain PostgreSQL required"),
]

TENANT = 1
OTHER_TENANT = 2
PRIMARY = 10
TARGET = 11
FOREIGN = 20
TARGET_CERT = 111
CREATED = datetime(2026, 11, 1)
EXPIRES = datetime(2099, 1, 1)
TABLE_NAMES = (
    "permissions",
    "tenant_plans",
    "tenant_plan_permissions",
    "tenants",
    "tenant_domains",
    "domain_ssl_certificates",
    "system_config_groups",
    "system_configs",
    "system_config_values",
)


class _ScheduleNotEstablished(RuntimeError):
    """中文: 调度失败不是产品 RED。 EN: Scheduling failure is not product RED."""


class _DnsFixtureNotEstablished(RuntimeError):
    """中文: DNS 夹具失败单独分类。 EN: DNS fixture failure is not product RED."""


class _TxtDnsProtocol(asyncio.DatagramProtocol):
    """中文: 只回答一个虚构 TXT 名称。 EN: Answer one exact fictional TXT name."""

    def __init__(self, qname, token):
        self.qname = dns.name.from_text(qname)
        self.token = token
        self.transport = None
        self.queries = []
        self.answers = []
        self.errors = []
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            query = dns.message.from_wire(data)
            if len(query.question) != 1:
                raise _DnsFixtureNotEstablished("Exactly one DNS question required")
            question = query.question[0]
            actual = (question.name, question.rdtype, question.rdclass, addr[0])
            expected = (
                self.qname,
                dns.rdatatype.TXT,
                dns.rdataclass.IN,
                "127.0.0.1",
            )
            self.queries.append(actual)
            response = dns.message.make_response(query)
            response.flags |= dns.flags.AA
            if actual != expected:
                self.errors.append("UnexpectedDNSQuestionOrPeer")
                response.set_rcode(dns.rcode.NXDOMAIN)
            else:
                response.answer.append(
                    dns.rrset.from_text(
                        self.qname, 0, "IN", "TXT", '"' + self.token + '"'
                    )
                )
                self.answers.append(actual)
            self.transport.sendto(response.to_wire(), addr)
        except Exception as error:
            self.errors.append(type(error).__name__)

    def error_received(self, error):
        self.errors.append(type(error).__name__)

    def connection_lost(self, error):
        if error is not None:
            self.errors.append(type(error).__name__)
        if not self.closed.done():
            self.closed.set_result(None)

    def require_queries(self, count):
        """中文: 原始问答数必须精确。 EN: Require exact real DNS question/answer count."""
        expected = (
            self.qname,
            dns.rdatatype.TXT,
            dns.rdataclass.IN,
            "127.0.0.1",
        )
        if self.errors or self.queries != [expected] * count:
            raise _DnsFixtureNotEstablished("Exact loopback DNS questions absent")
        if self.answers != self.queries:
            raise _DnsFixtureNotEstablished("Exact loopback TXT answers absent")


async def _ordinary_role(connection):
    """中文: 核对数据库、账号和五个权限。 EN: Verify fixture identity and five flags."""
    actual = (
        await connection.execute(
            text(
                "SELECT current_database(),current_user,rolsuper,rolcreatedb,"
                "rolcreaterole,rolbypassrls,rolreplication FROM pg_roles "
                "WHERE rolname=current_user"
            )
        )
    ).one()
    assert tuple(actual) == (
        fixture_url.database,
        fixture_url.username,
        False,
        False,
        False,
        False,
        False,
    )


async def _seed(db, hostnames, token, prefix):
    """中文: 真实套餐、域名、证书和配置行。 EN: Actual fictional FK/config fixtures."""
    await db.execute(
        TenantPlan.__table__.insert(),
        {
            "id": 1,
            "name": "Fictional deleted-domain plan",
            "code": "deleted-domain-audit",
            "is_active": True,
            "quota": {
                "allow_custom_domain": True,
                "max_custom_domains": 0,
                "allow_custom_ssl": True,
            },
            "created_at": CREATED,
            "updated_at": CREATED,
        },
    )
    await db.execute(
        Tenant.__table__.insert(),
        [
            {
                "id": identity,
                "name": f"Fictional deleted-domain tenant {identity}",
                "code": f"deleted-domain-{identity}",
                "plan_id": 1,
                "is_active": True,
                "created_at": CREATED,
                "updated_at": CREATED,
            }
            for identity in (TENANT, OTHER_TENANT)
        ],
    )
    await db.execute(
        Domain.__table__.insert(),
        [
            {
                "id": identity,
                "tenant_id": tenant,
                "domain": hostnames[identity],
                "is_verified": identity != TARGET,
                "verified_at": None if identity == TARGET else CREATED,
                "verification_token": token
                if identity == TARGET
                else f"fictional-proof-{identity}",
                "is_primary": identity != TARGET,
                "ssl_status": "pending" if identity == TARGET else "active",
                "ssl_expires_at": None if identity == TARGET else EXPIRES,
                "remark": f"Fictional deleted-domain sentinel {identity}",
                "created_at": CREATED,
                "updated_at": CREATED,
            }
            for identity, tenant in (
                (PRIMARY, TENANT),
                (TARGET, TENANT),
                (FOREIGN, OTHER_TENANT),
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
                "status": "failed" if identity == TARGET else "active",
                "auto_renew": True,
                "issued_at": None if identity == TARGET else CREATED,
                "expires_at": None if identity == TARGET else EXPIRES,
                "serial_number": f"fictional-deleted-domain-{identity}",
                "created_at": CREATED,
                "updated_at": CREATED,
            }
            for identity, tenant in (
                (PRIMARY, TENANT),
                (TARGET, TENANT),
                (FOREIGN, OTHER_TENANT),
            )
        ],
    )
    await db.execute(
        SystemConfigGroup.__table__.insert(),
        {
            "id": 1,
            "code": "platform_domain",
            "name_key": "config.group.platform_domain.name",
            "scope": "admin_only",
            "is_active": True,
            "created_at": CREATED,
            "updated_at": CREATED,
        },
    )
    await db.execute(
        SystemConfig.__table__.insert(),
        {
            "id": 1,
            "group_id": 1,
            "key": "domain_verification_prefix",
            "name_key": "config.platform.domain_verification_prefix.name",
            "scope": "admin_only",
            "value_type": "string",
            "created_at": CREATED,
            "updated_at": CREATED,
        },
    )
    await db.execute(
        SystemConfigValue.__table__.insert(),
        {
            "id": 1,
            "config_id": 1,
            "tenant_id": 0,
            "value": json.dumps(prefix),
            "created_at": CREATED,
            "updated_at": CREATED,
        },
    )


@pytest_asyncio.fixture
async def deleted_domain_pg(monkeypatch, record_property):
    """中文: 自有九表和 UDP DNS 生命周期。 EN: Owned nine-table/UDP DNS lifetime."""
    identity = uuid4().hex
    schema = "domain_deleted_write_" + identity
    hostnames = {
        PRIMARY: f"primary-{identity}.example.invalid",
        TARGET: f"candidate-{identity}.example.invalid",
        FOREIGN: f"foreign-{identity}.example.invalid",
    }
    token = "fictional-proof-" + identity
    prefix = "_fixture-proof"
    server_settings = {
        "statement_timeout": "12000",
        "lock_timeout": "9000",
        "idle_in_transaction_session_timeout": "20000",
    }
    control = create_async_engine(
        DSN,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": 5, "server_settings": server_settings},
    )
    engine = None
    created = False
    transport = None
    protocol = None
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
                "server_settings": {**server_settings, "search_path": schema},
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
        monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".managed.example.invalid")
        monkeypatch.setattr(settings, "DOMAIN_VERIFICATION_PREFIX", "_unused-fallback")
        monkeypatch.setattr(config_service_module, "_config_id_cache", {})
        monkeypatch.setattr(config_service_module, "_config_value_cache", {})
        monkeypatch.setattr(cors_module, "_verified_custom_domain_hosts", set())
        async with sessions() as db, db.begin():
            await _seed(db, hostnames, token, prefix)
        async with sessions() as db:
            service = TenantDomainTenantService(db, TENANT)
            target = await service.get_by_id(TARGET)
            assert (target.is_verified, target.is_primary, target.is_deleted) == (
                False,
                False,
                False,
            )
            assert service._should_inject_hosts() is False
            await service.ensure_custom_domain_entitled(TENANT, target)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _TxtDnsProtocol(prefix + "." + hostnames[TARGET] + ".", token),
            local_addr=("127.0.0.1", 0),
        )
        address = transport.get_extra_info("sockname")
        assert address[0] == "127.0.0.1" and 0 < address[1] <= 65535
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = address[1]
        resolver.search = []
        resolver.use_search_by_default = False
        resolver.timeout = 0.5
        resolver.lifetime = 2.0
        resolver.cache = None
        monkeypatch.setattr(dns.resolver, "default_resolver", resolver)
        record_property("classification", "behavioral")
        record_property("dns_transport", "owned_loopback_udp_txt_actual_resolver")
        record_property("configuration", "actual_three_table_prefix_distinct_from_env")
        yield SimpleNamespace(
            sessions=sessions,
            tables=tables,
            hostnames=hostnames,
            token=token,
            dns=protocol,
        )
    finally:
        try:
            if transport is not None:
                transport.close()
                await asyncio.wait_for(protocol.closed, 2)
                if protocol.errors:
                    raise _DnsFixtureNotEstablished("Loopback DNS fixture has errors")
                record_property("owned_dns_cleanup", "transport_closed")
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
    """中文: 新物理连接读完整九表和 xmin。 EN: Fresh complete nine-table row images."""
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


def _row(snapshot, model, identity):
    rows = [row for row in snapshot[model.__tablename__] if row["id"] == identity]
    assert len(rows) == 1, "Exactly one physical fixture row is required"
    return rows[0]


def _primary_ids(snapshot, tenant):
    return [
        row["id"]
        for row in snapshot[Domain.__tablename__]
        if row["tenant_id"] == tenant and row["is_primary"] and not row["is_deleted"]
    ]


def _assert_deleted_delta(before, after):
    """中文: 只允许真实删除/撤销差量。 EN: Permit only actual deletion/revocation delta."""
    original = _row(before, Domain, TARGET)
    actual = _row(after, Domain, TARGET)
    assert (original["is_primary"], original["is_verified"]) == (False, False)
    assert re.fullmatch(r"deleted-11-[0-9a-f]{16}\.invalid", actual["domain"])
    assert (
        actual["is_deleted"],
        actual["is_verified"],
        actual["is_primary"],
        actual["verified_at"],
        actual["verification_token"],
        actual["ssl_status"],
        actual["ssl_expires_at"],
    ) == (True, False, False, None, None, "none", None)
    certificate = _row(after, Certificate, TARGET_CERT)
    assert (certificate["status"], certificate["auto_renew"]) == ("revoked", False)
    expected = deepcopy(before)
    for model, row_id in ((Domain, TARGET), (Certificate, TARGET_CERT)):
        previous = _row(before, model, row_id)
        current = _row(after, model, row_id)
        assert current["is_deleted"] is True
        assert current["delete_level"] == "tenant"
        assert current["recycle_stage"] == "module"
        assert current["promoted_to_global_at"] is None
        assert isinstance(current["deleted_at"], datetime)
        assert current["updated_at"] >= current["deleted_at"] >= previous["updated_at"]
        assert current["_xmin"] != previous["_xmin"]
        fields = {
            "is_deleted",
            "deleted_at",
            "delete_level",
            "recycle_stage",
            "promoted_to_global_at",
            "updated_at",
            "_xmin",
        }
        fields.update(
            {
                "domain",
                "is_verified",
                "verified_at",
                "verification_token",
                "is_primary",
                "ssl_status",
                "ssl_expires_at",
            }
            if model is Domain
            else {"status", "auto_renew"}
        )
        _row(expected, model, row_id).update({key: current[key] for key in fields})
    assert after == expected, "Every other column, row and xmin must remain exact"
    assert _primary_ids(after, TENANT) == [PRIMARY]
    assert _primary_ids(after, OTHER_TENANT) == [FOREIGN]


def _assert_verified_delta(before, after, fixture):
    """中文: 验证仅提交精确目标差量。 EN: Verification commits only its exact delta."""
    original = _row(before, Domain, TARGET)
    actual = _row(after, Domain, TARGET)
    assert (
        original["is_verified"],
        original["is_deleted"],
        original["verified_at"],
        original["ssl_status"],
    ) == (False, False, None, "pending")
    assert (
        actual["domain"],
        actual["is_deleted"],
        actual["is_verified"],
        actual["is_primary"],
        actual["verification_token"],
        actual["ssl_status"],
    ) == (fixture.hostnames[TARGET], False, True, False, fixture.token, "none")
    assert isinstance(actual["verified_at"], datetime)
    assert actual["updated_at"] >= actual["verified_at"] >= original["updated_at"]
    assert actual["_xmin"] != original["_xmin"]
    expected = deepcopy(before)
    fields = {"is_verified", "verified_at", "ssl_status", "updated_at", "_xmin"}
    _row(expected, Domain, TARGET).update({key: actual[key] for key in fields})
    assert after == expected, "Verification must preserve every other row/column/xmin"
    assert _primary_ids(after, TENANT) == [PRIMARY]
    assert _primary_ids(after, OTHER_TENANT) == [FOREIGN]


async def _attempt_verify(db):
    """中文: 业务拒绝也交回调用方提交。 EN: Caller commits even a rejected command."""
    try:
        result = await TenantDomainTenantService(db, TENANT).verify_domain(TARGET)
    except AppException as error:
        return None, (
            error.status_code,
            error.code,
            error.message,
            error.data,
            error.debug,
        )
    return result, None


def _assert_not_found(result, denial):
    assert denial == (404, 4040, translate("tenant_domain.not_found"), None, None)
    assert result is None


async def _wait_for_blocker(fixture, pending, holder_pid, waiter_pid, record_property):
    """中文: 只要求真实阻塞，不绑死 UPDATE。 EN: Require a real edge, not UPDATE shape."""
    deadline = monotonic() + 1.5
    async with fixture.sessions() as monitor:
        while monotonic() < deadline:
            await monitor.rollback()
            row = (
                await monitor.execute(
                    text(
                        "SELECT state,wait_event_type,wait_event,query,"
                        "pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                        "WHERE pid=:pid"
                    ),
                    {"pid": waiter_pid},
                )
            ).mappings().one_or_none()
            if (
                row is not None
                and holder_pid in row["blockers"]
                and row["state"] == "active"
                and row["wait_event_type"] == "Lock"
            ):
                statement_kind = row["query"].lstrip().split(maxsplit=1)[0].lower()
                record_property("schedule_evidence", "actual_pg_blocking_edge_observed")
                record_property("waiter_statement_kind", statement_kind)
                record_property("holder_pid", holder_pid)
                record_property("waiter_pid", waiter_pid)
                return
            if pending.done():
                break
            await asyncio.sleep(0.01)
    record_property("schedule_evidence", "not_established_not_product_red")
    raise _ScheduleNotEstablished("Required PostgreSQL blocking edge absent")


async def _settle_pending(pending, holder, waiter):
    """中文: 取消后先释放持锁方。 EN: Bound cleanup releases the holder on every path."""
    if not pending.done():
        pending.cancel()
    try:
        await holder.rollback()
    finally:
        try:
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 5)
        finally:
            await waiter.rollback()


@pytest.mark.behavioral
async def test_waiting_verification_cannot_rewrite_committed_domain_tombstone(
    deleted_domain_pg, record_property
):
    """中文: 行为；删除提交后保留清理值。 EN: Behavioral; deletion cleanup stays durable."""
    fixture = deleted_domain_pg
    before = await _snapshot(fixture)
    async with fixture.sessions() as deleter, fixture.sessions() as verifier:
        assert (
            await TenantDomainTenantService(deleter, TENANT).remove_domain(TARGET)
        ) is True
        assert await _snapshot(fixture) == before
        holder_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await verifier.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(_attempt_verify(verifier))
        try:
            await _wait_for_blocker(
                fixture, pending, holder_pid, waiter_pid, record_property
            )
            await deleter.commit()
            result, denial = await asyncio.wait_for(pending, 5)
            fixture.dns.require_queries(0 if not fixture.dns.queries else 1)
            deleted = await _snapshot(fixture)
            _assert_deleted_delta(before, deleted)
            await verifier.commit()
            after = await _snapshot(fixture)
            actual = _row(after, Domain, TARGET)
            assert (actual["is_verified"], actual["verified_at"]) == (
                False,
                None,
            ), "Committed deletion must keep verification proof cleared"
            assert after == deleted, "Rejected verification must preserve full rows/xmin"
            _assert_not_found(result, denial)
        finally:
            await _settle_pending(pending, deleter, verifier)


@pytest.mark.behavioral
async def test_fresh_verification_after_committed_deletion_is_not_found_without_dml(
    deleted_domain_pg,
):
    """中文: 行为；新会话拒绝已删除目标。 EN: Behavioral; fresh deleted target rejects."""
    fixture = deleted_domain_pg
    before = await _snapshot(fixture)
    async with fixture.sessions() as deleter:
        assert (
            await TenantDomainTenantService(deleter, TENANT).remove_domain(TARGET)
        ) is True
        await deleter.commit()
    deleted = await _snapshot(fixture)
    _assert_deleted_delta(before, deleted)
    async with fixture.sessions() as verifier:
        result, denial = await _attempt_verify(verifier)
        await verifier.commit()
    assert await _snapshot(fixture) == deleted
    _assert_not_found(result, denial)
    fixture.dns.require_queries(0)


@pytest.mark.behavioral
async def test_real_txt_verification_commits_exact_delta_only_when_caller_commits(
    deleted_domain_pg,
):
    """中文: 行为；正常真实 TXT 验证控制。 EN: Behavioral; ordinary actual TXT control."""
    fixture = deleted_domain_pg
    before = await _snapshot(fixture)
    async with fixture.sessions() as verifier:
        result, denial = await _attempt_verify(verifier)
        fixture.dns.require_queries(1)
        assert denial is None
        assert (result.id, result.tenant_id, result.is_verified, result.is_deleted) == (
            TARGET,
            TENANT,
            True,
            False,
        )
        assert await _snapshot(fixture) == before
        await verifier.commit()
    _assert_verified_delta(before, await _snapshot(fixture), fixture)


@pytest.mark.behavioral
async def test_waiting_verification_succeeds_after_real_deletion_rollback(
    deleted_domain_pg, record_property
):
    """中文: 行为；删除回滚后验证仍可提交。 EN: Behavioral; real delete rollback control."""
    fixture = deleted_domain_pg
    before = await _snapshot(fixture)
    async with fixture.sessions() as deleter, fixture.sessions() as verifier:
        assert (
            await TenantDomainTenantService(deleter, TENANT).remove_domain(TARGET)
        ) is True
        holder_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await verifier.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(_attempt_verify(verifier))
        try:
            await _wait_for_blocker(
                fixture, pending, holder_pid, waiter_pid, record_property
            )
            await deleter.rollback()
            result, denial = await asyncio.wait_for(pending, 5)
            fixture.dns.require_queries(1)
            assert denial is None
            assert (result.id, result.is_verified, result.is_deleted) == (
                TARGET,
                True,
                False,
            )
            assert await _snapshot(fixture) == before
            await verifier.commit()
        finally:
            await _settle_pending(pending, deleter, verifier)
    _assert_verified_delta(before, await _snapshot(fixture), fixture)
