"""存储迁移工具插件 / Storage Migration Tool Plugin"""

from app.plugins.base import PluginBase


class StorageMigrationPlugin(PluginBase):
    """跨驱动存储迁移工具 / Cross-driver storage migration tool"""

    async def on_install(self, ctx) -> None:
        ctx.get_logger().info("Storage migration plugin installed")

    async def on_enable(self, ctx) -> None:
        # 中文: 启用插件时，其他进程可能仍持有任务。保留原状态和占有记录。
        # EN: Another process may still own a task when the plugin is enabled.
        # Leave task state and ownership intact.
        ctx.get_logger().info("Storage migration plugin enabled")

    async def on_disable(self, ctx) -> None:
        ctx.get_logger().info("Storage migration plugin disabled")

    async def on_uninstall(self, ctx) -> None:
        ctx.get_logger().info("Storage migration plugin uninstalled")
