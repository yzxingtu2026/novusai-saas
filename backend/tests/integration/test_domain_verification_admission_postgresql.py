"""中文: 行为；真实 DNS 证明与域名写入准入的补充 PostgreSQL 边界。

EN: Behavioral supplement using the frozen owned PostgreSQL/DNS fixture. The
actual admin and tenant services keep their repositories, entitlement checks,
resolver, transaction and complete nine-table row/xmin readback. No business or
successful DNS return is mocked. DEBUG admin coverage disables hosts support as
environment configuration; it does not accept the hosts integration. No HTTP,
authentication, CA, Celery or provider operation is exercised.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import text, update

import app.core.hosts_helper as hosts_helper
from app.core.config import settings
from app.core.i18n import _ as translate
from app.enums import ErrorCode
from app.exceptions import AppException
from app.services.system.tenant_domain_service import (
    TenantDomainService,
    TenantDomainTenantService,
)
from tests.integration import test_domain_deleted_write_admission_postgresql as shared

deleted_domain_pg = shared.deleted_domain_pg

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not shared.DSN, reason="Explicit owned domain PostgreSQL required"
    ),
]


@pytest.fixture(autouse=True)
def verification_environment(monkeypatch):
    """中文: 企业端在 DEBUG 下仍须查询 DNS。 EN: Tenant DEBUG still needs actual DNS."""
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(hosts_helper, "_HOSTS_PATHS", {})
    assert hosts_helper.is_dev_local() is False


async def _attempt(service, domain_id=shared.TARGET):
    """中文: 保留完整业务拒绝供精确断言。 EN: Preserve the complete business denial."""
    try:
        result = await service.verify_domain(domain_id)
    except AppException as error:
        return None, (
            error.status_code,
            error.code,
            error.message,
            error.data,
            error.debug,
        )
    return result, None


def _assert_rejected(result, denial, message_key):
    """中文: 固定错误且无返回目标。 EN: Require the exact fixed business error."""
    assert result is None
    assert denial == (
        422,
        ErrorCode.VALIDATION_ERROR,
        translate(message_key),
        None,
        None,
    )


def _assert_writer_delta(before, after, changes):
    """中文: 只接受持锁方声明的字段差量。 EN: Accept only the holder's declared delta."""
    original = shared._row(before, shared.Domain, shared.TARGET)
    current = shared._row(after, shared.Domain, shared.TARGET)
    assert {key: current[key] for key in changes} == changes
    assert current["_xmin"] != original["_xmin"]
    assert current["updated_at"] >= original["updated_at"]
    expected = deepcopy(before)
    fields = {*changes, "updated_at", "_xmin"}
    shared._row(expected, shared.Domain, shared.TARGET).update(
        {key: current[key] for key in fields}
    )
    assert after == expected, "Only the exact writer row/columns/xmin may change"


@pytest.mark.behavioral
@pytest.mark.parametrize("debug", [False, True], ids=["real-dns", "admin-debug"])
async def test_admin_verification_keeps_dns_policy_and_exact_caller_commit(
    deleted_domain_pg, monkeypatch, debug
):
    """中文: 行为；管理员 DNS/DEBUG 策略不变。 EN: Behavioral; preserve admin DNS policy."""
    fixture = deleted_domain_pg
    monkeypatch.setattr(settings, "DEBUG", debug)
    before = await shared._snapshot(fixture)
    async with fixture.sessions() as verifier:
        service = TenantDomainService(verifier)
        assert service._should_inject_hosts() is True
        assert hosts_helper.is_dev_local() is False
        result, denial = await _attempt(service)
        assert denial is None
        assert (result.id, result.tenant_id, result.is_verified, result.is_deleted) == (
            shared.TARGET,
            shared.TENANT,
            True,
            False,
        )
        fixture.dns.require_queries(0 if debug else 1)
        assert await shared._snapshot(fixture) == before
        await verifier.commit()
    shared._assert_verified_delta(before, await shared._snapshot(fixture), fixture)


