"""DNS-01 provider facade and effective platform configuration boundaries."""

from __future__ import annotations

import math
import os
from typing import Any
from urllib.parse import urlsplit

from app.configs.service import ConfigService
from app.core.config import settings
from app.core.i18n import _
from app.core.logging import LogManager
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException

from .dns_provider_parts.aliyun import AliDnsProvider
from .dns_provider_parts.cloudflare import CloudflareDnsProvider
from .dns_provider_parts.common import DnsProvider, normalize_zone

logger = LogManager.get_logger("ssl")
_SUPPORTED_AUTOMATION_DNS_PROVIDERS = {"cloudflare", "aliyun"}
_LEGACY_UNSUPPORTED_DNS_PROVIDERS = {"dnspod"}
_ALIYUN_ENV = {
    "dns_aliyun_access_key_id": "DNS_ALIYUN_ACCESS_KEY_ID",
    "dns_aliyun_access_key_secret": "DNS_ALIYUN_ACCESS_KEY_SECRET",
    "dns_aliyun_zone": "DNS_ALIYUN_ZONE_NAME",
}


class ManualDnsProvider(DnsProvider):
    async def set_txt_record(self, record_name: str, record_value: str) -> None:
        _ = record_name, record_value
        logger.warning("Manual DNS challenge requires operator action")

    async def delete_txt_record(self, record_name: str, record_value: str) -> None:
        _ = record_name, record_value
        logger.warning("Manual DNS cleanup requires operator action")


def _canonical_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    return "aliyun" if provider == "alidns" else provider


async def _effective_config(db) -> tuple[str, dict[str, str]]:
    service = ConfigService(db)
    default = "manual" if settings.DEBUG else "cloudflare"
    provider = _canonical_provider(
        await service.get_platform_config("dns_provider", default=default) or default
    )
    keys = (
        list(_ALIYUN_ENV)
        if provider == "aliyun"
        else ["dns_cloudflare_api_token", "dns_cloudflare_zone_id"]
    )
    values = {}
    for key in keys:
        value = await service.get_platform_config(key, default=None)
        # Existing seeds use empty strings for unset credentials. Nonempty DB
        # overrides win; private worker env supplies values without DB storage.
        if (value is None or value == "") and key in _ALIYUN_ENV:
            env_name = _ALIYUN_ENV[key]
            value = os.getenv(env_name, getattr(settings, env_name, ""))
        values[key] = str(value or "").strip()
    return provider, values


def _build_provider(provider: str, values: dict[str, str]) -> DnsProvider:
    if provider == "aliyun":
        return AliDnsProvider(
            values["dns_aliyun_access_key_id"],
            values["dns_aliyun_access_key_secret"],
            values["dns_aliyun_zone"],
        )
    if provider == "cloudflare":
        return CloudflareDnsProvider(
            values["dns_cloudflare_api_token"], values["dns_cloudflare_zone_id"]
        )
    raise ValueError(_("ssl_certificate.dns_provider_invalid", provider=provider))


def _issue(code: str, key: str, **kwargs) -> dict[str, str]:
    return {
        "code": code,
        "message": _(f"ssl_certificate.{key}", **kwargs),
        "severity": "error",
    }


