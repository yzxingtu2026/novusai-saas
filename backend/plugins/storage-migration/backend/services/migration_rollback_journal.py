"""Typed private rollback restoration material; never infer historical authority."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

import anyio

from app.exceptions import StorageError

from .migration_local_receipts import new_receipt
from .migration_object_path import relative_parts

JOURNAL_STATES = frozenset({"source_restored", "target_deleting", "target_deleted"})
_SNAPSHOT_FIELDS = frozenset(
    {
        "version",
        "task_id",
        "log_id",
        "attachment_id",
        "tenant_id",
        "path",
        "size",
        "sha256",
        "migrated_at",
        "target_driver",
        "target_base_url",
        "target_meta",
        "target_origin_id",
        "target_root_path",
    }
)


class RollbackHold(Exception):
    """Fixed machine reason only; never retain configuration or provider text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def rollback_reason(exc: Exception, default: str, *, local_boundary=False) -> str:
    if isinstance(exc, RollbackHold):
        return exc.reason
    if local_boundary and isinstance(exc, StorageError) and type(exc.data) is dict:
        reason = exc.data.get("reason")
        if type(reason) is str and reason in {
            "storage_origin_unverified",
            "storage_origin_changed",
            "storage_origin_unavailable",
            "storage_content_unverified",
            "storage_content_changed",
            "storage_legacy_cache_retirement_required",
            "storage_migration_cleanup_unverified",
            "storage_migration_cleanup_claim_uncommitted",
            "storage_migration_preserved_copy_unverified",
            "storage_migration_lineage_unverified",
            "storage_recovery_ownership_lost",
            "storage_cache_generation_retirement_required",
            "storage_cache_cleanup_pending",
            "storage_cache_artifact_unverified",
        }:
            return reason
    return default


async def verify_originals(
    source_storage: Any, materials: list[dict[str, Any]]
) -> None:
    """Confirm the configured retained source and its bytes / 验证配置所指的保留源和字节。"""
    config = source_storage.config
    for material in materials:
        snapshot = (material["old_meta"] or {}).get("_storage_snapshot")
        if (
            type(snapshot) is not dict
            or snapshot.get("driver") != config.driver
            or snapshot.get("root_path") != config.root_path
            or snapshot.get("base_url") != config.base_url
        ):
            raise RollbackHold("storage_source_location_unverified")
        content = await source_storage.get(material["file_path"])
        if isinstance(content, (bytes, bytearray)):
            size, digest = len(content), hashlib.sha256(content).hexdigest()
        else:
            size, digest = await anyio.to_thread.run_sync(
                _digest_stream, content, material["size"]
            )
        if size != material["size"] or digest != material["hash"].lower():
            raise RollbackHold("storage_source_content_unverified")


def _digest_stream(content, maximum):
    count = 0
    hasher = hashlib.sha256()
    with content:
        while chunk := content.read(65536):
            count += len(chunk)
            if count > maximum:
                break
            hasher.update(chunk)
    return count, hasher.hexdigest()


def _cycle(log) -> str:
    value = log.get("migrated_at")
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RollbackHold("storage_restoration_material_invalid")
    return value.isoformat()


