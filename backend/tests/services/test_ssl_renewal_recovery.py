"""Read-only application audit: disposable SQLite and generated certificate keys.

External DNS/ACME boundaries are substituted; certificate storage and queries
execute unchanged application code. This is not a public issuance/TLS test.
"""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.base_model import utc_now
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant_domain import TenantDomain
from app.repositories.system.ssl_certificate_repository import SslCertificateRepository
from app.services.system.ssl_certificate_service import SslCertificateService
from tests.services import test_domain_lifecycle_audit as domain_fixtures
from tests.services.test_domain_lifecycle_audit import certificate

pytestmark = pytest.mark.behavioral
domain_db = domain_fixtures.domain_db


def seed_certificate(db, *, remaining_days):
    expiry = utc_now() + timedelta(days=remaining_days)
    db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="active",
            ssl_expires_at=expiry,
        )
    )
    db.session.add(
        DomainSslCertificate(
            id=1,
            domain_id=1,
            tenant_id=1,
            status="active",
            cert_type="platform",
            auto_renew=True,
            expires_at=expiry,
        )
    )
    db.session.commit()


@pytest.mark.asyncio
async def test_automatic_inspection_can_recover_after_expiry(domain_db):
    seed_certificate(domain_db, remaining_days=-1)
    repo = SslCertificateRepository(domain_db)
    assert [cert.id for cert in await repo.get_expiring_platform_certs()] == [1]
    await SslCertificateService(domain_db).mark_expired(1, 1)
    await domain_db.commit()
    assert [cert.id for cert in await repo.get_expiring_platform_certs()] == [1], (
        "An enabled platform certificate drops out of all later daily renewal "
        "queries once the inspection marks it expired."
    )


def test_inflight_automatic_job_preserves_new_manual_certificate(
    domain_db, monkeypatch
):
    from app.configs.service import ConfigService
    from app.services.system.acme_client import AcmeClient
    from app.tasks import ssl_tasks

    seed_certificate(domain_db, remaining_days=10)
    encryption_key = Fernet.generate_key().decode()
    uploaded_pem, uploaded_key = certificate(names=["shop.example.com"])
    renewal_pem, renewal_key = certificate(names=["shop.example.com"])

    async def config(_self, key, default=None):
        return encryption_key if key == "ssl_private_key_encryption_key" else default

    async def unused_dns(*_args):
        raise AssertionError("Real DNS must never be contacted")

    async def provider(_db):
        return SimpleNamespace(set_txt_record=unused_dns, delete_txt_record=unused_dns)

    @asynccontextmanager
    async def session():
        yield domain_db

    uploaded = []

    async def external_acme(_self, _domain, **_kwargs):
        # User publishes a replacement while the earlier job is at its external
        # network boundary. It invalidates the job's initial current-cert check.
        cert = await SslCertificateService(domain_db).upload_custom_cert(
            1,
            1,
            uploaded_pem,
            uploaded_key,
            check_quota=True,
        )
        uploaded.append(cert.id)
        await domain_db.commit()
        return renewal_pem, renewal_key, None

    monkeypatch.setattr(ConfigService, "get_platform_config", config)
    monkeypatch.setattr("app.services.system.dns_provider.get_dns_provider", provider)
    monkeypatch.setattr(AcmeClient, "provision_certificate", external_acme)
    monkeypatch.setattr(ssl_tasks, "_task_async_session", session)

    result = ssl_tasks.task_renew_ssl.run(1, automatic=True)
    domain_db.session.expire_all()
    active = domain_db.session.execute(
        select(DomainSslCertificate).where(
            DomainSslCertificate.domain_id == 1,
            DomainSslCertificate.status == "active",
        )
    ).scalar_one()
    assert len(uploaded) == 1
    assert (active.id, active.cert_type, active.auto_renew) == (
        uploaded[0],
        "custom",
        False,
    ), f"In-flight renewal replaced the newly uploaded certificate: {result['status']}"


def test_inspection_blocked_by_dns_is_not_reported_as_success(domain_db, monkeypatch):
    from app.configs.service import ConfigService
    from app.tasks import ssl_tasks

    seed_certificate(domain_db, remaining_days=10)

    @asynccontextmanager
    async def session():
        yield domain_db

    async def config(_self, _key, default=None):
        return default

    async def readiness(_db):
        return {"ready": False, "summary": "DNS provider not configured"}

    task = ssl_tasks.task_check_ssl_renewals
    monkeypatch.setattr(ConfigService, "get_platform_config", config)
    monkeypatch.setattr(
        "app.services.system.dns_provider.audit_dns_provider_config", readiness
    )
    monkeypatch.setattr(ssl_tasks, "_task_async_session", session)
    # Suppress only lifecycle DB bookkeeping; the task body and Celery's eager
    # success/failure classification run unchanged. No broker or live DB access.
    monkeypatch.setattr(task, "before_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task, "on_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task, "after_return", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task, "on_failure", lambda *_args, **_kwargs: None)
    result = task.apply(throw=False)
    assert result.result.stats["errors"] == 1
    assert result.result.stats["platform_renewals_triggered"] == 0
    assert result.state == "FAILURE", (
        "Blocked renewal returns normal stats, so Celery classifies it as "
        "SUCCESS and never invokes the failure-notification hook."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_inflight_result_respects_disabled_auto_renew(
    domain_db, monkeypatch, automatic
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "SSL_PRIVATE_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )

    # Only config storage is outside this disposable six-table schema.
    async def encryption(_self):
        return settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY

    monkeypatch.setattr(SslCertificateService, "_get_encryption_key", encryption)
    seed_certificate(domain_db, remaining_days=10)
    service = SslCertificateService(domain_db)
    await service.toggle_auto_renew(1, False)
    await domain_db.commit()
    pem, key = certificate(names=["shop.example.com"])
    result = await service.store_platform_cert(
        1,
        1,
        pem,
        key,
        expected_certificate_id=1,
        require_auto_renew=automatic,
        expected_domain="shop.example.com",
    )
    await domain_db.commit()
    current = await service.get_cert_detail(1)
    if automatic:
        assert result is None
        assert (current.id, current.auto_renew) == (1, False)
    else:
        assert result.id != 1
        assert (current.id, current.auto_renew) == (result.id, False)


@pytest.mark.asyncio
async def test_expiring_query_excludes_superseded_and_recent_failed_attempts(domain_db):
    seed_certificate(domain_db, remaining_days=-1)
    repo = SslCertificateRepository(domain_db)
    current = await repo.get_cert_by_domain(1)
    current.last_renewal_attempt = utc_now()
    await domain_db.commit()
    assert await repo.get_expiring_platform_certs() == []
    current.last_renewal_attempt = utc_now() - timedelta(days=1)
    await domain_db.commit()
    assert [row.id for row in await repo.get_expiring_platform_certs()] == [1]
    domain_db.session.add(
        DomainSslCertificate(
            id=2,
            domain_id=1,
            tenant_id=1,
            status="active",
            cert_type="custom",
            auto_renew=False,
            expires_at=utc_now() + timedelta(days=60),
        )
    )
    await domain_db.commit()
    assert await repo.get_expiring_platform_certs() == []