async def audit_dns_provider_config(
    db, *, check_provider: bool = False
) -> dict[str, Any]:
    """Default inspection is syntactic. Explicit probe only checks zone reads.

    `ready` retains configuration readiness for existing callers. An explicit
    probe also requires provider reads to succeed. Neither outcome proves DNS
    edit permission, authoritative delegation, CA issuance or deployed HTTPS.
    """
    provider, values = await _effective_config(db)
    issues = []
    adapter = None
    if provider not in _SUPPORTED_AUTOMATION_DNS_PROVIDERS:
        if provider == "manual":
            issues.append(
                _issue("manual_mode_not_supported", "manual_dns_not_supported")
            )
        elif provider in _LEGACY_UNSUPPORTED_DNS_PROVIDERS:
            issues.append(
                _issue(
                    "legacy_unsupported_provider",
                    "dns_provider_legacy_unsupported",
                    provider=provider,
                )
            )
        else:
            issues.append(
                _issue(
                    "unknown_provider", "dns_provider_invalid", provider=provider or "-"
                )
            )
    elif not all(values.values()):
        issues.append(
            _issue(
                f"{provider}_missing_credentials",
                "aliyun_dns_not_configured"
                if provider == "aliyun"
                else "cloudflare_dns_not_configured",
            )
        )
    else:
        try:
            adapter = _build_provider(provider, values)
        except (ValueError, RuntimeError):
            issues.append(
                _issue("invalid_provider_configuration", "dns_configuration_invalid")
            )
    configuration_ready = not issues and adapter is not None
    provider_checked = bool(check_provider and configuration_ready)
    provider_ready = None
    if provider_checked:
        try:
            await adapter.check_access()
        except (RuntimeError, ValueError):
            provider_ready = False
            issues.append(_issue("provider_check_failed", "dns_provider_check_failed"))
        else:
            provider_ready = True
    return {
        "provider_type": provider,
        "ready": configuration_ready and provider_ready is not False,
        "supported": provider in _SUPPORTED_AUTOMATION_DNS_PROVIDERS,
        "configuration_ready": configuration_ready,
        "provider_checked": provider_checked,
        "provider_ready": provider_ready,
        "readiness_level": "provider_read" if provider_checked else "configuration",
        "provider_check_kind": "zone_read" if provider_checked else None,
        "summary": issues[0]["message"]
        if issues
        else _(
            "ssl_certificate.dns_provider_read_checked"
            if provider_checked
            else "ssl_certificate.dns_provider_configured",
            provider="AliDNS" if provider == "aliyun" else "Cloudflare",
        ),
        "issues": issues,
    }


async def ensure_dns_provider_ready(db, *, allow_manual: bool = False) -> str:
    """Fail closed on missing/syntactically invalid config, without API scans."""
    audit = await audit_dns_provider_config(db)
    if audit["provider_type"] == "manual" and allow_manual:
        return "manual"
    if not audit["ready"]:
        raise BusinessException(
            message=audit["summary"], code=ErrorCode.CONFIG_VALIDATION_FAILED
        )
    return audit["provider_type"]


def _invalid(message_key: str) -> BusinessException:
    return BusinessException(
        message=_(f"ssl_certificate.{message_key}"),
        code=ErrorCode.CONFIG_VALIDATION_FAILED,
    )


async def validate_platform_ssl_config_patch(configs: dict[str, Any]) -> None:
    """Validate partial patches independently of provider, including CA modes."""
    if "dns_provider" in configs:
        provider = _canonical_provider(configs["dns_provider"])
        if provider not in _SUPPORTED_AUTOMATION_DNS_PROVIDERS and not (
            provider == "manual" and settings.DEBUG
        ):
            raise _invalid("dns_provider_not_supported")
        configs["dns_provider"] = provider
    if "dns_aliyun_zone" in configs and configs["dns_aliyun_zone"]:
        try:
            configs["dns_aliyun_zone"] = normalize_zone(str(configs["dns_aliyun_zone"]))
        except ValueError:
            raise _invalid("dns_zone_invalid") from None
    for key, expected_host in (
        ("acme_directory_url", "acme-v02.api.letsencrypt.org"),
        ("acme_staging_url", "acme-staging-v02.api.letsencrypt.org"),
    ):
        if key not in configs:
            continue
        # Keep main's public URL validator as the single parser authority.
        from .acme_client import validate_acme_directory_url

        try:
            normalized = validate_acme_directory_url(configs[key])
            if urlsplit(normalized).hostname.rstrip(".").lower() != expected_host:
                raise ValueError("CA mode mismatch")
        except (ValueError, TypeError, AttributeError):
            raise _invalid("dns_acme_mode_invalid") from None
        configs[key] = normalized
    if "ssl_auto_renew_days" in configs:
        value = configs["ssl_auto_renew_days"]
        try:
            days = float(value)
            if (
                isinstance(value, bool)
                or not math.isfinite(days)
                or not days.is_integer()
                or not 1 <= days <= 60
            ):
                raise ValueError("Invalid renewal window")
        except (TypeError, ValueError, OverflowError):
            raise _invalid("dns_renewal_days_invalid") from None
        configs["ssl_auto_renew_days"] = int(days)


async def get_dns_provider(db) -> DnsProvider:
    await ensure_dns_provider_ready(db)
    provider, values = await _effective_config(db)
    try:
        return _build_provider(provider, values)
    except (ValueError, RuntimeError):
        raise _invalid("dns_configuration_invalid") from None


__all__ = [
    "DnsProvider",
    "CloudflareDnsProvider",
    "AliDnsProvider",
    "ManualDnsProvider",
    "audit_dns_provider_config",
    "ensure_dns_provider_ready",
    "get_dns_provider",
    "validate_platform_ssl_config_patch",
]