def validate_material(log, task, deserialize):
    item = dict(log)
    previous = deserialize(item.get("old_meta"))
    if (
        type(item.get("attachment_id")) is not int
        or item["attachment_id"] <= 0
        or item.get("old_driver") != task["source_driver"]
        or (
            item.get("old_base_url") is not None
            and type(item["old_base_url"]) is not str
        )
        or (previous is not None and type(previous) is not dict)
    ):
        raise RollbackHold("storage_restoration_material_invalid")
    item["file_path"] = "/".join(relative_parts(item["file_path"]))
    item["target_path"] = item["file_path"]
    forward = deserialize(item.get("forward_snapshot"))
    if type(forward) is dict and forward.get("version") in {2, 3}:
        target = forward.get("target")
        source = forward.get("source")
        frozen_log = forward.get("log")
        frozen_task = forward.get("task")
        path = forward.get("target_path")
        if (
            type(target) is not dict
            or type(source) is not dict
            or type(frozen_log) is not dict
            or type(frozen_task) is not dict
            or frozen_task.get("id") != task["id"]
            or frozen_log.get("id") != item["id"]
            or frozen_log.get("task_id") != task["id"]
            or frozen_log.get("attachment_id") != item["attachment_id"]
            or frozen_log.get("file_path") != item["file_path"]
            or source.get("path") != item["file_path"]
            or source.get("id") != item["attachment_id"]
            or target.get("path") != path
            or target.get("driver") != task["target_driver"]
            or forward.get("token") != item.get("forward_token")
            or type(path) is not str
            or "/".join(relative_parts(path)) != path
        ):
            raise RollbackHold("storage_restoration_material_invalid")
        item["target_path"] = path
    item["old_meta"] = previous
    item["forward_snapshot"] = forward
    item["cycle"] = _cycle(item)
    state = item.get("rollback_state")
    if state not in JOURNAL_STATES | {"none"}:
        raise RollbackHold("storage_rollback_journal_invalid")
    snapshot = deserialize(item.get("rollback_snapshot"))
    if state == "none":
        if snapshot is not None:
            raise RollbackHold("storage_rollback_journal_invalid")
        item["rollback_snapshot"] = None
        return item
    if (
        type(snapshot) is not dict
        or set(snapshot)
        != _SNAPSHOT_FIELDS
        | ({"target_path"} if snapshot.get("version") in {2, 3} else set())
        | ({"target_local_receipt"} if snapshot.get("version") == 3 else set())
        or type(snapshot.get("version")) is not int
        or snapshot["version"] not in {1, 2, 3}
        or any(
            type(snapshot.get(key)) is not int
            for key in ("task_id", "log_id", "attachment_id")
        )
        or snapshot.get("task_id") != task["id"]
        or snapshot.get("log_id") != item["id"]
        or snapshot.get("attachment_id") != item["attachment_id"]
        or snapshot.get("path") != item["file_path"]
        or snapshot.get("target_path", snapshot.get("path")) != item["target_path"]
        or snapshot.get("migrated_at") != item["cycle"]
        or snapshot.get("target_driver") != task["target_driver"]
        or type(snapshot.get("size")) is not int
        or snapshot["size"] < 0
        or type(snapshot.get("sha256")) is not str
        or re.fullmatch(r"[a-fA-F0-9]{64}", snapshot["sha256"]) is None
        or (
            snapshot.get("tenant_id") is not None
            and type(snapshot["tenant_id"]) is not int
        )
        or (
            snapshot.get("target_meta") is not None
            and type(snapshot["target_meta"]) is not dict
        )
        or (
            snapshot.get("target_base_url") is not None
            and type(snapshot["target_base_url"]) is not str
        )
        or type(snapshot.get("target_root_path")) is not str
        or not snapshot["target_root_path"]
        or (
            snapshot.get("target_origin_id") is not None
            and type(snapshot["target_origin_id"]) is not str
        )
        or (
            snapshot["version"] == 3
            and snapshot["target_local_receipt"]
            != forward["target"]["local_object_identity"]
        )
    ):
        raise RollbackHold("storage_rollback_journal_invalid")
    item.update(
        rollback_snapshot=snapshot, size=snapshot["size"], hash=snapshot["sha256"]
    )
    return item


