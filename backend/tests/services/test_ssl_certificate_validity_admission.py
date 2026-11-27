"""Behavioral: reject unusable uploads before replacing current TLS material.

Real generated certificates, encryption, service logic and disposable SQLite
persistence execute here. No DNS, CA, provider or reverse-proxy call is made.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.core.base_model import utc_now
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant_domain import TenantDomain
from app.services.system import ssl_certificate_service as ssl_module
from app.services.system.ssl_certificate_service import SslCertificateService
from tests.services import test_domain_lifecycle_audit as domain_fixtures
from tests.services import test_ssl_certificate_export as export_fixtures

pytestmark = pytest.mark.behavioral
domain_db = domain_fixtures.domain_db
export_db = export_fixtures.export_db


def certificate_with_validity(starts_at, expires_at):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shop.example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(starts_at)
        .not_valid_after(expires_at)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("shop.example.com")]), False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


def persisted_material(db, domain_id):
    """Capture the full affected persisted state, including encryption/history."""
    db.session.expire_all()
    domain = db.session.get(TenantDomain, domain_id)
    certificates = db.session.execute(
        select(DomainSslCertificate)
        .where(DomainSslCertificate.domain_id == domain_id)
        .order_by(DomainSslCertificate.id)
    ).scalars()
    return domain.to_dict(), [cert.to_dict() for cert in certificates]


async def replace_certificate(service, operation, domain_id, current_id, pem, key):
    if operation == "upload":
        return await service.upload_custom_cert(
            domain_id, 1, pem, key, check_quota=True
        )
    return await service.store_platform_cert(
        domain_id,
        1,
        pem,
        key,
        expected_certificate_id=current_id,
        expected_domain="shop.example.com",
    )


async def assert_future_material_preserves_current_certificate(
    export_db, monkeypatch, starts_in, operation
):
    domain, current, _, _ = export_fixtures.seed(export_db)
    now = utc_now().replace(microsecond=0)
    monkeypatch.setattr(ssl_module, "utc_now", lambda: now)
    service = SslCertificateService(export_db)
    before = persisted_material(export_db, domain.id)
    publication_before = await service.export_publishable_certificates()
    pem, key = certificate_with_validity(now + starts_in, now + timedelta(days=90))

    with pytest.raises(BusinessException) as exc:
        await replace_certificate(service, operation, domain.id, current.id, pem, key)

    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    # Commit without rolling back: admission must precede any replacement write.
    await export_db.commit()
    assert persisted_material(export_db, domain.id) == before
    assert (await service.get_cert_detail(domain.id)).id == current.id
    assert await service.export_publishable_certificates() == publication_before


async def assert_exact_current_second_replaces_and_exports(
    export_db, monkeypatch, operation
):
    domain, old, _, _ = export_fixtures.seed(export_db)
    now = utc_now().replace(microsecond=0)
    monkeypatch.setattr(ssl_module, "utc_now", lambda: now)
    pem, key = certificate_with_validity(now, now + timedelta(days=90))
    service = SslCertificateService(export_db)

    replacement = await replace_certificate(
        service, operation, domain.id, old.id, pem, key
    )
    await export_db.commit()
    export_db.session.expire_all()

    assert export_db.session.get(DomainSslCertificate, old.id).status == "revoked"
    assert (
        replacement.tenant_id,
        replacement.domain_id,
        replacement.cert_type,
        replacement.status,
        replacement.issued_at,
        replacement.auto_renew,
    ) == (
        1,
        domain.id,
        "custom" if operation == "upload" else "platform",
        "active",
        now,
        operation != "upload",
    )
    assert replacement.id != old.id
    assert (
        export_db.session.get(TenantDomain, domain.id).ssl_status,
        export_db.session.get(TenantDomain, domain.id).ssl_expires_at,
    ) == ("active", now + timedelta(days=90))
    publication = await service.export_publishable_certificates()
    assert [item["certificate_id"] for item in publication["certificates"]] == [
        replacement.id
    ]
    assert publication["certificates"][0]["certificate_pem"] == pem
    assert publication["certificates"][0]["private_key_pem"] == key


async def assert_invalid_material_retains_current_without_rollback(
    export_db, monkeypatch, invalid, operation
):
    domain, current, _, _ = export_fixtures.seed(export_db)
    now = utc_now().replace(microsecond=0)
    monkeypatch.setattr(ssl_module, "utc_now", lambda: now)
    pem, key = certificate_with_validity(
        now - timedelta(days=90), now - timedelta(seconds=1)
    )
    if invalid == "malformed":
        pem = "not a PEM certificate"
    service = SslCertificateService(export_db)
    before = persisted_material(export_db, domain.id)
    publication_before = await service.export_publishable_certificates()

    with pytest.raises(BusinessException if invalid == "expired" else ValueError):
        await replace_certificate(service, operation, domain.id, current.id, pem, key)

    await export_db.commit()
    assert persisted_material(export_db, domain.id) == before
    assert (await service.get_cert_detail(domain.id)).id == current.id
    assert await service.export_publishable_certificates() == publication_before


@pytest.mark.asyncio
@pytest.mark.parametrize("starts_in", [timedelta(seconds=1), timedelta(days=1)])
async def test_future_valid_upload_preserves_current_certificate_and_publication(
    export_db, monkeypatch, starts_in
):
    await assert_future_material_preserves_current_certificate(
        export_db, monkeypatch, starts_in, "upload"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("starts_in", [timedelta(seconds=1), timedelta(days=1)])
async def test_future_valid_platform_store_preserves_current_certificate_and_publication(
    export_db, monkeypatch, starts_in
):
    await assert_future_material_preserves_current_certificate(
        export_db, monkeypatch, starts_in, "platform_store"
    )


@pytest.mark.asyncio
async def test_upload_valid_from_exact_current_second_replaces_and_exports(
    export_db, monkeypatch
):
    await assert_exact_current_second_replaces_and_exports(
        export_db, monkeypatch, "upload"
    )


@pytest.mark.asyncio
async def test_platform_store_valid_from_exact_current_second_replaces_and_exports(
    export_db, monkeypatch
):
    await assert_exact_current_second_replaces_and_exports(
        export_db, monkeypatch, "platform_store"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["expired", "malformed"])
async def test_invalid_upload_retains_current_material_without_outer_rollback(
    export_db, monkeypatch, invalid
):
    await assert_invalid_material_retains_current_without_rollback(
        export_db, monkeypatch, invalid, "upload"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["expired", "malformed"])
async def test_invalid_platform_store_retains_current_material_without_outer_rollback(
    export_db, monkeypatch, invalid
):
    await assert_invalid_material_retains_current_without_rollback(
        export_db, monkeypatch, invalid, "platform_store"
    )
