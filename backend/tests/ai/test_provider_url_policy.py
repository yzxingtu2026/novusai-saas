"""Test type: behavioral.

Provider URLs are treated as an outbound security boundary.  These tests use
literal addresses and a patched resolver; no third-party network is contacted.
"""

from __future__ import annotations

import socket

import pytest

from app.ai.provider_url_policy import (
    ProviderUrlPolicyError,
    provider_config_allows_private_network,
    resolve_provider_url,
    validate_provider_base_url,
)


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://127.0.0.1/v1", "private_network_not_allowed"),
        ("http://224.0.0.1/v1", "private_network_not_allowed"),
        ("http://169.254.169.254/latest", "private_network_not_allowed"),
        ("https://user:secret@example.com/v1", "userinfo_not_allowed"),
        ("https://example.com/v1?api_key=secret", "query_or_fragment_not_allowed"),
        ("ftp://example.com/v1", "invalid_url"),
    ],
)
def test_provider_base_url_rejects_unsafe_shapes(url: str, reason: str) -> None:
    with pytest.raises(ProviderUrlPolicyError) as exc_info:
        validate_provider_base_url(url)
    assert exc_info.value.reason == reason


def test_provider_base_url_requires_explicit_private_opt_in() -> None:
    assert (
        validate_provider_base_url(
            "http://127.0.0.1:11434/v1",
            allow_private_network=True,
        )
        == "http://127.0.0.1:11434/v1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.170.2/credentials",
        "http://169.254.170.23/v1/credentials",
        "http://[fd00:ec2::254]/latest/meta-data",
    ],
)
def test_private_provider_opt_in_never_allows_cloud_metadata(url: str) -> None:
    with pytest.raises(ProviderUrlPolicyError) as exc_info:
        validate_provider_base_url(url, allow_private_network=True)

    assert exc_info.value.reason == "private_network_not_allowed"


def test_provider_config_private_flag_is_strict() -> None:
    assert provider_config_allows_private_network({"allow_private_network": "true"})
    assert not provider_config_allows_private_network(
        {"allow_private_network": "maybe"}
    )
    assert not provider_config_allows_private_network({})


def test_resolve_provider_url_pins_ip_and_preserves_host(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    target = resolve_provider_url("https://provider.example/v1")
    assert target.request_url == "https://93.184.216.34/v1"
    assert target.host_header == "provider.example"
    assert target.sni_hostname == "provider.example"
