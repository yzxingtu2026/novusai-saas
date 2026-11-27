from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core.config import settings
from app.core.managed_https import get_managed_https
from app.schemas.tenant.domain import TenantDomainResponse

pytestmark = pytest.mark.behavioral


@pytest.fixture
def pem(tmp_path, monkeypatch):
    path = tmp_path / "fullchain.pem"
    monkeypatch.setattr(settings, "MANAGED_HTTPS_CERTIFICATE_PATH", str(path))
    monkeypatch.setattr(settings, "MANAGED_HTTPS_AUTO_RENEW", False)

    def write(days=90, names=None, starts=-1):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ignored.example")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=starts))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(item)
                        for item in (names or ["e2.example.com", "*.e2.example.com"])
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert

    return path, write


def test_wildcard_metadata_uses_real_certificate_and_manual_renewal(pem):
    _, write = pem
    cert = write()
    result = get_managed_https("ACME.e2.example.com.")
    assert result == {
        "configured": True,
        "covered": True,
        "status": "active",
        "expires_at": cert.not_valid_after_utc,
        "auto_renew": False,
    }
    assert get_managed_https("nested.acme.e2.example.com")["status"] == "unconfigured"
    assert get_managed_https("unrelated.example.com")["covered"] is False


@pytest.mark.parametrize("days,status", [(20, "expiring"), (-1, "expired")])
def test_certificate_validity(pem, days, status):
    _, write = pem
    write(days=days, starts=-2)
    assert get_managed_https("acme.e2.example.com")["status"] == status


def test_missing_malformed_and_future_certificate_do_not_claim_active(pem):
    path, write = pem
    assert get_managed_https("acme.e2.example.com")["configured"] is False
    path.write_text("invalid")
    assert get_managed_https("acme.e2.example.com")["status"] == "unconfigured"
    write(starts=1)
    assert get_managed_https("acme.e2.example.com")["status"] == "unconfigured"


def test_response_ignores_stale_database_ssl_status_and_translates_legacy_remark(pem):
    _, write = pem
    write()
    now = datetime.now(timezone.utc)
    response = TenantDomainResponse(
        id=1,
        tenant_id=1,
        domain="acme.e2.example.com",
        domain_type="default",
        is_verified=True,
        is_primary=True,
        ssl_status="none",
        remark="tenant_domain.default_domain_remark",
        created_at=now,
        updated_at=now,
    )
    data = response.model_dump()
    assert data["managed_https"]["status"] == "active"
    assert data["managed_https"]["expires_at"].endswith("+00:00")
    assert data["remark"] != "tenant_domain.default_domain_remark"
    response.domain_type = "custom"
    response = TenantDomainResponse.model_validate(response)
    assert response.managed_https is None