def check_attachment(attachment, item, task, *, restored):
    snapshot = item.get("rollback_snapshot")
    forward = item.get("forward_snapshot")
    if (
        attachment is None
        or attachment.is_deleted
        or attachment.status != "active"
        or attachment.path
        != (
            item["file_path"]
            if restored
            else item.get("target_path", item["file_path"])
        )
        or (
            task["scope"].startswith("tenant:")
            and attachment.tenant_id != int(task["scope"].split(":", 1)[1])
        )
        or type(attachment.size) is not int
        or attachment.size < 0
        or type(attachment.hash) is not str
        or re.fullmatch(r"[a-fA-F0-9]{64}", attachment.hash) is None
    ):
        raise RollbackHold("storage_restoration_attachment_changed")
    if restored:
        if (
            snapshot is None
            or attachment.tenant_id != snapshot["tenant_id"]
            or attachment.size != snapshot["size"]
            or attachment.hash != snapshot["sha256"]
            or attachment.driver != item["old_driver"]
            or attachment.base_url != item["old_base_url"]
            or attachment.meta != item["old_meta"]
            or (
                type(forward) is dict
                and forward.get("version") == 3
                and (
                    getattr(attachment, "public_id", None)
                    != forward["source"]["public_id"]
                    or getattr(attachment, "local_object_identity", None)
                    != forward["source"]["local_object_identity"]
                )
            )
        ):
            raise RollbackHold("storage_restoration_attachment_changed")
    elif attachment.driver != task["target_driver"]:
        raise RollbackHold("storage_restoration_attachment_changed")
    if not restored:
        if (
            type(forward) is not dict
            or forward.get("token") != item.get("forward_token")
            or not item.get("forward_token")
        ):
            raise RollbackHold("storage_rollback_journal_required")
        target, source = forward["target"], forward["source"]
        if (
            forward["task"]["id"] != task["id"]
            or forward["log"]["id"] != item["id"]
            or source["id"] != attachment.id
            or source["path"] != item["file_path"]
            or source["driver"] != item["old_driver"]
            or (source["base_url"] or "") != item["old_base_url"]
            or source["meta"] != item["old_meta"]
            or attachment.status != source["status"]
            or attachment.meta != target["meta"]
            or attachment.base_url != target["base_url"]
            or attachment.tenant_id != source["tenant_id"]
            or attachment.size != source["size"]
            or attachment.hash != source["hash"]
            or (
                forward.get("version") in {2, 3}
                and attachment.created_at.isoformat() != source["created_at"]
            )
            or (
                forward.get("version") == 3
                and (
                    getattr(attachment, "public_id", None) != source["public_id"]
                    or getattr(attachment, "local_object_identity", None)
                    != target["local_object_identity"]
                )
            )
            or target["driver"] != task["target_driver"]
            or target["migrated_at"] != item["cycle"]
        ):
            raise RollbackHold("storage_restoration_attachment_changed")


def freeze_snapshot(attachment, item, task, target_config):
    meta = attachment.meta
    if meta is not None and type(meta) is not dict:
        raise RollbackHold("storage_restoration_material_invalid")
    storage = (meta or {}).get("_storage_snapshot")
    if storage is not None and (
        type(storage) is not dict
        or storage.get("driver") != target_config.driver
        or storage.get("root_path") != target_config.root_path
    ):
        raise RollbackHold("storage_target_location_unverified")
    forward = item.get("forward_snapshot")
    local_proof = type(forward) is dict and forward.get("version") == 3
    new_path = item.get("target_path", item["file_path"])
    separated = new_path != item["file_path"]
    return {
        "version": 3 if local_proof else 2 if separated else 1,
        "task_id": task["id"],
        "log_id": item["id"],
        "attachment_id": attachment.id,
        "tenant_id": attachment.tenant_id,
        "path": item["file_path"],
        **({"target_path": new_path} if local_proof or separated else {}),
        **(
            {"target_local_receipt": forward["target"]["local_object_identity"]}
            if local_proof
            else {}
        ),
        "size": attachment.size,
        "sha256": attachment.hash,
        "migrated_at": item["cycle"],
        "target_driver": attachment.driver,
        "target_base_url": attachment.base_url,
        "target_meta": meta,
        "target_origin_id": None,
        "target_root_path": target_config.root_path,
    }


def target_driver(storage_manager, target_config, item):
    snapshot = item["rollback_snapshot"]
    proof = snapshot.get("target_local_receipt")
    if not proof:
        raise RollbackHold("storage_target_verification_unavailable")
    if (
        target_config.driver != snapshot["target_driver"]
        or target_config.root_path != snapshot["target_root_path"]
    ):
        raise RollbackHold("storage_target_location_unverified")
    driver = storage_manager.get_driver(target_config)
    if new_receipt(driver) is None or item["target_path"].startswith("managed-v4/"):
        raise RollbackHold("storage_target_verification_unavailable")
    return driver, proof
