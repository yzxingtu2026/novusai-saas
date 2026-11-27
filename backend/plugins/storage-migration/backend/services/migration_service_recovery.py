"""Recovery and cleanup operations for storage migration tasks."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.core.i18n import _
from app.exceptions import StorageError
from app.models.tenant.attachment import Attachment
from app.services.common.storage_config_resolver import StorageConfigResolver

from .migration_forward_attempt import UNCERTAIN, forward_hold_required
from .migration_helpers import deserialize_json_field
from .migration_local_receipts import new_receipt, remove_exact_pair
from .migration_rollback_journal import verify_originals
from .migration_rollback_service import rollback_task as rollback_task


async def retry_failed(
    service: Any,
    task_id: int,
    *,
    text: Any,
) -> dict[str, Any]:
    """Reset failed file logs and restart the task."""
    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] not in ("completed", "failed"):
        return {"error": f"Cannot retry task in '{task['status']}' status"}

    admitted = await service._db.scalar(
        text(
            f"SELECT 1 FROM {service.TASK_TABLE} "
            "WHERE id=:id AND status IN ('completed', 'failed') "
            "AND recovery_token IS NULL AND source_cleanup_started_at IS NULL "
            "AND source_cleanup_completed_at IS NULL "
            "AND COALESCE(source_cleanup_deleted_files, 0)=0 FOR UPDATE"
        ),
        {"id": task_id},
    )
    if admitted != 1:
        await service._db.rollback()
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_conflict",
        }
    if await forward_hold_required(service._db, task_id):
        await service._db.rollback()
        return {"error": _("storage.error.driver_error"), "reason": UNCERTAIN}

    reset_q = text(
        f"""
        UPDATE {service.LOG_TABLE}
        SET status = 'pending',
            error_message = NULL,
            migrated_at = NULL
        WHERE task_id = :task_id AND status = 'failed'
        """
    )
    result = await service._db.execute(reset_q, {"task_id": task_id})
    reset_count = result.rowcount
    if reset_count == 0:
        await service._db.rollback()
        return {"error": "No failed files to retry"}

    await service._db.execute(
        text(
            f"""
            UPDATE {service.TASK_TABLE}
            SET failed_files = failed_files - :count,
                status = 'pending',
                error_message = NULL,
                completed_at = NULL,
                updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": task_id, "count": reset_count},
    )
    await service._db.commit()

    return await service.start_task(task_id)


