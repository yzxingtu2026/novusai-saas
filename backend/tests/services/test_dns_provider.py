"""Behavioral DNS configuration boundaries; no provider network or secrets."""

from __future__ import annotations

import pytest

from app.exceptions import BusinessException
from app.services.system import dns_provider as module

pytestmark = pytest.mark.behavioral
ZONE_ID = "a" * 32


def configure(monkeypatch, values):
    class SuppliedConfig:
        def __init__(self, _db):
            pass

        async def get_platform_config(self, key, default=None):
            return values.get(key, default)

    monkeypatch.setattr(module, "ConfigService", SuppliedConfig)
    for key in (
        "DNS_ALIYUN_ACCESS_KEY_ID",
        "DNS_ALIYUN_ACCESS_KEY_SECRET",
        "DNS_ALIYUN_ZONE_NAME",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(module.settings, "DEBUG", False)


async def test_readiness_is_configuration_only_without_network(monkeypatch):
    configure(
        monkeypatch,
        {
            "dns_provider": "cloudflare",
            "dns_cloudflare_api_token": "token",
            "dns_cloudflare_zone_id": ZONE_ID,
        },
    )
    import httpx

    def forbid_network(*args, **kwargs):
        raise AssertionError("Configuration inspection must not probe DNS")

    monkeypatch.setattr(httpx, "AsyncClient", forbid_network)
    result = await module.audit_dns_provider_config(None)
    assert result["ready"] is True
    assert result["configuration_ready"] is True
    assert result["provider_checked"] is False
    assert result["provider_ready"] is None
    assert result["readiness_level"] == "configuration"


@pytest.mark.parametrize("zone", ["zone-123", "../../other", "", "a" * 31])
async def test_bad_cloudflare_zone_cannot_be_ready(monkeypatch, zone):
    configure(
        monkeypatch,
        {
            "dns_provider": "cloudflare",
            "dns_cloudflare_api_token": "token",
            "dns_cloudflare_zone_id": zone,
        },
    )
    result = await module.audit_dns_provider_config(None)
    assert result["ready"] is False
    assert result["issues"]


async def test_aliyun_env_fallback_and_nonempty_db_override(monkeypatch):
    configure(
        monkeypatch,
        {
            "dns_provider": "alidns",
            "dns_aliyun_access_key_id": "db-id",
            "dns_aliyun_access_key_secret": "",
        },
    )
    monkeypatch.setenv("DNS_ALIYUN_ACCESS_KEY_ID", "env-id")
    monkeypatch.setenv("DNS_ALIYUN_ACCESS_KEY_SECRET", "env-secret")
    monkeypatch.setenv("DNS_ALIYUN_ZONE_NAME", "EXAMPLE.COM.")
    result = await module.audit_dns_provider_config(None)
    assert result["provider_type"] == "aliyun"
    assert result["configuration_ready"] is True
    provider = await module.get_dns_provider(None)
    assert provider._access_key_id == "db-id"
    assert provider._access_key_secret == "env-secret"
    assert provider._zone_name == "example.com"
    assert "env-secret" not in repr(result)


@pytest.mark.parametrize(
    "zone", ["https://example.com", "*.example.com", "127.0.0.1", "example.com:443"]
)
async def test_aliyun_invalid_zone_blocked(monkeypatch, zone):
    configure(
        monkeypatch,
        {
            "dns_provider": "aliyun",
            "dns_aliyun_access_key_id": "id",
            "dns_aliyun_access_key_secret": "secret",
            "dns_aliyun_zone": zone,
        },
    )
    result = await module.audit_dns_provider_config(None)
    assert result["ready"] is False
    with pytest.raises(BusinessException):
        await module.get_dns_provider(None)


async def test_readiness_missing_credentials_and_manual_fail_closed(monkeypatch):
    for values in (
        {"dns_provider": "aliyun"},
        {"dns_provider": "manual"},
        {"dns_provider": "dnspod"},
    ):
        configure(monkeypatch, values)
        assert (await module.audit_dns_provider_config(None))["ready"] is False
        with pytest.raises(BusinessException):
            await module.ensure_dns_provider_ready(None)


@pytest.mark.parametrize(
    "patch",
    [
        {"acme_directory_url": "https://example.invalid/directory"},
        {
            "acme_directory_url": "https://acme-staging-v02.api.letsencrypt.org/directory"
        },
        {"acme_staging_url": "https://acme-v02.api.letsencrypt.org/directory"},
        {
            "acme_staging_url": "https://acme-staging-v02.api.letsencrypt.org/directory?x=1"
        },
        {"ssl_auto_renew_days": 0},
        {"ssl_auto_renew_days": 90},
        {"ssl_auto_renew_days": 61},
        {"ssl_auto_renew_days": 1.5},
        {"ssl_auto_renew_days": True},
        {"ssl_auto_renew_days": "nan"},
    ],
)
async def test_ssl_patch_rejects_invalid_ca_mode_and_renewal_window(patch):
    with pytest.raises(BusinessException):
        await module.validate_platform_ssl_config_patch(patch)


async def test_ssl_patch_normalizes_alias_zone_and_renewal_days():
    patch = {
        "dns_provider": " AliDNS ",
        "dns_aliyun_zone": "EXAMPLE.COM.",
        "ssl_auto_renew_days": "30",
    }
    await module.validate_platform_ssl_config_patch(patch)
    assert patch == {
        "dns_provider": "aliyun",
        "dns_aliyun_zone": "example.com",
        "ssl_auto_renew_days": 30,
    }


async def test_manual_debug_does_not_bypass_other_ssl_validation(monkeypatch):
    monkeypatch.setattr(module.settings, "DEBUG", True)
    await module.validate_platform_ssl_config_patch({"dns_provider": "manual"})
    with pytest.raises(BusinessException):
        await module.validate_platform_ssl_config_patch(
            {"dns_provider": "manual", "ssl_auto_renew_days": 0}
        )


@pytest.mark.parametrize("provider_kind", ["aliyun", "cloudflare"])
@pytest.mark.parametrize("denied", [False, True])
async def test_explicit_provider_read_check_never_writes(
    monkeypatch, provider_kind, denied
):
    import httpx

    values = {
        "dns_provider": provider_kind,
        "dns_aliyun_access_key_id": "id",
        "dns_aliyun_access_key_secret": "secret",
        "dns_aliyun_zone": "example.com",
        "dns_cloudflare_api_token": "token",
        "dns_cloudflare_zone_id": ZONE_ID,
    }
    configure(monkeypatch, values)
    real_client = httpx.AsyncClient
    actions = []

    def handler(request):
        if provider_kind == "aliyun":
            from urllib.parse import parse_qsl

            params = dict(parse_qsl(request.content.decode()))
            assert params["DomainName"] == "example.com"
            assert params["Action"] == "DescribeDomainRecords"
            actions.append(params["Action"])
            payload = (
                {"Code": "Denied", "Message": "secret"}
                if denied
                else {
                    "PageNumber": 1,
                    "PageSize": 100,
                    "TotalCount": 0,
                    "DomainRecords": {"Record": []},
                }
            )
        else:
            assert request.method == "GET"
            actions.append(request.url.path)
            if denied:
                payload = {"success": False, "errors": [{"message": "token"}]}
            elif request.url.path.endswith("/dns_records"):
                payload = {
                    "success": True,
                    "result": [],
                    "result_info": {"page": 1, "total_pages": 0},
                }
            else:
                payload = {
                    "success": True,
                    "result": {"id": ZONE_ID, "name": "example.com"},
                }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = await module.audit_dns_provider_config(None, check_provider=True)
    assert result["configuration_ready"] is True
    assert result["provider_checked"] is True
    assert result["provider_ready"] is not denied
    assert result["ready"] is not denied
    assert result["readiness_level"] == "provider_read"
    assert len(actions) == (2 if provider_kind == "cloudflare" and not denied else 1)
    assert "secret" not in result["summary"]


@pytest.mark.parametrize("locale", ["en", "zh_CN"])
def test_dns_metadata_and_translations(locale):
    from app.configs.definitions.platform.ssl import PLATFORM_SSL_GROUP
    from app.core.i18n import _, set_locale

    set_locale(locale)
    fields = {item.key: item for item in PLATFORM_SSL_GROUP.configs}
    for key in (
        "dns_aliyun_access_key_id",
        "dns_aliyun_access_key_secret",
        "dns_aliyun_zone",
    ):
        assert fields[key].display_rules[0].value == "aliyun"
        assert _(fields[key].name_key) != fields[key].name_key
        assert _(fields[key].description_key) != fields[key].description_key
    assert fields["dns_aliyun_access_key_id"].is_encrypted is True
    assert fields["dns_aliyun_access_key_secret"].is_encrypted is True
    assert "aliyun" in {option.value for option in fields["dns_provider"].options}
    assert [
        (rule.type, rule.value)
        for rule in fields["ssl_auto_renew_days"].validation_rules
    ] == [("min", 1), ("max", 60)]
