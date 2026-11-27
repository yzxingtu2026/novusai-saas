"""Canonical domain, HTTP authority and Origin parsing (no configuration I/O)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def normalize_hostname(value: str) -> str:
    """Normalize a hostname only; reject URLs, ports and wildcard labels."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Expected a hostname")
    if "%" in value:
        raise ValueError("Scoped addresses are not public hostnames")
    raw = value.removesuffix(".").lower()
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        pass
    try:
        host = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid IDNA hostname") from exc
    if len(host) > 253 or not all(_LABEL.fullmatch(part) for part in host.split(".")):
        raise ValueError("Invalid hostname")
    return host


def normalize_tenant_suffix(value: str) -> str:
    host = normalize_hostname(value.strip().removeprefix("."))
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f".{host}"
    raise ValueError("Tenant suffix must be a DNS hostname")


def normalize_host_header(value: str) -> str:
    """Parse an HTTP Host authority, including bracketed IPv6 and valid ports."""
    if not value or value != value.strip() or any(c.isspace() for c in value):
        return ""
    if any(c in value for c in "/\\?#@"):
        return ""
    try:
        parsed = urlsplit(f"//{value}")
        if parsed.hostname is None or parsed.username is not None:
            return ""
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return ""
        if value.endswith(":"):
            return ""
        return normalize_hostname(parsed.hostname)
    except ValueError:
        return ""


def normalize_configured_host(value: str) -> str:
    """Configuration and already parsed hosts may contain unbracketed IPv6."""
    try:
        return normalize_hostname(value)
    except ValueError:
        return normalize_host_header(value)


@dataclass(frozen=True)
class HttpOrigin:
    scheme: str
    host: str
    port: int | None

    @property
    def normalized(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        port = f":{self.port}" if self.port not in {None, default_port} else ""
        return f"{self.scheme}://{host}{port}"


def parse_http_origin(value: str) -> HttpOrigin | None:
    """Parse scheme + authority only; a client Origin is never a full URL."""
    if not value or value != value.strip() or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(c in value for c in "\\?#")
        ):
            return None
        host = normalize_host_header(parsed.netloc)
        if not host:
            return None
        return HttpOrigin(parsed.scheme, host, parsed.port)
    except ValueError:
        return None
