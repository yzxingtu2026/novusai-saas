"""Public certificate metadata for deployment-managed tenant HTTPS."""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from cryptography import x509

from app.core.config import settings
from app.core.domains import normalize_hostname


@lru_cache(maxsize=4)
def _parse_certificate(pem: bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem)


def get_managed_https(domain: str) -> dict:
    """Report configured public PEM coverage, never infer it from database SSL rows.

    The deployment supplies the same public certificate served by its reverse
    proxy. This is configuration evidence; external TLS probes verify live use.
    No private key is read and no network request is made during serialization.
    """
    result = {
        "configured": False,
        "covered": False,
        "status": "unconfigured",
        "expires_at": None,
        "auto_renew": False,
    }
    if not settings.MANAGED_HTTPS_CERTIFICATE_PATH:
        return result
    try:
        path = Path(settings.MANAGED_HTTPS_CERTIFICATE_PATH)
        if path.stat().st_size > 1024 * 1024:
            return result
        cert = _parse_certificate(path.read_bytes())
        hostname = normalize_hostname(domain)
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        for name in names:
            if name.startswith("*."):
                suffix = normalize_hostname(name[2:])
                if (
                    hostname.endswith("." + suffix)
                    and hostname.count(".") == suffix.count(".") + 1
                ):
                    result["covered"] = True
            elif hostname == normalize_hostname(name):
                result["covered"] = True
        result["configured"] = True
        result["expires_at"] = cert.not_valid_after_utc
        if not result["covered"]:
            return result
        now = datetime.now(timezone.utc)
        if now < cert.not_valid_before_utc:
            return result
        if now >= cert.not_valid_after_utc:
            result["status"] = "expired"
        elif cert.not_valid_after_utc - now <= timedelta(days=30):
            result["status"] = "expiring"
        else:
            result["status"] = "active"
        result["auto_renew"] = settings.MANAGED_HTTPS_AUTO_RENEW
    except (OSError, ValueError, x509.ExtensionNotFound):
        # Misconfigured or unavailable certificate must not claim HTTPS readiness.
        return result
    return result
