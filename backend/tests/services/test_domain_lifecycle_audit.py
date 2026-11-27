"""Behavioral domain regressions using real certificates and repository SQL.

SQLite verifies local persistence outcomes; it does not prove PostgreSQL locking,
broker delivery, ACME issuance, or reverse-proxy certificate installation.
"""

from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import dns.rdata
import dns.rdataclass
import dns.rdatatype
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.base_model import Base, utc_now
from app.core.config import settings
from app.enums.domain import DomainSslStatus, SslCertStatus
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain
from app.models.tenant.tenant_plan import TenantPlan
from app.repositories.tenant.tenant_domain_tenant_repository import (
    TenantDomainTenantRepository,
)
from app.schemas.tenant.domain import TenantDomainCreateRequest
from app.services.system.ssl_certificate_service import SslCertificateService
from app.services.system.tenant_domain_service import TenantDomainService
from app.services.tenant.quota_service import QuotaService

pytestmark = pytest.mark.behavioral


def certificate(*, names=None, cn="shop.example.com"):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(utc_now() - timedelta(days=1))
        .not_valid_after(utc_now() + timedelta(days=30))
    )
    if names is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False
        )
    cert = builder.sign(key, hashes.SHA256())
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


class AsyncSqliteSession:
    """Async-shaped boundary backed by a real disposable SQLAlchemy session."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, instance):
        self.session.add(instance)

    async def flush(self):
        self.session.flush()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    @asynccontextmanager
    async def begin_nested(self):
        with self.session.begin_nested():
            yield


@pytest.fixture
def domain_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            TenantPlan.__table__,
            Tenant.__table__,
            TenantDomain.__table__,
            DomainSslCertificate.__table__,
            Base.metadata.tables["permissions"],
            Base.metadata.tables["tenant_plan_permissions"],
        ],
    )
    monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".e2.dakkii.cn")
    with Session(engine, expire_on_commit=False) as session:
        plan = TenantPlan(
            id=1,
            code="domain-test",
            name="Domain Test",
            is_active=True,
            quota={
                "allow_custom_domain": True,
                "max_custom_domains": 5,
                "allow_custom_ssl": True,
            },
        )
        tenant = Tenant(
            id=1,
            name="Domain Tenant",
            code="tc0llkrpw",
            is_active=True,
            plan_id=1,
            is_deleted=False,
        )
        session.add_all([plan, tenant])
        session.commit()
        yield AsyncSqliteSession(session)
    engine.dispose()


@pytest.mark.parametrize("host", ["a.b.example.com", "example.com"])
def test_wildcard_certificate_rejects_uncovered_hosts(host):
    pem, _ = certificate(names=["*.example.com"], cn="other.example.com")
    with pytest.raises(BusinessException) as exc:
        SslCertificateService._validate_domain_in_cert(pem, host)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_certificate_san_takes_precedence_over_common_name():
    pem, _ = certificate(names=["other.example.com"], cn="shop.example.com")
    with pytest.raises(BusinessException):
        SslCertificateService._validate_domain_in_cert(pem, "shop.example.com")


@pytest.mark.parametrize(
    "names,host",
    [
        (["*.example.com"], "Shop.Example.Com."),
        (["SHOP.EXAMPLE.COM"], "shop.example.com"),
        (["xn--bcher-kva.example"], "bücher.example"),
    ],
)
def test_certificate_hostname_normalization(names, host):
    pem, _ = certificate(names=names, cn="other.example.com")
    SslCertificateService._validate_domain_in_cert(pem, host)


def test_schema_accepts_lookalike_without_reserving_unrelated_domain(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".e2.dakkii.cn")
    assert (
        TenantDomainCreateRequest(domain="shop.note2.dakkii.cn").domain
        == "shop.note2.dakkii.cn"
    )
    assert (
        TenantDomainCreateRequest(domain="BÜCHER.example.").domain
        == "xn--bcher-kva.example"
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "a.e2.dakkii.cn", "e2.dakkii.cn"])
def test_schema_rejects_ip_and_reserved_hostnames(host, monkeypatch):
    monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".e2.dakkii.cn")
    with pytest.raises(ValidationError):
        TenantDomainCreateRequest(domain=host)


@pytest.mark.asyncio
async def test_effective_suffix_is_runtime_without_config_db_reads(
    mock_db, monkeypatch
):
    monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", ".e2.dakkii.cn")
    assert await TenantDomainService(mock_db)._get_domain_suffix() == ".e2.dakkii.cn"
    assert (
        await QuotaService(mock_db, MagicMock())._get_domain_suffix() == ".e2.dakkii.cn"
    )
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_txt_verification_joins_chunks_and_uses_configured_prefix(
    mock_db, monkeypatch
):
    service = TenantDomainService(mock_db)
    service._get_verification_prefix = AsyncMock(return_value="_ownership")
    record = dns.rdata.from_text(
        dns.rdataclass.IN, dns.rdatatype.TXT, '"token-" "chunks"'
    )
    resolve = MagicMock(return_value=[record])
    monkeypatch.setattr("dns.resolver.resolve", resolve)
    domain = TenantDomain(domain="shop.example.com", verification_token="token-chunks")
    assert await service._verify_dns_txt_record(domain) is True
    resolve.assert_called_once_with("_ownership.shop.example.com", "TXT", lifetime=5.0)
    domain.verification_token = " token-chunks "
    assert await service._verify_dns_txt_record(domain) is False


@pytest.mark.asyncio
async def test_managed_default_never_requires_individual_dns_challenge(
    domain_db, monkeypatch
):
    service = TenantDomainService(domain_db)
    monkeypatch.setattr(service, "_should_inject_hosts", lambda: False)
    readiness = AsyncMock(side_effect=AssertionError("No individual DNS challenge"))
    monkeypatch.setattr(
        "app.services.system.dns_provider.ensure_dns_provider_ready", readiness
    )
    send = MagicMock(side_effect=AssertionError("No individual ACME task"))
    monkeypatch.setattr(service, "_send_ssl_provision_task", send)
    domain = await service.create_default_domain(1, "tc0llkrpw")
    assert (
        domain.domain,
        domain.is_verified,
        domain.ssl_status,
        domain.verification_token,
    ) == (
        "tc0llkrpw.e2.dakkii.cn",
        True,
        DomainSslStatus.ACTIVE.value,
        None,
    )
    assert (
        await service.start_ssl_provision(domain.id)
    ).ssl_status == DomainSslStatus.ACTIVE.value
    domain.ssl_status = DomainSslStatus.NONE.value
    await domain_db.flush()
    assert await service.batch_provision_ssl(1) == 0
    assert readiness.await_count == 0
    send.assert_not_called()


@pytest.mark.asyncio
async def test_registration_service_reserves_managed_namespace(domain_db):
    with pytest.raises(BusinessException) as exc:
        await TenantDomainService(domain_db).add_custom_domain(1, "victim.e2.dakkii.cn")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert domain_db.session.execute(select(TenantDomain)).scalars().all() == []


@pytest.mark.asyncio
async def test_default_creation_cannot_claim_another_tenant_code(domain_db):
    with pytest.raises(BusinessException) as exc:
        await TenantDomainService(domain_db).create_default_domain(1, "victim")
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert domain_db.session.execute(select(TenantDomain)).scalars().all() == []


@pytest.mark.asyncio
async def test_existing_cross_code_managed_domain_cannot_receive_default_exemption(
    domain_db,
):
    domain = TenantDomain(tenant_id=1, domain="victim.e2.dakkii.cn", is_verified=True)
    with pytest.raises(BusinessException) as exc:
        await TenantDomainService(domain_db).ensure_custom_domain_entitled(1, domain)
    assert exc.value.code == ErrorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_failed_replacement_preserves_current_valid_certificate(domain_db):
    domain = TenantDomain(
        id=1,
        tenant_id=1,
        domain="shop.example.com",
        is_verified=True,
        ssl_status=DomainSslStatus.PROVISIONING.value,
    )
    expiry = utc_now() + timedelta(days=30)
    cert = DomainSslCertificate(
        id=1,
        domain_id=1,
        tenant_id=1,
        status=SslCertStatus.ACTIVE.value,
        cert_type="platform",
        expires_at=expiry,
    )
    domain_db.session.add_all([domain, cert])
    await domain_db.commit()
    await SslCertificateService(domain_db).mark_provision_failed(
        1, "DNS provider unavailable"
    )
    await domain_db.commit()
    domain_db.session.refresh(domain)
    assert cert.status == SslCertStatus.ACTIVE.value
    assert cert.renewal_error == "DNS provider unavailable"
    assert (domain.ssl_status, domain.ssl_expires_at) == (
        DomainSslStatus.ACTIVE.value,
        expiry,
    )


@pytest.mark.asyncio
async def test_certificate_upload_rejects_cross_tenant_owner_before_mutations(
    domain_db,
):
    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="none",
        )
    )
    await domain_db.commit()
    pem, key = certificate(names=["shop.example.com"])
    with pytest.raises(BusinessException) as exc:
        await SslCertificateService(domain_db).upload_custom_cert(1, 999, pem, key)
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert domain_db.session.execute(select(DomainSslCertificate)).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["", "a.b", "-tenant", "tenant_1", "a" * 64, "::1"])
async def test_default_creation_reports_invalid_code_as_business_error(domain_db, code):
    with pytest.raises(BusinessException) as exc:
        await TenantDomainService(domain_db).create_default_domain(1, code)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert domain_db.session.execute(select(TenantDomain)).scalars().all() == []


@pytest.mark.asyncio
async def test_soft_deleted_binding_releases_name_and_requires_fresh_proof(domain_db):
    old = TenantDomain(
        id=1,
        tenant_id=2,
        domain="SHOP.EXAMPLE.COM.",
        is_deleted=True,
        is_verified=True,
        is_primary=True,
        verification_token="old-proof",
        verified_at=utc_now(),
        ssl_status="active",
    )
    domain_db.session.add(old)
    domain_db.session.add(
        DomainSslCertificate(
            id=1,
            domain_id=1,
            tenant_id=2,
            status="active",
            cert_type="platform",
            auto_renew=True,
        )
    )
    await domain_db.commit()
    new = await TenantDomainService(domain_db).add_custom_domain(1, "Shop.Example.Com.")
    await domain_db.commit()
    assert (
        new.domain,
        new.tenant_id,
        new.is_verified,
        new.is_primary,
        new.ssl_status,
    ) == (
        "shop.example.com",
        1,
        False,
        False,
        "pending",
    )
    assert len(new.verification_token) == 32 and new.verification_token != "old-proof"
    domain_db.session.refresh(old)
    assert old.domain.startswith("deleted-1-") and old.domain.endswith(".invalid")
    assert (
        old.is_verified,
        old.is_primary,
        old.verification_token,
        old.verified_at,
    ) == (
        False,
        False,
        None,
        None,
    )

    old_cert = domain_db.session.get(DomainSslCertificate, 1)
    assert (old_cert.status, old_cert.auto_renew) == ("revoked", False)


@pytest.mark.asyncio
async def test_unique_conflict_leaves_outer_session_usable(domain_db):
    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=2,
            domain="shop.example.com",
            ssl_status="none",
        )
    )
    await domain_db.commit()
    service = TenantDomainService(domain_db)
    with pytest.raises(BusinessException) as exc:
        await service._create_unique_domain(
            {
                "tenant_id": 1,
                "domain": "shop.example.com",
                "ssl_status": "pending",
            }
        )
    assert exc.value.code == ErrorCode.DUPLICATE_ENTRY
    domain_db.session.get(Tenant, 1).contact_name = "Outer transaction survived"
    await domain_db.commit()
    assert domain_db.session.get(Tenant, 1).contact_name == "Outer transaction survived"


@pytest.mark.asyncio
async def test_cross_tenant_duplicate_is_global_and_case_insensitive(domain_db):
    old = TenantDomain(
        id=1,
        tenant_id=2,
        domain="SHOP.EXAMPLE.COM.",
        is_deleted=False,
        is_verified=True,
        ssl_status="active",
    )
    domain_db.session.add(old)
    await domain_db.commit()
    repo = TenantDomainTenantRepository(domain_db, tenant_id=1)
    assert await repo.domain_exists("shop.example.com") is True
    assert await repo.get_by_domain("shop.example.com") is None
    with pytest.raises(BusinessException) as exc:
        await TenantDomainService(domain_db).add_custom_domain(1, "SHOP.EXAMPLE.COM")
    assert exc.value.code == ErrorCode.DUPLICATE_ENTRY
    assert domain_db.session.execute(select(TenantDomain.domain)).scalars().all() == [
        "SHOP.EXAMPLE.COM."
    ]


@pytest.mark.asyncio
async def test_ssl_entitlement_rechecks_plan_after_queued_request(domain_db):
    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="provisioning",
        )
    )
    await domain_db.commit()
    service = SslCertificateService(domain_db)
    assert (await service.ensure_domain_ssl_entitled(1, 1)).domain == "shop.example.com"
    plan = domain_db.session.get(TenantPlan, 1)
    plan.is_active = False
    await domain_db.commit()
    with pytest.raises(BusinessException) as exc:
        await service.ensure_domain_ssl_entitled(1, 1)
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert domain_db.session.get(TenantDomain, 1).ssl_status == "provisioning"


@pytest.mark.asyncio
async def test_ssl_prepare_does_not_dispatch_and_broker_failure_sets_failed(
    domain_db, monkeypatch
):
    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="none",
        )
    )
    await domain_db.commit()
    service = TenantDomainService(domain_db)
    monkeypatch.setattr(
        "app.services.system.dns_provider.ensure_dns_provider_ready", AsyncMock()
    )
    send = MagicMock(side_effect=RuntimeError("Broker unavailable"))
    monkeypatch.setattr(service, "_send_ssl_provision_task", send)
    assert (await service.start_ssl_provision(1)).ssl_status == "provisioning"
    send.assert_not_called()
    await domain_db.commit()
    with pytest.raises(RuntimeError, match="Broker unavailable"):
        await service.dispatch_ssl_provision(1)
    domain_db.session.expire_all()
    assert domain_db.session.get(TenantDomain, 1).ssl_status == "failed"


@pytest.mark.parametrize("operation", ["provision", "renew"])
def test_worker_rechecks_entitlement_before_acme(domain_db, monkeypatch, operation):
    from app.tasks import ssl_tasks

    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="provisioning",
        )
    )
    domain_db.session.add(
        DomainSslCertificate(
            id=1,
            domain_id=1,
            tenant_id=1,
            status="active",
            cert_type="platform",
            expires_at=utc_now() + timedelta(days=30),
        )
    )
    domain_db.session.get(TenantPlan, 1).is_active = False
    domain_db.session.commit()

    @asynccontextmanager
    async def session():
        yield domain_db

    monkeypatch.setattr(ssl_tasks, "_task_async_session", session)
    provider = AsyncMock(side_effect=AssertionError("ACME must not be reached"))
    monkeypatch.setattr("app.services.system.dns_provider.get_dns_provider", provider)
    task = (
        ssl_tasks.task_provision_ssl
        if operation == "provision"
        else ssl_tasks.task_renew_ssl
    )
    result = task.run(1)
    assert result == {
        "error": "domain_not_entitled",
        f"{'domain' if operation == 'provision' else 'cert'}_id": 1,
    }
    assert provider.await_count == 0
    assert domain_db.session.get(DomainSslCertificate, 1).status == "active"


def test_automatic_renewal_rechecks_disabled_flag(domain_db, monkeypatch):
    from app.tasks import ssl_tasks

    domain_db.session.add(
        DomainSslCertificate(
            id=1,
            domain_id=1,
            tenant_id=1,
            status="active",
            cert_type="platform",
            auto_renew=False,
            expires_at=utc_now() + timedelta(days=30),
        )
    )
    domain_db.session.commit()

    @asynccontextmanager
    async def session():
        yield domain_db

    monkeypatch.setattr(ssl_tasks, "_task_async_session", session)
    provider = AsyncMock(side_effect=AssertionError("ACME must not be reached"))
    monkeypatch.setattr("app.services.system.dns_provider.get_dns_provider", provider)
    assert ssl_tasks.task_renew_ssl.run(1, automatic=True) == {
        "error": "auto_renew_disabled",
        "cert_id": 1,
    }
    assert provider.await_count == 0


def test_renewal_does_not_replace_new_certificate_from_stale_expired_job(
    domain_db, monkeypatch
):
    from app.tasks import ssl_tasks

    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="active",
        )
    )
    domain_db.session.add_all(
        [
            DomainSslCertificate(
                id=1,
                domain_id=1,
                tenant_id=1,
                status="expired",
                cert_type="platform",
                auto_renew=True,
                expires_at=utc_now() - timedelta(days=1),
                created_at=utc_now() - timedelta(days=90),
            ),
            DomainSslCertificate(
                id=2,
                domain_id=1,
                tenant_id=1,
                status="active",
                cert_type="platform",
                auto_renew=True,
                expires_at=utc_now() + timedelta(days=90),
            ),
        ]
    )
    domain_db.session.commit()

    @asynccontextmanager
    async def session():
        yield domain_db

    monkeypatch.setattr(ssl_tasks, "_task_async_session", session)
    provider = AsyncMock(side_effect=AssertionError("ACME must not be reached"))
    monkeypatch.setattr("app.services.system.dns_provider.get_dns_provider", provider)
    assert ssl_tasks.task_renew_ssl.run(1, automatic=True) == {
        "error": "cert_not_current",
        "cert_id": 1,
    }
    assert provider.await_count == 0
    assert domain_db.session.get(DomainSslCertificate, 2).status == "active"


@pytest.mark.asyncio
async def test_uploaded_certificate_persists_encrypted_key_and_correct_identity(
    domain_db, monkeypatch
):
    from cryptography.fernet import Fernet

    domain_db.session.add(
        TenantDomain(
            id=1,
            tenant_id=1,
            domain="shop.example.com",
            is_verified=True,
            ssl_status="none",
        )
    )
    await domain_db.commit()
    service = SslCertificateService(domain_db)
    encryption_key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        service, "_get_encryption_key", AsyncMock(return_value=encryption_key)
    )
    pem, private_key = certificate(names=["shop.example.com"])
    cert = await service.upload_custom_cert(1, 1, pem, private_key, check_quota=True)
    await domain_db.commit()
    assert (
        cert.domain_id,
        cert.tenant_id,
        cert.status,
        cert.cert_type,
        cert.auto_renew,
    ) == (
        1,
        1,
        "active",
        "custom",
        False,
    )
    assert cert.certificate == pem
    assert cert.private_key_encrypted != private_key
    assert (
        Fernet(encryption_key.encode())
        .decrypt(cert.private_key_encrypted.encode())
        .decode()
        == private_key
    )
    domain_db.session.expire_all()
    assert domain_db.session.get(TenantDomain, 1).ssl_expires_at == cert.expires_at


@pytest.mark.asyncio
async def test_custom_only_deployment_skips_default_binding_and_counts_custom_quota(
    domain_db, monkeypatch
):
    from app.schemas.tenant.domain import TenantDomainResponse

    monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", "")
    service = TenantDomainService(domain_db)
    assert await service.create_default_domain(1, "tc0llkrpw") is None
    assert domain_db.session.scalars(select(TenantDomain)).all() == []

    domain = await service.add_custom_domain(1, "SHOP.EXAMPLE.COM.")
    await domain_db.commit()
    assert domain.domain == "shop.example.com"

    payload = TenantDomainResponse.model_validate(domain, from_attributes=True)
    assert payload.domain_type == "custom"
    assert payload.managed_https is None
    assert await service.get_cname_target(1) is None

    tenant = await service._get_tenant_with_plan(1)
    quota = await QuotaService(domain_db, tenant).check_domain_quota(additional=0)
    assert (quota.allowed, quota.current, quota.limit, quota.remaining) == (True, 1, 5, 4)
