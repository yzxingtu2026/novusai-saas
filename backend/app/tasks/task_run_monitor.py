"""TaskRun 监控租约回收任务（v2）。/ TaskRun monitor-lease reaper task (v2).

纯同步 BaseTask 平台任务；不做业务派单 / 不自动重试；异常必须标记 task_failed。
Pure sync BaseTask platform task; no business dispatch / no auto-retry; exceptions must mark task_failed.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import LogManager
from app.repositories.system.task_run_lease_repository import (
    TaskRunLeaseRepository,
    leased_handlers,
)
from app.tasks.base import BaseTask, register_task

logger = LogManager.get_logger("queue")

# 单轮回收上限。/ Per-round reaping ceiling.
REAP_BATCH_LIMIT: int = 100


@register_task(
    name="app.tasks.task_run_monitor.reconcile_expired_task_runs",
    queue="scheduled",
    description="Reconcile expired TaskRun leases / 回收过期 TaskRun 监控租约",
    max_retries=0,
    base=BaseTask,
)
def reconcile_expired_task_runs(
    self: BaseTask,
) -> dict[str, Any]:
    """中文: 每分钟扫描 opt-in handler 的过期租约，running + 过期 -> unknown。
    异常原样抛出，由 Celery 标记 task_failed。

    EN: Per-minute scan of opt-in handler expired leases; running + expired -> unknown.
    Exceptions propagate; Celery marks task_failed.
    """
    _ = self
    handlers = leased_handlers()
    results: dict[str, Any] = {
        "handlers_scanned": len(handlers),
        "handlers_reaped": [],
        "total_lost": 0,
    }

    for handler_path in handlers:
        # 异常不吞；per-handler 失败必须上浮为 task_failed。
        # No swallow; per-handler failure must surface as task_failed.
        reap_ids = TaskRunLeaseRepository.reap_lost_runs(
            handler_path=handler_path,
            batch_limit=REAP_BATCH_LIMIT,
        )
        if reap_ids:
            results["handlers_reaped"].append(
                {
                    "handler": handler_path,
                    "run_ids": reap_ids,
                    "count": len(reap_ids),
                }
            )
            results["total_lost"] += len(reap_ids)

    if results["total_lost"]:
        logger.info(
            "TaskRun lease reaper: handlers_scanned={} total_lost={}",
            results["handlers_scanned"],
            results["total_lost"],
        )
    return results


__all__ = [
    "REAP_BATCH_LIMIT",
    "reconcile_expired_task_runs",
]
