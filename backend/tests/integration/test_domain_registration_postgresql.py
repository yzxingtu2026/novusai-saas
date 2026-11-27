"""Behavioral: domain registration with real concurrent PostgreSQL transactions.

NOVUSAI_DOMAIN_AUDIT_POSTGRES_DSN must name an explicitly disposable loopback
PostgreSQL database. Each test owns a UUID schema with only six required tables,
including their real foreign keys/indexes; no application DATABASE_URL is used.
The DSN is read only from the environment and must never be logged.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.base_model import Base, utc_now
from app.core.config import settings
from app.core.i18n import _
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain
from app.models.tenant.tenant_plan import TenantPlan
from app.repositories.system.tenant_domain_repository import TenantDomainRepository
from app.repositories.tenant.tenant_domain_tenant_repository import (
    TenantDomainTenantRepository,
)
from app.services.system.tenant_domain_service import (
    TenantDomainService,
    TenantDomainTenantService,
)
from app.services.tenant.quota_service import QuotaService

pytestmark = pytest.mark.behavioral


@pytest_asyncio.fixture
async def postgres_domains(monkeypatch):
    dsn = os.getenv("NOVUSAI_DOMAIN_AUDIT_POSTGRES_DSN")
    if not dsn:
        pytest.skip(
            "NOVUSAI_DOMAIN_AUDIT_POSTGRES_DSN requires disposable local PostgreSQL"
        )
    try:
        url = make_url(dsn)
    except Exception:
        pytest.fail("Invalid disposable PostgreSQL DSN (value withheld)", pytrace=False)
    if url.get_backend_name() != "postgresql" or url.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        pytest.fail(
            "Domain audit PostgreSQL must be an explicitly disposable loopback database",
            pytrace=False,
        )
    schema = "domain_registration_" + uuid4().hex
    engine = create_async_engine(
        url.set(drivername="postgresql+asyncpg"),
        isolation_level="READ COMMITTED",
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "15000",
                "lock_timeout": "12000",
                "application_name": "novusai_domain_registration_test",
            }
        },
    )
    scoped = engine.execution_options(schema_translate_map={None: schema})
    factory = async_sessionmaker(scoped, class_=AsyncSession, expire_on_commit=False)
    schema_created = False
    try:
        async with engine.begin() as connection:
            assert (
                await connection.scalar(text("SELECT current_database()"))
                == url.database
            )
            await connection.execute(CreateSchema(schema))
            schema_created = True
        async with scoped.begin() as connection:
            await connection.run_sync(
                lambda conn: Base.metadata.create_all(
                    conn,
                    tables=[
                        Base.metadata.tables[name]
                        for name in (
                            "permissions",
                            "tenant_plans",
                            "tenant_plan_permissions",
                            "tenants",
                            "tenant_domains",
                            "domain_ssl_certificates",
                        )
                    ],
                )
            )
        monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".managed.example.test")
        async with factory() as db:
            plan = TenantPlan(
                id=1,
                code="domain-audit",
                name="Domain Audit",
                is_active=True,
                quota={
                    "allow_custom_domain": True,
                    "max_custom_domains": 1,
                    "allow_custom_ssl": True,
                },
            )
            db.add(plan)
            await db.flush()
            db.add_all(
                [
                    Tenant(
                        id=1,
                        code="tenantone",
                        name="Tenant One",
                        is_active=True,
                        plan_id=1,
                    ),
                    Tenant(
                        id=2,
                        code="tenanttwo",
                        name="Tenant Two",
                        is_active=True,
                        plan_id=1,
                    ),
                ]
            )
            await db.commit()
        yield factory
    finally:
        if schema_created:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


@dataclass
class Race:
    """Hold the successful transaction until PostgreSQL observes the contender."""

    factory: async_sessionmaker[AsyncSession]
    release: asyncio.Event = field(default_factory=asyncio.Event)
    pids: set[int] = field(default_factory=set)
    blocked_by: list[int] = field(default_factory=list)

    async def observe_lock(self):
        try:
            async with self.factory() as observer:
                async with asyncio.timeout(10):
                    while not self.blocked_by:
                        for pid in tuple(self.pids):
                            blockers = await observer.scalar(
                                select(func.pg_blocking_pids(pid))
                            )
                            if blockers:
                                assert set(blockers).issubset(self.pids)
                                self.blocked_by = blockers
                        if not self.blocked_by:
                            await asyncio.sleep(0.02)
        finally:
            self.release.set()

    async def register(self, tenant_id, host):
        async with self.factory() as db:
            pid = await db.scalar(select(func.pg_backend_pid()))
            self.pids.add(pid)
            assert (
                await db.scalar(text("SHOW transaction_isolation")) == "read committed"
            )
            service = TenantDomainTenantService(db, tenant_id)
            try:
                domain = await service.add_custom_domain(tenant_id, host)
                await self.release.wait()
                result = {
                    "tenant_id": tenant_id,
                    "host": domain.domain,
                    "status": "created",
                    "id": domain.id,
                }
            except BusinessException as exc:
                # No outer rollback: a uniqueness error must have been confined
                # to the real service savepoint and the request remains usable.
                assert db.is_active is True
                assert await db.scalar(select(1)) == 1
                cause = exc.__cause__
                result = {
                    "tenant_id": tenant_id,
                    "status": "rejected",
                    "code": exc.code,
                    "sqlstate": getattr(cause.orig, "sqlstate", None)
                    if isinstance(cause, IntegrityError)
                    else None,
                }
                await db.execute(
                    update(Tenant)
                    .where(Tenant.id == tenant_id)
                    .values(
                        remark=f"request survived {exc.code.value}",
                    )
                )
            await db.commit()
            return result

    async def run(self, registrations):
        values = await asyncio.wait_for(
            asyncio.gather(
                *(self.register(tenant_id, host) for tenant_id, host in registrations),
                self.observe_lock(),
            ),
            timeout=20,
        )
        assert len(self.pids) == 2
        assert self.blocked_by, (
            "Requests did not overlap while holding a real PostgreSQL lock"
        )
        return values[:-1]


@pytest.mark.asyncio
async def test_same_hostname_cross_tenant_unique_race_preserves_loser_session(
    postgres_domains, monkeypatch
):
    barrier = asyncio.Barrier(2)
    original = TenantDomainTenantRepository.domain_exists

    async def synchronized_exists(repo, domain, exclude_id=None):
        result = await original(repo, domain, exclude_id)
        if not result:
            # Both requests pass the real existence check before either INSERT.
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return result

    monkeypatch.setattr(
        TenantDomainTenantRepository, "domain_exists", synchronized_exists
    )
    race = Race(postgres_domains)
    results = await race.run([(1, "SHARED.EXAMPLE.TEST."), (2, "shared.example.test")])
    winners = [r for r in results if r["status"] == "created"]
    losers = [r for r in results if r["status"] == "rejected"]
    assert len(winners) == len(losers) == 1
    assert losers[0]["code"] == ErrorCode.DUPLICATE_ENTRY
    assert losers[0]["sqlstate"] == "23505"
    async with postgres_domains() as db:
        rows = (
            await db.execute(select(TenantDomain.domain, TenantDomain.tenant_id))
        ).all()
        assert rows == [("shared.example.test", winners[0]["tenant_id"])]
        assert await db.scalar(
            select(Tenant.remark).where(Tenant.id == losers[0]["tenant_id"])
        ) == (f"request survived {ErrorCode.DUPLICATE_ENTRY.value}")


@pytest.mark.asyncio
async def test_same_tenant_max_one_quota_serializes_different_hostnames(
    postgres_domains, monkeypatch
):
    barrier = asyncio.Barrier(2)
    original = QuotaService._lock_tenant_row

    async def synchronized_lock(service):
        # Synchronize before the actual SELECT FOR UPDATE; never fake the lock
        # or count. The winner holds its transaction while the loser blocks.
        await asyncio.wait_for(barrier.wait(), timeout=10)
        await original(service)

    monkeypatch.setattr(QuotaService, "_lock_tenant_row", synchronized_lock)
    race = Race(postgres_domains)
    results = await race.run([(1, "first.example.test"), (1, "second.example.test")])
    winners = [r for r in results if r["status"] == "created"]
    losers = [r for r in results if r["status"] == "rejected"]
    assert len(winners) == len(losers) == 1
    assert losers[0]["code"] == ErrorCode.FORBIDDEN
    assert losers[0]["sqlstate"] is None
    async with postgres_domains() as db:
        rows = (
            await db.execute(select(TenantDomain.domain, TenantDomain.tenant_id))
        ).all()
        assert rows == [(winners[0]["host"], 1)]
        assert await db.scalar(select(Tenant.remark).where(Tenant.id == 1)) == (
            f"request survived {ErrorCode.FORBIDDEN.value}"
        )


@pytest.mark.asyncio
async def test_delete_rebind_other_tenant_clears_proof_and_revokes_old_certificate(
    postgres_domains,
):
    async with postgres_domains() as db:
        old = await TenantDomainTenantService(db, 1).add_custom_domain(
            1, "rebind.example.test"
        )
        old_id, old_token = old.id, old.verification_token
        old.is_verified = True
        old.verified_at = utc_now()
        old.ssl_status = "active"
        old.ssl_expires_at = utc_now() + timedelta(days=30)
        cert = DomainSslCertificate(
            domain_id=old.id,
            tenant_id=1,
            cert_type="platform",
            status="active",
            auto_renew=True,
            expires_at=old.ssl_expires_at,
        )
        db.add(cert)
        await db.flush()
        cert_id = cert.id
        await db.commit()

    async with postgres_domains() as db:
        assert await TenantDomainTenantService(db, 1).remove_domain(old_id) is True
        await db.commit()

    async with postgres_domains() as db:
        new = await TenantDomainTenantService(db, 2).add_custom_domain(
            2, "REBIND.EXAMPLE.TEST."
        )
        new_id = new.id
        await db.commit()

    async with postgres_domains() as db:
        old = await db.get(TenantDomain, old_id)
        new = await db.get(TenantDomain, new_id)
        cert = await db.get(DomainSslCertificate, cert_id)
        assert old.domain.startswith(f"deleted-{old_id}-") and old.domain.endswith(
            ".invalid"
        )
        assert (
            old.is_deleted,
            old.is_verified,
            old.verification_token,
            old.verified_at,
            old.ssl_status,
        ) == (
            True,
            False,
            None,
            None,
            "none",
        )
        assert (
            new.tenant_id,
            new.domain,
            new.is_verified,
            new.is_primary,
            new.ssl_status,
        ) == (
            2,
            "rebind.example.test",
            False,
            False,
            "pending",
        )
        assert len(new.verification_token) == 32 and new.verification_token != old_token
        assert cert is not None, (
            "Soft domain deletion must retain revoked certificate history"
        )
        assert cert.is_deleted is True
        assert (cert.tenant_id, cert.domain_id, cert.status, cert.auto_renew) == (
            1,
            old_id,
            "revoked",
            False,
        )
        assert (
            await TenantDomainTenantService(db, 1).get_by_domain("rebind.example.test")
            is None
        )
        assert (
            await TenantDomainTenantService(db, 2).get_by_domain("rebind.example.test")
        ).id == new_id


async def seed_primary_switch_domains(factory):
    """An unlimited entitled plan must still serialize each tenant's primaries."""
    async with factory() as db:
        plan = await db.get(TenantPlan, 1)
        plan.quota = {
            "allow_custom_domain": True,
            "max_custom_domains": 0,
            "allow_custom_ssl": True,
        }
        for tenant_id, code in ((1, "tenantone"), (2, "tenanttwo")):
            for offset, host in (
                (0, f"{code}.managed.example.test"),
                (1, f"first-{code}.example.test"),
                (2, f"second-{code}.example.test"),
            ):
                db.add(
                    TenantDomain(
                        id=tenant_id * 10 + offset,
                        tenant_id=tenant_id,
                        domain=host,
                        is_primary=offset == 0,
                        is_verified=True,
                        ssl_status="active",
                    )
                )
        await db.commit()


