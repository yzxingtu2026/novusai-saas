"""
Plugin version management. / 插件版本管理。

Provides version backup, upgrade, rollback, and history query functionality.
/ 提供版本备份、升级、回滚、历史查询功能。
"""

from __future__ import annotations

import contextlib
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.base_model import utc_now
from app.core.i18n import _
from app.core.logging import get_logger
from app.core.response import resolve_public_error_message, serialize_datetime_for_api
from app.enums.plugin import PluginStatusEnum, PluginVersionStatusEnum
from app.plugins.exceptions import PluginError, PluginNotFoundError
from app.plugins.loader import PLUGINS_DIR
from app.plugins.version_mutation_preflight import require_version_mutation_writable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

VERSIONS_DIR = PLUGINS_DIR / ".versions"
_SAFE_VERSION_COMPONENT_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?"
    r"(?:\+[A-Za-z0-9][A-Za-z0-9.-]*)?$"
)


def _validate_version_component(version: str) -> str:
    """Return a safe semantic-version directory component."""
    normalized = str(version or "").strip()
    if not _SAFE_VERSION_COMPONENT_RE.fullmatch(normalized) or ".." in normalized:
        raise PluginError(message="Invalid plugin version path component")
    return normalized


class VersionManager:
    """Plugin version manager / 插件版本管理器"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def _archive_before_mutation(self, plugin_name: str, version: str) -> None:
        try:
            self.archive_version(plugin_name, version)
        except OSError as error:
            raise PluginError(
                message=_("plugin.error.version_archive_failed"),
                extra={"reason": "plugin_version_archive_failed"},
            ) from error

    def archive_version(self, plugin_name: str, version: str) -> Path:
        """
        Archive current plugin version to .versions/{name}/{version}/
        / 备份插件当前版本

        Returns:
            Backup directory path / 备份目录路径
        """
        from app.plugins.table_ownership import get_plugin_owned_table_prefix

        get_plugin_owned_table_prefix(plugin_name)
        safe_version = _validate_version_component(version)
        source = PLUGINS_DIR / plugin_name
        if not source.is_dir():
            raise PluginNotFoundError(
                message=f"Plugin directory not found: {source}",
            )

        target = VERSIONS_DIR / plugin_name / safe_version
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        logger.info("Archived {} v{} to {}", plugin_name, version, target)
        return target

    async def upgrade(
        self,
        plugin_id: int,
        new_source: Path,
    ) -> None:
        """Upgrade plugin (full flow locked, avoid concurrent race conditions).
        / 升级插件（全流程加锁）"""
        from app.plugins.lifecycle import _plugin_lock

        async with _plugin_lock(plugin_id):
            await self._upgrade_unlocked(plugin_id, new_source)

    async def _upgrade_unlocked(
        self,
        plugin_id: int,
        new_source: Path,
    ) -> None:
        """
        Upgrade plugin: preflight → backup → disable old → replace → migrate → enable new.
        / 升级插件：预检 → 备份 → 禁用旧版 → 替换 → 迁移 → 启用新版

        Auto-rollback to old version on failure.
        / 失败时自动回滚。
        """
        from sqlalchemy import select

        from app.models.system.plugin import Plugin
        from app.models.system.plugin_version import PluginVersion
        from app.plugins.lifecycle import PluginLifecycle
        from app.plugins.loader import PluginLoader
        from app.plugins.module_loader import unload_plugin_modules

        result = await self._db.execute(
            select(Plugin).where(
                Plugin.id == plugin_id,
                Plugin.is_deleted.is_(False),
            )
        )
        plugin = result.scalar_one_or_none()
        if not plugin:
            raise PluginNotFoundError(message=f"Plugin ID {plugin_id} not found")

        plugin_name = plugin.name
        old_version = plugin.version
        lifecycle = PluginLifecycle(self._db)
        loader = PluginLoader()

        # Parse new version manifest / 解析新版本 manifest
        new_manifest = loader.load_manifest_from_path(new_source)
        new_version = new_manifest.version

        if new_manifest.name != plugin_name:
            raise PluginError(
                message=(
                    f"Upgrade package mismatch: expected plugin '{plugin_name}', "
                    f"but got '{new_manifest.name}'"
                ),
            )

        # Pre-upgrade security scan (high risk fail-close) / 升级前安全扫描
        from app.plugins.security_scan import assert_plugin_security_clean

        assert_plugin_security_clean(
            new_source,
            plugin_name=plugin_name,
            action="upgrade",
        )

        if new_version == old_version:
            raise PluginError(
                message=f"New version ({new_version}) is the same as current ({old_version})",
            )

        # 中文: 确定不可写或实际备份失败时，不能先禁用当前运行时或触发其钩子。
        # EN: Known write denial or an actual archive failure must precede disable and its hooks.
        target_dir = PLUGINS_DIR / plugin_name
        old_backup = (
            VERSIONS_DIR / plugin_name / _validate_version_component(old_version)
        )
        require_version_mutation_writable(
            target_dir, old_backup, python_requirements=new_manifest.dependencies.python
        )
        self._archive_before_mutation(plugin_name, old_version)

        # 中文: 外层 upgrade 已持有同插件锁，继续使用原禁用实现。
        # EN: The outer upgrade holds the plugin lock, so use the existing disable implementation.
        was_enabled = plugin.status == PluginStatusEnum.ENABLED.value
        if was_enabled:
            await lifecycle._disable_impl(plugin_id, preserve_plan_grants=True)

        # 3. Replace files / 替换文件
        try:
            shutil.rmtree(target_dir)
            shutil.copytree(new_source, target_dir)

            # Clear module cache, ensure subsequent on_upgrade/enable uses new code
            # / 清理模块缓存
            unload_plugin_modules(plugin_name)

            # 4. Install new version Python deps
            # pip must be installed before migration since migration scripts may import new deps
            # / 安装新版 Python 依赖
            if new_manifest.dependencies.python:
                await lifecycle._install_python_deps(
                    plugin_name, new_manifest.dependencies.python
                )

            # 5. Run migration (via lifecycle public interface) / 执行迁移
            await lifecycle.run_alembic_upgrade(plugin_name)

            # 6. Update DB record / 更新 DB 记录
            plugin.version = new_version
            plugin.manifest = new_manifest.model_dump()
            from app.plugins.preview import resolve_i18n

            plugin.display_name = resolve_i18n(new_manifest.display_name)
            new_py_deps = (
                getattr(getattr(new_manifest, "dependencies", None), "python", None)
                or []
            )
            plugin.installed_packages = new_py_deps

            # Archive old version / 旧版本归档
            from sqlalchemy import update

            await self._db.execute(
                update(PluginVersion)
                .where(
                    PluginVersion.plugin_id == plugin_id,
                    PluginVersion.status == PluginVersionStatusEnum.ACTIVE.value,
                )
                .values(status=PluginVersionStatusEnum.ARCHIVED.value)
            )

            # New version record / 新版本记录
            version_record = PluginVersion(
                plugin_id=plugin_id,
                version=new_version,
                manifest=new_manifest.model_dump(),
                status=PluginVersionStatusEnum.ACTIVE.value,
                installed_at=utc_now(),
            )
            self._db.add(version_record)
            await self._db.flush()

            # 7. Call on_upgrade / 调用 on_upgrade
            try:
                plugin_cls = loader.load_plugin_class(plugin_name)
                from app.plugins.context_factory import create_plugin_context

                ctx = create_plugin_context(
                    plugin_name=plugin_name,
                    manifest=new_manifest,
                    db=self._db,
                    granted_capabilities=plugin.granted_capabilities or [],
                )
                await plugin_cls().on_upgrade(ctx, old_version)
            except Exception as exc:
                logger.warning("on_upgrade failed for {}: {}", plugin_name, exc)

            # 8. Re-enable (if previously enabled, call _enable_impl to avoid nested locks)
            # / 重新启用
            if was_enabled:
                await lifecycle._enable_impl(plugin_id)

            logger.info(
                "Plugin {} upgraded: {} → {}", plugin_name, old_version, new_version
            )

        except Exception as exc:
            # Rollback: restore old version files / 回滚
            logger.error("Upgrade failed for {}, rolling back: {}", plugin_name, exc)
            if old_backup.exists():
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                shutil.copytree(old_backup, target_dir)
                unload_plugin_modules(plugin_name)
                plugin.version = old_version
                if was_enabled:
                    with contextlib.suppress(Exception):
                        await lifecycle._enable_impl(plugin_id)
            raise PluginError(
                message=resolve_public_error_message(
                    exc,
                    fallback_message=f"Upgrade failed for '{plugin_name}'",
                ),
            ) from exc

    async def rollback(self, plugin_id: int, target_version: str) -> None:
        """Rollback to specified version (full flow locked, avoid concurrent race conditions).
        / 回滚到指定版本（全流程加锁）"""
        from app.plugins.lifecycle import _plugin_lock

        async with _plugin_lock(plugin_id):
            await self._rollback_unlocked(plugin_id, target_version)

    async def _rollback_unlocked(self, plugin_id: int, target_version: str) -> None:
        """Rollback to specified version. / 回滚到指定版本。"""
        from sqlalchemy import select

        from app.models.system.plugin import Plugin
        from app.plugins.lifecycle import PluginLifecycle
        from app.plugins.module_loader import unload_plugin_modules

        result = await self._db.execute(
            select(Plugin).where(
                Plugin.id == plugin_id,
                Plugin.is_deleted.is_(False),
            )
        )
        plugin = result.scalar_one_or_none()
        if not plugin:
            raise PluginNotFoundError(message=f"Plugin ID {plugin_id} not found")

        plugin_name = plugin.name
        safe_target_version = _validate_version_component(target_version)
        versions_root = (VERSIONS_DIR / plugin_name).resolve()
        backup_path = versions_root / safe_target_version
        backup_dir = backup_path.resolve()
        if (
            not backup_dir.is_relative_to(versions_root)
            or backup_dir == versions_root
            or backup_path.is_symlink()
            or not backup_dir.is_dir()
        ):
            raise PluginNotFoundError(
                message=f"Version {target_version} backup not found for '{plugin_name}'",
            )

        # 中文: 先验证目标归档，再禁用运行时、备份或执行独立事务的迁移回退。
        # EN: Validate the target before runtime disable, backup or independently
        # committed migration downgrade can change the current installation.
        from app.plugins.loader import PluginLoader

        loader = PluginLoader()
        archived_manifest = loader.load_manifest_from_path(backup_dir)
        if (
            archived_manifest.name != plugin_name
            or archived_manifest.version != safe_target_version
        ):
            raise PluginError(
                message=(
                    "Rollback archive identity mismatch: expected "
                    f"'{plugin_name}' v{safe_target_version}"
                ),
            )

        lifecycle = PluginLifecycle(self._db)

        # 中文: 回退同样先验证可写性并完成当前源码备份，避免失败前改变运行状态。
        # EN: Rollback also checks writes and archives current code before runtime state changes.
        target_dir = PLUGINS_DIR / plugin_name
        current_archive = (
            VERSIONS_DIR / plugin_name / _validate_version_component(plugin.version)
        )
        require_version_mutation_writable(target_dir, current_archive)
        self._archive_before_mutation(plugin_name, plugin.version)

        # Disable current (call _disable_impl since outer rollback() already holds lock)
        # / 禁用当前
        was_enabled = plugin.status == PluginStatusEnum.ENABLED.value
        if was_enabled:
            await lifecycle._disable_impl(plugin_id, preserve_plan_grants=True)

        # 中文: 在替换文件前回滚 alembic 迁移，确保 Alembic 仍能读取当前版本迁移脚本。
        # EN: Downgrade before file replacement so Alembic can still read the
        # current version's migration scripts.
        try:
            await lifecycle.run_alembic_downgrade(plugin_name)
        except Exception as exc:
            logger.error(
                "Rollback {} blocked: alembic downgrade failed: {}",
                plugin_name,
                exc,
            )
            if was_enabled:
                with contextlib.suppress(Exception):
                    await lifecycle._enable_impl(plugin_id)
            raise PluginError(
                message=(
                    f"Rollback blocked for '{plugin_name}': alembic downgrade failed"
                ),
            ) from exc

        # 中文: 迁移仍读取当前文件，成功后才恢复已验证的目标版本。
        # EN: Keep current migration files until downgrade succeeds, then restore
        # the validated target version.
        shutil.rmtree(target_dir)
        shutil.copytree(backup_dir, target_dir)
        unload_plugin_modules(plugin_name)

        # Update DB / 更新 DB
        restored_manifest = loader.load_manifest(plugin_name)

        if (
            restored_manifest.name != plugin_name
            or restored_manifest.version != safe_target_version
        ):
            raise PluginError(
                message=(
                    "Rollback archive identity mismatch: expected "
                    f"'{plugin_name}' v{safe_target_version}"
                )
            )

        plugin.version = safe_target_version
        plugin.manifest = restored_manifest.model_dump()
        restored_py_deps = (
            getattr(getattr(restored_manifest, "dependencies", None), "python", None)
            or []
        )
        plugin.installed_packages = restored_py_deps

        from sqlalchemy import select, update

        from app.models.system.plugin_version import PluginVersion

        await self._db.execute(
            update(PluginVersion)
            .where(
                PluginVersion.plugin_id == plugin_id,
                PluginVersion.status == PluginVersionStatusEnum.ACTIVE.value,
            )
            .values(status=PluginVersionStatusEnum.ARCHIVED.value)
        )
        version_row = await self._db.execute(
            select(PluginVersion).where(
                PluginVersion.plugin_id == plugin_id,
                PluginVersion.version == safe_target_version,
                PluginVersion.is_deleted.is_(False),
            )
        )
        target_version_row = version_row.scalar_one_or_none()
        if target_version_row:
            target_version_row.status = PluginVersionStatusEnum.ACTIVE.value
            target_version_row.manifest = restored_manifest.model_dump()
            target_version_row.rolled_back_at = utc_now()
        else:
            self._db.add(
                PluginVersion(
                    plugin_id=plugin_id,
                    version=safe_target_version,
                    manifest=restored_manifest.model_dump(),
                    status=PluginVersionStatusEnum.ACTIVE.value,
                    installed_at=utc_now(),
                    rolled_back_at=utc_now(),
                )
            )
        await self._db.flush()

        # Re-enable (call _enable_impl to avoid nested locks) / 重新启用
        if was_enabled:
            await lifecycle._enable_impl(plugin_id)

        logger.info("Plugin {} rolled back to v{}", plugin_name, target_version)

    async def list_versions(self, plugin_id: int) -> list[dict]:
        """Query plugin version history / 查询插件版本历史"""
        from sqlalchemy import select

        from app.models.system.plugin_version import PluginVersion

        result = await self._db.execute(
            select(PluginVersion)
            .where(
                PluginVersion.plugin_id == plugin_id,
                PluginVersion.is_deleted.is_(False),
            )
            .order_by(PluginVersion.created_at.desc())
        )
        versions = result.scalars().all()
        return [
            {
                "id": v.id,
                "version": v.version,
                "status": v.status,
                "changelog": v.changelog,
                "installed_at": serialize_datetime_for_api(v.installed_at),
                "rolled_back_at": serialize_datetime_for_api(v.rolled_back_at),
            }
            for v in versions
        ]
