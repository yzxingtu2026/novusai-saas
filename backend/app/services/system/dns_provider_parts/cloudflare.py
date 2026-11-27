"""Cloudflare DNS-01 transport, zone checks and owned-record pagination."""

from __future__ import annotations

import re

import httpx

from .common import (
    MAX_PAGES,
    DnsProviderError,
    OwnedTxtProvider,
    bounded_json,
    normalize_record_name,
    normalize_zone,
    positive_int,
    relative_record_name,
    require_record_id,
    require_string,
)


class CloudflareDnsProvider(OwnedTxtProvider):
    def __init__(self, api_token: str, zone_id: str):
        super().__init__()
        self._api_token = require_string(api_token)
        if not isinstance(zone_id, str) or not re.fullmatch(
            r"[0-9a-fA-F]{32}", zone_id
        ):
            raise DnsProviderError("zone_invalid")
        self._zone_id = zone_id
        self._zone_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
        self._base_url = self._zone_url + "/dns_records"
        self._zone_name: str | None = None

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            data = await bounded_json(
                client,
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._api_token}",
                    "Content-Type": "application/json",
                },
                **kwargs,
            )
        if data.get("success") is not True:
            raise DnsProviderError("request_failed")
        return data

    async def _get_zone(self) -> str:
        if self._zone_name is None:
            data = await self._request("GET", self._zone_url)
            zone = data.get("result")
            if not isinstance(zone, dict) or zone.get("id") != self._zone_id:
                raise DnsProviderError("response_invalid")
            self._zone_name = normalize_zone(require_string(zone.get("name")))
        return self._zone_name

    async def _records(self, name: str | None = None) -> list[dict]:
        records = []
        page = 1
        while page <= MAX_PAGES:
            params = {"type": "TXT", "page": page, "per_page": 100}
            if name is not None:
                params["name"] = name
            data = await self._request("GET", self._base_url, params=params)
            items = data.get("result")
            info = data.get("result_info")
            if (
                not isinstance(items, list)
                or not all(isinstance(item, dict) for item in items)
                or not isinstance(info, dict)
            ):
                raise DnsProviderError("response_invalid")
            if positive_int(info.get("page")) != page:
                raise DnsProviderError("response_invalid")
            # Cloudflare may report zero pages for an empty result.
            total = info.get("total_pages")
            if isinstance(total, bool):
                raise DnsProviderError("response_invalid")
            pages = 1 if total == 0 else positive_int(total)
            if pages > MAX_PAGES:
                raise DnsProviderError("pagination_limit")
            records.extend(items)
            if page >= pages:
                return records
            page += 1
        raise DnsProviderError("pagination_limit")

    async def check_access(self) -> None:
        await self._get_zone()
        await self._records()

    async def _create_record(self, name: str, value: str) -> str:
        relative_record_name(name, await self._get_zone())
        data = await self._request(
            "POST",
            self._base_url,
            json={"type": "TXT", "name": name, "content": value, "ttl": 60},
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise DnsProviderError("response_invalid")
        return require_record_id(result.get("id"))

    async def _delete_owned_record(self, name: str, value: str, record_id: str) -> None:
        relative_record_name(name, await self._get_zone())
        for record in await self._records(name):
            if record.get("id") != record_id:
                continue
            if (
                record.get("type") != "TXT"
                or record.get("content") != value
                or normalize_record_name(record.get("name", "")) != name
            ):
                raise DnsProviderError("record_changed")
            data = await self._request("DELETE", f"{self._base_url}/{record_id}")
            if (
                not isinstance(data.get("result"), dict)
                or data["result"].get("id") != record_id
            ):
                raise DnsProviderError("response_invalid")
            return
