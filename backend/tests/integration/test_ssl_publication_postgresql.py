"""Certificate writes must serialize on real PostgreSQL domain locks."""

import asyncio

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.core.config import settings
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant_domain import TenantDomain
from app.services.system.ssl_certificate_service import SslCertificateService
from tests.services.test_domain_lifecycle_audit import certificate

pytest_plugins = ["tests.integration.test_domain_registration_postgresql"]
pytestmark = pytest.mark.behavioral


@pytest.mark.asyncio
async def test_concurrent_upload_and_old_renewal_observe_domain_lock(
    postgres_domains, monkeypatch
):
    monkeypatch.setattr(
        settings, "SSL_PRIVATE_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )

    # The test schema deliberately excludes platform configuration tables.
    async def encryption(_self):
        return settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY

    monkeypatch.setattr(SslCertificateService, "_get_encryption_key", encryption)
    pem1, key1 = certificate(names=["shop.example.com"])
    pem2, key2 = certificate(names=["shop.example.com"])
    factory = postgres_domains
    async with factory() as seed:
        seed.add(
            TenantDomain(
                id=1,
                tenant_id=1,
                domain="shop.example.com",
                is_verified=True,
                ssl_status="pending",
            )
        )
        await seed.commit()
        original = await SslCertificateService(seed).store_platform_cert(
            1, 1, pem1, key1
        )
        original_id = original.id
        await seed.commit()

    held, release = asyncio.Event(), asyncio.Event()
    pids = {}

    async def upload():
        async with factory() as db:
            pids["upload"] = await db.scalar(select(func.pg_backend_pid()))
            new = await SslCertificateService(db).upload_custom_cert(1, 1, pem2, key2)
            held.set()
            await release.wait()
            await db.commit()
            return new.id

    async def old_job():
        await held.wait()
        async with factory() as db:
            pids["renewal"] = await db.scalar(select(func.pg_backend_pid()))
            result = await SslCertificateService(db).store_platform_cert(
                1,
                1,
                pem1,
                key1,
                expected_certificate_id=original_id,
                require_auto_renew=True,
                expected_domain="shop.example.com",
            )
            await db.commit()
            return result

    async def observe():
        try:
            async with factory() as db:
                async with asyncio.timeout(10):
                    while True:
                        if "renewal" in pids:
                            blockers = await db.scalar(
                                select(func.pg_blocking_pids(pids["renewal"]))
                            )
                            if blockers:
                                assert blockers == [pids["upload"]]
                                return True
                        await asyncio.sleep(0.02)
        finally:
            release.set()

    async with asyncio.timeout(15):
        uploaded_id, stale, observed = await asyncio.gather(
            upload(), old_job(), observe()
        )
    assert observed is True
    assert stale is None
    async with factory() as db:
        rows = list(
            (
                await db.execute(
                    select(DomainSslCertificate).order_by(DomainSslCertificate.id)
                )
            ).scalars()
        )
        assert [(row.id, row.status, row.cert_type) for row in rows] == [
            (original_id, "revoked", "platform"),
            (uploaded_id, "active", "custom"),
        ]
