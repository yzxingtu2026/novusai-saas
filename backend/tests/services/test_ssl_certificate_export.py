"""Behavioral publication export using SQLite, generated certs and real crypto."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization

from app.configs import service as config_module
from app.core.base_model import Base, utc_now
from app.core.config import settings
from app.exceptions import BusinessException
from app.models.system.config import SystemConfig, SystemConfigGroup, SystemConfigValue
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain
from app.models.tenant.tenant_plan import TenantPlan
from app.ops import export_domain_certificates as cli
from app.services.system.ssl_certificate_service import SslCertificateService
from tests.services import test_domain_lifecycle_audit as domain_fixtures
from tests.services.test_domain_lifecycle_audit import certificate

pytestmark = pytest.mark.behavioral
domain_db = domain_fixtures.domain_db


@pytest.fixture
def export_db(domain_db, monkeypatch):
    # Extend the disposable fixture for actual ConfigService env fallback reads;
    # repository, entitlement checks and encryption execute without substitutes.
    Base.metadata.create_all(
        domain_db.session.bind,
        tables=[
            SystemConfigGroup.__table__,
            SystemConfig.__table__,
            SystemConfigValue.__table__,
        ],
    )
    config_module._config_id_cache.clear()
    config_module._config_value_cache.clear()
    monkeypatch.setattr(
        settings, "SSL_PRIVATE_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    yield domain_db
    config_module._config_id_cache.clear()
    config_module._config_value_cache.clear()


def seed(db, *, host="shop.example.com", tenant_id=1, cert_type="platform"):
    pem, key = certificate(names=[host], cn=host)
    info = SslCertificateService._parse_cert_info(pem)
    domain = TenantDomain(
        tenant_id=tenant_id,
        domain=host,
        is_verified=True,
        is_deleted=False,
        ssl_status="active",
    )
    db.session.add(domain)
    db.session.flush()
    row = DomainSslCertificate(
        domain_id=domain.id,
        tenant_id=tenant_id,
        status="active",
        cert_type=cert_type,
        is_deleted=False,
        certificate=pem,
        private_key_encrypted=SslCertificateService._encrypt_private_key(
            key, settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY
        ),
        issued_at=info["issued_at"],
        expires_at=info["expires_at"],
    )
    db.session.add(row)
    db.session.commit()
    return domain, row, pem, key


def key_public(pem):
    return (
        serialization.load_pem_private_key(pem.encode(), password=None)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


@pytest.mark.parametrize("cert_type", ["platform", "custom"])
async def test_active_custom_hostname_exports_decrypted_matching_key_and_fingerprint(
    export_db, cert_type
):
    domain, row, pem, key = seed(export_db, cert_type=cert_type)
    original_ciphertext = row.private_key_encrypted
    parsed = x509.load_pem_x509_certificate(pem.encode())
    snapshot = await SslCertificateService(export_db).export_publishable_certificates()
    assert snapshot["version"] == 1
    assert len(snapshot["certificates"]) == 1
    item = snapshot["certificates"][0]
    assert (
        item["domain_id"],
        item["tenant_id"],
        item["certificate_id"],
        item["domain"],
    ) == (domain.id, 1, row.id, "shop.example.com")
    assert item["certificate_pem"] == pem
    assert item["private_key_pem"] == key
    assert key_public(item["private_key_pem"]) == parsed.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert item["fingerprint_sha256"] == parsed.fingerprint(hashes.SHA256()).hex()
    assert item["expires_at"] == parsed.not_valid_after_utc.isoformat().replace(
        "+00:00", "Z"
    )
    assert item["chain_pem"] is None
    assert original_ciphertext != key
    assert (
        export_db.session.get(DomainSslCertificate, row.id).private_key_encrypted
        == original_ciphertext
    )


@pytest.mark.parametrize(
    "state",
    [
        "managed",
        "managed_apex",
        "domain_deleted",
        "unverified",
        "cert_deleted",
        "revoked",
        "expired",
        "tenant_deleted",
        "tenant_inactive",
        "tenant_expired",
        "no_plan",
        "plan_inactive",
        "custom_domain_denied",
    ],
)
async def test_nonpublishable_rows_are_excluded(export_db, state):
    host = {"managed": "tc0llkrpw.e2.dakkii.cn", "managed_apex": "e2.dakkii.cn"}.get(
        state, "shop.example.com"
    )
    domain, row, _, _ = seed(export_db, host=host)
    tenant = export_db.session.get(Tenant, 1)
    plan = export_db.session.get(TenantPlan, 1)
    if state == "domain_deleted":
        domain.is_deleted = True
    elif state == "unverified":
        domain.is_verified = False
    elif state == "cert_deleted":
        row.is_deleted = True
    elif state == "revoked":
        row.status = "revoked"
    elif state == "expired":
        row.expires_at = utc_now() - timedelta(days=1)
    elif state == "tenant_deleted":
        tenant.is_deleted = True
    elif state == "tenant_inactive":
        tenant.is_active = False
    elif state == "tenant_expired":
        tenant.expires_at = utc_now() - timedelta(days=1)
    elif state == "no_plan":
        tenant.plan_id = None
    elif state == "plan_inactive":
        plan.is_active = False
    elif state == "custom_domain_denied":
        plan.quota = {**plan.quota, "allow_custom_domain": False}
    export_db.session.commit()
    assert await SslCertificateService(export_db).export_publishable_certificates() == {
        "version": 1,
        "certificates": [],
    }


async def test_newer_revoked_row_prevents_export_of_older_active_certificate(export_db):
    domain, row, _, _ = seed(export_db)
    export_db.session.add(
        DomainSslCertificate(
            domain_id=domain.id,
            tenant_id=1,
            status="revoked",
            cert_type="platform",
            is_deleted=False,
            expires_at=row.expires_at,
        )
    )
    export_db.session.commit()
    assert await SslCertificateService(export_db).export_publishable_certificates() == {
        "version": 1,
        "certificates": [],
    }


@pytest.mark.parametrize(
    "failure",
    [
        "invalid_pem",
        "invalid_key",
        "wrong_key",
        "corrupt_ciphertext",
        "wrong_encryption_key",
        "wrong_hostname",
        "pem_expired",
        "pem_future",
        "cross_tenant",
    ],
)
async def test_invalid_material_fails_whole_snapshot_closed(export_db, failure):
    domain, row, _, _ = seed(export_db)
    seed(export_db, host="valid.example.com")
    if failure == "invalid_pem":
        row.certificate = "not-a-certificate"
    elif failure == "invalid_key":
        row.private_key_encrypted = SslCertificateService._encrypt_private_key(
            "not-a-private-key", settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY
        )
    elif failure == "wrong_key":
        _, other_key = certificate(names=[domain.domain])
        row.private_key_encrypted = SslCertificateService._encrypt_private_key(
            other_key, settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY
        )
    elif failure == "corrupt_ciphertext":
        row.private_key_encrypted = "not-a-fernet-token"
    elif failure == "wrong_encryption_key":
        settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY = Fernet.generate_key().decode()
    elif failure == "wrong_hostname":
        domain.domain = "other.example.com"
    elif failure == "cross_tenant":
        export_db.session.add(
            Tenant(
                id=2,
                name="Other tenant",
                code="other",
                is_active=True,
                is_deleted=False,
                plan_id=1,
            )
        )
        row.tenant_id = 2
    else:
        # Generate real PEM validity independent of the DB expiry metadata.
        old = x509.load_pem_x509_certificate(row.certificate.encode())
        private_pem = SslCertificateService._decrypt_private_key(
            row.private_key_encrypted, settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY
        )
        key = serialization.load_pem_private_key(private_pem.encode(), password=None)
        start = (
            utc_now() + timedelta(days=1)
            if failure == "pem_future"
            else utc_now() - timedelta(days=2)
        )
        end = (
            utc_now() + timedelta(days=2)
            if failure == "pem_future"
            else utc_now() - timedelta(days=1)
        )
        changed = (
            x509.CertificateBuilder()
            .subject_name(old.subject)
            .issuer_name(old.issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .add_extension(
                old.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value,
                False,
            )
            .sign(key, hashes.SHA256())
        )
        row.certificate = changed.public_bytes(serialization.Encoding.PEM).decode()
    export_db.session.commit()
    with pytest.raises((BusinessException, ValueError, InvalidToken)):
        await SslCertificateService(export_db).export_publishable_certificates()


def install_cli_db(monkeypatch, db):
    from app.core import database

    @asynccontextmanager
    async def session():
        try:
            yield db
        finally:
            # Match AsyncSession context lifetime before engine disposal.
            db.session.close()

    monkeypatch.setattr(database, "async_session_factory", session)

    # The real in-memory SQLite engine remains the storage boundary. Only the
    # async disposal adapter replaces the production process engine lifetime.
    class Disposal:
        def __init__(self):
            self.disposed = 0

        async def dispose(self):
            self.disposed += 1
            db.session.bind.dispose()

    disposal = Disposal()
    monkeypatch.setattr(database, "async_engine", disposal)
    return disposal


def test_cli_exports_real_service_snapshot_as_one_json_line(
    export_db, monkeypatch, capsys
):
    _, row, pem, key = seed(export_db)
    disposal = install_cli_db(monkeypatch, export_db)
    export_snapshot = cli.export_snapshot

    async def noisy_export():
        print("synthetic dependency stdout")
        print("synthetic dependency stderr", file=sys.stderr)
        return await export_snapshot()

    monkeypatch.setattr(cli, "export_snapshot", noisy_export)
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    snapshot = json.loads(captured.out)
    assert snapshot["version"] == 1
    assert snapshot["certificates"][0]["certificate_id"] == row.id
    assert snapshot["certificates"][0]["certificate_pem"] == pem
    assert snapshot["certificates"][0]["private_key_pem"] == key
    assert disposal.disposed == 1


def test_cli_failure_emits_only_safe_error_and_no_partial_private_snapshot(
    export_db, monkeypatch, capsys
):
    _, invalid, _, _ = seed(export_db)
    _, valid, pem, key = seed(export_db, host="valid.example.com")
    secret = "synthetic-exception-secret-TEST"
    invalid.private_key_encrypted = secret
    export_db.session.commit()
    disposal = install_cli_db(monkeypatch, export_db)
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "domain_certificate_export_failed\n"
    assert all(
        value not in captured.err
        for value in (
            secret,
            pem,
            key,
            valid.private_key_encrypted,
            settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY,
        )
    )
    assert disposal.disposed == 1


def test_cli_does_not_serialize_exception_secrets(monkeypatch, capsys):
    async def unavailable_database():
        print("synthetic dependency stdout")
        print("synthetic dependency stderr", file=sys.stderr)
        # A failing external boundary; the exporter itself handles the real
        # exception, rather than substituting successful service output.
        raise RuntimeError(
            "postgresql://secret-user:synthetic-password@private-host/db\nPRIVATE KEY"
        )

    monkeypatch.setattr(cli, "export_snapshot", unavailable_database)
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "domain_certificate_export_failed\n"
