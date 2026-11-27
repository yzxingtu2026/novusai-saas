"""Shared helpers for storage migration services."""

from __future__ import annotations

import json
from datetime import timezone
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text, update

from app.services.common.storage_config_resolver import (
    infer_attachment_storage_scope,
    merge_attachment_storage_snapshot,
    strip_internal_attachment_meta,
)
from app.storage.base import StorageConfig, StorageVisibility

from . import migration_forward_attempt as forward
from .migration_local_receipts import new_receipt


def normalize_scope(scope: str) -> str:
    normalized = str(scope or "all").strip()
    if not normalized or normalized == "all":
        return "all"
    if not normalized.startswith("tenant:"):
        raise ValueError("scope must be 'all' or 'tenant:{id}'")

    tenant_part = normalized.split(":", 1)[1].strip()
    tenant_id = int(tenant_part)
    if tenant_id <= 0:
        raise ValueError("tenant scope id must be a positive integer")
    return f"tenant:{tenant_id}"


def scopes_overlap(left: str, right: str) -> bool:
    if left == "all" or right == "all":
        return True
    return left == right


def deserialize_json_field(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return value
    return value


def json_dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


async def execute_single_file_migration(
    *,
    attachment_model: Any,
    db: Any,
    file_path: str,
    log_id: int,
    log_table: str,
    attachment_id: int,
    source_driver: Any,
    target_driver: Any,
    target_driver_name: str,
    target_base_url: str,
    target_storage_config: StorageConfig,
    now_factory: Any,
    logger: Any,
) -> bool:
    """Migrate one attachment and persist migration log updates."""
    upload_started = False
    attempt = None
    phase = "prepared"

    try:
        # A committed attempt must fence this log before any object I/O.
        # The runner owns independent readback of an unacknowledged COMMIT.
        if log_table != forward.LOG:
            raise RuntimeError("storage_forward_admission_failed")
        attempt = await forward.prepare_forward_attempt(
            db, log_id, attachment_id, file_path
        )
        # Driver instances were resolved before file admission. A concurrent
        # task-config change cannot redirect an old runner's physical I/O.
        expected_target = attempt.snapshot["task"]["target_config_snapshot"]
        version = attempt.snapshot["version"]
        actual_target = forward.config_identity(target_storage_config, version)
        actual_source_config = getattr(source_driver, "config", None)
        if (
            not forward._equal(expected_target, actual_target)
            or actual_source_config is None
            or not forward._equal(
                attempt.snapshot["task"]["source_config_snapshot"],
                forward.config_identity(actual_source_config, version),
            )
            or target_driver_name != actual_target["driver"]
            or target_base_url != target_driver.get_base_url()
        ):
            raise RuntimeError("storage_migration_configuration_changed")

        # Use only the source identity frozen by durable admission. A later
        # live read must not silently substitute another tenant's metadata.
        attachment_meta = SimpleNamespace(**attempt.snapshot["source"])
        source_snapshot = (
            attachment_meta.meta.get("_storage_snapshot")
            if isinstance(attachment_meta.meta, dict)
            else None
        )
        if source_snapshot is not None and (
            type(source_snapshot) is not dict
            or source_snapshot.get("driver") != attachment_meta.driver
            or source_snapshot.get("root_path") != actual_source_config.root_path
            or source_snapshot.get("base_url") != actual_source_config.base_url
        ):
            raise RuntimeError("storage_migration_source_location_changed")

        visibility = StorageVisibility(
            attachment_meta.visibility or StorageVisibility.PRIVATE.value
        )
        metadata = (
            attachment_meta.meta if isinstance(attachment_meta.meta, dict) else None
        )
        object_metadata = strip_internal_attachment_meta(metadata)
        storage_scope = (
            infer_attachment_storage_scope(
                metadata,
                file_path,
                attachment_meta.tenant_id,
            )
            or "platform"
        )
        content = await source_driver.get(file_path)  # type: ignore[union-attr]
        file_data = (
            BytesIO(content) if isinstance(content, (bytes, bytearray)) else content
        )

        try:
            if await target_driver.exists(file_path) is not False:
                raise RuntimeError("storage_migration_target_already_exists")
            upload_started = True
            phase = "upload"
            receipt = new_receipt(target_driver)
            put = target_driver.put_owned if receipt is not None else target_driver.put
            upload_result = await put(
                path=file_path,
                content=file_data,
                mime_type=attachment_meta.mime_type,
                visibility=visibility,
                metadata=object_metadata,
                **({"receipt": receipt} if receipt is not None else {}),
            )
            if (
                upload_result.size != attachment_meta.size
                or upload_result.path != file_path
                or (
                    attachment_meta.hash
                    and upload_result.hash != attachment_meta.hash.lower()
                )
            ):
                raise RuntimeError("storage_migration_target_content_changed")
        finally:
            # get() gave ownership of this stream to the helper. Release it on
            # success, write failure and cancellation without deleting bytes.
            file_data.close()
        local_identity = (
            receipt.snapshot(target_driver._get_cache_root())
            if receipt is not None and len(receipt.files) == 2
            else None
        )
        updated_meta = merge_attachment_storage_snapshot(
            metadata,
            target_storage_config,
            storage_scope,
        )
        migrated_at = now_factory()
        migrated_at = (
            migrated_at.replace(tzinfo=timezone.utc)
            if migrated_at.tzinfo is None
            else migrated_at.astimezone(timezone.utc)
        )

        phase = "uploaded"
        await forward.record_uploaded(
            db,
            attempt,
            updated_meta=updated_meta,
            target_driver_name=target_driver_name,
            target_base_url=target_base_url,
            migrated_at=migrated_at,
            target_path=upload_result.path,
            local_identity=local_identity,
        )
        phase = "business"
        # Keep task/log/attachment ownership through the actual write and
        # COMMIT. A frozen receipt is evidence, never a lock substitute.
        await forward.lock_forward_attempt(db, attempt)

        values = {
            "path": upload_result.path,
            "driver": target_driver_name,
            "base_url": target_base_url,
            "meta": updated_meta,
        }
        if hasattr(attachment_model, "local_object_identity"):
            values["local_object_identity"] = local_identity
        await db.execute(
            update(attachment_model)
            .where(attachment_model.id == attachment_id)
            .values(**values)
        )

        await db.execute(
            text(
                f"""
                UPDATE {log_table}
                SET status = 'success',
                    new_driver = :driver,
                    new_base_url = :base_url,
                    migrated_at = :now,
                    rollback_state = 'none',
                    rollback_snapshot = NULL,
                    error_message = NULL
                WHERE id = :id
                """
            ),
            {
                "id": log_id,
                "driver": target_driver_name,
                "base_url": target_base_url,
                "now": migrated_at,
            },
        )
        await forward.mark_forward_succeeded(db, attempt)
        await db.commit()
        return True
    except Exception as exc:
        if isinstance(exc, forward.ForwardCommitUncertain):
            raise
        if attempt is None:
            # Admission produced no acknowledged attempt. Do not expose a
            # database/configuration exception or start storage work.
            raise RuntimeError("storage_forward_admission_failed") from None
        if upload_started:
            # Even a failed PUT may have written bytes. Neither a business
            # rollback nor a lost ACK grants compensation DELETE authority.
            logger.warning(
                "Migration attempt requires readback log={} reason={}",
                log_id,
                "storage_forward_outcome_unknown",
            )
            raise forward.ForwardCommitUncertain(attempt, phase) from None
        try:
            await db.rollback()
            await forward.fail_forward_attempt(db, attempt)
        except Exception:
            raise forward.ForwardCommitUncertain(attempt, "prepared") from None
        logger.warning(
            "Migration attempt failed before upload log={} reason={}",
            log_id,
            "storage_forward_source_unavailable",
        )
        return False
