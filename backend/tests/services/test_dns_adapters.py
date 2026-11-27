"""Behavioral tests at the actual RPC/HTTP boundary, with synthetic credentials."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from urllib.parse import parse_qsl, quote

import httpx
import pytest

from app.services.system import dns_provider as module

pytestmark = pytest.mark.behavioral
ZONE_ID = "a" * 32
NAME = "_acme-challenge.app.example.com"


@pytest.fixture
def http_boundary(monkeypatch):
    real_client = httpx.AsyncClient
    options = []

    def install(handler):
        def client_factory(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client_factory)
        return options

    return install


def test_alidns_rpc_signature_fixed_vector():
    from app.services.system.dns_provider_parts.aliyun import sign_alidns_parameters

    params = {
        "AccessKeyId": "testid",
        "Action": "AddDomainRecord",
        "DomainName": "example.com",
        "Format": "JSON",
        "RR": "_acme-challenge.app",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": "fixture-nonce",
        "SignatureVersion": "1.0",
        "Timestamp": "2015-01-09T12:00:00Z",
        "Type": "TXT",
        "Value": "token+ /~*",
        "Version": "2015-01-09",
    }
    assert (
        sign_alidns_parameters(params, "testsecret") == "aQoOlYY0bvlRujk/4ZKJTqQeG1s="
    )
    assert "Signature" not in params


def assert_rpc_signature(request):
    assert request.method == "POST"
    assert str(request.url) == "https://alidns.aliyuncs.com/"
    params = dict(parse_qsl(request.content.decode()))
    signature = params.pop("Signature")
    canonical = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items())
    )
    message = "POST&%2F&" + quote(canonical, safe="~")
    expected = base64.b64encode(
        hmac.new(b"testsecret&", message.encode(), hashlib.sha1).digest()
    ).decode()
    assert signature == expected
    assert params["SignatureMethod"] == "HMAC-SHA1"
    assert params["Version"] == "2015-01-09"
    return params


async def test_aliyun_signed_create_paginated_owned_cleanup(http_boundary):
    calls = []

    def handler(request):
        p = assert_rpc_signature(request)
        calls.append(p)
        if p["Action"] == "AddDomainRecord":
            assert p["DomainName"] == "example.com"
            assert p["RR"] == "_acme-challenge.app"
            assert p["Type"] == "TXT" and p["Value"] == "token+ /~*"
            return httpx.Response(200, json={"RecordId": "owned-id"})
        if p["Action"] == "DescribeDomainRecords":
            page = int(p["PageNumber"])
            record = {
                "RecordId": "foreign-id" if page == 1 else "owned-id",
                "RR": "_acme-challenge.app",
                "Type": "TXT",
                "Value": "token+ /~*",
            }
            return httpx.Response(
                200,
                json={
                    "PageNumber": page,
                    "PageSize": 1,
                    "TotalCount": 2,
                    "DomainRecords": {"Record": [record]},
                },
            )
        assert p["Action"] == "DeleteDomainRecord"
        assert p["RecordId"] == "owned-id"
        return httpx.Response(200, json={"RecordId": "owned-id"})

    options = http_boundary(handler)
    provider = module.AliDnsProvider("testid", "testsecret", "EXAMPLE.COM.")
    await provider.set_txt_record(NAME.upper() + ".", "token+ /~*")
    await provider.delete_txt_record(NAME, "token+ /~*")
    await provider.delete_txt_record(NAME, "token+ /~*")
    assert [p["Action"] for p in calls] == [
        "AddDomainRecord",
        "DescribeDomainRecords",
        "DescribeDomainRecords",
        "DeleteDomainRecord",
    ]
    assert len({p["SignatureNonce"] for p in calls}) == 4
    assert all(
        o["trust_env"] is False
        and o["follow_redirects"] is False
        and o["timeout"] == 30
        for o in options
    )


@pytest.mark.parametrize(
    "name",
    [
        "_acme-challenge.notexample.com",
        "_acme-challenge.example.com.evil.test",
        "https://example.com",
        "_acme-challenge.127.0.0.1",
        "_acme-challenge.app.example.com ",
    ],
)
async def test_aliyun_zone_mismatch_rejected_before_network(http_boundary, name):
    http_boundary(lambda _request: pytest.fail("Zone mismatch must not reach provider"))
    provider = module.AliDnsProvider("testid", "testsecret", "example.com")
    with pytest.raises(ValueError):
        await provider.set_txt_record(name, "token")


async def test_aliyun_rpc_errors_sanitized_and_no_borrowed_cleanup(http_boundary):
    calls = []

    def handler(request):
        calls.append(assert_rpc_signature(request)["Action"])
        return httpx.Response(
            200, json={"Code": "InvalidAccessKeyId", "Message": "contains-testsecret"}
        )

    http_boundary(handler)
    provider = module.AliDnsProvider("testid", "testsecret", "example.com")
    with pytest.raises(RuntimeError) as exc:
        await provider.set_txt_record(NAME, "token")
    assert "testsecret" not in str(exc.value)
    await provider.delete_txt_record(NAME, "token")
    assert calls == ["AddDomainRecord"]


async def test_aliyun_concurrent_workers_only_delete_their_record_ids(http_boundary):
    records = {}
    deleted = []

    def handler(request):
        p = assert_rpc_signature(request)
        if p["Action"] == "AddDomainRecord":
            record_id = str(len(records) + 1)
            records[record_id] = {
                "RecordId": record_id,
                "RR": p["RR"],
                "Value": p["Value"],
                "Type": "TXT",
            }
            return httpx.Response(200, json={"RecordId": record_id})
        if p["Action"] == "DescribeDomainRecords":
            return httpx.Response(
                200,
                json={
                    "PageNumber": 1,
                    "PageSize": 100,
                    "TotalCount": len(records),
                    "DomainRecords": {"Record": list(records.values())},
                },
            )
        deleted.append(p["RecordId"])
        del records[p["RecordId"]]
        return httpx.Response(200, json={"RecordId": p["RecordId"]})

    http_boundary(handler)
    first = module.AliDnsProvider("testid", "testsecret", "example.com")
    second = module.AliDnsProvider("testid", "testsecret", "example.com")
    await asyncio.gather(
        first.set_txt_record(NAME, "same-token"),
        second.set_txt_record(NAME, "same-token"),
    )
    await first.delete_txt_record(NAME, "same-token")
    assert deleted == ["1"] and list(records) == ["2"]
    await second.delete_txt_record(NAME, "same-token")
    assert deleted == ["1", "2"] and records == {}


async def test_cloudflare_cleanup_pagination_success_and_ownership(http_boundary):
    deleted = []
    pages = []
    reject_delete = True

    def handler(request):
        if request.url.path == f"/client/v4/zones/{ZONE_ID}":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"id": ZONE_ID, "name": "example.com"},
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200, json={"success": True, "result": {"id": "owned-id"}}
            )
        if request.method == "GET":
            page = int(request.url.params["page"])
            pages.append(page)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {
                            "id": "foreign-id" if page == 1 else "owned-id",
                            "name": NAME,
                            "content": "token",
                            "type": "TXT",
                        }
                    ],
                    "result_info": {"page": page, "total_pages": 2},
                },
            )
        assert request.url.path.endswith("/owned-id")
        deleted.append("owned-id")
        return httpx.Response(
            200, json={"success": not reject_delete, "result": {"id": "owned-id"}}
        )

    http_boundary(handler)
    provider = module.CloudflareDnsProvider("token", ZONE_ID)
    await provider.set_txt_record(NAME, "token")
    with pytest.raises(RuntimeError):
        await provider.delete_txt_record(NAME, "token")
    reject_delete = False
    await provider.delete_txt_record(NAME, "token")
    assert deleted == ["owned-id", "owned-id"]
    assert pages == [1, 2, 1, 2]


async def test_cloudflare_wrong_zone_blocks_write(http_boundary):
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(
            200,
            json={"success": True, "result": {"id": ZONE_ID, "name": "other.example"}},
        )

    http_boundary(handler)
    with pytest.raises(ValueError):
        await module.CloudflareDnsProvider("token", ZONE_ID).set_txt_record(
            NAME, "token"
        )
    assert methods == ["GET"]


@pytest.mark.parametrize("provider_kind", ["aliyun", "cloudflare"])
async def test_provider_response_stream_size_bound(http_boundary, provider_kind):
    class TooLarge(httpx.AsyncByteStream):
        consumed = 0

        async def __aiter__(self):
            for _ in range(10):
                self.consumed += 1
                yield b"x" * 65536

    stream = TooLarge()
    http_boundary(lambda _request: httpx.Response(200, stream=stream))
    provider = (
        module.AliDnsProvider("testid", "testsecret", "example.com")
        if provider_kind == "aliyun"
        else module.CloudflareDnsProvider("token", ZONE_ID)
    )
    with pytest.raises(RuntimeError) as exc:
        await provider.set_txt_record(NAME, "token")
    assert exc.value.code == "response_too_large"
    assert stream.consumed < 10


async def test_aliyun_failed_cleanup_retains_id_for_retry(http_boundary):
    denied = True
    deletes = []

    def handler(request):
        p = assert_rpc_signature(request)
        if p["Action"] == "AddDomainRecord":
            return httpx.Response(200, json={"RecordId": "owned-id"})
        if p["Action"] == "DescribeDomainRecords":
            return httpx.Response(
                200,
                json={
                    "PageNumber": 1,
                    "PageSize": 100,
                    "TotalCount": 1,
                    "DomainRecords": {
                        "Record": [
                            {
                                "RecordId": "owned-id",
                                "RR": "_acme-challenge.app",
                                "Type": "TXT",
                                "Value": "token",
                            }
                        ]
                    },
                },
            )
        deletes.append(p["RecordId"])
        return httpx.Response(
            200,
            json={"Code": "Denied", "Message": "testsecret"}
            if denied
            else {"RecordId": "owned-id"},
        )

    http_boundary(handler)
    provider = module.AliDnsProvider("testid", "testsecret", "example.com")
    await provider.set_txt_record(NAME, "token")
    with pytest.raises(RuntimeError):
        await provider.delete_txt_record(NAME, "token")
    denied = False
    await provider.delete_txt_record(NAME, "token")
    await provider.delete_txt_record(NAME, "token")
    assert deletes == ["owned-id", "owned-id"]


async def test_same_instance_same_token_cannot_borrow_pending_id(http_boundary):
    creates = []

    def handler(request):
        p = assert_rpc_signature(request)
        creates.append(p["Action"])
        assert p["Action"] == "AddDomainRecord"
        return httpx.Response(200, json={"RecordId": "owned-id"})

    http_boundary(handler)
    provider = module.AliDnsProvider("testid", "testsecret", "example.com")
    results = await asyncio.gather(
        provider.set_txt_record(NAME, "token"),
        provider.set_txt_record(NAME, "token"),
        return_exceptions=True,
    )
    assert sum(item is None for item in results) == 1
    failures = [item for item in results if isinstance(item, RuntimeError)]
    assert len(failures) == 1 and failures[0].code == "record_already_owned"
    assert creates == ["AddDomainRecord"]


@pytest.mark.parametrize("kind", ["aliyun", "cloudflare"])
async def test_provider_transport_exception_does_not_expose_request(
    kind, http_boundary
):
    def handler(request):
        raise httpx.ConnectError("testsecret provider request detail", request=request)

    http_boundary(handler)
    provider = (
        module.AliDnsProvider("testid", "testsecret", "example.com")
        if kind == "aliyun"
        else module.CloudflareDnsProvider("testsecret", ZONE_ID)
    )
    with pytest.raises(RuntimeError) as exc:
        await provider.set_txt_record(NAME, "token")
    assert exc.value.code == "request_failed"
    assert "testsecret" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


async def test_aliyun_unicode_zone_and_apex_challenge_canonicalized(http_boundary):
    calls = []

    def handler(request):
        p = assert_rpc_signature(request)
        calls.append(p)
        return httpx.Response(200, json={"RecordId": "owned-id"})

    http_boundary(handler)
    provider = module.AliDnsProvider("testid", "testsecret", "BÜCHER.EXAMPLE.")
    await provider.set_txt_record("_acme-challenge.BÜCHER.EXAMPLE.", "token")
    assert calls[0]["DomainName"] == "xn--bcher-kva.example"
    assert calls[0]["RR"] == "_acme-challenge"


@pytest.mark.parametrize(
    "payload",
    [
        {"PageNumber": 1, "PageSize": 100, "TotalCount": 1, "DomainRecords": []},
        {
            "PageNumber": 1,
            "PageSize": 1,
            "TotalCount": 1001,
            "DomainRecords": {"Record": []},
        },
        {
            "PageNumber": 2,
            "PageSize": 100,
            "TotalCount": 1,
            "DomainRecords": {"Record": []},
        },
    ],
)
async def test_aliyun_malformed_or_unbounded_pagination_fails_closed(
    payload, http_boundary
):
    calls = []

    def handler(request):
        calls.append(assert_rpc_signature(request)["Action"])
        return httpx.Response(200, json=payload)

    http_boundary(handler)
    provider = module.AliDnsProvider("testid", "testsecret", "example.com")
    with pytest.raises(RuntimeError):
        await provider.check_access()
    assert calls == ["DescribeDomainRecords"]


async def test_modified_owned_cloudflare_record_is_not_deleted(http_boundary):
    deletes = []

    def handler(request):
        if request.url.path == f"/client/v4/zones/{ZONE_ID}":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"id": ZONE_ID, "name": "example.com"},
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200, json={"success": True, "result": {"id": "owned-id"}}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {
                            "id": "owned-id",
                            "name": NAME,
                            "type": "TXT",
                            "content": "changed",
                        }
                    ],
                    "result_info": {"page": 1, "total_pages": 1},
                },
            )
        deletes.append(str(request.url))
        return httpx.Response(200, json={"success": True})

    http_boundary(handler)
    provider = module.CloudflareDnsProvider("token", ZONE_ID)
    await provider.set_txt_record(NAME, "token")
    with pytest.raises(RuntimeError) as exc:
        await provider.delete_txt_record(NAME, "token")
    assert exc.value.code == "record_changed"
    assert deletes == []
