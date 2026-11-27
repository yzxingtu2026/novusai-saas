"""Real ACME-library protocol regressions at a scripted CA transport boundary."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import josepy
import pytest
import requests
from acme import client, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.system.acme_client import AcmeClient

pytestmark = pytest.mark.behavioral
BASE = "https://acme-staging-v02.api.letsencrypt.org/"


class ScriptedCa(client.ClientV2):
    """Only transport is replaced; real new_order/poll/finalize parse protocol."""

    def __init__(self):
        super().__init__(messages.Directory({"newOrder": BASE + "new-order"}), None)
        self.answered = False
        self.pem = None
        self.csr = None
        self.issuer_key = rsa.generate_private_key(65537, 2048)

    def response(self, payload, *, uri=None):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        if uri:
            response.headers["Location"] = uri
        return response

    def challenge(self):
        return {
            "type": "dns-01",
            "url": BASE + "challenge",
            "token": "MDEyMzQ1Njc4OWFiY2RlZg",
            "status": "valid" if self.answered else "pending",
        }

    def order(self, status):
        return {
            "status": status,
            "authorizations": [BASE + "authz"],
            "finalize": BASE + "finalize",
            "identifiers": [{"type": "dns", "value": "shop.example.com"}],
            "certificate": BASE + "cert",
        }

    def _post(self, url, body, **_kwargs):
        if url == BASE + "new-order":
            assert [item.value for item in body.identifiers] == ["shop.example.com"]
            return self.response(self.order("pending"), uri=BASE + "order")
        if url == BASE + "challenge":
            self.answered = True
            response = self.response(self.challenge())
            response.headers["Link"] = f'<{BASE}authz>;rel="up"'
            return response
        if url == BASE + "finalize":
            self.csr = body.csr
            builder = (
                x509.CertificateBuilder()
                .subject_name(self.csr.subject)
                .issuer_name(self.csr.subject)
                .public_key(self.csr.public_key())
                .serial_number(1)
                .not_valid_before(datetime.now() - timedelta(days=1))
                .not_valid_after(datetime.now() + timedelta(days=90))
            )
            for extension in self.csr.extensions:
                builder = builder.add_extension(extension.value, extension.critical)
            self.pem = (
                builder.sign(self.issuer_key, hashes.SHA256())
                .public_bytes(serialization.Encoding.PEM)
                .decode()
            )
            return self.response(self.order("valid"))
        raise AssertionError("Unexpected CA transport endpoint")

    def _post_as_get(self, url):
        if url == BASE + "authz":
            return self.response(
                {
                    "identifier": {"type": "dns", "value": "shop.example.com"},
                    "status": "valid" if self.answered else "pending",
                    "challenges": [self.challenge()],
                },
                uri=url,
            )
        if url == BASE + "order":
            return self.response(self.order("valid" if self.pem else "ready"))
        if url == BASE + "cert":
            response = requests.Response()
            response.status_code = 200
            response._content = self.pem.encode()
            return response
        raise AssertionError("Unexpected CA transport endpoint")


@pytest.mark.asyncio
async def test_issued_certificate_matches_returned_key_with_real_library(monkeypatch):
    wrapper = AcmeClient(use_staging=True)
    ca = ScriptedCa()
    wrapper._client = ca
    wrapper._account_key = josepy.JWKRSA(key=rsa.generate_private_key(65537, 2048))
    dns_records = {}

    async def setter(name, value):
        dns_records[name] = value

    async def deleter(name, value):
        assert dns_records.pop(name) == value

    async def propagated(name, value):
        assert dns_records[name] == value

    original_poll = wrapper._poll_challenge_status

    async def fast_poll(authz):
        return await original_poll(authz, timeout=0.05, interval=0)

    monkeypatch.setattr(wrapper, "_wait_for_dns_propagation", propagated)
    monkeypatch.setattr(wrapper, "_poll_challenge_status", fast_poll)
    monkeypatch.setattr("acme.client.time.sleep", lambda _seconds: None)
    pem, key, chain = await asyncio.wait_for(
        wrapper.provision_certificate("shop.example.com", setter, deleter), timeout=2
    )
    cert = x509.load_pem_x509_certificate(pem.encode())
    private = serialization.load_pem_private_key(key.encode(), password=None)
    assert cert.public_key().public_numbers() == private.public_key().public_numbers()
    assert ca.csr.public_key().public_numbers() == private.public_key().public_numbers()
    assert cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName) == ["shop.example.com"]
    assert dns_records == {}
    assert chain is None


@pytest.mark.asyncio
async def test_real_invalid_authorization_fails_immediately():
    wrapper = AcmeClient(use_staging=True)

    class Transport:
        def poll(self, _authz):
            return SimpleNamespace(
                body=SimpleNamespace(status=messages.STATUS_INVALID)
            ), None

    wrapper._client = Transport()
    with pytest.raises(RuntimeError, match="validation failed"):
        await wrapper._poll_challenge_status(None, timeout=0.01, interval=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_dns_is_cleaned_when_propagation_fails_or_job_is_cancelled(
    monkeypatch, cancelled
):
    wrapper = AcmeClient(use_staging=True)
    wrapper._client = ScriptedCa()
    wrapper._account_key = josepy.JWKRSA(key=rsa.generate_private_key(65537, 2048))
    records = {}

    async def setter(name, value):
        records[name] = value

    async def deleter(name, value):
        assert records.pop(name) == value

    async def propagation(*_args):
        raise asyncio.CancelledError() if cancelled else TimeoutError("propagation")

    monkeypatch.setattr(wrapper, "_wait_for_dns_propagation", propagation)
    with pytest.raises(asyncio.CancelledError if cancelled else TimeoutError):
        await wrapper.provision_certificate("shop.example.com", setter, deleter)
    assert records == {}
    assert wrapper._client.answered is False


@pytest.mark.asyncio
async def test_propagation_timeout_never_answers_challenge(monkeypatch):
    import dns.resolver

    def missing(*_args, **_kwargs):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", missing)
    with pytest.raises(TimeoutError, match="DNS propagation timed out"):
        await AcmeClient(use_staging=True)._wait_for_dns_propagation(
            "_acme-challenge.shop.example.com",
            "missing",
            timeout=0.01,
            interval=0,
        )
