"""Reuse Design's local upload receipts / 复用 Design 本地上传凭据。"""

from __future__ import annotations

import hashlib
from typing import Any

import anyio

from app.storage.drivers.local import LocalStorageDriver

from .migration_object_path import relative_parts


def new_receipt(driver: Any):
    """Adapt the installed driver capability / 按已安装驱动能力选用凭据。"""
    if (
        type(driver) is not LocalStorageDriver
        or not callable(getattr(driver, "put_owned", None))
        or not callable(getattr(driver, "owned_metadata_path", None))
    ):
        return None
    from app.storage.local_upload_receipt import LocalUploadReceipt

    if not callable(getattr(LocalUploadReceipt, "snapshot", None)) or not callable(
        getattr(LocalUploadReceipt, "from_snapshot", None)
    ):
        return None
    return LocalUploadReceipt(driver.root)


async def remove_exact_pair(driver, path: str, identity: dict | None) -> str | None:
    """Delete only the recorded file and sidecar / 只结算实写记录中的文件和侧写。"""
    if type(identity) is not dict or new_receipt(driver) is None:
        return None
    from app.storage.local_upload_receipt import LocalUploadReceipt

    def remove() -> str | None:
        parts = relative_parts(path)
        root = driver.root.resolve(strict=True)
        leaf = driver._full_path(path)
        sidecar = driver.owned_metadata_path(path)
        cache_namespace = identity.get("cacheNamespace")
        if not isinstance(cache_namespace, str):
            return None
        previous_cache = (
            root
            if cache_namespace == "."
            else root.joinpath(*relative_parts(cache_namespace))
        )
        if (
            leaf != root.joinpath(*parts)
            or "managed-v4" in parts
            or previous_cache.resolve() != previous_cache
            or driver._count_variants(path)
        ):
            return None
        prefix = hashlib.md5(path.encode(), usedforsecurity=False).hexdigest()[:8] + "_"
        if previous_cache.is_dir() and any(
            child.name.startswith(prefix) for child in previous_cache.iterdir()
        ):
            return None
        receipt = LocalUploadReceipt.from_snapshot(
            root, identity, {path, sidecar.relative_to(root).as_posix()}
        )
        existed = leaf.exists() or sidecar.exists()
        if receipt is None or not receipt.cleanup():
            return None
        return "deleted" if existed else "absent"

    return await anyio.to_thread.run_sync(remove)
