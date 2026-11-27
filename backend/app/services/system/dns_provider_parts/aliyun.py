"""AliDNS HTTPS RPC 2015-01-09, signed with the official HMAC-SHA1 scheme."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .common import (
    MAX_PAGES,
    DnsProviderError,
    OwnedTxtProvider,
    bounded_json,
    normalize_zone,
    positive_int,
    relative_record_name,
    require_record_id,
    require_string,
)


def sign_alidns_parameters(params: dict[str, str], secret: str) -> str:
    """RFC3986 sorted canonical query; signing key is AccessKeySecret + '&'."""
    canonical = "&".join(
        f"{quote(key, safe='~')}={quote(value, safe='~')}"
        for key, value in sorted(params.items())
        if key != "Signature"
    )
    string_to_sign = "POST&%2F&" + quote(canonical, safe="~")
    return base64.b64encode(
        hmac.new(
            (secret + "&").encode(), string_to_sign.encode(), hashlib.sha1
        ).digest()
    ).decode("ascii")


class AliDnsProvider(OwnedTxtProvider):
    def __init__(self, access_key_id: str, access_key_secret: str, zone_name: str):
        super().__init__()
        self._access_key_id = require_string(access_key_id)
        self._access_key_secret = require_string(access_key_secret)
        self._zone_name = normalize_zone(zone_name)

    async def _rpc(self, action: str, **fields: str) -> dict:
        params = {
            "AccessKeyId": self._access_key_id,
            "Action": action,
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": "2015-01-09",
            **fields,
        }
        params["Signature"] = sign_alidns_parameters(params, self._access_key_secret)
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            data = await bounded_json(
                client, "POST", "https://alidns.aliyuncs.com/", data=params
            )
        if data.get("Code"):
            # Never retain provider Message, signed body or credential values.
            raise DnsProviderError("request_failed")
        return data

    async def _records(self, rr: str | None = None) -> list[dict]:
        records = []
        page = 1
        while page <= MAX_PAGES:
            fields = {
                "DomainName": self._zone_name,
                "PageNumber": str(page),
                "PageSize": "100",
                "TypeKeyWord": "TXT",
            }
            if rr is not None:
                fields["RRKeyWord"] = rr
                fields["SearchMode"] = "EXACT"
            data = await self._rpc("DescribeDomainRecords", **fields)
            container = data.get("DomainRecords")
            if not isinstance(container, dict):
                raise DnsProviderError("response_invalid")
            items = container.get("Record")
            if not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items
            ):
                raise DnsProviderError("response_invalid")
            if positive_int(data.get("PageNumber")) != page:
                raise DnsProviderError("response_invalid")
            size = positive_int(data.get("PageSize"))
            total = data.get("TotalCount")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise DnsProviderError("response_invalid")
            records.extend(items)
            pages = max(1, math.ceil(total / size))
            if pages > MAX_PAGES:
                raise DnsProviderError("pagination_limit")
            if page >= pages:
                return records
            page += 1
        raise DnsProviderError("pagination_limit")

    async def check_access(self) -> None:
        # Exact zone API access only: no TXT write/delete and no CA call.
        await self._records()

    async def _create_record(self, name: str, value: str) -> str:
        rr = relative_record_name(name, self._zone_name)
        data = await self._rpc(
            "AddDomainRecord",
            DomainName=self._zone_name,
            RR=rr,
            Type="TXT",
            Value=value,
            TTL="600",
        )
        return require_record_id(data.get("RecordId"))

    async def _delete_owned_record(self, name: str, value: str, record_id: str) -> None:
        rr = relative_record_name(name, self._zone_name)
        for record in await self._records(rr):
            if record.get("RecordId") != record_id:
                continue
            if (
                record.get("RR") != rr
                or record.get("Value") != value
                or record.get("Type") != "TXT"
            ):
                raise DnsProviderError("record_changed")
            data = await self._rpc("DeleteDomainRecord", RecordId=record_id)
            if data.get("RecordId") != record_id:
                raise DnsProviderError("response_invalid")
            return
        # Already removed externally; never delete a same-valued foreign record.