def primary_switch_service(surface, db, tenant_id):
    if surface == "tenant":
        return TenantDomainTenantService(db, tenant_id)
    return TenantDomainService(db)


def pause_primary_winner_after_clear(monkeypatch, surface, cleared, release):
    repository = (
        TenantDomainTenantRepository if surface == "tenant" else TenantDomainRepository
    )
    original = repository.clear_primary_flag

    async def clear_and_pause(repo, tenant_id):
        # Execute the real UPDATE first. With the repair, this transaction also
        # holds the owning tenant admission lock; without it only old-primary
        # rows are locked. Do not substitute SQL results or manufacture locks.
        await original(repo, tenant_id)
        if repo.db.info.get("primary_switch_winner"):
            cleared.set()
            await asyncio.wait_for(release.wait(), timeout=10)

    monkeypatch.setattr(repository, "clear_primary_flag", clear_and_pause)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "admin"])
async def test_unlimited_plan_concurrent_primary_switches_commit_one_primary(
    postgres_domains, monkeypatch, surface
):
    await seed_primary_switch_domains(postgres_domains)
    cleared, release = asyncio.Event(), asyncio.Event()
    pids = {}
    observed_blockers = []
    pause_primary_winner_after_clear(monkeypatch, surface, cleared, release)

    async def switch(role, domain_id):
        async with postgres_domains() as db:
            pids[role] = await db.scalar(select(func.pg_backend_pid()))
            assert (
                await db.scalar(text("SHOW transaction_isolation")) == "read committed"
            )
            if role == "winner":
                db.info["primary_switch_winner"] = True
            else:
                await asyncio.wait_for(cleared.wait(), timeout=10)
            domain = await primary_switch_service(surface, db, 1).set_primary_domain(
                1, domain_id
            )
            assert (domain.id, domain.tenant_id, domain.is_primary) == (
                domain_id,
                1,
                True,
            )
            await db.commit()
            return domain.id

    async def observe_actual_contention():
        try:
            await asyncio.wait_for(cleared.wait(), timeout=10)
            async with postgres_domains() as observer:
                async with asyncio.timeout(8):
                    while not observed_blockers:
                        if "contender" in pids:
                            blockers = await observer.scalar(
                                select(func.pg_blocking_pids(pids["contender"]))
                            )
                            if blockers:
                                assert blockers == [pids["winner"]]
                                observed_blockers.extend(blockers)
                        if not observed_blockers:
                            await asyncio.sleep(0.02)
        finally:
            # The first writer cannot promote/commit until the other real
            # backend is waiting. This exposes the stale UPDATE snapshot on RED
            # and allows the repaired contender to refresh after tenant lock.
            release.set()

    tasks = [
        asyncio.create_task(switch("winner", 11)),
        asyncio.create_task(switch("contender", 12)),
        asyncio.create_task(observe_actual_contention()),
    ]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert results[:2] == [11, 12]
    assert len(set(pids.values())) == 2
    assert observed_blockers == [pids["winner"]]
    # Reopen after both caller-owned commits; neither identity-map state nor
    # an uncommitted winner establishes the persisted primary contract.
    async with postgres_domains() as db:
        primary_ids = list(
            (
                await db.scalars(
                    select(TenantDomain.id)
                    .where(
                        TenantDomain.tenant_id == 1,
                        TenantDomain.is_deleted.is_(False),
                        TenantDomain.is_primary.is_(True),
                    )
                    .order_by(TenantDomain.id)
                )
            ).all()
        )
        assert primary_ids == [12]
        primary = await primary_switch_service(surface, db, 1).get_primary_domain(1)
        assert (primary.id, primary.domain, primary.tenant_id) == (
            12,
            "second-tenantone.example.test",
            1,
        )
        assert (await TenantDomainTenantService(db, 2).get_primary_domain(2)).id == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "admin"])
