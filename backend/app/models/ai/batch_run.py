"""
批量运行模型 / Batch Run Model

跟踪智能体批量执行任务的进度和结果
Tracks agent batch execution task progress and results.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base_model import TenantModel
from app.core.i18n import _
from app.enums.agent import BatchRunStatusEnum


class BatchRun(TenantModel):
    """
    批量运行记录 / Batch run record.

    记录一次批量执行的状态、进度和结果。
    由 BatchEngine 创建，Celery 任务异步执行。
    """

    __tablename__ = "batch_runs"
    __data_permission__ = True
    __data_permission_creator_scope__ = "tenant_admin"

    # 允许前端筛选的字段 / Fields exposed for list filtering
    __filterable__ = {
        "id": "id",
        "agent_id": "agent_id",
        "status": "status",
        "tenant_id": "tenant_id",
        "created_at": "created_at",
    }

    # 允许排序的字段 / Sortable columns for UI
    __sortable__ = {
        "id": "id",
        "created_at": "created_at",
        "completed_at": "completed_at",
        "total_items": "total_items",
    }

    # ==================== 关联 ==================== / Associations

    agent_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment=_("enum.batch_run.agent_id"),
    )
    created_by: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment=_("enum.batch_run.created_by"),
    )

    # ==================== 状态 ==================== / Status

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BatchRunStatusEnum.PENDING.value,
        index=True,
        comment=_("enum.batch_run.status"),
    )

    # ==================== 进度 ==================== / Progress

    total_items: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=_("enum.batch_run.total_items"),
    )
    completed_items: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=_("enum.batch_run.completed_items"),
    )
    failed_items: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=_("enum.batch_run.failed_items"),
    )
    max_workers: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        comment=_("enum.batch_run.max_workers"),
    )

    # ==================== 结果 ==================== / Results payload

    results: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment=_("enum.batch_run.results"),
    )
    errors: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment=_("enum.batch_run.errors"),
    )
    input_items: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment=_("enum.batch_run.input_items"),
    )

    # ==================== Celery ==================== / Celery 异步任务

    celery_task_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=_("enum.batch_run.celery_task_id"),
    )

    # Internal dispatch recovery metadata. A null due time does not opt in.
    dispatch_recovery_due_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    dispatch_recovery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 中文: 未设置租约的历史任务不参加运行中失联判定。
    # EN: Runs without an execution lease are outside the expiration scan.
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # ==================== 时间 ==================== / Timestamps

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment=_("enum.batch_run.started_at"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment=_("enum.batch_run.completed_at"),
    )

    # ==================== 复合索引 ==================== / Composite indexes

    __table_args__ = (
        Index("ix_batch_runs_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_batch_runs_tenant_status", "tenant_id", "status"),
        Index(
            "ix_batch_runs_dispatch_recovery_due",
            "status",
            "dispatch_recovery_due_at",
            "id",
            postgresql_where=text(
                "is_deleted IS FALSE AND dispatch_recovery_due_at IS NOT NULL"
            ),
        ),
        Index(
            "ix_batch_runs_execution_lease_due",
            "execution_lease_expires_at",
            "id",
            postgresql_where=text(
                "is_deleted IS FALSE AND status = 'running' "
                "AND execution_lease_expires_at IS NOT NULL"
            ),
        ),
    )

    # ==================== 关系 ==================== / Relationships

    agent = relationship(
        "Agent",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<BatchRun(id={self.id}, agent_id={self.agent_id}, status={self.status})>"
        )


if TYPE_CHECKING:
    pass


__all__ = ["BatchRun"]
