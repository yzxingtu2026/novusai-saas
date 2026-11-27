"""LeasedTenantTask: opt-in 监控租约任务基类（v2）。/ LeasedTenantTask: opt-in monitor-lease base (v2).

关键约束 / Key constraints:
  - 请求级状态存放在 self.request._monitor_lease，每次 before_start 重置，防止 Ignore / retry 跨调用泄漏。
    Per-request state lives on self.request._monitor_lease, reset each before_start to prevent cross-call leakage.
  - 状态中保存不可变的 celery_task_id / effective_tenant_id / handler_path；回调永不读取 self._tenant_id（共享实例）。
    State holds immutable celery_task_id / effective_tenant_id / handler_path; callbacks never read self._tenant_id (shared instance).
  - 元数据缺失 / handler / 企业不匹配 -> RuntimeError fail-closed，禁止业务无监控执行。
    Missing / mismatched metadata -> RuntimeError fail-closed; never run business unmonitored.
  - register_active_run 返回 None 仅表示重复（-> Ignore），数据库错误原样抛出。
    register_active_run returns None only for duplicates (-> Ignore); DB errors propagate.
  - 心跳线程遇瞬时 DB 错误继续尝试；False 才真正停止。
    Heartbeat thread retries on transient DB errors; False means true terminal stop.
  - 回调捕获 DB 错误仅记日志不覆盖业务结果；finally 必停线程。
    Callbacks catch DB errors as log-only, never overwrite business result; always stop thread in finally.
"""

from __future__ import annotations

import threading
from typing import Any

from celery.exceptions import Ignore
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import LogManager
from app.repositories.system.task_run_lease_repository import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_DURATION_SECONDS,
    TaskRunLeaseRepository,
)
from app.tasks.base import TenantTask

logger = LogManager.get_logger("queue")

_HEARTBEAT_JOIN_TIMEOUT: float = 2.0
_MONITOR_LEASE_ATTR: str = "_monitor_lease"


def _heartbeat_loop(
    *,
    run_id: int,
    lease_token: str,
    celery_task_id: str,
    effective_tenant_id: int,
    handler_path: str,
    duration_seconds: int,
    heartbeat_seconds: int,
    stop_event: threading.Event,
) -> None:
    """心跳循环：瞬时 DB 错误重试，False 才退出。
    Heartbeat loop: retry on transient DB errors, exit only on False."""
    while not stop_event.is_set():
        if stop_event.wait(timeout=float(heartbeat_seconds)):
            return
        try:
            ok = TaskRunLeaseRepository.heartbeat(
                run_id=run_id,
                lease_token=lease_token,
                celery_task_id=celery_task_id,
                effective_tenant_id=effective_tenant_id,
                handler_path=handler_path,
                duration_seconds=duration_seconds,
            )
        except SQLAlchemyError as exc:
            # 瞬时连接错误：记日志，下轮再试。
            # Transient connection error: log and retry next iteration.
            logger.warning(
                "Lease heartbeat transient DB error: run_id={} error_type={}",
                run_id,
                type(exc).__name__,
            )
            continue
        except Exception as exc:
            logger.warning(
                "Lease heartbeat unexpected error: run_id={} error_type={}",
                run_id,
                type(exc).__name__,
            )
            continue

        if not ok:
            # True no-match / terminal：令牌失效或行已终态。
            # True no-match / terminal: token invalidated or row terminal.
            logger.info(
                "Lease heartbeat terminal stop: run_id={} reason=no_match",
                run_id,
            )
            return


def _stop_heartbeat(state: dict[str, Any] | None) -> None:
    """停止心跳线程并置空引用。/ Stop heartbeat thread and clear references."""
    if not state:
        return
    stop_event = state.get("stop_event")
    thread = state.get("thread")
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=_HEARTBEAT_JOIN_TIMEOUT)
    state["stop_event"] = None
    state["thread"] = None