async def test_primary_switch_for_another_tenant_commits_while_first_is_held(
    postgres_domains, monkeypatch, surface
):
    await seed_primary_switch_domains(postgres_domains)
    cleared, release = asyncio.Event(), asyncio.Event()
    pause_primary_winner_after_clear(monkeypatch, surface, cleared, release)

    async def held_first_tenant():
        async with postgres_domains() as db:
            db.info["primary_switch_winner"] = True
            domain = await primary_switch_service(surface, db, 1).set_primary_domain(
                1, 11
            )
            await db.commit()
            return domain.id

    first = asyncio.create_task(held_first_tenant())
    try:
        await asyncio.wait_for(cleared.wait(), timeout=10)
        async with postgres_domains() as db:
            second = await asyncio.wait_for(
                primary_switch_service(surface, db, 2).set_primary_domain(2, 21),
                timeout=5,
            )
            assert (second.id, second.tenant_id, second.is_primary) == (21, 2, True)
            await db.commit()
        # The unrelated tenant has committed before the first is released.
        assert first.done() is False
        async with postgres_domains() as db:
            assert (
                await TenantDomainTenantService(db, 2).get_primary_domain(2)
            ).id == 21
    finally:
        release.set()
        if not first.done():
            try:
                await asyncio.wait_for(first, timeout=10)
            finally:
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)
    assert await first == 11
    async with postgres_domains() as db:
        assert list(
            (
                await db.execute(
                    select(TenantDomain.tenant_id, TenantDomain.id)
                    .where(TenantDomain.is_primary.is_(True))
                    .order_by(TenantDomain.tenant_id)
                )
            ).all()
        ) == [(1, 11), (2, 21)]
        assert (await TenantDomainTenantService(db, 1).get_primary_domain(1)).id == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "admin"])
