"""
Agent batch processing Celery task / 智能体批处理 Celery 任务

Asynchronously executes batch requests, processing items one by one
and updating BatchRun progress in real time.
异步执行批量请求，逐项处理并实时更新 BatchRun 进度

Note: In Celery Worker (Windows --pool=solo), asyncio.new_event_loop()
between retries invalidates the module-level async_session_factory's event loop.
Therefore, an independent async engine + session must be created on each task call.
注意：Celery Worker (Windows --pool=solo) 中，asyncio.new_event_loop() 在 retries 之间
会导致模块级 async_session_factory 绑定的 event loop 失效。
因此必须在每次任务调用时创建独立的 async engine + session。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded

from app.core.base_model import utc_now
from app.core.logging import LogManager
from app.tasks.async_db import task_async_session as _task_async_session
from app.tasks.base import BaseTask, register_task

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.ai.batch_run import BatchRun

logger = LogManager.get_logger("task")

DISPATCH_RECOVERY_BATCH_SIZE = 20
EXECUTION_LEASE_DURATION = timedelta(seconds=90)
EXECUTION_HEARTBEAT_SECONDS = 20


async def _heartbeat_batch_execution(
    *, batch_run_id: int, tenant_id: int, started_at: datetime
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from app.repositories.ai.batch_run_repository import renew_batch_execution_lease

    while True:
        await asyncio.sleep(EXECUTION_HEARTBEAT_SECONDS)
        try:
            async with _task_async_session() as db, db.begin():
                renewed = await renew_batch_execution_lease(
                    db,
                    batch_run_id=batch_run_id,
                    tenant_id=tenant_id,
                    started_at=started_at,
                    duration=EXECUTION_LEASE_DURATION,
                )
            if not renewed:
                return
        except SQLAlchemyError as exc:
            logger.warning(
                "Batch execution heartbeat failed: batch_run_id={} error_type={}",
                batch_run_id,
                type(exc).__name__,
            )


def _settle_batch_execution(batch_run: BatchRun, *, succeeded: int, failed: int) -> str:
    from app.enums.agent import BatchRunStatusEnum

    if failed == 0:
        status = BatchRunStatusEnum.COMPLETED.value
    elif succeeded == 0:
        status = BatchRunStatusEnum.FAILED.value
    else:
        status = BatchRunStatusEnum.PARTIAL_FAILED.value
    batch_run.status = status
    batch_run.completed_at = utc_now()
    batch_run.execution_lease_expires_at = None
    return status


def _record_batch_cancellation(
    batch_run: BatchRun, completed_at: datetime | None
) -> None:
    if batch_run.results is None:
        batch_run.results = []
    if completed_at is None:
        batch_run.completed_at = utc_now()
    batch_run.execution_lease_expires_at = None


async def _notify_batch_terminal(
    db: AsyncSession, batch_run: BatchRun, total: int
) -> None:
    from app.core.i18n import DEFAULT_LOCALE, _
    from app.core.sio_bridge import notify_user_sync
    from app.enums.agent import BatchRunStatusEnum
    from app.services.common.notification_service import (
        DefaultTemplateCopy,
        NotificationService,
    )

    status = batch_run.status
    recipient_id = batch_run.created_by
    if not recipient_id or status not in (
        BatchRunStatusEnum.COMPLETED.value,
        BatchRunStatusEnum.FAILED.value,
        BatchRunStatusEnum.PARTIAL_FAILED.value,
    ):
        return

    succeeded = batch_run.completed_items
    failed = batch_run.failed_items
    processed = succeeded + failed
    completed = status == BatchRunStatusEnum.COMPLETED.value
    data = {
        "batch_id": batch_run.id,
        "completed": processed,
        "total": total,
        "progress": processed * 100 // total if total else 100,
        "succeeded": succeeded,
        "failed": failed,
        "status": status,
        "error": next(
            (
                str(item["error"])
                for item in batch_run.errors or ()
                if item.get("error")
            ),
            f"{failed}/{total} items failed",
        ),
    }
    fallback_title = f"Batch {batch_run.id}: {succeeded}/{total} succeeded"
    fallback_body = None
    default_copy = None
    if status == BatchRunStatusEnum.PARTIAL_FAILED.value:
        title_key = "ai.batch_notification.partial_failed.title"
        body_key = "ai.batch_notification.partial_failed.body"
        fallback_title = _(title_key)
        fallback_body = _(body_key, **data)
        default_copy = DefaultTemplateCopy(
            original=(
                "批处理执行失败",
                "批处理任务执行失败，错误信息：{error}",
            ),
            replacement=(
                _(title_key, locale=DEFAULT_LOCALE),
                _(body_key, locale=DEFAULT_LOCALE),
            ),
        )

    payload = await NotificationService(db).build_ws_payload(
        template_code="ai.batch_complete" if completed else "ai.batch_failed",
        data=data,
        fallback_category="ai",
        fallback_title=fallback_title,
        fallback_body=fallback_body,
        fallback_priority="normal" if completed else "high",
        default_copy=default_copy,
    )
    if payload:
        notify_user_sync("tenant_admin", recipient_id, payload)


@register_task(
    queue="ai_gateway",
    description="Execute agent batch processing / 执行智能体批处理",
    max_retries=1,
)
def execute_batch_run(self: BaseTask, batch_run_id: int, tenant_id: int) -> dict:
    """
    Celery async batch execution / Celery 异步执行批处理

    1. Load BatchRun + Agent from DB / 从 DB 加载 BatchRun + Agent
    2. Iterate items, call TaskEngine.execute() for each / 遍历 items，逐项调用 TaskEngine.execute()
    3. Update BatchRun.completed_items / failed_items in real time / 实时更新 BatchRun.completed_items / failed_items
    4. Update status and completed_at after all done / 全部完成后更新 status 和 completed_at

    Args:
        batch_run_id: Batch run ID / 批量运行 ID
        tenant_id: Tenant ID / 企业 ID

    Returns:
        Execution summary / 执行摘要
    """

    async def _execute() -> dict:
        from app.ai.engine.task import TaskEngine
        from app.ai.engine.types import ExecutionRequest
        from app.ai.gateway import AIGateway
        from app.ai.tools.sandbox import SandboxConfig, ToolSandbox
        from app.core.redis import RedisManager
        from app.enums.agent import AgentExecutionModeEnum, BatchRunStatusEnum
        from app.models.ai.batch_run import BatchRun
        from app.repositories.ai.agent_repository import AgentRepository
        from app.repositories.ai.batch_run_repository import execution_lease_deadline

        async with _task_async_session() as db:
            owns_redis = False
            batch_run = None
            started_at = None
            heartbeat = None
            running = BatchRunStatusEnum.RUNNING.value
            cancelled = BatchRunStatusEnum.CANCELLED.value
            unknown = BatchRunStatusEnum.UNKNOWN.value
            expected_status = BatchRunStatusEnum.PENDING.value
            try:
                # 1. Load BatchRun / 加载 BatchRun
                from sqlalchemy import select

                stmt = select(BatchRun).where(
                    BatchRun.id == batch_run_id,
                    BatchRun.tenant_id == tenant_id,
                    BatchRun.is_deleted.is_(False),
                )
                result = await db.execute(stmt)
                batch_run = result.scalar_one_or_none()

                if not batch_run:
                    logger.error("BatchRun {} not found", batch_run_id)
                    return {"error": "BatchRun not found"}

                if batch_run.status != expected_status:
                    return {"status": batch_run.status}

                # 2. Load Agent / 加载 Agent
                agent_repo = AgentRepository(db, tenant_id)
                agent = await agent_repo.get_by_id(batch_run.agent_id)
                if agent:
                    # The worker skips the API lifespan. Keep the pool on this task loop.
                    owns_redis = RedisManager._pool is None
                    if owns_redis:
                        await RedisManager.init()

                # Claim the still-pending row after Redis setup. Cancellation and
                # another worker may have committed while this task was waiting.
                await db.refresh(
                    batch_run, ["status", "is_deleted"], with_for_update=True
                )
                if batch_run.is_deleted:
                    return {"error": "BatchRun not found"}
                if batch_run.status != expected_status:
                    return {"status": batch_run.status}

                batch_run.dispatch_recovery_due_at = None
                if not agent:
                    batch_run.status = BatchRunStatusEnum.FAILED.value
                    batch_run.completed_at = utc_now()
                    await db.commit()
                    return {"error": "Agent not found"}

                started_at = utc_now()
                batch_run.status = running
                batch_run.started_at = started_at
                batch_run.execution_lease_expires_at = execution_lease_deadline(
                    EXECUTION_LEASE_DURATION
                )
                await db.commit()
                expected_status = running
                heartbeat = asyncio.create_task(
                    _heartbeat_batch_execution(
                        batch_run_id=batch_run_id,
                        tenant_id=tenant_id,
                        started_at=started_at,
                    )
                )
                execution_lock = (
                    select(BatchRun.status, BatchRun.completed_at)
                    .where(
                        BatchRun.id == batch_run_id,
                        BatchRun.tenant_id == tenant_id,
                        BatchRun.is_deleted.is_(False),
                        BatchRun.started_at == started_at,
                    )
                    .with_for_update()
                )
                admission_lock = execution_lock.add_columns(
                    (
                        BatchRun.execution_lease_expires_at
                        > execution_lease_deadline(timedelta(0))
                    ).label("lease_active")
                )

                # 4. Resolve skill bindings + read platform security config / 解析技能绑定 + 读取平台安全配置
                gateway = AIGateway(db)

                # Immutable billing context for batch (tenant admin–initiated) / 批处理计费归属快照
                from app.enums.common import UserRoleEnum
                from app.services.ai.agent_service import AgentService

                _batch_user_role = UserRoleEnum.TENANT_ADMIN.value
                _batch_billing_ctx = await AgentService(
                    db,
                    tenant_id,
                ).build_usage_attribution_context(
                    agent=agent,
                    user_id=batch_run.created_by,
                    user_role=_batch_user_role,
                    user_role_id=None,
                )

                # TaskEngine.execute() resolves tools per live item request /
                # TaskEngine.execute() 会按每个 live item request 解析工具。
                # Do not eager resolve the full agent inventory here; batch warmup
                # is not a live routing owner and should not recreate a second
                # broad-inventory seam before item execution starts /
                # 不要在这里 eager resolve 全量 agent inventory；batch warmup
                # 不是 live routing owner，不应在 item execution 开始前重建第二条
                # broad-inventory seam。

                # Read platform Toolkit security config / 读取平台 Toolkit 安全配置
                from app.configs.service import ConfigService

                _cfg = ConfigService(db)
                _toolkit_security_level = str(
                    await _cfg.get_platform_config(
                        "toolkit_security_level", default="normal"
                    )
                )
                _toolkit_memory_limit_mb = int(
                    await _cfg.get_platform_config(
                        "toolkit_memory_limit_mb", default=256
                    )
                )

                sandbox = ToolSandbox(
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    config=SandboxConfig(),
                    gateway=gateway,
                    db=db,
                    agent=agent,
                    toolkit_security_level=_toolkit_security_level,
                    toolkit_memory_limit_mb=_toolkit_memory_limit_mb,
                )
                task_engine = TaskEngine(
                    db=db,
                    gateway=gateway,
                    sandbox=sandbox,
                )

                # 5. Execute items one by one / 逐项执行
                items = batch_run.input_items or []
                succeeded = 0
                failed = 0
                all_results: list[dict] = []
                all_errors: list[dict] = []
                status = running
                settled_here = False

                for idx, item_data in enumerate(items):
                    admission = (await db.execute(admission_lock)).one_or_none()
                    if admission is None:
                        await db.rollback()
                        return {"error": "Batch execution claim no longer exists"}
                    status = admission.status
                    if status == cancelled:
                        _record_batch_cancellation(batch_run, admission.completed_at)
                    # Release the row lock before any model or tool work.
                    await db.commit()
                    if status != running or not admission.lease_active:
                        break

                    item_id = item_data.get("item_id", str(idx))
                    input_vars = item_data.get("input_variables", {})

                    try:
                        item_request = ExecutionRequest(
                            agent_id=agent.id,
                            tenant_id=tenant_id,
                            input_variables=input_vars,
                            execution_mode=AgentExecutionModeEnum.TASK.value,
                            stream=False,
                            skip_quota=True,
                            skip_persistence=True,
                            user_role=_batch_user_role,
                            billing_context=_batch_billing_ctx,
                        )

                        exec_result = await task_engine.execute(
                            agent,
                            item_request,
                        )

                        if exec_result.success:
                            succeeded += 1
                            all_results.append(
                                {
                                    "item_id": item_id,
                                    "output": exec_result.output,
                                    "total_tokens": exec_result.total_tokens,
                                }
                            )
                        else:
                            failed += 1
                            all_errors.append(
                                {
                                    "item_id": item_id,
                                    "error": exec_result.error,
                                }
                            )

                    except Exception as exc:
                        failed += 1
                        all_errors.append(
                            {
                                "item_id": item_id,
                                "error": str(exc),
                            }
                        )
                        logger.error(
                            "Batch item {} failed: {}",
                            item_id,
                            str(exc),
                            exc_info=True,
                        )

                    owner = (await db.execute(execution_lock)).one_or_none()
                    if owner is None:
                        await db.rollback()
                        return {"error": "Batch execution claim no longer exists"}
                    status = owner.status
                    if status not in (running, cancelled, unknown):
                        await db.rollback()
                        return {"status": status}

                    # The same claim can checkpoint an admitted item after cancellation
                    # or a sweep, but cannot reopen the batch or start another item.
                    batch_run.completed_items = succeeded
                    batch_run.failed_items = failed
                    batch_run.results = list(all_results)
                    batch_run.errors = list(all_errors) if all_errors else None
                    if status == running and idx + 1 == len(items):
                        status = _settle_batch_execution(
                            batch_run, succeeded=succeeded, failed=failed
                        )
                        settled_here = True
                    elif status == cancelled:
                        _record_batch_cancellation(batch_run, owner.completed_at)
                    await db.commit()
                    if status == unknown:
                        break

                    # Socket.IO real-time progress push / Socket.IO 实时进度推送
                    try:
                        from app.core.sio_bridge import notify_user_sync
                        from app.services.common.notification_service import (
                            NotificationService,
                        )

                        if batch_run.created_by:
                            percent = int((idx + 1) / len(items) * 100)
                            progress_data = {
                                "batch_id": batch_run_id,
                                "current": idx + 1,
                                "completed": idx + 1,
                                "total": len(items),
                                "progress": percent,
                                "percent": percent,
                                "succeeded": succeeded,
                                "failed": failed,
                            }
                            payload = await NotificationService(db).build_ws_payload(
                                template_code="ai.batch_progress",
                                data=progress_data,
                                fallback_category="ai",
                                fallback_title=(
                                    f"Batch {batch_run_id}: {idx + 1}/{len(items)}"
                                ),
                                fallback_priority="normal",
                            )
                            if payload:
                                notify_user_sync(
                                    "tenant_admin",
                                    batch_run.created_by,
                                    payload,
                                )
                    except Exception:
                        pass
                    if status != running:
                        break

                # Older persisted rows can have an empty input snapshot.
                if not items:
                    owner = (await db.execute(execution_lock)).one_or_none()
                    if owner is None:
                        await db.rollback()
                        return {"error": "Batch execution claim no longer exists"}
                    status = owner.status
                    if status == running:
                        batch_run.completed_items = 0
                        batch_run.failed_items = 0
                        batch_run.results = []
                        batch_run.errors = None
                        status = _settle_batch_execution(
                            batch_run, succeeded=succeeded, failed=failed
                        )
                        settled_here = True
                    elif status == cancelled:
                        _record_batch_cancellation(batch_run, owner.completed_at)
                    await db.commit()

                if settled_here:
                    with suppress(Exception):
                        await _notify_batch_terminal(db, batch_run, len(items))

                logger.info(
                    "Batch {} execution ended: status={} succeeded={}/{} failed={}",
                    batch_run_id,
                    status,
                    succeeded,
                    len(items),
                    failed,
                )

                return {
                    "batch_run_id": batch_run_id,
                    "total": len(items),
                    "succeeded": succeeded,
                    "failed": failed,
                    "status": status,
                }

            except Exception as exc:
                logger.error(
                    "Batch {} execution error: {}",
                    batch_run_id,
                    str(exc),
                    exc_info=True,
                )
                if batch_run is not None:
                    with suppress(Exception):
                        await db.rollback()
                        fields = ["status", "is_deleted"]
                        if expected_status == running:
                            fields.extend(("started_at", "tenant_id"))
                        await db.refresh(batch_run, fields, with_for_update=True)
                        if batch_run.is_deleted:
                            return {"error": "BatchRun not found"}
                        if batch_run.status != expected_status:
                            return {"status": batch_run.status}
                        if expected_status == running and (
                            batch_run.started_at != started_at
                            or batch_run.tenant_id != tenant_id
                        ):
                            return {"error": "Batch execution claim no longer exists"}
                        batch_run.status = BatchRunStatusEnum.FAILED.value
                        batch_run.completed_at = utc_now()
                        batch_run.dispatch_recovery_due_at = None
                        batch_run.execution_lease_expires_at = None
                        await db.commit()
                return {"error": str(exc)}
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.warning(
                            "Batch execution heartbeat stopped: batch_run_id={} error_type={}",
                            batch_run_id,
                            type(exc).__name__,
                        )
                if owns_redis:
                    with suppress(Exception):
                        await RedisManager.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_execute())
    finally:
        loop.close()


def publish_batch_run(batch_run_id: int, tenant_id: int, celery_task_id: str) -> None:
    execute_batch_run.apply_async(
        kwargs={"batch_run_id": batch_run_id, "tenant_id": tenant_id},
        task_id=celery_task_id,
        retry=False,
    )


async def _recover_batch_dispatches() -> dict[str, int]:
    from app.repositories.ai.batch_run_repository import (
        BatchRunRepository,
        lock_next_batch_dispatch,
        mark_expired_batch_executions_unknown,
    )

    counts = {"claimed": 0, "confirmed": 0, "unconfirmed": 0}
    async with _task_async_session() as db:
        for _ in range(DISPATCH_RECOVERY_BATCH_SIZE):
            async with db.begin():
                now = utc_now()
                batch = await lock_next_batch_dispatch(db, now)
                if batch is None:
                    break
                task_id = batch.celery_task_id
                assert task_id is not None
                batch_id, tenant_id = batch.id, batch.tenant_id
                attempt = batch.dispatch_recovery_attempts + 1
                # Persist the next eligible time before connecting to Redis.
                # A dead publisher leaves this lease for the next scan.
                delay = min(120 * 2 ** min(attempt - 1, 3), 900)
                batch.dispatch_recovery_attempts = attempt
                batch.dispatch_recovery_due_at = now + timedelta(seconds=delay)
            counts["claimed"] += 1

            try:
                publish_batch_run(batch_id, tenant_id, task_id)
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                counts["unconfirmed"] += 1
                logger.warning(
                    "Batch dispatch recovery publish unconfirmed: "
                    "batch_run_id={} attempt={} error_type={}",
                    batch_id,
                    attempt,
                    type(exc).__name__,
                )
            else:
                async with db.begin():
                    await BatchRunRepository(db, tenant_id).confirm_dispatch(
                        batch_id, task_id, recovery_attempt=attempt
                    )
                counts["confirmed"] += 1

        async with db.begin():
            expired = await mark_expired_batch_executions_unknown(
                db, limit=DISPATCH_RECOVERY_BATCH_SIZE
            )
    if expired:
        logger.warning(
            "Batch executions expired with unknown outcome: count={}", expired
        )
    return counts


@register_task(
    queue="scheduled",
    max_retries=0,
    soft_time_limit=45,
    time_limit=60,
    description="Recover committed AI batch dispatches",
)
def recover_batch_dispatches(self: BaseTask) -> dict[str, int]:
    return asyncio.run(_recover_batch_dispatches())


__all__ = ["execute_batch_run", "recover_batch_dispatches"]
