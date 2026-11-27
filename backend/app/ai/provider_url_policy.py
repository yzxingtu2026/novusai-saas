"""Outbound URL policy for AI providers.

Provider endpoints are operator supplied and are therefore an SSRF boundary.
This module intentionally contains only deterministic, side-effect free URL
validation so it can be reused by admin writes, workers and adapters.
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_METADATA_IPS = frozenset(
    {
        "100.100.100.200",
        "169.254.169.254",
        "169.254.170.2",
        "169.254.170.23",
        "fd00:ec2::23",
        "fd00:ec2::254",
    }
)
_PRIVATE_PROVIDER_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "127.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    )
)


class ProviderUrlPolicyError(ValueError):
    """Raised when a provider endpoint violates the outbound URL policy."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProviderUrlTarget:
    """A provider URL pinned to an address that passed policy checks."""

    request_url: str
    host_header: str
    sni_hostname: str


def _address_is_allowed(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private_network: bool,
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if str(address) in _METADATA_IPS:
        return False
    # ``is_global`` is true for IPv4 multicast in Python's ipaddress module.
    # Multicast, reserved and unspecified destinations are never valid provider
    # endpoints, including when private-network access is explicitly enabled.
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    if address.is_global:
        return True
    return allow_private_network and any(
        address in network for network in _PRIVATE_PROVIDER_NETWORKS
    )


def validate_provider_base_url(
    base_url: str | None,
    *,
    allow_private_network: bool = False,
) -> str | None:
    """Normalize and validate an operator supplied provider base URL.

    Credentials and query/fragment components are rejected because they are
    frequently used to smuggle API keys into logs or proxy caches.  Private
    literal addresses require an explicit per-provider opt-in; hostname DNS
    resolution is deliberately left to the transport layer.
    """

    cleaned = str(base_url or "").strip()
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ProviderUrlPolicyError("invalid_url") from exc
    if scheme not in _ALLOWED_SCHEMES or not hostname:
        raise ProviderUrlPolicyError("invalid_url")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderUrlPolicyError("userinfo_not_allowed")
    if parsed.query or parsed.fragment:
        raise ProviderUrlPolicyError("query_or_fragment_not_allowed")
    if port is not None and not (1 <= port <= 65535):
        raise ProviderUrlPolicyError("invalid_port")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not _address_is_allowed(
        literal,
        allow_private_network=allow_private_network,
    ):
        raise ProviderUrlPolicyError("private_network_not_allowed")
    return cleaned.rstrip("/")


def provider_config_allows_private_network(config: object) -> bool:
    """Return whether a provider explicitly opts into private endpoints."""

    if not isinstance(config, dict):
        return False
    value = config.get("allow_private_network")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_provider_url(
    url: str,
    *,
    allow_private_network: bool = False,
    timeout_seconds: float = 5.0,
) -> ProviderUrlTarget:
    """Resolve a provider endpoint once and return an address-pinned target.

    The connection uses the resolved address while retaining the original Host
    header/SNI, preventing DNS rebinding between validation and request.
    """

    normalized = validate_provider_base_url(
        url,
        allow_private_network=allow_private_network,
    )
    if normalized is None:  # pragma: no cover - guarded by caller defaults
        raise ProviderUrlPolicyError("invalid_url")
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        addresses.add(ipaddress.ip_address(hostname))
    except ValueError:
        try:
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                socket.getaddrinfo,
                hostname,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            try:
                results = future.result(timeout=timeout_seconds)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        except (socket.gaierror, FutureTimeoutError, OSError) as exc:
            raise ProviderUrlPolicyError("dns_resolution_failed") from exc
        for item in results:
            try:
                addresses.add(ipaddress.ip_address(item[4][0].split("%", 1)[0]))
            except (IndexError, ValueError):
                continue
    if not addresses:
        raise ProviderUrlPolicyError("dns_resolution_failed")
    if any(
        not _address_is_allowed(
            address,
            allow_private_network=allow_private_network,
        )
        for address in addresses
    ):
        raise ProviderUrlPolicyError("private_network_not_allowed")
    address = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    pinned_host = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if scheme == "https" else 80
    netloc = pinned_host if port == default_port else f"{pinned_host}:{port}"
    request_url = urlunsplit((scheme, netloc, parsed.path or "/", "", ""))
    original_host = f"[{hostname}]" if ":" in hostname else hostname
    if port != default_port:
        original_host = f"{original_host}:{port}"
    return ProviderUrlTarget(
        request_url=request_url,
        host_header=original_host,
        sni_hostname=hostname,
    )


__all__ = [
    "ProviderUrlPolicyError",
    "ProviderUrlTarget",
    "provider_config_allows_private_network",
    "resolve_provider_url",
    "validate_provider_base_url",
]
