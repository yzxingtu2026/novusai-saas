"""Synchronous monitor lease CAS repository / 同步监控租约原子仓储。

Monitoring loss is not business failure; no operation here dispatches work.
数据库时钟、行锁与令牌约束；不派发或重跑业务。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.base_repository import BaseRepository
from app.core.database import sync_session_factory
from app.models.system.task_run import TaskRun

DEFAULT_LEASE_DURATION_SECONDS = 90
DEFAULT_HEARTBEAT_SECONDS = 20


def leased_handlers() -> tuple[str, ...]:
    """Read opt-ins from the actual Celery task registry / 从真实任务注册表获取启用租约的处理器。"""
    from app.tasks.base import get_task_registry

    return tuple(
        sorted(
            name
            for name, entry in get_task_registry().copy().items()
            if entry.get("monitors_execution_lease") is True
        )
    )


@dataclass(frozen=True)
class LeaseRegistration:
    run_id: int
    lease_token: str
    celery_task_id: str
    effective_tenant_id: int | None
    handler_path: str


def _duration(seconds: int) -> timedelta:
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("Lease duration must be an integer in 1..3600")
    return timedelta(seconds=seconds)


def _scope(tenant_id: int | None):
    # NULL scopes platform rows only, never all tenants. / NULL 仅匹配平台行。
    return TaskRun.effective_tenant_id == tenant_id


def _identity(
    run_id: int,
    lease_token: str,
    celery_task_id: str,
    effective_tenant_id: int | None,
    handler_path: str,
):
    return (
        TaskRun.id == run_id,
        TaskRun.celery_task_id == celery_task_id,
        TaskRun.lease_token == lease_token,
        TaskRun.handler_path_snapshot == handler_path,
        TaskRun.is_deleted.is_(False),
        TaskRun.status.in_(("running", "unknown")),
        _scope(effective_tenant_id),
    )


def _bound_transaction(db) -> None:
    # Bound waits; connection errors propagate for retry, not token mismatch.
    # 限制锁和语句等待；连接异常上抛，不伪装成令牌失效。
    db.execute(text("SET LOCAL lock_timeout = '3s'"))
    db.execute(text("SET LOCAL statement_timeout = '5s'"))


class TaskRunLeaseRepository(BaseRepository[TaskRun]):
    """Sync worker repository; each operation owns an independent transaction."""

    model = TaskRun

    @staticmethod
    def register_active_run(
        *,
        celery_task_id: str,
        effective_tenant_id: int | None,
        owner_tenant_id: int | None,
        task_definition_id: int | None,
        binding_id: int | None,
        task_code_snapshot: str,
        task_name_snapshot: str,
        handler_path_snapshot: str,
        trigger_source: str,
        run_kind: str,
        queue: str,
        priority: int | None,
        run_key: str | None,
        trigger_slot: str | None,
        trigger_id: str | None,
        retry_of_run_id: int | None,
        retry_of_task_id: str | None,
        args_summary: dict[str, Any] | None,
        trace_id: str | None,
        lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
        retry_attempt: int = 0,
    ) -> LeaseRegistration | None:
        if handler_path_snapshot not in leased_handlers():
            raise ValueError("Handler not opted in for lease monitoring")
        duration = _duration(lease_duration_seconds)
        token = uuid4().hex
        with sync_session_factory() as db:
            try:
                with db.begin():
                    _bound_transaction(db)
                    if retry_attempt > 0:
                        # Rotate only after the prior token explicitly settled RETRYING.
                        # 重试仅轮换同一次 Celery 执行的 retrying 行，不接管活跃副本。
                        candidate = db.execute(
                            select(TaskRun.id, TaskRun.lease_token).where(
                                TaskRun.celery_task_id == celery_task_id,
                                _scope(effective_tenant_id),
                                TaskRun.handler_path_snapshot == handler_path_snapshot,
                                TaskRun.run_key == run_key,
                                TaskRun.status == "retrying",
                                TaskRun.lease_token.is_not(None),
                                TaskRun.is_deleted.is_(False),
                            )
                        ).first()
                        if candidate is None:
                            return None
                        run_id = db.execute(
                            update(TaskRun)
                            .where(
                                TaskRun.id == candidate.id,
                                TaskRun.lease_token == candidate.lease_token,
                                TaskRun.celery_task_id == celery_task_id,
                                _scope(effective_tenant_id),
                                TaskRun.handler_path_snapshot == handler_path_snapshot,
                                TaskRun.status == "retrying",
                                TaskRun.is_deleted.is_(False),
                            )
                            .values(
                                status="running",
                                lease_token=token,
                                lease_expires_at=func.clock_timestamp() + duration,
                                retry_count=retry_attempt,
                                finished_at=None,
                            )
                            .returning(TaskRun.id)
                        ).scalar_one_or_none()
                        if run_id is None:
                            return None
                    else:
                        run = TaskRun(
                            celery_task_id=celery_task_id,
                            run_key=run_key,
                            effective_tenant_id=effective_tenant_id,
                            owner_tenant_id=owner_tenant_id,
                            task_definition_id=task_definition_id,
                            binding_id=binding_id,
                            task_code_snapshot=task_code_snapshot,
                            task_name_snapshot=task_name_snapshot,
                            handler_path_snapshot=handler_path_snapshot,
                            trigger_source=trigger_source,
                            run_kind=run_kind,
                            queue=queue,
                            priority=priority,
                            trigger_slot=trigger_slot,
                            trigger_id=trigger_id,
                            retry_of_run_id=retry_of_run_id,
                            retry_of_task_id=retry_of_task_id,
                            args_summary=args_summary,
                            trace_id=trace_id,
                            status="running",
                            lease_token=token,
                            started_at=func.timezone("UTC", func.clock_timestamp()),
                            lease_expires_at=func.clock_timestamp() + duration,
                        )
                        db.add(run)
                        db.flush()
                        run_id = run.id
                return LeaseRegistration(
                    run_id,
                    token,
                    celery_task_id,
                    effective_tenant_id,
                    handler_path_snapshot,
                )
            except IntegrityError as exc:
                # Only actual unique conflicts are deduplication candidates.
                # 仅唯一键冲突可去重，外键等故障保持异常。
                if getattr(exc.orig, "pgcode", None) != "23505":
                    raise
                return None

    @staticmethod
    def heartbeat(
        *,
        run_id: int,
        lease_token: str,
        celery_task_id: str,
        effective_tenant_id: int | None,
        handler_path: str,
        duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
    ) -> bool:
        return _transition(
            run_id,
            lease_token,
            celery_task_id,
            effective_tenant_id,
            handler_path,
            {
                "status": "running",
                "lease_expires_at": func.clock_timestamp()
                + _duration(duration_seconds),
            },
        )

    @staticmethod
    def settle_success(
        *,
        run_id: int,
        lease_token: str,
        celery_task_id: str,
        effective_tenant_id: int | None,
        handler_path: str,
        result_summary: Any,
        summary: str | None,
        duration_ms: int | None,
    ) -> bool:
        return _transition(
            run_id,
            lease_token,
            celery_task_id,
            effective_tenant_id,
            handler_path,
            {
                "status": "success",
                "result_summary": result_summary,
                "summary": summary,
                "finished_at": func.timezone("UTC", func.clock_timestamp()),
                "duration_ms": duration_ms,
                "error_message_public": None,
                "error_message_internal": None,
                "traceback_internal": None,
            },
        )

    @staticmethod
    def settle_failure(
        *,
        run_id: int,
        lease_token: str,
        celery_task_id: str,
        effective_tenant_id: int | None,
        handler_path: str,
        error_message_public: str | None,
        error_message_internal: str | None,
        traceback_internal: str | None,
        summary: str | None,
        duration_ms: int | None,
    ) -> bool:
        return _transition(
            run_id,
            lease_token,
            celery_task_id,
            effective_tenant_id,
            handler_path,
            {
                "status": "failed",
                "error_message_public": error_message_public,
                "error_message_internal": error_message_internal,
                "traceback_internal": traceback_internal,
                "summary": summary,
                "finished_at": func.timezone("UTC", func.clock_timestamp()),
                "duration_ms": duration_ms,
            },
        )

    @staticmethod
    def settle_retrying(
        *,
        run_id: int,
        lease_token: str,
        celery_task_id: str,
        effective_tenant_id: int | None,
        handler_path: str,
        retry_count: int,
        error_message_public: str | None,
        summary: str | None,
    ) -> bool:
        return _transition(
            run_id,
            lease_token,
            celery_task_id,
            effective_tenant_id,
            handler_path,
            {
                "status": "retrying",
                "retry_count": retry_count,
                "error_message_public": error_message_public,
                "summary": summary,
            },
        )

    @staticmethod
    def reap_lost_runs(*, handler_path: str, batch_limit: int = 100) -> list[int]:
        if handler_path not in leased_handlers():
            raise ValueError("Handler not opted in for lease monitoring")
        if type(batch_limit) is not int or not 1 <= batch_limit <= 1000:
            raise ValueError("Monitor batch limit must be an integer in 1..1000")
        with sync_session_factory() as db, db.begin():
            _bound_transaction(db)
            guards = (
                TaskRun.handler_path_snapshot == handler_path,
                TaskRun.is_deleted.is_(False),
                TaskRun.status == "running",
                TaskRun.lease_token.is_not(None),
                TaskRun.lease_expires_at < func.clock_timestamp(),
            )
            ids = list(
                db.scalars(
                    select(TaskRun.id)
                    .where(*guards)
                    .order_by(TaskRun.lease_expires_at, TaskRun.id)
                    .limit(batch_limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if not ids:
                return []
            # Locks guard ownership through commit; return actual updated ids.
            # 持行锁并重复条件，返回实际修改行，而非候选数。
            return list(
                db.scalars(
                    update(TaskRun)
                    .where(TaskRun.id.in_(ids), *guards)
                    .values(status="unknown", monitor_lost_at=func.clock_timestamp())
                    .returning(TaskRun.id)
                )
            )


def _transition(
    run_id, lease_token, celery_task_id, effective_tenant_id, handler_path, values
) -> bool:
    if handler_path not in leased_handlers():
        return False
    with sync_session_factory() as db, db.begin():
        _bound_transaction(db)
        affected = db.execute(
            update(TaskRun)
            .where(
                *_identity(
                    run_id,
                    lease_token,
                    celery_task_id,
                    effective_tenant_id,
                    handler_path,
                )
            )
            .values(**values)
            .returning(TaskRun.id)
        ).scalar_one_or_none()
        return affected is not None
