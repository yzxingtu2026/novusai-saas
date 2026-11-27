"""
批量运行 Repository / Batch Run Repository
"""

from datetime import datetime, timedelta

from sqlalchemy import DateTime, Interval, and_, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, raiseload
from sqlalchemy.sql import ColumnElement

from app.core.base_repository import TenantRepository
from app.enums.agent import BatchRunStatusEnum
from app.models.ai.batch_run import BatchRun


def _unconfirmed_dispatch() -> tuple:
    return (
        BatchRun.is_deleted.is_(False),
        BatchRun.status == BatchRunStatusEnum.PENDING.value,
        BatchRun.dispatch_recovery_due_at.is_not(None),
        BatchRun.celery_task_id.is_not(None),
    )


async def lock_next_batch_dispatch(db: AsyncSession, now: datetime) -> BatchRun | None:
    """Lock one due row using the scheduler's platform session."""
    result = await db.execute(
        select(BatchRun)
        .options(
            load_only(
                BatchRun.id,
                BatchRun.tenant_id,
                BatchRun.celery_task_id,
                BatchRun.dispatch_recovery_due_at,
                BatchRun.dispatch_recovery_attempts,
            ),
            raiseload("*"),
        )
        .where(*_unconfirmed_dispatch(), BatchRun.dispatch_recovery_due_at <= now)
        .order_by(BatchRun.dispatch_recovery_due_at, BatchRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


def execution_lease_deadline(duration: timedelta) -> ColumnElement[datetime]:
    """以 PostgreSQL UTC 语句时钟计算截止时间 / Compute a deadline from PostgreSQL's UTC statement clock."""
    now = func.timezone("UTC", func.statement_timestamp(), type_=DateTime())
    return now + literal(duration, type_=Interval())


def _leased_execution() -> tuple[ColumnElement[bool], ...]:
    # 中文: 内联固定状态使 PostgreSQL 通用查询计划也能匹配部分索引。
    # EN: Inlining the fixed status lets generic PostgreSQL plans match the partial index.
    running = literal(BatchRunStatusEnum.RUNNING.value, literal_execute=True)
    return (
        BatchRun.is_deleted.is_(False),
        BatchRun.status == running,
        BatchRun.execution_lease_expires_at.is_not(None),
    )


async def renew_batch_execution_lease(
    db: AsyncSession,
    *,
    batch_run_id: int,
    tenant_id: int,
    started_at: datetime,
    duration: timedelta,
) -> bool:
    """续租仍归属原认领者的运行 / Renew only the original, still-running claim."""
    table = BatchRun.__table__
    stmt = (
        update(table)
        .where(
            BatchRun.id == batch_run_id,
            BatchRun.tenant_id == tenant_id,
            BatchRun.started_at == started_at,
            *_leased_execution(),
        )
        .values(
            execution_lease_expires_at=execution_lease_deadline(duration),
            updated_at=table.c.updated_at,
        )
        .returning(table.c.id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def mark_expired_batch_executions_unknown(db: AsyncSession, *, limit: int) -> int:
    """短事务标记已过期的运行 / Classify expired runs in the caller's short transaction."""
    expired = (
        select(BatchRun.id)
        .where(
            *_leased_execution(),
            BatchRun.execution_lease_expires_at
            <= execution_lease_deadline(timedelta()),
        )
        .order_by(BatchRun.execution_lease_expires_at, BatchRun.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("expired_batch_executions")
    )
    table = BatchRun.__table__
    stmt = (
        update(table)
        .where(table.c.id.in_(select(expired.c.id)))
        .values(status=BatchRunStatusEnum.UNKNOWN.value, updated_at=table.c.updated_at)
        .returning(table.c.id)
    )
    return len((await db.scalars(stmt)).all())


class BatchRunRepository(TenantRepository[BatchRun]):
    """
    企业级批量运行 Repository / Tenant batch run repository.

    提供按智能体查询、更新进度等方法
    """

    model = BatchRun

    @staticmethod
    def _sanitize_create_data(data: dict) -> dict:
        payload = dict(data)
        if payload.get("created_by") is None:
            payload.pop("created_by", None)
        return payload

    async def create(self, data: dict) -> BatchRun:
        return await super().create(self._sanitize_create_data(data))

    async def create_many(self, data_list: list[dict]) -> list[BatchRun]:
        return await super().create_many(
            [self._sanitize_create_data(data) for data in data_list]
        )

    async def lock_for_cancellation(
        self, agent_id: int, run_id: int
    ) -> BatchRun | None:
        """按原读取权限锁定批次行 / Lock a batch under its existing read scope."""
        stmt = (
            select(BatchRun)
            .options(raiseload("*"))
            .where(
                BatchRun.id == run_id,
                BatchRun.agent_id == agent_id,
                self._tenant_scope_column() == self.tenant_id,
                BatchRun.is_deleted.is_(False),
            )
            .with_for_update(of=BatchRun)
            .execution_options(populate_existing=True)
        )
        return await self.db.scalar(self._apply_data_permission_if_needed(stmt))

    async def confirm_dispatch(
        self,
        batch_run_id: int,
        celery_task_id: str,
        *,
        recovery_attempt: int | None = None,
    ) -> None:
        """Close the eligible dispatch intent for this exact batch and task."""
        stmt = update(BatchRun).where(
            BatchRun.id == batch_run_id,
            BatchRun.tenant_id == self.tenant_id,
            BatchRun.celery_task_id == celery_task_id,
            *_unconfirmed_dispatch(),
        )
        if recovery_attempt is not None:
            stmt = stmt.where(BatchRun.dispatch_recovery_attempts == recovery_attempt)
        await self.db.execute(stmt.values(dispatch_recovery_due_at=None))

    async def get_by_agent(
        self,
        agent_id: int,
        skip: int = 0,
        limit: int = 20,
    ) -> list[BatchRun]:
        """
        获取智能体的批量运行记录 / Get batch run records for agent.

        Args:
            agent_id: 智能体 ID
            skip: 跳过数量
            limit: 返回数量

        Returns:
            BatchRun 列表
        """
        stmt = (
            select(BatchRun)
            .where(
                and_(
                    BatchRun.tenant_id == self.tenant_id,
                    BatchRun.agent_id == agent_id,
                    BatchRun.is_deleted.is_(False),
                )
            )
            .order_by(BatchRun.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        stmt = self._apply_data_permission_if_needed(stmt)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def update_progress(
        self,
        batch_run_id: int,
        completed_items: int,
        failed_items: int,
        status: str | None = None,
    ) -> None:
        """
        更新批量运行进度 / Update batch run progress.

        Args:
            batch_run_id: 批量运行 ID
            completed_items: 已完成数量
            failed_items: 失败数量
            status: 状态（可选）
        """
        values = {
            "completed_items": completed_items,
            "failed_items": failed_items,
        }
        if status:
            values["status"] = status

        stmt = (
            update(BatchRun)
            .where(
                and_(
                    BatchRun.id == batch_run_id,
                    BatchRun.tenant_id == self.tenant_id,
                )
            )
            .values(**values)
        )
        permission_condition = self._build_data_permission_condition()
        if permission_condition is not None:
            stmt = stmt.where(permission_condition)
        await self.db.execute(stmt)
        await self.db.flush()

    async def get_active_runs(self, agent_id: int) -> list[BatchRun]:
        """获取进行中的批量运行 / Get in-progress batch runs for the agent."""
        stmt = (
            select(BatchRun)
            .where(
                and_(
                    BatchRun.tenant_id == self.tenant_id,
                    BatchRun.agent_id == agent_id,
                    BatchRun.status.in_(
                        [
                            BatchRunStatusEnum.PENDING.value,
                            BatchRunStatusEnum.RUNNING.value,
                        ]
                    ),
                    BatchRun.is_deleted.is_(False),
                )
            )
            .order_by(BatchRun.created_at.desc())
        )
        stmt = self._apply_data_permission_if_needed(stmt)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())


__all__ = [
    "BatchRunRepository",
    "execution_lease_deadline",
    "mark_expired_batch_executions_unknown",
    "renew_batch_execution_lease",
]
