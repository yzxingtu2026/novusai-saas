"""Bounded provider transport, canonical zones and per-instance TXT ownership."""

from __future__ import annotations

import abc
import asyncio
import ipaddress
import json
import re
from typing import Any

import httpx

from app.core.domains import normalize_hostname
from app.core.i18n import _

MAX_RESPONSE_BYTES = 256 * 1024
MAX_PAGES = 1000


class DnsProviderError(RuntimeError):
    """Safe failure text; provider response bodies and credentials never escape."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_(f"ssl_certificate.dns_{code}"))


def normalize_zone(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(_("ssl_certificate.dns_zone_invalid"))
    host = normalize_hostname(value.strip())
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError(_("ssl_certificate.dns_zone_invalid"))


def normalize_record_name(value: str) -> str:
    prefix = "_acme-challenge."
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value.lower().startswith(prefix)
    ):
        raise ValueError(_("ssl_certificate.dns_record_invalid"))
    name = prefix + normalize_zone(value[len(prefix) :])
    if len(name) > 253:
        raise ValueError(_("ssl_certificate.dns_record_invalid"))
    return name


def relative_record_name(record_name: str, zone: str) -> str:
    name = normalize_record_name(record_name)
    if not name.endswith("." + zone):
        raise ValueError(_("ssl_certificate.dns_zone_mismatch"))
    return name[: -(len(zone) + 1)]


def require_string(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(c.isspace() for c in value)
    ):
        raise DnsProviderError("credentials_invalid")
    return value


async def bounded_json(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> dict[str, Any]:
    """Stream before decoding; Content-Length alone cannot enforce the limit."""
    try:
        async with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise DnsProviderError("request_failed")
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise DnsProviderError("response_too_large")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise DnsProviderError("response_too_large")
                raw.extend(chunk)
    except httpx.HTTPError:
        raise DnsProviderError("request_failed") from None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise DnsProviderError("response_invalid") from None
    if not isinstance(payload, dict):
        raise DnsProviderError("response_invalid")
    return payload


def positive_int(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not str(value).isdigit()
    ):
        raise DnsProviderError("response_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise DnsProviderError("response_invalid") from None
    if parsed < 1:
        raise DnsProviderError("response_invalid")
    return parsed


def require_record_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise DnsProviderError("response_invalid")
    return value


class DnsProvider(abc.ABC):
    @abc.abstractmethod
    async def set_txt_record(self, record_name: str, record_value: str) -> None:
        """Create a TXT challenge without replacing another operation's record."""

    @abc.abstractmethod
    async def delete_txt_record(self, record_name: str, record_value: str) -> None:
        """Clean up records owned by this instance only."""

    async def check_access(self) -> None:
        """Read-only API check, not a TXT write or CA validation."""
        raise DnsProviderError("provider_unsupported")


class OwnedTxtProvider(DnsProvider):
    """Callbacks share an instance for one operation; no adoption by TXT value."""

    def __init__(self):
        self._created: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def set_txt_record(self, record_name: str, record_value: str) -> None:
        name = normalize_record_name(record_name)
        if (
            not isinstance(record_value, str)
            or not record_value
            or len(record_value) > 4096
        ):
            raise ValueError(_("ssl_certificate.dns_record_invalid"))
        key = (name, record_value)
        async with self._lock:
            if key in self._created:
                raise DnsProviderError("record_already_owned")
            self._created[key] = await self._create_record(name, record_value)

    async def delete_txt_record(self, record_name: str, record_value: str) -> None:
        name = normalize_record_name(record_name)
        key = (name, record_value)
        async with self._lock:
            record_id = self._created.get(key)
            if record_id is None:
                return
            await self._delete_owned_record(name, record_value, record_id)
            del self._created[key]

    @abc.abstractmethod
    async def _create_record(self, name: str, value: str) -> str: ...

    @abc.abstractmethod
    async def _delete_owned_record(
        self, name: str, value: str, record_id: str
    ) -> None: ...
