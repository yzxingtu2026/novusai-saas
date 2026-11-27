"""Behavioral coverage for deployed suffix and public DNS setup guidance."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.public.tenant import get_domain_verification_info
from app.core.config import settings


@pytest.mark.behavioral
@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".managed.example", ""])
async def test_public_domain_guidance_returns_route_target_but_no_verification_secret(
    monkeypatch, suffix
):
    monkeypatch.setattr(settings, "TENANT_DOMAIN_SUFFIX", suffix)
    request = Request(
        {
            "type": "http",
            "state": {
                "tenant_ctx": SimpleNamespace(
                    is_resolved=True, tenant=SimpleNamespace(code="shop")
                )
            },
        }
    )
    result = await get_domain_verification_info(request, "ORDER.EXAMPLE.COM.")
    guidance = result["data"]

    assert guidance["domain"] == "order.example.com"
    assert guidance.get("txt_name") is None
    assert guidance.get("txt_value") is None
    assert guidance["is_verified"] is False
    if suffix:
        assert guidance["cname_target"] == "shop.managed.example"
        assert guidance["cname_name"] == "order.example.com"
        assert "shop.managed.example" in guidance["instructions"]
    else:
        assert guidance.get("cname_target") is None
        assert guidance.get("cname_name") is None
        assert "shop.managed.example" not in guidance["instructions"]
