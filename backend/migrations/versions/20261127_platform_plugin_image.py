"""Add Design fixed-image requests / 新增 Design 固定镜像请求。

Revision ID: platform_plugin_image_v1
Revises: 20260612_0051_context_tools
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "platform_plugin_image_v1"
down_revision = "20260612_0051_context_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_image_operations",
        sa.Column("request_id", sa.String(32), primary_key=True),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(18), nullable=False),
        sa.Column("plugin_name", sa.String(100), nullable=False),
        sa.Column("plugin_id", sa.Integer()),
        sa.Column("operator_id", sa.Integer(), nullable=False),
        sa.Column("target_version_id", sa.Integer()),
        sa.Column("build", sa.JSON(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("deployment_completed_at", sa.DateTime(timezone=True)),
        sa.Column("deployment_attestation", sa.JSON(none_as_null=True)),
        sa.CheckConstraint(
            "operation IN ('install','upgrade','rollback')", name="ck_piop_operation"
        ),
        sa.CheckConstraint(
            "phase IN ('requested','prepared','old_started','old_completed','apply_started','applied','uncertain','verified')",
            name="ck_piop_phase",
        ),
        sa.CheckConstraint(
            "(operation = 'install' OR plugin_id IS NOT NULL) "
            "AND (phase NOT IN ('applied','verified') OR plugin_id IS NOT NULL)",
            name="ck_piop_plugin_identity",
        ),
        sa.CheckConstraint(
            "(operation = 'rollback') = (target_version_id IS NOT NULL)",
            name="ck_piop_rollback_identity",
        ),
        sa.CheckConstraint(
            "(phase IN ('applied','verified')) = (applied_at IS NOT NULL)",
            name="ck_piop_applied",
        ),
        sa.CheckConstraint(
            "(phase = 'verified' AND deployment_completed_at IS NOT NULL "
            "AND deployment_attestation IS NOT NULL "
            "AND trim(CAST(deployment_attestation AS TEXT)) <> 'null') OR "
            "(phase <> 'verified' AND deployment_completed_at IS NULL AND deployment_attestation IS NULL)",
            name="ck_piop_attestation",
        ),
    )
    op.create_index(
        "uq_piop_open_plugin",
        "plugin_image_operations",
        ["plugin_name"],
        unique=True,
        postgresql_where=sa.text("phase <> 'verified'"),
        sqlite_where=sa.text("phase <> 'verified'"),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE plugin_image_operations IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM plugin_image_operations LIMIT 1")):
        raise RuntimeError("plugin_image_operation_history_present")
    op.drop_table("plugin_image_operations")
