"""Read and fence image delivery requests / 镜像交付请求读取和锁定。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import raiseload

from app.models.system.admin import Admin
from app.models.system.plugin import Plugin
from app.models.system.plugin_image_operation import PluginImageOperation
from app.models.system.plugin_version import PluginVersion


class PluginImageOperationRepository:
    def __init__(self, db):
        self.db = db

    async def get_actor(self, operator_id):
        return await self.db.scalar(
            select(Admin).where(
                Admin.id == operator_id,
                Admin.is_active.is_(True),
                Admin.is_deleted.is_(False),
            )
        )

    async def plugin(self, name, *, lock=False):
        query = select(Plugin).where(Plugin.name == name, Plugin.is_deleted.is_(False))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self.db.scalar(query.options(raiseload("*")))

    async def request(self, request_id, *, lock=False):
        query = select(PluginImageOperation).where(
            PluginImageOperation.request_id == request_id
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self.db.scalar(query)

    async def latest_request(self, operator_id, operation, *, plugin_name, plugin_id):
        plugin_filter = (
            PluginImageOperation.plugin_name == plugin_name
            if operation == "install"
            else PluginImageOperation.plugin_id == plugin_id
        )
        return await self.db.scalar(
            select(PluginImageOperation)
            .where(
                PluginImageOperation.operator_id == operator_id,
                PluginImageOperation.operation == operation,
                plugin_filter,
            )
            .order_by(
                PluginImageOperation.prepared_at.desc(),
                PluginImageOperation.request_id.desc(),
            )
            .limit(1)
        )

    async def add_request(self, fields):
        return await self.db.scalar(
            insert(PluginImageOperation)
            .values(**fields)
            .on_conflict_do_nothing()
            .returning(PluginImageOperation.request_id)
        )

    async def version(self, plugin_id, version_id):
        return await self.db.scalar(
            select(PluginVersion).where(
                PluginVersion.plugin_id == plugin_id,
                PluginVersion.id == version_id,
                PluginVersion.is_deleted.is_(False),
            )
        )

    async def active_version(self, plugin_id):
        rows = await self.db.scalars(
            select(PluginVersion)
            .where(
                PluginVersion.plugin_id == plugin_id,
                PluginVersion.status == "active",
                PluginVersion.is_deleted.is_(False),
            )
            .limit(2)
        )
        versions = rows.all()
        return versions[0] if len(versions) == 1 else None