@pytest.mark.behavioral
@pytest.mark.parametrize("preload", [False, True], ids=["fresh", "retained-foreign"])
async def test_tenant_verification_cannot_use_foreign_domain_identity(
    deleted_domain_pg, preload
):
    """中文: 行为；预载外企身份仍不可验证。 EN: Behavioral; foreign identity stays scoped."""
    fixture = deleted_domain_pg
    before = await shared._snapshot(fixture)
    async with fixture.sessions() as verifier:
        if preload:
            retained = await TenantDomainService(verifier).get_by_id(shared.FOREIGN)
            assert (retained.id, retained.tenant_id, retained.is_deleted) == (
                shared.FOREIGN,
                shared.OTHER_TENANT,
                False,
            )
        result, denial = await _attempt(
            TenantDomainTenantService(verifier, shared.TENANT), shared.FOREIGN
        )
        shared._assert_not_found(result, denial)
        await verifier.commit()
    fixture.dns.require_queries(0)
    assert await shared._snapshot(fixture) == before


@pytest.mark.behavioral
async def test_retained_domain_proof_cannot_authorize_replaced_token(
    deleted_domain_pg,
):
    """中文: 行为；旧 ORM 证明不能验证新 token。 EN: Behavioral; retained proof is stale."""
    fixture = deleted_domain_pg
    before = await shared._snapshot(fixture)
    changes = {"verification_token": fixture.token + "-replaced"}
    async with fixture.sessions() as verifier:
        service = TenantDomainTenantService(verifier, shared.TENANT)
        retained = await service.get_by_id(shared.TARGET)
        assert (retained.domain, retained.verification_token) == (
            fixture.hostnames[shared.TARGET],
            fixture.token,
        )
        async with fixture.sessions() as writer:
            await writer.execute(
                update(shared.Domain)
                .where(shared.Domain.id == shared.TARGET)
                .values(**changes)
            )
            await writer.commit()
        changed = await shared._snapshot(fixture)
        _assert_writer_delta(before, changed, changes)
        assert retained.verification_token == fixture.token
        result, denial = await _attempt(service)
        fixture.dns.require_queries(1)
        _assert_rejected(result, denial, "tenant_domain.verify_failed")
        await verifier.commit()
    assert await shared._snapshot(fixture) == changed


@pytest.mark.behavioral
@pytest.mark.parametrize(
    "field", ["verification_token", "domain"], ids=["token", "host"]
)
async def test_dns_proof_cannot_cross_a_committed_identity_change(
    deleted_domain_pg, record_property, field
):
    """中文: 行为；DNS 后提交身份变更必须拒绝。 EN: Behavioral; proof identity is fenced."""
    fixture = deleted_domain_pg
    before = await shared._snapshot(fixture)
    changes = {
        field: fixture.token + "-replaced"
        if field == "verification_token"
        else "replacement-" + fixture.hostnames[shared.TARGET]
    }
    async with fixture.sessions() as writer, fixture.sessions() as verifier:
        await writer.execute(
            update(shared.Domain)
            .where(shared.Domain.id == shared.TARGET)
            .values(**changes)
        )
        assert await shared._snapshot(fixture) == before
        holder_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await verifier.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(
            _attempt(TenantDomainTenantService(verifier, shared.TENANT))
        )
        try:
            await shared._wait_for_blocker(
                fixture, pending, holder_pid, waiter_pid, record_property
            )
            # 中文: 真实旧 token 的 DNS 问答先完成，随后才提交新身份。
            # EN: The real old-token DNS exchange completes before identity commit.
            fixture.dns.require_queries(1)
            assert fixture.dns.token == fixture.token
            await writer.commit()
            result, denial = await asyncio.wait_for(pending, 5)
            changed = await shared._snapshot(fixture)
            _assert_writer_delta(before, changed, changes)
            _assert_rejected(result, denial, "tenant_domain.verify_failed")
            await verifier.commit()
            assert await shared._snapshot(fixture) == changed
            fixture.dns.require_queries(1)
        finally:
            await shared._settle_pending(pending, writer, verifier)


