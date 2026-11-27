"""附件响应缓存策略 / Cache policy for attachment delivery.

Test type: behavioral.
"""

from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.public import attachments as public_attachments
from app.api.shared._attachment_helpers import build_attachment_delivery_response
from app.api.user import attachments as user_attachments
from app.configs.service import ConfigService
from app.core.config import settings
from app.core.deps import get_current_active_tenant_user, get_db
from app.services.common import ImageProcessService
from app.services.tenant.attachment_download_service import AttachmentDownloadService
from app.storage import StorageConfig

pytestmark = pytest.mark.asyncio

OBJECT_URL = "https://storage.fixture.invalid/object?signature=fixture"
PROCESSED_URL = "https://storage.fixture.invalid/processed?signature=fixture"
LOCAL_CONTENT = b"fixture attachment"
IMAGE_CONTENT = b"fixture processed image"


@pytest.fixture(params=("public", "private"))
def attachment(request):
    return SimpleNamespace(
        id=41,
        tenant_id=7,
        uploader_id=9,
        source="tenant_user",
        visibility=request.param,
        business_type=None,
        driver="s3",
        original_name="fixture.txt",
        path="fixture.txt",
        size=len(LOCAL_CONTENT),
    )


@pytest.fixture
def download_service(monkeypatch, attachment):
    monkeypatch.setattr(
        settings, "SECRET_KEY", "fictional-attachment-test-signing-key-42"
    )
    monkeypatch.setattr(
        AttachmentDownloadService, "get_attachment", AsyncMock(return_value=attachment)
    )
    monkeypatch.setattr(AttachmentDownloadService, "record_download", AsyncMock())
    monkeypatch.setattr(
        AttachmentDownloadService,
        "get_redirect_url",
        AsyncMock(return_value=OBJECT_URL),
    )
    return AttachmentDownloadService(AsyncMock(), tenant_id=attachment.tenant_id)


@pytest.fixture
async def attachment_client(download_service, attachment):
    app = FastAPI()
    app.include_router(public_attachments.router, prefix="/api/public")
    app.include_router(user_attachments.router, prefix="/api/user")
    app.dependency_overrides[get_db] = lambda: download_service.db
    app.dependency_overrides[get_current_active_tenant_user] = lambda: SimpleNamespace(
        id=attachment.uploader_id, tenant_id=attachment.tenant_id
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        yield client


def _expected_cache_control(attachment, public_policy=None):
    if attachment.visibility == "private":
        return "private, no-store"
    return public_policy


async def test_local_attachment_access_delivers_bytes_with_cache_policy(
    attachment_client, download_service, attachment, monkeypatch, tmp_path
) -> None:
    attachment.driver = "local"
    (tmp_path / attachment.path).write_bytes(LOCAL_CONTENT)
    monkeypatch.setattr(
        AttachmentDownloadService,
        "_resolve_storage_config_for_attachment",
        AsyncMock(return_value=StorageConfig(driver="local", root_path=str(tmp_path))),
    )
    url = download_service.build_client_access_url(
        attachment.id, attachment.tenant_id, attachment.visibility
    )

    response = await attachment_client.get(url)

    assert response.status_code == 200, response.text
    assert response.content == LOCAL_CONTENT
    assert (
        response.headers["content-disposition"] == 'attachment; filename="fixture.txt"'
    )
    assert response.headers.get("cache-control") == _expected_cache_control(attachment)
    download_service.record_download.assert_awaited_once_with(attachment)
    download_service.get_redirect_url.assert_not_awaited()


@pytest.mark.parametrize("surface", ("public", "user"))
async def test_cloud_attachment_route_keeps_redirect_and_cache_policy(
    attachment_client, download_service, attachment, surface
) -> None:
    if surface == "public":
        url = download_service.build_client_access_url(
            attachment.id, attachment.tenant_id, attachment.visibility
        )
    else:
        url = f"/api/user/attachments/{attachment.id}/download"

    response = await attachment_client.get(url)

    assert response.status_code == 307, response.text
    assert response.headers["location"] == OBJECT_URL
    assert response.headers.get("cache-control") == _expected_cache_control(attachment)


async def test_shared_attachment_delivery_keeps_redirect_and_cache_policy(
    download_service, attachment
) -> None:
    response = await build_attachment_delivery_response(
        download_service, attachment_id=attachment.id, expires=60, preview=True
    )

    assert response.status_code == 307
    assert response.headers["location"] == OBJECT_URL
    assert response.headers.get("cache-control") == _expected_cache_control(attachment)


@pytest.mark.parametrize("mode", ("disabled", "empty", "cloud", "bytes"))
async def test_signed_image_response_preserves_content_and_sets_cache_policy(
    attachment_client, download_service, attachment, mode, monkeypatch
) -> None:
    monkeypatch.setattr(
        ConfigService, "get_platform_config", AsyncMock(return_value=60)
    )
    monkeypatch.setattr(public_attachments, "_image_rate_buckets", defaultdict(list))
    monkeypatch.setattr(
        ImageProcessService, "is_enabled", AsyncMock(return_value=mode != "disabled")
    )
    monkeypatch.setattr(
        ImageProcessService,
        "parse_params",
        AsyncMock(return_value=SimpleNamespace(is_empty=lambda: mode == "empty")),
    )
    rendered = (IMAGE_CONTENT, "image/png") if mode == "bytes" else PROCESSED_URL
    monkeypatch.setattr(
        ImageProcessService,
        "get_processed_image_response",
        AsyncMock(return_value=rendered),
    )
    url = download_service.build_preview_url(
        attachment.id, attachment.tenant_id, attachment.visibility
    )

    response = await attachment_client.get(url if mode == "empty" else f"{url}&w=320")

    if mode == "bytes":
        assert response.status_code == 200, response.text
        assert response.content == IMAGE_CONTENT
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"] == "inline"
        public_policy = "public, max-age=31536000"
    else:
        assert response.status_code == 307, response.text
        assert response.headers["location"] == (
            PROCESSED_URL if mode == "cloud" else OBJECT_URL
        )
        public_policy = None
    assert response.headers.get("cache-control") == _expected_cache_control(
        attachment, public_policy
    )
