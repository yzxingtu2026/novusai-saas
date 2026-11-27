"""Journaled local rollback: restore availability before deleting any target."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.core.i18n import _
from app.services.common.storage_config_resolver import StorageConfigResolver
from app.storage.drivers.local import LocalStorageDriver

from .migration_forward_attempt import UNCERTAIN, forward_hold_required
from .migration_local_receipts import remove_exact_pair
from .migration_rollback_journal import (
    RollbackHold,
    check_attachment,
    freeze_snapshot,
    rollback_reason,
    target_driver,
    validate_material,
    verify_originals,
)


async def rollback_task(
    service,
    task_id,
    *,
    storage_manager,
    update,
    text,
    deserialize_json_field,
    attachment_model,
    logger,
):
    """Durably restore all routing, then settle each proven target independently."""
    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] not in (
        "completed",
        "failed",
        "rollback_failed",
        "rollback_interrupted",
    ):
        return {"error": f"Cannot rollback task in '{task['status']}' status"}
    if (
        task.get("source_cleanup_started_at")
        or task.get("source_cleanup_completed_at")
        or task.get("source_cleanup_deleted_files", 0)
    ):
        return {
            "error": "Cannot rollback after source cleanup has started or completed"
        }
    if (
        task["status"] == "rollback_interrupted"
        and task.get("rollback_can_resume") is not True
    ):
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_rollback_journal_required",
        }
    conflict = await service._find_conflicting_active_task(
        source_driver=task["source_driver"],
        target_driver=task["target_driver"],
        scope=task["scope"],
        exclude_task_id=task_id,
    )
    if conflict:
        return {
            "error": (
                "Cannot rollback while another active migration touches the same driver scope: "
                f"task #{conflict['id']} ({conflict['status']})"
            )
        }
    token = uuid4().hex
    claimed = (
        (
            await service._db.execute(
                text(f"""
        UPDATE {service.TASK_TABLE} SET status='rolling_back', recovery_token=:token,
            error_message=NULL, updated_at=now()
        WHERE id=:id AND status IN ('completed','failed','rollback_failed','rollback_interrupted')
          AND recovery_token IS NULL AND source_cleanup_started_at IS NULL
          AND source_cleanup_completed_at IS NULL
          AND COALESCE(source_cleanup_deleted_files,0)=0
          AND (status <> 'rollback_interrupted' OR (
              EXISTS (SELECT 1 FROM {service.LOG_TABLE} WHERE task_id=:id AND status='success')
              AND NOT EXISTS (SELECT 1 FROM {service.LOG_TABLE} WHERE task_id=:id AND status='success'
                  AND (rollback_state NOT IN ('source_restored','target_deleting','target_deleted')
                       OR rollback_snapshot IS NULL))))
        RETURNING *
    """),
                {"id": task_id, "token": token},
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
    task = service._normalize_task_row(dict(claimed))
    await service._db.commit()

    def lost():
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_ownership_lost",
        }

    async def owns():
        return (
            await service._db.scalar(
                text(
                    f"SELECT 1 FROM {service.TASK_TABLE} WHERE id=:id "
                    "AND status='rolling_back' AND recovery_token=:token FOR UPDATE"
                ),
                {"id": task_id, "token": token},
            )
        ) == 1

    async def hold(reason):
        message = f"{_('storage.error.driver_error')} ({reason})"
        result = await service._db.execute(
            text(
                f"UPDATE {service.TASK_TABLE} SET status='rollback_failed', recovery_token=NULL, "
                "error_message=:message,updated_at=now() WHERE id=:id "
                "AND status='rolling_back' AND recovery_token=:token"
            ),
            {"id": task_id, "token": token, "message": message},
        )
        if result.rowcount != 1:
            await service._db.rollback()
            return lost()
        await service._db.commit()
        logger.warning("Rollback held task={} reason={}", task_id, reason)
        return {"error": message, "reason": reason}

    async def materials():
        rows = (
            (
                await service._db.execute(
                    text(
                        f"SELECT * FROM {service.LOG_TABLE} WHERE task_id=:id AND status='success' ORDER BY id FOR UPDATE"
                    ),
                    {"id": task_id},
                )
            )
            .mappings()
            .all()
        )
        items = [await verified_material(row) for row in rows]
        if (
            len(items) != task["migrated_files"]
            or len({item["attachment_id"] for item in items}) != len(items)
            or len({item["file_path"] for item in items}) != len(items)
        ):
            raise RollbackHold("storage_restoration_material_incomplete")
        return items

    async def attachment(item):
        return (
            await service._db.execute(
                select(attachment_model)
                .where(attachment_model.id == item["attachment_id"])
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def material(log_id):
        row = (
            (
                await service._db.execute(
                    text(
                        f"SELECT * FROM {service.LOG_TABLE} WHERE id=:log AND task_id=:task "
                        "AND status='success' FOR UPDATE"
                    ),
                    {"log": log_id, "task": task_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RollbackHold("storage_rollback_journal_incomplete")
        return await verified_material(row)

    async def verified_material(row):
        item = validate_material(row, task, deserialize_json_field)
        if item.get("forward_token"):
            valid = await service._db.scalar(
                text(
                    "SELECT 1 FROM px_storage_migration_attempts WHERE token=:token "
                    "AND task_id=:task AND log_id=:log AND state='succeeded' "
                    "AND CAST(snapshot AS jsonb)=CAST(:snapshot AS jsonb) FOR UPDATE"
                ),
                {
                    "token": item["forward_token"],
                    "task": task_id,
                    "log": item["id"],
                    "snapshot": json.dumps(item["forward_snapshot"]),
                },
            )
            if valid != 1:
                raise RollbackHold("storage_rollback_journal_required")
        return item

    async def verify_source_route(items):
        resolver = StorageConfigResolver(service._db)
        for item in items:
            row = SimpleNamespace(
                tenant_id=item["rollback_snapshot"]["tenant_id"],
                path=item["file_path"],
                driver=item["old_driver"],
                meta=item["old_meta"],
            )
            resolved = await resolver.resolve_for_attachment_record(row)
            if resolved != source_config:
                raise RollbackHold("storage_source_location_unverified")
        await verify_originals(source, items)

    # Lock the complete restoration set and validate every file before any change.
    if not await owns():
        await service._db.rollback()
        return lost()
    try:
        items = await materials()
        for field, driver in (
            ("source_config_snapshot", task["source_driver"]),
            ("target_config_snapshot", task["target_driver"]),
        ):
            config = deserialize_json_field(task.get(field))
            if (
                type(config) is not dict
                or config.get("driver") != driver
                or type(config.get("root_path")) is not str
                or not config["root_path"]
                or type(config.get("options", {})) is not dict
            ):
                raise RollbackHold("storage_configuration_unavailable")
        source_config = await service._resolve_config_with_snapshot(
            service._db,
            task["source_config_snapshot"],
            task["source_driver"],
            task["scope"],
        )
        target_config = await service._resolve_config_with_snapshot(
            service._db,
            task["target_config_snapshot"],
            task["target_driver"],
            task["scope"],
        )
        source = storage_manager.get_driver(source_config)
        target = storage_manager.get_driver(target_config)
        if (
            isinstance(source, LocalStorageDriver)
            and isinstance(target, LocalStorageDriver)
            and source.root.samefile(target.root)
        ):
            raise RollbackHold("storage_source_target_overlap")
        restored = [item["rollback_state"] != "none" for item in items]
        if any(restored) and not all(restored):
            raise RollbackHold("storage_rollback_journal_incomplete")
        for item in items:
            current = await attachment(item)
            check_attachment(current, item, task, restored=all(restored))
            if not all(restored):
                item.update(size=current.size, hash=current.hash)
                item["rollback_snapshot"] = freeze_snapshot(
                    current, item, task, target_config
                )
    except Exception as exc:
        return await hold(rollback_reason(exc, "storage_configuration_unavailable"))
    try:
        await verify_source_route(items)
    except Exception as exc:
        return await hold(
            rollback_reason(
                exc,
                "storage_source_verification_unavailable",
                local_boundary=isinstance(source, LocalStorageDriver),
            )
        )
    if not all(restored):
        for item in items:
            values = {
                "path": item["file_path"],
                "driver": item["old_driver"],
                "base_url": item["old_base_url"],
                "meta": item["old_meta"],
            }
            if hasattr(attachment_model, "local_object_identity"):
                values["local_object_identity"] = item["forward_snapshot"][
                    "source"
                ].get("local_object_identity")
            await service._db.execute(
                update(attachment_model)
                .where(attachment_model.id == item["attachment_id"])
                .values(**values)
            )
            await service._db.execute(
                text(
                    f"UPDATE {service.LOG_TABLE} SET rollback_state='source_restored', "
                    "rollback_snapshot=CAST(:snapshot AS jsonb) WHERE id=:id AND task_id=:task "
                    "AND status='success' AND rollback_state='none' AND rollback_snapshot IS NULL"
                ),
                {
                    "id": item["id"],
                    "task": task_id,
                    "snapshot": json.dumps(item["rollback_snapshot"]),
                },
            )
        # Failure or uncertain COMMIT propagates; target I/O has not started.
    await service._db.commit()

    for original in items:
        if not await owns():
            await service._db.rollback()
            return lost()
        try:
            item = await material(original["id"])
            if item["rollback_snapshot"] != original["rollback_snapshot"]:
                raise RollbackHold("storage_rollback_journal_changed")
            check_attachment(await attachment(item), item, task, restored=True)
            if item["rollback_state"] == "target_deleted":
                await service._db.commit()
                continue
            await verify_source_route([item])
            target_driver(storage_manager, target_config, item)
            await service._db.execute(
                text(
                    f"UPDATE {service.LOG_TABLE} SET rollback_state='target_deleting' "
                    "WHERE id=:id AND task_id=:task AND status='success'"
                ),
                {"id": item["id"], "task": task_id},
            )
        except Exception as exc:
            return await hold(
                rollback_reason(
                    exc, "storage_restoration_material_invalid", local_boundary=True
                )
            )
        await service._db.commit()
        if not await owns():
            await service._db.rollback()
            return lost()
        try:
            current = await material(item["id"])
            if (
                current["rollback_snapshot"] != item["rollback_snapshot"]
                or current["rollback_state"] != "target_deleting"
            ):
                raise RollbackHold("storage_rollback_journal_changed")
            check_attachment(await attachment(current), current, task, restored=True)
            await verify_source_route([current])
            deleting_driver, proof = target_driver(
                storage_manager, target_config, current
            )
            result = await remove_exact_pair(
                deleting_driver, current["target_path"], proof
            )
            if result not in {"deleted", "absent"}:
                raise RollbackHold("storage_target_object_changed")
        except Exception as exc:
            return await hold(
                rollback_reason(exc, "storage_delete_failed", local_boundary=True)
            )
        await service._db.execute(
            text(
                f"UPDATE {service.LOG_TABLE} SET rollback_state='target_deleted' "
                "WHERE id=:id AND task_id=:task AND status='success' AND rollback_state='target_deleting'"
            ),
            {"id": current["id"], "task": task_id},
        )
        # A failed receipt COMMIT leaves target_deleting and source routing durable.
        await service._db.commit()

    if not await owns():
        await service._db.rollback()
        return lost()
    try:
        final = await materials()
        for item in final:
            if item["rollback_state"] != "target_deleted":
                raise RollbackHold("storage_rollback_journal_incomplete")
            check_attachment(await attachment(item), item, task, restored=True)
    except Exception as exc:
        return await hold(rollback_reason(exc, "storage_restoration_material_invalid"))
    # Routing was restored before I/O. Never rewrite later attachment metadata here.
    await service._db.execute(
        text(f"""
        UPDATE {service.LOG_TABLE} SET status='pending', new_driver=NULL,
            new_base_url=NULL, migrated_at=NULL, error_message=NULL
        WHERE task_id=:id AND status='success' AND rollback_state='target_deleted'
    """),
        {"id": task_id},
    )
    settled = await service._db.execute(
        text(f"""
        UPDATE {service.TASK_TABLE} SET status='pending', recovery_token=NULL,
            migrated_files=0,migrated_bytes=0,failed_files=0,skipped_files=0,
            started_at=NULL,completed_at=NULL,error_message=NULL,updated_at=now()
        WHERE id=:id AND status='rolling_back' AND recovery_token=:token
    """),
        {"id": task_id, "token": token},
    )
    if settled.rowcount != 1:
        await service._db.rollback()
        return lost()
    await service._db.commit()
    return {
        "status": "pending",
        "task_id": task_id,
        "reverted_files": len(final),
        "target_delete_errors": 0,
    }