async def cleanup_source_files(
    service: Any,
    task_id: int,
    *,
    storage_manager: Any,
    text: Any,
    utc_now: Any,
    logger: Any,
) -> dict[str, Any]:
    """Delete source-side files after a completed migration."""
    from datetime import timezone

    from app.core.i18n import _

    def current_utc():
        moment = utc_now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] not in ("completed", "cleanup_interrupted"):
        return {"error": "Can only cleanup source files for completed tasks"}
    if task.get("source_cleanup_completed_at"):
        return {"error": "Source files were already cleaned up for this task"}

    try:
        source_config = await service._resolve_config_with_snapshot(
            service._db,
            task.get("source_config_snapshot"),
            task["source_driver"],
            task["scope"],
        )
        source_driver = storage_manager.get_driver(source_config)
    except Exception:
        logger.warning(
            "Source cleanup unavailable task={} reason={}",
            task_id,
            "storage_configuration_unavailable",
        )
        return {
            "error": _("storage.error.config_error"),
            "reason": "storage_configuration_unavailable",
        }
    if new_receipt(source_driver) is None or await service._db.scalar(
        text(
            f"SELECT EXISTS(SELECT 1 FROM {service.LOG_TABLE} log "
            "WHERE task_id=:task AND status='success' AND ("
            "jsonb_typeof(CAST(log.forward_snapshot AS jsonb)->'source'->'local_object_identity') "
            "IS DISTINCT FROM 'object' OR NOT EXISTS("
            "SELECT 1 FROM px_storage_migration_attempts attempt "
            "WHERE attempt.token=log.forward_token AND attempt.task_id=log.task_id "
            "AND attempt.log_id=log.id AND attempt.state='succeeded' "
            "AND CAST(attempt.snapshot AS jsonb)=CAST(log.forward_snapshot AS jsonb))))"
        ),
        {"task": task_id},
    ):
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_origin_unverified",
        }

    started_at = current_utc()
    recovery_token = uuid4().hex
    claimed = (
        (
            await service._db.execute(
                text(
                    f"""
            UPDATE {service.TASK_TABLE}
            SET status = 'source_cleaning', recovery_token = :token,
                source_cleanup_started_at = COALESCE(source_cleanup_started_at, :started_at),
                updated_at = now()
            WHERE id = :id AND status IN ('completed', 'cleanup_interrupted')
              AND recovery_token IS NULL AND source_cleanup_completed_at IS NULL
            RETURNING *
            """
                ),
                {"id": task_id, "started_at": started_at, "token": recovery_token},
            )
        )
        .mappings()
        .one_or_none()
    )
    if claimed is None:
        await service._db.rollback()
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_conflict",
        }
    if await forward_hold_required(service._db, task_id):
        await service._db.rollback()
        return {"error": _("storage.error.driver_error"), "reason": UNCERTAIN}
    claimed = service._normalize_task_row(dict(claimed))
    if any(
        claimed.get(field) != task.get(field)
        for field in ("source_driver", "scope", "source_config_snapshot")
    ):
        await service._db.rollback()
        return {
            "error": _("storage.error.config_error"),
            "reason": "storage_recovery_identity_changed",
        }
    # 中文: 开始标记先独立提交，之后即使进程退出也不能再回滚删除目标副本。
    # EN: Commit the irreversible source-cleanup fence before any storage I/O.
    await service._db.commit()

    logs_q = text(
        f"""
        SELECT id
        FROM {service.LOG_TABLE}
        WHERE task_id = :task_id AND status = 'success'
        """
    )
    logs_result = await service._db.execute(logs_q, {"task_id": task_id})

    deleted = 0
    already_absent = 0
    errors = 0
    for row in logs_result.mappings().all():
        owned = await service._db.scalar(
            text(
                f"SELECT 1 FROM {service.TASK_TABLE} "
                "WHERE id=:id AND status='source_cleaning' AND recovery_token=:token "
                "FOR UPDATE"
            ),
            {"id": task_id, "token": recovery_token},
        )
        if owned != 1:
            await service._db.rollback()
            return {
                "error": _("storage.error.driver_error"),
                "reason": "storage_recovery_ownership_lost",
            }
        reason = None
        try:
            binding = (
                (
                    await service._db.execute(
                        text(
                            f"SELECT log.attachment_id, log.file_path, log.forward_snapshot FROM {service.LOG_TABLE} log "
                            "JOIN px_storage_migration_attempts attempt ON "
                            "attempt.token=log.forward_token AND attempt.log_id=log.id AND attempt.task_id=log.task_id "
                            "WHERE log.task_id=:task AND log.id=:log AND log.status='success' AND attempt.state='succeeded' "
                            "AND CAST(attempt.snapshot AS jsonb)=CAST(log.forward_snapshot AS jsonb) FOR UPDATE"
                        ),
                        {"task": task_id, "log": row["id"]},
                    )
                )
                .mappings()
                .one()
            )
            frozen = deserialize_json_field(binding["forward_snapshot"])
            original, target = frozen["source"], frozen["target"]
            retained = await service._db.scalar(
                select(Attachment)
                .where(Attachment.id == binding["attachment_id"])
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                retained is None
                or retained.is_deleted
                or retained.status != "active"
                or retained.public_id != original["public_id"]
                or retained.tenant_id != original["tenant_id"]
                or retained.size != original["size"]
                or retained.hash != original["hash"]
                or retained.driver != target["driver"]
                or retained.path != target["path"]
                or retained.meta != target["meta"]
                or retained.base_url != target["base_url"]
                or original["path"] != binding["file_path"]
                or original["driver"] != task["source_driver"]
                or frozen["task"]["id"] != task_id
                or frozen["log"]["id"] != row["id"]
            ):
                raise StorageError(
                    data={"reason": "storage_migration_preserved_copy_unverified"}
                )
            target_config = await StorageConfigResolver(
                service._db
            ).resolve_for_attachment_record(retained)
            target_driver = storage_manager.get_driver(target_config)
            if hasattr(target_driver, "root") and target_driver.root.samefile(
                source_driver.root
            ):
                raise StorageError(
                    data={"reason": "storage_migration_preserved_copy_unverified"}
                )
            await verify_originals(
                target_driver,
                [
                    {
                        "old_meta": retained.meta,
                        "file_path": retained.path,
                        "size": retained.size,
                        "hash": retained.hash,
                    }
                ],
            )
            outcome = await remove_exact_pair(
                source_driver, binding["file_path"], original["local_object_identity"]
            )
            if outcome == "deleted":
                deleted += 1
            elif outcome == "absent":
                already_absent += 1
            else:
                reason = "storage_migration_cleanup_unverified"
        except Exception as exc:
            reason = "storage_delete_failed"
            if isinstance(exc, StorageError):
                data = exc.data
                supplied_reason = data.get("reason") if type(data) is dict else None
                if type(supplied_reason) is str and supplied_reason in {
                    "storage_origin_unverified",
                    "storage_origin_changed",
                    "storage_origin_unavailable",
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
                    reason = supplied_reason
        if reason is not None:
            logger.warning(
                "Source cleanup held task={} reason={}",
                task_id,
                reason,
            )
            errors += 1
        await service._db.commit()

    completed_at = current_utc() if errors == 0 else None
    settled = await service._db.execute(
        text(
            f"""
            UPDATE {service.TASK_TABLE}
            SET status = 'completed', recovery_token = NULL,
                source_cleanup_completed_at = :completed_at,
                source_cleanup_deleted_files = :deleted_files,
                source_cleanup_error_count = :error_count,
                updated_at = now()
            WHERE id = :id AND status = 'source_cleaning' AND recovery_token = :token
            """
        ),
        {
            "id": task_id,
            "token": recovery_token,
            "completed_at": completed_at,
            "deleted_files": deleted,
            "error_count": errors,
        },
    )
    if settled.rowcount != 1:
        await service._db.rollback()
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_ownership_lost",
        }
    await service._db.commit()

    return {
        "task_id": task_id,
        "deleted_files": deleted,
        "already_absent_files": already_absent,
        "errors": errors,
        "cleanup_completed_at": completed_at,
    }