class LeasedTenantTask(TenantTask):
    """带监控租约的 TenantTask 基类。/ TenantTask base with monitor lease."""

    abstract = True

    LEASE_HANDLER_PATH: str = ""
    LEASE_DURATION_SECONDS: int = DEFAULT_LEASE_DURATION_SECONDS
    LEASE_HEARTBEAT_SECONDS: int = DEFAULT_HEARTBEAT_SECONDS

    # ── 请求级生命周期 / Per-request lifecycle ────────────────────────────
    def before_start(self, task_id: str, args: tuple, kwargs: dict) -> None:
        """先重置租约状态，再走基类流程。/ Reset lease state first, then base flow."""
        # 必须在 super() 之前重置；防止上一轮 Ignore / retry 在同一实例上残留。
        # Must reset before super(); prevents residue from prior Ignore / retry on same instance.
        setattr(self.request, _MONITOR_LEASE_ATTR, None)
        super().before_start(task_id, args, kwargs)

    # ── 启动 / Start ──────────────────────────────────────────────────────
    def _record_task_run_start(
        self,
        task_id: str,
        args: tuple,
        kwargs: dict,
    ) -> None:
        """Fail-closed 租约注册；元数据缺失 / 不匹配直接 RuntimeError。
        Fail-closed lease registration; missing / mismatched metadata raises RuntimeError."""
        context = self._get_task_run_context()
        if not context:
            raise RuntimeError("Leased task requires monitoring metadata")

        # 元数据校验：handler_path 必须匹配；effective_tenant_id 必填。
        # Metadata check: handler_path must match; effective_tenant_id required.
        if not self.LEASE_HANDLER_PATH:
            raise RuntimeError(
                f"LeasedTenantTask {self.name} missing LEASE_HANDLER_PATH"
            )
        handler_path = self.LEASE_HANDLER_PATH
        if handler_path != context["handler_path_snapshot"]:
            raise RuntimeError(
                "Handler mismatch for leased task: "
                f"expected={handler_path!r} "
                f"actual={context['handler_path_snapshot']!r}"
            )
        effective_tenant_id = context["effective_tenant_id"]
        if (
            effective_tenant_id is None
            or effective_tenant_id != self._normalize_tenant_id(kwargs.get("tenant_id"))
        ):
            raise RuntimeError(
                f"LeasedTenantTask {self.name} requires effective_tenant_id; "
                "cannot run business unmonitored"
            )

        run_key = self._build_context_run_key(context)
        queue = context.get("queue") or getattr(self, "queue", "default") or "default"
        safe_args = self._safe_json(list(args)) if args else None
        safe_kwargs = self._safe_json(dict(kwargs)) if kwargs else None
        args_summary = None
        if safe_args is not None or safe_kwargs is not None:
            args_summary = {"args": safe_args, "kwargs": safe_kwargs}

        retry_attempt = int(getattr(self.request, "retries", 0) or 0)

        # 数据库错误原样抛出；None 仅表示重复 -> Ignore。
        # DB errors propagate; None means duplicate only -> Ignore.
        registration = TaskRunLeaseRepository.register_active_run(
            celery_task_id=task_id,
            effective_tenant_id=effective_tenant_id,
            owner_tenant_id=context["owner_tenant_id"],
            task_definition_id=context["task_definition_id"],
            binding_id=context["binding_id"],
            task_code_snapshot=context["task_code_snapshot"],
            task_name_snapshot=context["task_name_snapshot"],
            handler_path_snapshot=handler_path,
            trigger_source=context["trigger_source"],
            run_kind=context["run_kind"],
            queue=queue,
            priority=context["priority"],
            run_key=run_key,
            trigger_slot=context["trigger_slot"],
            trigger_id=context["trigger_id"],
            retry_of_run_id=context["retry_of_run_id"],
            retry_of_task_id=context["retry_of_task_id"],
            args_summary=args_summary,
            trace_id=self._get_trace_id(),
            lease_duration_seconds=self.LEASE_DURATION_SECONDS,
            retry_attempt=retry_attempt,
        )

        if registration is None:
            # None 仅表示重复；查既有行并 raise Ignore。
            # None means duplicate only; look up existing row and raise Ignore.
            raise Ignore()

        # 保存不可变状态到 self.request。/ Save immutable state on self.request.
        stop_event = threading.Event()
        state: dict[str, Any] = {
            "run_id": registration.run_id,
            "lease_token": registration.lease_token,
            "celery_task_id": task_id,
            "effective_tenant_id": effective_tenant_id,
            "handler_path": registration.handler_path,
            "stop_event": stop_event,
            "thread": None,
        }
        setattr(self.request, _MONITOR_LEASE_ATTR, state)

        thread = threading.Thread(
            target=_heartbeat_loop,
            kwargs={
                "run_id": registration.run_id,
                "lease_token": registration.lease_token,
                "celery_task_id": task_id,
                "effective_tenant_id": effective_tenant_id,
                "handler_path": handler_path,
                "duration_seconds": self.LEASE_DURATION_SECONDS,
                "heartbeat_seconds": self.LEASE_HEARTBEAT_SECONDS,
                "stop_event": stop_event,
            },
            name=f"lease-heartbeat-{registration.run_id}",
            daemon=True,
        )
        thread.start()
        state["thread"] = thread

    # ── 成功 / Success ────────────────────────────────────────────────────
    def _record_task_run_success(
        self,
        task_id: str,
        retval: Any,
        elapsed: float,
    ) -> None:
        state: dict[str, Any] | None = getattr(self.request, _MONITOR_LEASE_ATTR, None)
        if not state or state["celery_task_id"] != task_id:
            return
        try:
            ok = TaskRunLeaseRepository.settle_success(
                run_id=int(state["run_id"]),
                lease_token=state["lease_token"],
                celery_task_id=state["celery_task_id"],
                effective_tenant_id=state["effective_tenant_id"],
                handler_path=state["handler_path"],
                result_summary=self._safe_json(retval),
                summary=self._summarize_payload(retval),
                duration_ms=int(elapsed * 1000),
            )
            if not ok:
                logger.warning(
                    "Lease success settle no-match: task={} run_id={}",
                    self.name,
                    state["run_id"],
                )
        except SQLAlchemyError as exc:
            # DB 错误仅记日志，不覆盖业务成功。
            # DB error: log only, never overwrite business success.
            logger.warning(
                "Lease success monitor write failed: task={} run_id={} error_type={}",
                self.name,
                state.get("run_id"),
                type(exc).__name__,
            )
        finally:
            _stop_heartbeat(state)
            setattr(self.request, _MONITOR_LEASE_ATTR, None)

    # ── 失败 / Failure ────────────────────────────────────────────────────
    def _record_task_run_failure(
        self,
        task_id: str,
        exc: Exception,
        einfo: Any,
        elapsed: float,
    ) -> None:
        state: dict[str, Any] | None = getattr(self.request, _MONITOR_LEASE_ATTR, None)
        if not state or state["celery_task_id"] != task_id:
            return
        try:
            ok = TaskRunLeaseRepository.settle_failure(
                run_id=int(state["run_id"]),
                lease_token=state["lease_token"],
                celery_task_id=state["celery_task_id"],
                effective_tenant_id=state["effective_tenant_id"],
                handler_path=state["handler_path"],
                error_message_public=str(exc)[:500],
                error_message_internal=str(exc)[:2000],
                traceback_internal=str(einfo)[:5000] if einfo else None,
                summary=str(exc)[:500],
                duration_ms=int(elapsed * 1000),
            )
            if not ok:
                logger.warning(
                    "Lease failure settle no-match: task={} run_id={}",
                    self.name,
                    state["run_id"],
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "Lease failure monitor write failed: task={} run_id={} error_type={}",
                self.name,
                state.get("run_id"),
                type(exc).__name__,
            )
        finally:
            _stop_heartbeat(state)
            setattr(self.request, _MONITOR_LEASE_ATTR, None)

    # ── 重试 / Retry ──────────────────────────────────────────────────────
    def _record_task_run_retry(self, task_id: str, exc: Exception) -> None:
        """Celery RETRY 不走 after_return；必须在此停线程并清空状态。
        Celery RETRY bypasses after_return; must stop thread and clear state here."""
        state: dict[str, Any] | None = getattr(self.request, _MONITOR_LEASE_ATTR, None)
        if not state or state["celery_task_id"] != task_id:
            return
        try:
            retry_count = int(getattr(self.request, "retries", 0) or 0)
            ok = TaskRunLeaseRepository.settle_retrying(
                run_id=int(state["run_id"]),
                lease_token=state["lease_token"],
                celery_task_id=state["celery_task_id"],
                effective_tenant_id=state["effective_tenant_id"],
                handler_path=state["handler_path"],
                retry_count=retry_count,
                error_message_public=str(exc)[:500],
                summary=str(exc)[:500],
            )
            if not ok:
                logger.warning(
                    "Lease retry settle no-match: task={} run_id={}",
                    self.name,
                    state["run_id"],
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "Lease retry monitor write failed: task={} run_id={} error_type={}",
                self.name,
                state.get("run_id"),
                type(exc).__name__,
            )
        finally:
            # Celery 重试不走 after_return；必须在此彻底清理。
            # Celery retry bypasses after_return; must fully clean up here.
            _stop_heartbeat(state)
            setattr(self.request, _MONITOR_LEASE_ATTR, None)

    # ── 返回后清理 / After-return cleanup ─────────────────────────────────
    def after_return(
        self,
        status: str,
        retval: Any,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        try:
            state: dict[str, Any] | None = getattr(
                self.request, _MONITOR_LEASE_ATTR, None
            )
            _stop_heartbeat(state)
        finally:
            setattr(self.request, _MONITOR_LEASE_ATTR, None)
            super().after_return(status, retval, task_id, args, kwargs, einfo)

    # ── 内部辅助 / Internal helpers ───────────────────────────────────────
    def _get_trace_id(self) -> str | None:
        from app.middleware.trace import trace_id_var

        return trace_id_var.get() or None


__all__ = ["LeasedTenantTask"]