async def test_primary_switch_preserves_pending_changes_and_stricter_timeouts(
    postgres_domains, surface
):
    await seed_primary_switch_domains(postgres_domains)
    async with postgres_domains() as db:
        db.autoflush = False
        await db.execute(text("SET LOCAL lock_timeout = '100ms'"))
        await db.execute(text("SET LOCAL statement_timeout = '250ms'"))
        target = await db.get(TenantDomain, 11)
        target.remark = "pending caller-owned domain remark"

        result = await primary_switch_service(surface, db, 1).set_primary_domain(1, 11)

        assert (result.id, result.is_primary, result.remark) == (
            11,
            True,
            "pending caller-owned domain remark",
        )
        assert (
            await db.execute(
                text(
                    "SELECT current_setting('lock_timeout'), "
                    "current_setting('statement_timeout')"
                )
            )
        ).one() == ("100ms", "250ms")
        await db.commit()
    async with postgres_domains() as db:
        target = await db.get(TenantDomain, 11)
        assert (target.is_primary, target.remark) == (
            True,
            "pending caller-owned domain remark",
        )
        assert (await db.get(TenantDomain, 10)).is_primary is False


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "admin"])
async def test_primary_switch_timeout_is_safe_and_leaves_rollback_to_caller(
    postgres_domains, surface
):
    await seed_primary_switch_domains(postgres_domains)
    async with postgres_domains() as first, postgres_domains() as second:
        await first.execute(
            select(Tenant.id).where(Tenant.id == 1).with_for_update(key_share=True)
        )
        await second.execute(text("SET LOCAL lock_timeout = '75ms'"))
        target = await second.get(TenantDomain, 11)
        target.remark = "must roll back with failed caller transaction"
        with pytest.raises(BusinessException) as exc:
            await primary_switch_service(surface, second, 1).set_primary_domain(1, 11)
        assert exc.value.code == ErrorCode.CONFLICT
        assert exc.value.data == {"reason": "domain_primary_busy"}
        assert exc.value.message == _("tenant_domain.primary_switch_busy")
        assert exc.value.message != "tenant_domain.primary_switch_busy"
        await second.rollback()
        assert await second.scalar(select(1)) == 1
        await first.rollback()
    async with postgres_domains() as db:
        assert (await db.get(TenantDomain, 10)).is_primary is True
        target = await db.get(TenantDomain, 11)
        assert (target.is_primary, target.remark) == (False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "admin"])
