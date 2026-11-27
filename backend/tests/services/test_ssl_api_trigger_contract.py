"""Behavioral SSL trigger contracts through real routes, services and SQLite SQL.

Only DNS-provider readiness and broker delivery are replaced. These tests do not
prove ACME issuance, PostgreSQL locking, certificate publication or live TLS.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.base_model import utc_now
from app.core.config import settings
from app.core.i18n import _
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException, NotFoundException
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant_domain import TenantDomain
from app.models.tenant.tenant_plan import TenantPlan
from app.schemas.tenant.ssl import SslReplaceRequest
from tests.route_test_helpers import make_authenticated_request
from tests.services import test_domain_lifecycle_audit as lifecycle

pytestmark = pytest.mark.behavioral
domain_db = lifecycle.domain_db
certificate = lifecycle.certificate


@pytest.fixture
def ssl_boundary(monkeypatch):
    ready = AsyncMock(return_value="cloudflare")
    monkeypatch.setattr(
        "app.services.system.dns_provider.ensure_dns_provider_ready", ready
    )
    broker = MagicMock()
    monkeypatch.setattr("app.celery_app.celery_app", broker)
    return ready, broker


def add_domain(db, *, domain_id=1, managed=False, verified=True, ssl_status="none"):
    domain = TenantDomain(
        id=domain_id,
        tenant_id=1,
        domain="tc0llkrpw.e2.dakkii.cn" if managed else f"shop{domain_id}.example.com",
        is_verified=verified,
        is_primary=managed,
        ssl_status=ssl_status,
        is_deleted=False,
    )
    db.session.add(domain)
    db.session.commit()
    return domain


def add_cert(db, domain, *, cert_id=41, status="active", cert_type="platform"):
    cert = DomainSslCertificate(
        id=cert_id,
        domain_id=domain.id,
        tenant_id=domain.tenant_id,
        cert_type=cert_type,
        status=status,
        expires_at=utc_now() + timedelta(days=-1 if status == "expired" else 30),
        auto_renew=False,
        is_deleted=False,
    )
    db.session.add(cert)
    db.session.commit()
    return cert


async def call_ssl_route(
    actor, action, db, *, tenant_id=1, domain_id=1, permissions=("*",), data=None
):
    if actor == "admin":
        from app.api.admin.tenant_domains import router

        path = f"/tenants/{{tenant_id}}/domains/{{domain_id}}/ssl/{action}"
    else:
        from app.api.tenant.domains import router

        path = f"/domains/{{domain_id}}/ssl/{action}"
    endpoint = next(route.endpoint for route in router.routes if route.path == path)
    args = {
        "request": make_authenticated_request(*permissions, method="POST"),
        "db": db,
        "domain_id": domain_id,
        "current_admin": SimpleNamespace(id=1, tenant_id=tenant_id),
    }
    if actor == "admin":
        args["tenant_id"] = tenant_id
    if data is not None:
        args["data"] = data
    return await endpoint(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor,action",
    [("admin", "provision"), ("tenant", "provision"), ("admin", "replace")],
)
@pytest.mark.parametrize("configured", [False, True])
async def test_managed_provision_reports_actual_shared_pem_and_never_enqueues(
    domain_db, ssl_boundary, monkeypatch, tmp_path, actor, action, configured
):
    domain = add_domain(domain_db, managed=True, ssl_status="active")
    # A legacy per-domain certificate row must not determine shared HTTPS truth.
    add_cert(domain_db, domain)
    monkeypatch.setattr(settings, "MANAGED_HTTPS_AUTO_RENEW", False)
    monkeypatch.setattr(settings, "MANAGED_HTTPS_CERTIFICATE_PATH", "")
    if configured:
        pem, _key = certificate(names=["*.e2.dakkii.cn"])
        path = tmp_path / "public.pem"
        path.write_text(pem, encoding="utf-8")
        monkeypatch.setattr(settings, "MANAGED_HTTPS_CERTIFICATE_PATH", str(path))

    response = await call_ssl_route(
        actor,
        action,
        domain_db,
        data=SslReplaceRequest(mode="platform") if action == "replace" else None,
    )

    assert response["code"] == 0
    assert response["data"]["status"] == "managed_wildcard"
    managed = response["data"]["managed_https"]
    assert managed["configured"] is configured
    assert managed["covered"] is configured
    assert managed["status"] == ("expiring" if configured else "unconfigured")
    assert managed["auto_renew"] is False
    assert bool(managed["expires_at"]) is configured
    assert response["message"] != _("ssl_certificate.provision_started")
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()
    assert domain.ssl_status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize("status", ["active", "expired"])
async def test_manual_renew_accepts_current_platform_certificate(
    domain_db, ssl_boundary, actor, status
):
    domain = add_domain(domain_db)
    old = add_cert(domain_db, domain, cert_id=40, status="revoked")
    current = add_cert(domain_db, domain, status=status)

    response = await call_ssl_route(actor, "renew", domain_db)

    assert response["code"] == 0
    assert response["message"] == _("ssl_certificate.renew_started")
    ssl_boundary[0].assert_awaited_once_with(domain_db)
    ssl_boundary[1].send_task.assert_called_once_with(
        "app.tasks.ssl_tasks.task_renew_ssl",
        kwargs={"cert_id": current.id},
        queue="default",
    )
    assert old.status == "revoked"
    assert current.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize(
    "status,cert_type",
    [
        (None, "platform"),
        ("pending", "platform"),
        ("failed", "platform"),
        ("revoked", "platform"),
        ("active", "custom"),
        ("expired", "custom"),
    ],
)
async def test_manual_renew_rejects_ineligible_current_certificate_without_job(
    domain_db, ssl_boundary, actor, status, cert_type
):
    domain = add_domain(domain_db)
    if status is not None:
        add_cert(domain_db, domain, status=status, cert_type=cert_type)

    with pytest.raises(BusinessException) as exc:
        await call_ssl_route(actor, "renew", domain_db)

    assert exc.value.code == ErrorCode.CONFLICT
    assert exc.value.status_code == 409
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()
    assert domain.ssl_status == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
async def test_default_legacy_platform_certificate_cannot_receive_per_domain_renewal(
    domain_db, ssl_boundary, actor
):
    domain = add_domain(domain_db, managed=True)
    add_cert(domain_db, domain)

    with pytest.raises(BusinessException) as exc:
        await call_ssl_route(actor, "renew", domain_db)

    assert exc.value.code == ErrorCode.CONFLICT
    assert exc.value.status_code == 409
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize("status", ["active", "expired"])
async def test_regular_provision_requires_explicit_replacement_of_current_cert(
    domain_db, ssl_boundary, actor, status
):
    domain = add_domain(domain_db, ssl_status="failed")
    add_cert(domain_db, domain, status=status)

    with pytest.raises(BusinessException) as exc:
        await call_ssl_route(actor, "provision", domain_db)

    assert exc.value.code == ErrorCode.CONFLICT
    assert exc.value.status_code == 409
    assert domain.ssl_status == "failed"
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,cert_type",
    [("active", "platform"), ("expired", "platform"), ("active", "custom")],
)
async def test_force_replacement_captures_current_id_and_commits_before_dispatch(
    domain_db, ssl_boundary, monkeypatch, status, cert_type
):
    domain = add_domain(domain_db)
    current = add_cert(domain_db, domain, status=status, cert_type=cert_type)
    committed = []
    original_commit = domain_db.commit

    async def commit():
        committed.append(domain.ssl_status)
        await original_commit()

    def sent(*args, **kwargs):
        assert committed == ["provisioning"]

    monkeypatch.setattr(domain_db, "commit", commit)
    ssl_boundary[1].send_task.side_effect = sent
    response = await call_ssl_route(
        "admin", "replace", domain_db, data=SslReplaceRequest(mode="platform")
    )

    assert response["data"] == {"status": "provisioning"}
    ssl_boundary[1].send_task.assert_called_once_with(
        "app.tasks.ssl_tasks.task_provision_ssl",
        args=[domain.id],
        kwargs={"expected_certificate_id": current.id},
        queue="default",
        countdown=2,
    )
    assert current.status == status
    assert current.auto_renew is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "pending", "failed", "revoked"])
async def test_force_replacement_rejects_missing_or_invalid_certificate(
    domain_db, ssl_boundary, status
):
    domain = add_domain(domain_db)
    if status is not None:
        add_cert(domain_db, domain, status=status)

    with pytest.raises(BusinessException) as exc:
        await call_ssl_route(
            "admin", "replace", domain_db, data=SslReplaceRequest(mode="platform")
        )

    assert exc.value.code == ErrorCode.CONFLICT
    assert exc.value.status_code == 409
    assert domain.ssl_status == "none"
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize("action", ["provision", "renew"])
async def test_ssl_trigger_rechecks_plan_before_any_state_change_or_job(
    domain_db, ssl_boundary, actor, action
):
    domain = add_domain(domain_db)
    if action == "renew":
        add_cert(domain_db, domain)
    domain_db.session.get(TenantPlan, 1).is_active = False
    domain_db.session.commit()

    with pytest.raises(BusinessException) as exc:
        await call_ssl_route(actor, action, domain_db)

    assert exc.value.code == ErrorCode.FORBIDDEN
    assert domain.ssl_status == "none"
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize("action", ["provision", "renew"])
async def test_ssl_permission_decorator_blocks_trigger_without_grant(
    domain_db, ssl_boundary, actor, action
):
    domain = add_domain(domain_db)
    add_cert(domain_db, domain)

    with pytest.raises(HTTPException) as exc:
        await call_ssl_route(actor, action, domain_db, permissions=())

    assert exc.value.status_code == 403
    assert exc.value.detail == _("rbac.permission_denied")
    assert domain.ssl_status == "none"
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize("action", ["provision", "renew"])
async def test_ssl_trigger_rejects_another_tenants_domain(
    domain_db, ssl_boundary, actor, action
):
    domain = add_domain(domain_db)
    domain.tenant_id = 2
    domain_db.session.commit()

    with pytest.raises((HTTPException, NotFoundException)) as exc:
        await call_ssl_route(actor, action, domain_db)

    if isinstance(exc.value, HTTPException):
        assert exc.value.status_code == 404
    else:
        assert exc.value.code == ErrorCode.NOT_FOUND
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
async def test_batch_skips_managed_and_current_certificates_and_reports_count(
    domain_db, ssl_boundary
):
    add_domain(domain_db, managed=True)
    expired = add_domain(domain_db, domain_id=2, ssl_status="expired")
    add_cert(domain_db, expired, status="expired")
    eligible = add_domain(domain_db, domain_id=3)

    from app.api.admin.tenant_domains import router

    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/tenants/{tenant_id}/domains/ssl/batch-provision"
    )
    response = await endpoint(
        request=make_authenticated_request("*", method="POST"),
        db=domain_db,
        tenant_id=1,
        current_admin=SimpleNamespace(id=1),
    )

    assert response["data"] == {"triggered": 1, "skipped": 2}
    assert expired.ssl_status == "expired"
    assert eligible.ssl_status == "provisioning"
    ssl_boundary[1].send_task.assert_called_once_with(
        "app.tasks.ssl_tasks.task_provision_ssl",
        args=[eligible.id],
        queue="default",
        countdown=2,
    )


@pytest.mark.asyncio
async def test_force_expected_id_is_snapshot_not_refetched_after_commit(
    domain_db, ssl_boundary, monkeypatch
):
    domain = add_domain(domain_db)
    old = add_cert(domain_db, domain)
    original_commit = domain_db.commit

    async def concurrent_replacement():
        await original_commit()
        old.status = "revoked"
        add_cert(domain_db, domain, cert_id=42)

    monkeypatch.setattr(domain_db, "commit", concurrent_replacement)
    await call_ssl_route(
        "admin", "replace", domain_db, data=SslReplaceRequest(mode="platform")
    )

    ssl_boundary[1].send_task.assert_called_once_with(
        "app.tasks.ssl_tasks.task_provision_ssl",
        args=[domain.id],
        kwargs={"expected_certificate_id": 41},
        queue="default",
        countdown=2,
    )
    assert old.status == "revoked"
    assert domain_db.session.get(DomainSslCertificate, 42).status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
async def test_regular_provision_returns_pending_state_after_committed_dispatch(
    domain_db, ssl_boundary, actor
):
    domain = add_domain(domain_db)
    response = await call_ssl_route(actor, "provision", domain_db)

    assert response["data"] == {"status": "provisioning"}
    assert domain.ssl_status == "provisioning"
    ssl_boundary[1].send_task.assert_called_once_with(
        "app.tasks.ssl_tasks.task_provision_ssl",
        args=[domain.id],
        queue="default",
        countdown=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "tenant"])
@pytest.mark.parametrize("action", ["provision", "renew"])
async def test_unverified_domain_does_not_receive_ssl_trigger(
    domain_db, ssl_boundary, actor, action
):
    domain = add_domain(domain_db, verified=False)
    add_cert(domain_db, domain)
    with pytest.raises((HTTPException, BusinessException)) as exc:
        await call_ssl_route(actor, action, domain_db)

    if isinstance(exc.value, HTTPException):
        assert exc.value.status_code == 400
    else:
        assert exc.value.code in {ErrorCode.VALIDATION_ERROR, 4220}
    assert domain.ssl_status == "none"
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["plan", "permission", "ownership"])
async def test_force_replacement_preserves_all_access_gates(
    domain_db, ssl_boundary, gate
):
    domain = add_domain(domain_db)
    add_cert(domain_db, domain)
    if gate == "plan":
        domain_db.session.get(TenantPlan, 1).is_active = False
    elif gate == "ownership":
        domain.tenant_id = 2
    domain_db.session.commit()

    with pytest.raises((HTTPException, BusinessException)) as exc:
        await call_ssl_route(
            "admin",
            "replace",
            domain_db,
            permissions=() if gate == "permission" else ("*",),
            data=SslReplaceRequest(mode="platform"),
        )

    if isinstance(exc.value, HTTPException):
        assert exc.value.status_code == (404 if gate == "ownership" else 403)
    else:
        assert exc.value.code == ErrorCode.FORBIDDEN
    assert domain.ssl_status == "none"
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()


@pytest.mark.asyncio
async def test_empty_batch_reports_noop_not_started(domain_db, ssl_boundary):
    from app.api.admin.tenant_domains import router

    add_domain(domain_db, managed=True)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/tenants/{tenant_id}/domains/ssl/batch-provision"
    )
    response = await endpoint(
        request=make_authenticated_request("*", method="POST"),
        db=domain_db,
        tenant_id=1,
        current_admin=SimpleNamespace(id=1),
    )

    assert response["data"] == {"triggered": 0, "skipped": 1}
    assert response["message"] == _("ssl_certificate.batch_provision_noop")
    ssl_boundary[0].assert_not_awaited()
    ssl_boundary[1].send_task.assert_not_called()
