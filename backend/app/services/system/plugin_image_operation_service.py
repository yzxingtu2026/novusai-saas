"""Fixed-image state and evidence, separate from execution / 固定镜像状态与证据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio

from app.plugins.exceptions import PluginError
from app.plugins.frontend_contract import validate_runtime_frontend_contract
from app.plugins.license import assert_plugin_license_active
from app.plugins.loader import PLUGINS_DIR, PluginLoader
from app.plugins.security_scan import assert_plugin_security_clean
from app.rbac.services.permission_service import PermissionService
from app.repositories.system.plugin_image_operation_repository import (
    PluginImageOperationRepository,
)
from app.services.system.plugin_image_operation_artifacts import (
    archive_identity,
    private_json,
    tree_digest,
)
from app.services.system.plugin_image_operation_contract import (
    BuildContract,
    DatabaseObservation,
    DeploymentAttestation,
    HostObservation,
    ReleaseContract,
    canonical_sha,
    deployment_is_verified,
    timestamp,
)
from app.services.system.plugin_install_preview_service import (
    assert_install_preview_token,
    decode_install_preview_token,
)


class ImageOperationError(PluginError):
    def __init__(self, reason, *, status=409):
        self.reason = reason
        super().__init__(status_code=status, extra={"reason": reason})


def _build(row):
    build = BuildContract.model_validate(row.build)
    if (
        build.request_id != row.request_id
        or build.operation != row.operation
        or build.operator_id != row.operator_id
        or build.plugin_name != row.plugin_name
        or build.target_plugin_version_id != row.target_version_id
        or (build.operation != "install" and build.source_plugin_id != row.plugin_id)
    ):
        raise ImageOperationError("plugin_image_identity_changed")
    return build


def _receipt(row):
    build = _build(row)
    return {
        "request_id": build.request_id,
        "operation": build.operation,
        "plugin_name": build.plugin_name,
        "plugin_id": row.plugin_id,
        "source_version": build.source_version,
        "target_version": build.target_version,
        "target_plugin_version_id": row.target_version_id,
        "phase": row.phase,
        "deployment_completed": deployment_is_verified(row),
    }


async def _require_actor(repo, operator_id, operation):
    actor = await repo.get_actor(operator_id)
    if actor is None:
        raise ImageOperationError("plugin_image_forbidden", status=403)
    permission_service = PermissionService(repo.db)
    permissions = await permission_service.get_admin_permissions(actor)
    if not permission_service.check_permission(permissions, f"plugin:{operation}"):
        raise ImageOperationError("plugin_image_forbidden", status=403)


class PluginImageOperationService:
    def __init__(self, session_factory=None, *, plugins_dir: Path = PLUGINS_DIR):
        if session_factory is None:
            from app.core.database import async_session_factory

            session_factory = async_session_factory
        self.sessions = session_factory
        self.plugins_dir = plugins_dir

    async def _request(
        self, repo, request_id, operator_id, *, operation=None, lock=False
    ):
        row = await repo.request(request_id)
        if (
            row is None
            or row.operator_id != operator_id
            or (operation is not None and row.operation != operation)
        ):
            raise ImageOperationError("plugin_image_request_unavailable", status=404)
        await _require_actor(repo, operator_id, row.operation)
        if lock:
            # 与正常版本更新同顺序先锁插件行 / Lock the plugin first, as version changes do.
            plugin = await repo.plugin(row.plugin_name, lock=True)
            row = await repo.request(request_id, lock=True)
        else:
            plugin = None
        return row, _build(row), plugin

    async def status(self, request_id, operator_id, *, operation=None):
        async with self.sessions() as db:
            row, _, _ = await self._request(
                PluginImageOperationRepository(db),
                request_id,
                operator_id,
                operation=operation,
            )
            return _receipt(row)

    async def latest_status(
        self, operator_id, *, operation, plugin_name=None, plugin_id=None
    ):
        async with self.sessions() as db:
            repo = PluginImageOperationRepository(db)
            await _require_actor(repo, operator_id, operation)
            row = await repo.latest_request(
                operator_id, operation, plugin_name=plugin_name, plugin_id=plugin_id
            )
            return _receipt(row) if row is not None else None

    async def prepare(
        self, build: BuildContract, operator_id: int, preview_token: str = ""
    ):
        if build.operator_id != operator_id:
            raise ImageOperationError("plugin_image_forbidden", status=403)
        async with self.sessions() as db:
            repo = PluginImageOperationRepository(db)
            await _require_actor(repo, operator_id, build.operation)
            plugin = await repo.plugin(build.plugin_name, lock=True)
            existing = await repo.request(build.request_id, lock=True)
            if existing is not None:
                return self._prepared_receipt(existing, build)
            await self._require_source(repo, plugin, build, build.source_status)
            if build.operation == "install":
                claims = decode_install_preview_token(preview_token)
                if claims.get("admin_id") != operator_id or claims.get(
                    "source"
                ) not in {
                    "upload",
                    "marketplace",
                }:
                    raise ImageOperationError(
                        "plugin_image_preview_invalid", status=403
                    )
                assert_install_preview_token(
                    claims,
                    source=claims["source"],
                    plugin_name=build.plugin_name,
                    version=build.target_version,
                    package_sha256=build.archive_sha256,
                    admin_id=operator_id,
                )
            if build.operation == "rollback":
                await self._require_rollback_history(repo, plugin, build)

            inserted = await repo.add_request(
                {
                    "request_id": build.request_id,
                    "operation": build.operation,
                    "phase": "requested",
                    "plugin_name": build.plugin_name,
                    "plugin_id": build.source_plugin_id,
                    "operator_id": operator_id,
                    "target_version_id": build.target_plugin_version_id,
                    "build": build.model_dump(),
                    "prepared_at": datetime.now(timezone.utc),
                }
            )
            existing = await repo.request(build.request_id)
            if existing is None:
                raise ImageOperationError("plugin_image_open_request_conflict")
            receipt = self._prepared_receipt(existing, build)
            if inserted:
                await db.commit()
            return receipt

    @staticmethod
    def _prepared_receipt(row, build):
        if _build(row) != build:
            raise ImageOperationError("plugin_image_request_conflict")
        return _receipt(row)

    @staticmethod
    async def _require_rollback_history(repo, plugin, build):
        history = await repo.version(plugin.id, build.target_plugin_version_id)
        if (
            history is None
            or history.status == "active"
            or history.version != build.target_version
            or canonical_sha(history.manifest) != build.target_manifest_sha256
        ):
            raise ImageOperationError("plugin_image_rollback_history_changed")

    async def _require_tree(self, build, *, target=False):
        root = self.plugins_dir / build.plugin_name
        expected = build.target_tree_sha256 if target else build.source_tree_sha256
        measured = await anyio.to_thread.run_sync(tree_digest, root)
        if measured != expected:
            raise ImageOperationError("plugin_image_installed_tree_changed")
        if target:
            manifest = PluginLoader(self.plugins_dir).load_manifest_from_path(root)
            if canonical_sha(manifest.model_dump()) != build.target_manifest_sha256:
                raise ImageOperationError("plugin_image_target_manifest_changed")
            validate_runtime_frontend_contract(root, manifest)
            assert_plugin_security_clean(
                root, plugin_name=build.plugin_name, action=build.operation
            )

    async def _require_source(self, repo, plugin, build, status, *, verify_tree=True):
        if build.operation == "install":
            if plugin is not None:
                raise ImageOperationError("plugin_image_install_source_exists")
            return
        if (
            plugin is None
            or plugin.id != build.source_plugin_id
            or plugin.status != status
            or plugin.version != build.source_version
            or canonical_sha(plugin.manifest) != build.source_manifest_sha256
        ):
            raise ImageOperationError("plugin_image_source_changed")
        if verify_tree:
            await self._require_tree(build)
        await assert_plugin_license_active(
            plugin.id,
            plugin.pricing_type,
            repo.db,
            plugin_name=plugin.name,
            operation=build.operation,
        )

    @staticmethod
    async def _archives(build, target_archive, source_archive):
        observed = await anyio.to_thread.run_sync(
            archive_identity, target_archive, build.archive_sha256
        )
        if observed != (
            build.plugin_name,
            build.target_version,
            build.target_manifest_sha256,
            build.target_tree_sha256,
        ):
            raise ImageOperationError("plugin_image_target_archive_changed")
        if build.operation == "install":
            if source_archive is not None:
                raise ImageOperationError("plugin_image_install_source_archive")
            return
        if source_archive is None:
            raise ImageOperationError("plugin_image_source_archive_required")
        observed = await anyio.to_thread.run_sync(
            archive_identity, source_archive, build.source_archive_sha256
        )
        if observed != (
            build.plugin_name,
            build.source_version,
            build.source_manifest_sha256,
            build.source_tree_sha256,
        ):
            raise ImageOperationError("plugin_image_source_archive_changed")

    async def build_contract(
        self, request_id, operator_id, *, target_archive, source_archive
    ):
        async with self.sessions() as db:
            row, build, _ = await self._request(
                PluginImageOperationRepository(db), request_id, operator_id
            )
            if row.phase == "uncertain":
                raise ImageOperationError("plugin_image_phase_unavailable")
            await self._archives(build, target_archive, source_archive)
            return build.model_dump()

    @staticmethod
    def _quiesced_status(build):
        return "disabled" if build.source_status == "enabled" else build.source_status

    async def _target(self, repo, row, build, plugin):
        expected_status = (
            "installed" if build.operation == "install" else build.source_status
        )
        if (
            plugin is None
            or plugin.status != expected_status
            or plugin.version != build.target_version
            or (row.plugin_id is not None and row.plugin_id != plugin.id)
            or canonical_sha(plugin.manifest) != build.target_manifest_sha256
        ):
            raise ImageOperationError("plugin_image_target_changed")
        active = await repo.active_version(plugin.id)
        if (
            active is None
            or active.version != plugin.version
            or active.manifest != plugin.manifest
            or (
                row.target_version_id is not None and active.id != row.target_version_id
            )
        ):
            raise ImageOperationError("plugin_image_history_changed")
        await assert_plugin_license_active(
            plugin.id,
            plugin.pricing_type,
            repo.db,
            plugin_name=plugin.name,
            operation=build.operation,
        )
        await self._require_tree(build, target=True)
        return DatabaseObservation(
            request_id=row.request_id,
            phase="applied",
            plugin_id=plugin.id,
            plugin_name=plugin.name,
            version=plugin.version,
            status=plugin.status,
            manifest_sha256=canonical_sha(plugin.manifest),
            active_version_id=active.id,
        )

    async def transition(
        self, request_id, operator_id, phase, *, target_archive, source_archive
    ):
        async with self.sessions() as db:
            repo = PluginImageOperationRepository(db)
            row, build, plugin = await self._request(
                repo, request_id, operator_id, lock=True
            )
            if row.phase == phase:
                return _receipt(row)
            previous = {
                "prepared": "requested",
                "old_started": "prepared",
                "old_completed": "old_started",
                "apply_started": "prepared"
                if build.operation == "install"
                else "old_completed",
                "applied": "apply_started",
            }
            if phase == "uncertain" and row.phase in {"old_started", "apply_started"}:
                row.phase = phase
            elif (
                phase not in previous
                or row.phase != previous[phase]
                or (build.operation == "install" and phase.startswith("old_"))
            ):
                raise ImageOperationError("plugin_image_phase_unavailable")
            else:
                await self._archives(build, target_archive, source_archive)
                if phase == "applied":
                    actual = await self._target(repo, row, build, plugin)
                    row.plugin_id = actual.plugin_id
                    row.applied_at = datetime.now(timezone.utc)
                else:
                    status = (
                        build.source_status
                        if phase in {"prepared", "old_started"}
                        else self._quiesced_status(build)
                    )
                    await self._require_source(
                        repo,
                        plugin,
                        build,
                        status,
                        verify_tree=phase != "apply_started",
                    )
                    if build.operation == "rollback":
                        await self._require_rollback_history(repo, plugin, build)
                    if phase == "apply_started":
                        await self._require_tree(build, target=True)
                row.phase = phase
            receipt = _receipt(row)
            await db.commit()
            return receipt

    async def observe_database(self, request_id, operator_id):
        async with self.sessions() as db:
            repo = PluginImageOperationRepository(db)
            row, build, _ = await self._request(repo, request_id, operator_id)
            if row.phase not in {"applied", "verified"}:
                raise ImageOperationError("plugin_image_phase_unavailable")
            plugin = await repo.plugin(build.plugin_name)
            return (await self._target(repo, row, build, plugin)).model_dump()

    async def record(
        self,
        request_id,
        operator_id,
        *,
        release_path,
        release_sha,
        observation_path,
        observation_sha,
        target_archive,
        source_archive,
    ):
        release = ReleaseContract.model_validate(
            await anyio.to_thread.run_sync(private_json, release_path, release_sha)
        )
        observation = HostObservation.model_validate(
            await anyio.to_thread.run_sync(
                private_json, observation_path, observation_sha
            )
        )
        async with self.sessions() as db:
            repo = PluginImageOperationRepository(db)
            row, build, plugin = await self._request(
                repo, request_id, operator_id, lock=True
            )
            if release.build_contract != build:
                raise ImageOperationError("plugin_image_build_changed")
            if row.phase == "verified":
                previous = DeploymentAttestation.model_validate(
                    row.deployment_attestation
                )
                if (
                    not deployment_is_verified(row)
                    or previous.release_contract_sha256 != release_sha
                    or previous.observation_sha256 != observation_sha
                ):
                    raise ImageOperationError("plugin_image_observation_conflict")
                return _receipt(row)
            if row.phase != "applied":
                raise ImageOperationError("plugin_image_phase_unavailable")
            await self._archives(build, target_archive, source_archive)
            actual = await self._target(repo, row, build, plugin)
            if observation.database != actual:
                raise ImageOperationError("plugin_image_observation_database_changed")
            now = datetime.now(timezone.utc)
            measured_at = timestamp(observation.observed_at)
            if (
                not max(row.applied_at, now - timedelta(minutes=5))
                <= measured_at
                <= now
            ):
                raise ImageOperationError("plugin_image_observation_stale")
            attestation = DeploymentAttestation(
                schema="novusai-plugin-image-attestation/v1",
                trust_boundary="trusted-host-observer-file-v1",
                release_contract_sha256=release_sha,
                release_contract_canonical_sha256=canonical_sha(release.model_dump()),
                observation_sha256=observation_sha,
                observation_canonical_sha256=canonical_sha(observation.model_dump()),
                release=release,
                observation=observation,
                completed_at=now.isoformat(),
            )
            row.phase = "verified"
            row.deployment_attestation = attestation.model_dump()
            row.deployment_completed_at = now
            receipt = _receipt(row)
            await db.commit()
            return receipt
