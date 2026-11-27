"""Merge the shared platform migrations.

Revision ID: platform_shared_v1
Revises: platform_ai_batch_exec_v1, platform_plugin_image_v1,
    platform_role_unique_v1, platform_task_lease_v1
"""

revision = "platform_shared_v1"
down_revision = (
    "platform_ai_batch_exec_v1",
    "platform_plugin_image_v1",
    "platform_role_unique_v1",
    "platform_task_lease_v1",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