@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "AUTOCOMMIT"])
async def test_primary_switch_rejects_transactions_without_fresh_retained_locks(
    postgres_domains, surface, isolation
):
    await seed_primary_switch_domains(postgres_domains)
    async with postgres_domains() as db:
        await db.connection(execution_options={"isolation_level": isolation})
        with pytest.raises(BusinessException) as exc:
            await primary_switch_service(surface, db, 1).set_primary_domain(1, 11)
        assert exc.value.code == ErrorCode.CONFLICT
        assert exc.value.data == {"reason": "domain_primary_transaction_unsupported"}
        assert exc.value.message == _(
            "tenant_domain.primary_switch_transaction_unsupported"
        )
        assert (
            exc.value.message != "tenant_domain.primary_switch_transaction_unsupported"
        )
        await db.rollback()
    async with postgres_domains() as db:
        assert (await db.get(TenantDomain, 10)).is_primary is True
        assert (await db.get(TenantDomain, 11)).is_primary is False


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tenant", "admin"])
@pytest.mark.parametrize("changed", ["verification", "plan"])
async def test_primary_switch_rechecks_preloaded_state_after_tenant_lock_wait(
    postgres_domains, surface, changed
):
    await seed_primary_switch_domains(postgres_domains)
    async with postgres_domains() as first, postgres_domains() as second:
        first_pid = await first.scalar(select(func.pg_backend_pid()))
        second_pid = await second.scalar(select(func.pg_backend_pid()))
        await first.execute(
            select(Tenant.id).where(Tenant.id == 1).with_for_update(key_share=True)
        )
        service = primary_switch_service(surface, second, 1)
        target = await service.get_by_id(11)
        await service.ensure_custom_domain_entitled(1, target)
        assert target.is_verified is True
        waiting = asyncio.create_task(service.set_primary_domain(1, 11))
        try:
            async with postgres_domains() as observer:
                async with asyncio.timeout(5):
                    while True:
                        blockers = await observer.scalar(
                            select(func.pg_blocking_pids(second_pid))
                        )
                        if blockers:
                            assert blockers == [first_pid]
                            break
                        assert waiting.done() is False
                        await asyncio.sleep(0.02)
            if changed == "verification":
                await first.execute(
                    update(TenantDomain)
                    .where(TenantDomain.id == 11)
                    .values(is_verified=False)
                )
            else:
                await first.execute(
                    update(TenantPlan).where(TenantPlan.id == 1).values(is_active=False)
                )
            await first.commit()
            with pytest.raises(BusinessException) as exc:
                await asyncio.wait_for(waiting, timeout=5)
            assert exc.value.code == (
                ErrorCode.VALIDATION_ERROR
                if changed == "verification"
                else ErrorCode.FORBIDDEN
            )
            await second.rollback()
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    async with postgres_domains() as db:
        assert (await db.get(TenantDomain, 10)).is_primary is True
        assert (await db.get(TenantDomain, 11)).is_primary is False
