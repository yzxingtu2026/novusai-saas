"""Persist fixed-image delivery receipts / 持久化固定镜像交付回执。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import Base


class PluginImageOperation(Base):
    __tablename__ = "plugin_image_operations"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('install','upgrade','rollback')", name="ck_piop_operation"
        ),
        CheckConstraint(
            "phase IN ('requested','prepared','old_started','old_completed','apply_started','applied','uncertain','verified')",
            name="ck_piop_phase",
        ),
        CheckConstraint(
            "(operation = 'install' OR plugin_id IS NOT NULL) "
            "AND (phase NOT IN ('applied','verified') OR plugin_id IS NOT NULL)",
            name="ck_piop_plugin_identity",
        ),
        CheckConstraint(
            "(operation = 'rollback') = (target_version_id IS NOT NULL)",
            name="ck_piop_rollback_identity",
        ),
        CheckConstraint(
            "(phase IN ('applied','verified')) = (applied_at IS NOT NULL)",
            name="ck_piop_applied",
        ),
        CheckConstraint(
            "(phase = 'verified' AND deployment_completed_at IS NOT NULL "
            "AND deployment_attestation IS NOT NULL "
            "AND trim(CAST(deployment_attestation AS TEXT)) <> 'null') OR "
            "(phase <> 'verified' AND deployment_completed_at IS NULL AND deployment_attestation IS NULL)",
            name="ck_piop_attestation",
        ),
        Index(
            "uq_piop_open_plugin",
            "plugin_name",
            unique=True,
            postgresql_where=text("phase <> 'verified'"),
            sqlite_where=text("phase <> 'verified'"),
        ),
    )

    request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str] = mapped_column(String(18), nullable=False)
    plugin_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 历史回执不阻塞正常卸载和账户清理 / Historical receipts do not block existing deletion flows.
    plugin_id: Mapped[int | None] = mapped_column(Integer)
    operator_id: Mapped[int] = mapped_column(Integer)
    target_version_id: Mapped[int | None] = mapped_column(Integer)
    build: Mapped[dict] = mapped_column(JSON, nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deployment_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    deployment_attestation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
