"""Behavioral: SaaS Admin and CLI with exact synthetic artifacts / 管理端和 CLI 合成制品。"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from alembic.migration import MigrationContext
from alembic.operations import Operations
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.api.admin.plugins import admin_plugin_controller
from app.cli_commands.entrypoint import cli
from app.core.deps import get_current_active_admin
from app.exceptions.base import AppException
from app.models.system.admin import Admin
from app.models.system.plugin import Plugin
from app.models.system.plugin_image_operation import PluginImageOperation
from app.models.system.plugin_version import PluginVersion
from app.plugins.loader import PluginLoader
from app.services.system.plugin_image_operation_artifacts import (
    ArtifactError,
    file_digest,
)
from app.services.system.plugin_image_operation_contract import (
    BuildContract,
    TreeEntry,
    canonical_sha,
    tree_sha,
)
from app.services.system.plugin_image_operation_service import (
    ImageOperationError,
    PluginImageOperationService,
)
from app.services.system.plugin_install_preview_service import (
    create_install_preview_token,
)
from tests.integration.storage_migration_test_support import (
    create_model_tables,
)
from tests.integration.storage_migration_test_support import (
    owned_storage as _owned_storage,
)

pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]
owned_storage = _owned_storage
NAME = "fixed-image-fixture"
REQUEST = "01" * 16


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


async def migrate(env, action):
    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    migration = versions / "20261127_platform_plugin_image.py"
    module = importlib.import_module(f"migrations.versions.{migration.stem}")

    def run(bind):
        with Operations.context(MigrationContext.configure(bind)):
            getattr(module, action)()

    async with env.engine.begin() as conn:
        await conn.run_sync(run)


def artifact(root, version):
    package = root / NAME
    package.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_dump(
        {
            "name": NAME,
            "version": version,
            "display_name": {"en": "Fictional fixture"},
            "scope": "admin_only",
        }
    ).encode()
    (package / "plugin.yaml").write_bytes(raw)
    manifest = PluginLoader().load_manifest_from_path(package).model_dump()
    entries = [
        TreeEntry(path="plugin.yaml", kind="file", size=len(raw), sha256=digest(raw))
    ]
    archive = root / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("package/plugin.yaml", raw)
    return package, archive, manifest, entries


async def setup(
    env, *, source_version, target_version, operation="upgrade", source_status="enabled"
):
    await create_model_tables(env, Admin, Plugin, PluginVersion)
    await migrate(env, "upgrade")
    source = (
        artifact(env.root / "installed", source_version) if source_version else None
    )
    target = artifact(env.root / "build", target_version)
    async with env.factory() as db:
        db.add_all(
            [
                Admin(
                    id=9,
                    username="nine",
                    email="9@fixture.invalid",
                    password_hash="fixture",
                    is_super=True,
                ),
                Admin(
                    id=8,
                    username="eight",
                    email="8@fixture.invalid",
                    password_hash="fixture",
                    is_super=True,
                ),
                Admin(
                    id=7,
                    username="seven",
                    email="7@fixture.invalid",
                    password_hash="fixture",
                    is_super=False,
                ),
            ]
        )
        if source:
            db.add(
                Plugin(
                    id=11,
                    name=NAME,
                    display_name=NAME,
                    version=source_version,
                    scope="admin_only",
                    status=source_status,
                    manifest=source[2],
                    config={"protected": "fixture-original"},
                )
            )
            await db.flush()
            db.add(
                PluginVersion(
                    id=12, plugin_id=11, version=source_version, manifest=source[2]
                )
            )
            if operation == "rollback":
                db.add(
                    PluginVersion(
                        id=13,
                        plugin_id=11,
                        version=target_version,
                        manifest=target[2],
                        status="archived",
                    )
                )
        await db.commit()
    build = BuildContract(
        schema="novusai-plugin-image-build/v1",
        request_id=REQUEST,
        operation=operation,
        operator_id=9,
        plugin_name=NAME,
        source_plugin_id=11 if source else None,
        target_plugin_version_id=13 if operation == "rollback" else None,
        source_version=source_version or "__uninstalled__",
        target_version=target_version,
        source_status=source_status if source else "not_installed",
        source_manifest_sha256=canonical_sha(source[2] if source else {}),
        target_manifest_sha256=canonical_sha(target[2]),
        archive_sha256=file_digest(target[1]),
        source_archive_sha256=file_digest(source[1]) if source else None,
        source_tree_sha256=tree_sha(source[3]) if source else "0" * 64,
        target_tree_sha256=tree_sha(target[3]),
        archive_members=target[3],
        source_archive_members=source[3] if source else [],
    )
    service = PluginImageOperationService(
        env.factory, plugins_dir=env.root / "installed"
    )
    return service, build, source, target


def json_file(root, name, payload):
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path, file_digest(path)


def host_evidence(root, build, database, *, bad_role=False):
    fake_config = json.dumps(
        {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "7" * 64]},
        }
    ).encode()
    fake_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "digest": "sha256:" + digest(fake_config),
                "size": len(fake_config),
                "mediaType": "application/vnd.oci.image.config.v1+json",
            },
            "layers": [
                {
                    "digest": "sha256:" + "9" * 64,
                    "size": 12,
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                }
            ],
        }
    ).encode()
    image = {
        "reference": "registry.invalid/customer/design-backend@sha256:"
        + digest(fake_manifest),
        "daemon_image_id": "sha256:" + digest(fake_config),
        "manifest_base64": base64.b64encode(fake_manifest).decode(),
        "config_base64": base64.b64encode(fake_config).decode(),
    }
    roles = ["backend", "worker", "beat"]
    images = {
        role: {
            **image,
            "reference": image["reference"].replace(
                "/design-backend@", f"/design-{role}@"
            ),
        }
        for role in roles
    }
    release, release_sha = json_file(
        root,
        "release.json",
        {
            "schema": "novusai-design-plugin-image-release/v1",
            "operation_id": "02" * 16,
            "build_contract": build.model_dump(),
            "required_roles": roles,
            "images": images,
            "observer_sha256": "c" * 64,
            "role_probes": {
                key: {"health_source": "readiness_probe", "sha256": "b" * 64}
                for key in roles
            },
        },
    )
    observation, observation_sha = json_file(
        root,
        "observation.json",
        {
            "schema": "novusai-plugin-image-observation/v1",
            "operation_id": "02" * 16,
            "release_contract_sha256": release_sha,
            "observer_sha256": "c" * 64,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "database": database,
            "roles": {
                key: {
                    "container_id": str(index) * 64,
                    "image_id": image["daemon_image_id"],
                    "image_reference": images[key]["reference"],
                    "state": "running",
                    "health": "healthy",
                    "health_source": "readiness_probe",
                    "probe_sha256": "b" * 64,
                    "probe_result": "passed",
                    "plugin_tree_sha256": "3" * 64
                    if bad_role and key == "worker"
                    else build.target_tree_sha256,
                    "plugin_manifest_sha256": build.target_manifest_sha256,
                }
                for index, key in enumerate(roles, 1)
            },
        },
    )
    return {
        "release_path": release,
        "release_sha": release_sha,
        "observation_path": observation,
        "observation_sha": observation_sha,
    }


async def test_admin_to_private_host_receipt_preserves_plugin_state(
    owned_storage, monkeypatch
):
    env = owned_storage
    service, build, source, target = await setup(
        env, source_version="1.0.0", target_version="2.0.0"
    )
    monkeypatch.setattr(
        "app.api.admin.plugin_image_operation_routes.PluginImageOperationService",
        lambda: service,
    )
    app = FastAPI()
    app.include_router(admin_plugin_controller.router, prefix="/admin")

    async def actor(request: Request):
        actor_id = int(request.headers.get("x-test-actor", "9"))
        request.state.user_permissions = (
            {"plugin:upgrade", "plugin:rollback"} if actor_id in {8, 9} else set()
        )
        return type("Actor", (), {"id": actor_id})()

    app.dependency_overrides[get_current_active_admin] = actor

    @app.exception_handler(AppException)
    async def errors(_request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        path = "/admin/plugins/image-upgrades"
        body = {"build": build.model_dump()}
        first = await client.post(path, json=body)
        assert first.status_code == 202, first.text
        assert first.json()["data"]["phase"] == "requested"
        assert (await client.post(path, json=body)).json() == first.json()
        assert (
            await client.get(f"{path}/{REQUEST}", headers={"x-test-actor": "8"})
        ).status_code == 404
        assert (
            await client.get(f"/admin/plugins/image-rollbacks/{REQUEST}")
        ).status_code == 404
        denied = build.model_copy(update={"request_id": "03" * 16, "operator_id": 7})
        assert (
            await client.post(
                path, json={"build": denied.model_dump()}, headers={"x-test-actor": "7"}
            )
        ).status_code == 403
        public = (await client.get(f"{path}/{REQUEST}")).json()["data"]
        assert public == first.json()["data"]
        assert "sha256" not in repr(public) and "fixture-original" not in repr(public)
        latest = "/admin/plugins/11/image-upgrades/latest"
        response = await client.get(latest)
        assert response.status_code == 200
        assert response.json()["data"] == public
        response = await client.get(latest, headers={"x-test-actor": "8"})
        assert response.status_code == 200
        assert response.json()["data"] is None
        assert (
            await client.get(latest, headers={"x-test-actor": "7"})
        ).status_code == 403
        assert (await client.get("/admin/plugins/12/image-upgrades/latest")).json()[
            "data"
        ] is None
        assert (await client.get("/admin/plugins/11/image-rollbacks/latest")).json()[
            "data"
        ] is None

    shell_archives = {"target_archive": target[1], "source_archive": source[1]}
    assert (
        await service.build_contract(REQUEST, 9, **shell_archives) == build.model_dump()
    )
    with pytest.raises(ArtifactError, match="archive_mismatch"):
        await service.build_contract(
            REQUEST, 9, target_archive=source[1], source_archive=source[1]
        )
    with pytest.raises(ImageOperationError) as failure:
        await service.transition(REQUEST, 9, "applied", **shell_archives)
    assert failure.value.reason == "plugin_image_phase_unavailable"
    assert (await service.transition(REQUEST, 9, "prepared", **shell_archives))[
        "phase"
    ] == "prepared"
    assert (await service.transition(REQUEST, 9, "old_started", **shell_archives))[
        "phase"
    ] == "old_started"
    with pytest.raises(ImageOperationError) as failure:
        await service.transition(REQUEST, 9, "old_completed", **shell_archives)
    assert failure.value.reason == "plugin_image_source_changed"
    async with env.factory() as db:
        plugin = await db.get(Plugin, 11)
        plugin.status = "disabled"
        await db.commit()
    assert (await service.transition(REQUEST, 9, "old_completed", **shell_archives))[
        "phase"
    ] == "old_completed"
    (source[0] / "plugin.yaml").write_bytes((target[0] / "plugin.yaml").read_bytes())
    assert (await service.transition(REQUEST, 9, "apply_started", **shell_archives))[
        "phase"
    ] == "apply_started"
    async with env.factory() as db:
        plugin = await db.get(Plugin, 11)
        plugin.status, plugin.version, plugin.manifest = "enabled", "2.0.0", target[2]
        old = await db.get(PluginVersion, 12)
        old.status = "archived"
        db.add(PluginVersion(id=14, plugin_id=11, version="2.0.0", manifest=target[2]))
        await db.commit()
    applied = await service.transition(REQUEST, 9, "applied", **shell_archives)
    assert (applied["phase"], applied["deployment_completed"]) == ("applied", False)
    database = await service.observe_database(REQUEST, 9)
    assert (database["plugin_id"], database["active_version_id"]) == (11, 14)
    with pytest.raises(ValueError, match="plugin_image_delivery_role_identity"):
        await service.record(
            REQUEST,
            9,
            **shell_archives,
            **host_evidence(env.root, build, database, bad_role=True),
        )
    assert await service.status(REQUEST, 9) == applied
    exact = host_evidence(env.root, build, database)
    verified = await service.record(REQUEST, 9, **shell_archives, **exact)
    assert (verified["phase"], verified["deployment_completed"]) == ("verified", True)
    assert await service.record(REQUEST, 9, **shell_archives, **exact) == verified
    async with env.factory() as db:
        assert (await db.get(Plugin, 11)).config == {"protected": "fixture-original"}
        assert await db.scalar(select(PluginImageOperation.request_id)) == REQUEST

    with pytest.raises(RuntimeError, match="plugin_image_operation_history_present"):
        await migrate(env, "downgrade")


async def test_rollback_checks_historical_id_and_recovery_is_terminal(owned_storage):
    env = owned_storage
    service, build, source, target = await setup(
        env,
        source_version="2.0.0",
        target_version="1.0.0",
        operation="rollback",
        source_status="disabled",
    )
    wrong = build.model_copy(update={"target_plugin_version_id": 12})
    with pytest.raises(ImageOperationError) as error:
        await service.prepare(wrong, 9)
    assert error.value.reason == "plugin_image_rollback_history_changed"
    assert (await service.prepare(build, 9))["target_plugin_version_id"] == 13
    assert (
        await service.latest_status(
            9, operation="rollback", plugin_id=build.source_plugin_id
        )
    )["request_id"] == build.request_id
    other = build.model_copy(update={"request_id": "04" * 16})
    with pytest.raises(ImageOperationError) as error:
        await service.prepare(other, 9)
    assert error.value.reason == "plugin_image_open_request_conflict"
    archives = {"target_archive": target[1], "source_archive": source[1]}
    await service.transition(REQUEST, 9, "prepared", **archives)
    await service.transition(REQUEST, 9, "old_started", **archives)
    assert (await service.transition(REQUEST, 9, "uncertain", **archives))[
        "phase"
    ] == "uncertain"
    with pytest.raises(ImageOperationError) as error:
        await service.transition(REQUEST, 9, "old_completed", **archives)
    assert error.value.reason == "plugin_image_phase_unavailable"
    assert (await service.status(REQUEST, 9))["phase"] == "uncertain"


async def test_install_uses_original_preview_and_records_no_plugin_until_observed(
    owned_storage,
):
    env = owned_storage
    service, build, _, target = await setup(
        env, source_version=None, target_version="1.0.0", operation="install"
    )
    for actor in (None, 8):
        token = create_install_preview_token(
            source="upload",
            plugin_name=NAME,
            version="1.0.0",
            package_sha256=build.archive_sha256,
            admin_id=actor,
        )
        with pytest.raises(ImageOperationError) as error:
            await service.prepare(build, 9, token)
        assert error.value.reason == "plugin_image_preview_invalid"
    token = create_install_preview_token(
        source="upload",
        plugin_name=NAME,
        version="1.0.0",
        package_sha256=build.archive_sha256,
        admin_id=9,
    )
    assert (await service.prepare(build, 9, token))["plugin_id"] is None
    assert (
        await service.latest_status(
            9, operation="install", plugin_name=build.plugin_name
        )
    )["request_id"] == build.request_id
    async with env.factory() as db:
        assert await db.scalar(select(Plugin.id)) is None
    with pytest.raises(ImageOperationError) as error:
        await service.transition(
            REQUEST, 9, "old_started", target_archive=target[1], source_archive=None
        )
    assert error.value.reason == "plugin_image_phase_unavailable"
    async with env.factory() as db:
        assert (
            await db.scalar(text("SELECT count(*) FROM plugin_image_operations")) == 1
        )


async def test_installed_cli_reads_actual_owned_request(owned_storage, monkeypatch):
    import anyio

    env = owned_storage
    service, build, _, _ = await setup(
        env, source_version=None, target_version="1.0.0", operation="install"
    )
    token = create_install_preview_token(
        source="upload",
        plugin_name=NAME,
        version="1.0.0",
        package_sha256=build.archive_sha256,
        admin_id=9,
    )
    expected = await service.prepare(build, 9, token)
    monkeypatch.setattr(
        "app.services.system.plugin_image_operation_service.PluginImageOperationService",
        lambda: service,
    )

    def invoke():
        return CliRunner().invoke(
            cli,
            [
                "plugin",
                "image-delivery",
                "status",
                "--request-id",
                REQUEST,
                "--operator-id",
                "9",
            ],
        )

    result = await anyio.to_thread.run_sync(invoke)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"success": True, "data": expected}
