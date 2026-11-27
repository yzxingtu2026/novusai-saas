"""增加租户会话代际 / Add the tenant session epoch.

Revision ID: platform_epoch_v1
Revises: 20260612_0051_context_tools
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "platform_epoch_v1"
down_revision: str | Sequence[str] | None = "20260612_0051_context_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "session_revocation_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="租户会话生命周期版本 / Tenant session lifecycle epoch",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "session_revocation_epoch")