@pytest.mark.behavioral
@pytest.mark.parametrize("limit", [0, 5], ids=["unlimited-quota", "finite-quota"])
async def test_competing_real_verifications_keep_first_commit_without_second_delta(
    deleted_domain_pg, record_property, limit
):
    """中文: 行为；第二个验证不覆盖先提交证明。 EN: Behavioral; second proof adds no write."""
    fixture = deleted_domain_pg
    if limit:
        async with fixture.sessions() as configuration, configuration.begin():
            await configuration.execute(
                update(shared.TenantPlan)
                .where(shared.TenantPlan.id == 1)
                .values(quota={
                    "allow_custom_domain": True,
                    "max_custom_domains": limit,
                    "allow_custom_ssl": True,
                })
            )
    before = await shared._snapshot(fixture)
    async with fixture.sessions() as first, fixture.sessions() as second:
        first_result, first_denial = await _attempt(
            TenantDomainTenantService(first, shared.TENANT)
        )
        assert first_denial is None
        assert (first_result.id, first_result.is_verified) == (shared.TARGET, True)
        fixture.dns.require_queries(1)
        assert await shared._snapshot(fixture) == before
        holder_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(
            _attempt(TenantDomainTenantService(second, shared.TENANT))
        )
        try:
            await shared._wait_for_blocker(
                fixture, pending, holder_pid, waiter_pid, record_property
            )
            fixture.dns.require_queries(2)
            await first.commit()
            result, denial = await asyncio.wait_for(pending, 5)
            committed = await shared._snapshot(fixture)
            shared._assert_verified_delta(before, committed, fixture)
            _assert_rejected(result, denial, "tenant_domain.already_verified")
            await second.commit()
            assert await shared._snapshot(fixture) == committed
            fixture.dns.require_queries(2)
        finally:
            await shared._settle_pending(pending, first, second)


@pytest.mark.behavioral
async def test_tenant_verification_leaves_rollback_to_its_caller(
    deleted_domain_pg,
):
    """中文: 行为；验证不提交调用方事务。 EN: Behavioral; caller rollback keeps all rows."""
    fixture = deleted_domain_pg
    before = await shared._snapshot(fixture)
    async with fixture.sessions() as verifier:
        result, denial = await _attempt(
            TenantDomainTenantService(verifier, shared.TENANT)
        )
        assert denial is None
        assert (result.id, result.is_verified, result.is_deleted) == (
            shared.TARGET,
            True,
            False,
        )
        fixture.dns.require_queries(1)
        assert await shared._snapshot(fixture) == before
        await verifier.rollback()
    assert await shared._snapshot(fixture) == before


@pytest.mark.behavioral
async def test_admin_real_dns_cannot_rewrite_a_waited_committed_deletion(
    deleted_domain_pg, monkeypatch, record_property
):
    """中文: 行为；管理员也拒绝提交删除。 EN: Behavioral; admin rejects committed deletion."""
    fixture = deleted_domain_pg
    monkeypatch.setattr(settings, "DEBUG", False)
    before = await shared._snapshot(fixture)
    async with fixture.sessions() as deleter, fixture.sessions() as verifier:
        assert (
            await TenantDomainTenantService(deleter, shared.TENANT).remove_domain(
                shared.TARGET
            )
        ) is True
        assert await shared._snapshot(fixture) == before
        holder_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await verifier.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(_attempt(TenantDomainService(verifier)))
        try:
            await shared._wait_for_blocker(
                fixture, pending, holder_pid, waiter_pid, record_property
            )
            fixture.dns.require_queries(1)
            await deleter.commit()
            result, denial = await asyncio.wait_for(pending, 5)
            deleted = await shared._snapshot(fixture)
            shared._assert_deleted_delta(before, deleted)
            shared._assert_not_found(result, denial)
            await verifier.commit()
            assert await shared._snapshot(fixture) == deleted
            fixture.dns.require_queries(1)
        finally:
            await shared._settle_pending(pending, deleter, verifier)
