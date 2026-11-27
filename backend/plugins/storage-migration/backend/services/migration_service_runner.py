"""Runtime runner operations for storage migration tasks."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from app.core.i18n import _

from .migration_forward_attempt import forward_hold_required


def _aware_utc(now_factory: Any) -> Any:
    from datetime import timezone

    moment = now_factory()
    return (
        moment.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None
        else moment.astimezone(timezone.utc)
    )


async def _lock_current_task(
    service: Any,
    task_id: int,
    status: str,
    *,
    local_run_id: str | None = None,
    allow_owned: bool = False,
) -> str | None:
    """Lock the current phase and return its generation or an empty legacy marker."""
    from sqlalchemy import text

    return await service._db.scalar(
        text(
            f"SELECT COALESCE(forward_run_id, '') FROM {service.TASK_TABLE} "
            "WHERE id=:id AND status=:status "
            "AND (:allow_owned OR recovery_token IS NULL OR forward_run_id=:local_run_id) "
            "FOR UPDATE"
        ),
        {
            "id": task_id,
            "status": status,
            "local_run_id": local_run_id,
            "allow_owned": allow_owned,
        },
    )


def _create_background_task(asyncio_module: Any, coroutine: Any) -> Any:
    """Create a background task and close unscheduled mock coroutines in tests."""
    background_task = asyncio_module.create_task(coroutine)
    get_coro = getattr(background_task, "get_coro", None)
    scheduled_coroutine = get_coro() if callable(get_coro) else None
    if scheduled_coroutine is not coroutine and hasattr(coroutine, "close"):
        coroutine.close()
    return background_task


async def start_task(
    service: Any,
    task_id: int,
    *,
    utc_now: Any,
    asyncio_module: Any,
    activate_task: Any,
    clear_cancelled: Any,
    register_background_task: Any,
) -> dict[str, Any]:
    """Move a task into running state and boot its background worker."""
    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] not in ("pending", "paused"):
        return {"error": f"Cannot start task in '{task['status']}' status"}
    if await _lock_current_task(service, task_id, task["status"]) is None:
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_conflict",
        }
    if await forward_hold_required(service._db, task_id):
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_migration_outcome_unknown",
        }

    conflict = await service._find_conflicting_active_task(
        source_driver=task["source_driver"],
        target_driver=task["target_driver"],
        scope=task["scope"],
        exclude_task_id=task_id,
    )
    if conflict:
        return {"error": "Another migration holds the same storage driver scope"}

    run_id = uuid4().hex
    await service._update_task_status(
        task_id,
        "running",
        started_at=_aware_utc(utc_now),
        error_message=None,
        forward_run_id=run_id,
    )
    await service._db.commit()

    activate_task(task_id)
    clear_cancelled(task_id)
    background_task = _create_background_task(
        asyncio_module,
        service._run_migration(task_id, run_id),
    )
    register_background_task(task_id, run_id, background_task)
    return {"status": "running", "task_id": task_id}


async def pause_task(
    service: Any,
    task_id: int,
    *,
    get_background_run_id: Any,
    pause_runtime_task: Any,
) -> dict[str, Any]:
    """Pause the runtime gate for a running migration."""
    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] != "running":
        return {"error": f"Cannot pause task in '{task['status']}' status"}
    locked_run_id = await _lock_current_task(
        service, task_id, task["status"], local_run_id=get_background_run_id(task_id)
    )
    if locked_run_id is None:
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_conflict",
        }

    pause_runtime_task(task_id)
    await service._update_task_status(task_id, "paused")
    await service._db.commit()
    return {"status": "paused", "task_id": task_id}


async def resume_task(
    service: Any,
    task_id: int,
    *,
    asyncio_module: Any,
    activate_task: Any,
    clear_cancelled: Any,
    get_background_run_id: Any,
    register_background_task: Any,
) -> dict[str, Any]:
    """Resume a paused migration and recreate the worker if needed."""
    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] != "paused":
        return {"error": f"Cannot resume task in '{task['status']}' status"}
    local_run_id = get_background_run_id(task_id)
    locked_run_id = await _lock_current_task(
        service, task_id, task["status"], local_run_id=local_run_id
    )
    if locked_run_id is None:
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_conflict",
        }
    if await forward_hold_required(service._db, task_id):
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_migration_outcome_unknown",
        }

    conflict = await service._find_conflicting_active_task(
        source_driver=task["source_driver"],
        target_driver=task["target_driver"],
        scope=task["scope"],
        exclude_task_id=task_id,
    )
    if conflict:
        return {"error": "Another migration holds the same storage driver scope"}

    local_worker = locked_run_id == local_run_id
    run_id = locked_run_id if local_worker else uuid4().hex
    await service._update_task_status(
        task_id, "running", error_message=None, forward_run_id=run_id
    )
    await service._db.commit()
    activate_task(task_id)

    if not local_worker:
        clear_cancelled(task_id)
        background_task = _create_background_task(
            asyncio_module,
            service._run_migration(task_id, run_id),
        )
        register_background_task(task_id, run_id, background_task)

    return {"status": "running", "task_id": task_id}


async def cancel_task(
    service: Any,
    task_id: int,
    *,
    mark_cancelled: Any,
    pop_background_task: Any,
    clear_runtime: Any,
) -> dict[str, Any]:
    """Cancel a running or pending migration and release runtime state."""
    task = await service.get_task(task_id)
    if not task:
        return {"error": "Task not found"}
    if task["status"] not in ("running", "paused", "pending"):
        return {"error": f"Cannot cancel task in '{task['status']}' status"}
    if (
        await _lock_current_task(service, task_id, task["status"], allow_owned=True)
        is None
    ):
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_recovery_conflict",
        }
    if await forward_hold_required(service._db, task_id):
        return {
            "error": _("storage.error.driver_error"),
            "reason": "storage_migration_outcome_unknown",
        }

    await service._update_task_status(task_id, "cancelled", release_owner=True)
    await service._db.commit()
    mark_cancelled(task_id)

    background_task = pop_background_task(task_id)
    if background_task and not background_task.done():
        background_task.cancel()

    clear_runtime(task_id)
    return {"status": "cancelled", "task_id": task_id}


async def run_migration(
    service: Any,
    task_id: int,
    run_id: str,
    *,
    service_factory: Any,
    async_session_factory: Any,
    storage_manager: Any,
    text: Any,
    utc_now: Any,
    logger: Any,
    get_pause_event: Any,
    is_cancelled: Any,
) -> None:
    """Transfer files with durable outcomes, never failure-by-missing-ACK."""
    from .migration_forward_attempt import (
        ForwardCommitUncertain,
        _bound_reads,
        reconcile_forward_attempt,
    )

    owner = None
    halted = asyncio.Event()
    confirmed: set[int] = set()

    async def hold() -> None:
        halted.set()
        async with async_session_factory() as db:
            await _bound_reads(db)
            owned = await db.scalar(
                text(
                    f"SELECT 1 FROM {service.TASK_TABLE} WHERE id=:id AND recovery_token=:owner "
                    "AND status IN ('running','paused','migration_uncertain') FOR UPDATE"
                ),
                {"id": task_id, "owner": owner},
            )
            if owned != 1:
                return
            counts = (
                (
                    await db.execute(
                        text(
                            f"SELECT COUNT(*) FILTER (WHERE status='success') AS migrated, "
                            "COALESCE(SUM(file_size) FILTER (WHERE status='success'),0) AS bytes, "
                            f"COUNT(*) FILTER (WHERE status='failed') AS failed FROM {service.LOG_TABLE} "
                            "WHERE task_id=:id AND id=ANY(CAST(:confirmed AS bigint[]))"
                        ),
                        {"id": task_id, "confirmed": sorted(confirmed)},
                    )
                )
                .mappings()
                .one()
            )
            await db.execute(
                text(
                    f"UPDATE {service.TASK_TABLE} SET status='migration_uncertain', "
                    "error_message='storage_migration_outcome_unknown',updated_at=now() "
                    ",migrated_files=:migrated,migrated_bytes=:bytes,failed_files=:failed "
                    "WHERE id=:id AND recovery_token=:owner "
                    "AND status IN ('running','paused','migration_uncertain')"
                ),
                {"id": task_id, "owner": owner, **dict(counts)},
            )
            await db.commit()

    async def reconcile(attempt: Any) -> str:
        # A successfully committed Session may already have returned its physical
        # connection to the pool. Dispose a recycled writer before any readback.
        for _connection_attempt in range(2):
            async with async_session_factory() as db:
                pid = await db.scalar(text("SELECT pg_backend_pid()"))
                if pid == attempt.writer_pid:
                    await db.invalidate()
                    continue
                db.info["storage_forward_reconcile"] = True
                return await reconcile_forward_attempt(db, attempt)
        return "unknown"

    async def mark_precommit_failure(log_id: int) -> bool:
        async with async_session_factory() as db:
            task = (
                (
                    await db.execute(
                        text(
                            f"SELECT status,recovery_token FROM {service.TASK_TABLE} "
                            "WHERE id=:id FOR UPDATE"
                        ),
                        {"id": task_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not task
                or task["status"] != "running"
                or task["recovery_token"] != owner
            ):
                await db.rollback()
                return False
            result = await db.execute(
                text(
                    f"UPDATE {service.LOG_TABLE} log SET status='failed', "
                    "error_message='storage_migration_transfer_failed' "
                    f"FROM {service.TASK_TABLE} task WHERE log.id=:id "
                    "AND log.task_id=task.id AND task.id=:task "
                    "AND task.status='running' AND task.recovery_token=:owner "
                    "AND log.status='pending' AND (log.forward_token IS NULL OR "
                    "EXISTS (SELECT 1 FROM px_storage_migration_attempts prior "
                    "WHERE prior.token=log.forward_token AND prior.task_id=task.id "
                    "AND prior.log_id=log.id AND prior.state='failed'))"
                ),
                {"id": log_id, "task": task_id, "owner": owner},
            )
            if result.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def settle(*, final: bool) -> None:
        async with async_session_factory() as db:
            task = (
                (
                    await db.execute(
                        text(
                            f"SELECT status,recovery_token,total_files FROM {service.TASK_TABLE} WHERE id=:id FOR UPDATE"
                        ),
                        {"id": task_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not task
                or task["status"] not in ("running", "paused")
                or (not final and task["status"] == "paused")
                or task["recovery_token"] != owner
            ):
                return
            unresolved = await db.scalar(
                text(
                    f"SELECT EXISTS(SELECT 1 FROM {service.LOG_TABLE} WHERE task_id=:id "
                    "AND status NOT IN ('pending','success','failed','skipped')) "
                    "OR EXISTS(SELECT 1 FROM px_storage_migration_attempts WHERE task_id=:id "
                    "AND state IN ('prepared','uploaded','unknown'))"
                ),
                {"id": task_id},
            )
            if unresolved:
                await db.rollback()
                await hold()
                return
            counts = (
                (
                    await db.execute(
                        text(
                            f"SELECT COUNT(*) FILTER (WHERE status='success') AS migrated, "
                            "COALESCE(SUM(file_size) FILTER (WHERE status='success'),0) AS bytes, "
                            "COUNT(*) FILTER (WHERE status='failed') AS failed, "
                            "COUNT(*) FILTER (WHERE status='skipped') AS skipped,COUNT(*) AS total, "
                            f"COUNT(*) FILTER (WHERE status='pending') AS pending FROM {service.LOG_TABLE} WHERE task_id=:id"
                        ),
                        {"id": task_id},
                    )
                )
                .mappings()
                .one()
            )
            if counts["total"] != task["total_files"]:
                await db.rollback()
                await hold()
                return
            terminal = final and counts["pending"] == 0
            if task["status"] == "paused" and not terminal:
                return
            status = (
                (
                    "failed"
                    if counts["failed"] and not counts["migrated"]
                    else "completed"
                )
                if terminal
                else "running"
            )
            await db.execute(
                text(
                    f"UPDATE {service.TASK_TABLE} SET migrated_files=:migrated, migrated_bytes=:bytes, "
                    "failed_files=:failed,skipped_files=:skipped,status=:status,updated_at=now(), "
                    "completed_at=CASE WHEN :terminal THEN :completed ELSE completed_at END, "
                    "recovery_token=CASE WHEN :terminal THEN NULL ELSE recovery_token END, "
                    "forward_run_id=CASE WHEN :terminal THEN NULL ELSE forward_run_id END WHERE id=:id"
                ),
                {
                    **dict(counts),
                    "status": status,
                    "terminal": terminal,
                    "completed": _aware_utc(utc_now),
                    "id": task_id,
                },
            )
            await db.commit()

    try:
        async with async_session_factory() as db:
            # The UPDATE grants authority only to the worker that acquired it.
            owner = await db.scalar(
                text(
                    f"UPDATE {service.TASK_TABLE} SET recovery_token=:run_id "
                    "WHERE id=:id AND status='running' AND recovery_token IS NULL "
                    "AND forward_run_id=:run_id RETURNING recovery_token"
                ),
                {"id": task_id, "run_id": run_id},
            )
            if owner is None:
                return
            await db.commit()
            setup = service_factory(db)
            task = await setup.get_task(task_id)
            if not task or task["status"] not in ("running", "paused"):
                return
            if await forward_hold_required(db, task_id):
                await hold()
                return
            confirmed.update(
                (
                    await db.execute(
                        text(
                            f"SELECT id FROM {service.LOG_TABLE} WHERE task_id=:id AND status IN ('success','failed')"
                        ),
                        {"id": task_id},
                    )
                )
                .scalars()
                .all()
            )
            source_config = await setup._resolve_config_with_snapshot(
                db,
                task.get("source_config_snapshot"),
                task["source_driver"],
                task["scope"],
            )
            target_config = await setup._resolve_config_with_snapshot(
                db,
                task.get("target_config_snapshot"),
                task["target_driver"],
                task["scope"],
            )
            source_storage = storage_manager.get_driver(source_config)
            target_storage = storage_manager.get_driver(target_config)
            target_base_url = target_storage.get_base_url()
            semaphore = asyncio.Semaphore(max(1, int(task.get("concurrency") or 1)))

        async def migrate_one(entry: dict[str, Any]) -> None:
            outcome = None
            try:
                async with semaphore:
                    pause = get_pause_event(task_id)
                    if pause is not None:
                        await pause.wait()
                    if halted.is_set() or is_cancelled(task_id):
                        return
                    async with async_session_factory() as db:
                        db.info["storage_forward_owner"] = owner
                        try:
                            await service_factory(db)._migrate_single_file(
                                db=db,
                                log_id=entry["id"],
                                attachment_id=entry["attachment_id"],
                                file_path=entry["file_path"],
                                source_driver=source_storage,
                                target_driver=target_storage,
                                target_driver_name=task["target_driver"],
                                target_base_url=target_base_url,
                                target_storage_config=target_config,
                            )
                            confirmed.add(entry["id"])
                        except ForwardCommitUncertain as exc:
                            outcome = exc
                            await db.invalidate()
                    if outcome is not None:
                        result = await reconcile(outcome.attempt)
                        logger.warning(
                            "Migration settlement task={} log={} outcome={}",
                            task_id,
                            entry["id"],
                            result,
                        )
                        if result == "unknown":
                            await hold()
                        else:
                            confirmed.add(entry["id"])
            except asyncio.CancelledError:
                # Preserve the durable transfer marker if the owner is interrupted.
                await hold()
                raise
            except Exception:
                logger.warning(
                    "Migration worker failed task={} log={} reason={}",
                    task_id,
                    entry["id"],
                    "storage_migration_transfer_failed",
                )
                if outcome is not None:
                    await hold()
                else:
                    if await mark_precommit_failure(entry["id"]):
                        confirmed.add(entry["id"])
                        await settle(final=False)
                    else:
                        # A conflicting or stale receipt must stop the runner,
                        # never leave the same pending row in an infinite batch.
                        await hold()

        while not halted.is_set() and not is_cancelled(task_id):
            async with async_session_factory() as db:
                rows = await db.execute(
                    text(
                        f"SELECT id,attachment_id,file_path,file_size FROM {service.LOG_TABLE} "
                        "WHERE task_id=:id AND status='pending' ORDER BY id LIMIT 1000"
                    ),
                    {"id": task_id},
                )
                batch = [dict(row) for row in rows.mappings().all()]
            if not batch:
                break
            await asyncio.gather(*(migrate_one(entry) for entry in batch))
            if halted.is_set():
                # Other already-admitted files may acknowledge success after
                # the first unknown outcome. Settle those receipts once all
                # in-flight work finishes, without reopening task admission.
                await hold()
            else:
                await settle(final=False)
        if not halted.is_set() and not is_cancelled(task_id):
            await settle(final=True)
    except asyncio.CancelledError:
        if owner is not None:
            await hold()
        logger.info("Migration task {} cancelled", task_id)
    except Exception:
        logger.error(
            "Migration runner failed task={} reason={}",
            task_id,
            "storage_migration_outcome_unknown",
        )
        if owner is not None:
            try:
                await hold()
            except Exception:
                logger.warning("Migration outcome hold unavailable task={}", task_id)
