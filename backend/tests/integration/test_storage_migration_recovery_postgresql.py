"""Behavioral: real DB recovery with temporary native files / 真实数据库与本机文件恢复。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.core.logging import get_logger
from app.models.tenant.attachment import Attachment
from app.plugins.context_primitives import PluginDbProxy
from app.plugins.loader import PluginLoader
from app.plugins.module_loader import load_plugin_module
from app.services.common.storage_config_resolver import (
    build_attachment_storage_snapshot,
)
from app.storage.base import StorageConfig
from app.storage.drivers.local import LocalStorageDriver
from app.storage.manager import storage_manager
from tests.integration.storage_migration_test_support import (
    create_model_tables,
)
from tests.integration.storage_migration_test_support import (
    owned_storage as _owned_storage,
)
from tests.integration.test_storage_migration_recovery_ownership_migration_postgresql import (
    _migration,
)

pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]
owned_storage = _owned_storage
KEY = "tenants/101/owned/migration.txt"
PAYLOAD = b"fictional storage migration source"
SECRET = "synthetic-forward-snapshot-options"
HAS_LOCAL_RECEIPTS = (
    hasattr(Attachment, "local_object_identity")
    and callable(getattr(LocalStorageDriver, "put_owned", None))
    and callable(getattr(LocalStorageDriver, "owned_metadata_path", None))
)
MIGRATIONS = (
    "001_init",
    "002_add_cleanup_and_meta_columns",
    "003_recovery_ownership",
    "004_rollback_journal",
    "005_forward_attempt",
    "006_forward_recovery",
)


class FixtureSource(LocalStorageDriver):
    name = "fixture-migration-source"


class LostCommitAck(AsyncSession):
    async def commit(self):
        await super().commit()
        if self.info.get("storage_forward_owner"):
            number = self.info.get("fixture_commits", 0) + 1
            self.info["fixture_commits"] = number
            if number == 3:
                raise ConnectionError("synthetic response lost after actual COMMIT")


def fixture_metadata_path(config):
    driver = LocalStorageDriver(config)
    if HAS_LOCAL_RECEIPTS:
        return driver.owned_metadata_path(KEY)
    return driver._meta_path(KEY)


async def setup(env, monkeypatch, *, lose_ack=False, legacy=False, owned_source=False):
    await create_model_tables(env, Attachment)
    plugin = PluginLoader().load_manifest("storage-migration")
    routes = {
        item.handler: item.permission for item in plugin.extensions.api.admin_routes
    }
    assert routes["api.handlers.create_migration_task"] == "storage_migration:create"
    assert (
        routes["api.handlers.rollback_migration_task"] == "storage_migration:rollback"
    )
    assert routes["api.handlers.cleanup_source_files"] == "storage_migration:cleanup"
    module = load_plugin_module("storage-migration", "services.migration_service")
    monkeypatch.setitem(storage_manager._drivers, FixtureSource.name, FixtureSource)
    monkeypatch.setitem(storage_manager._drivers, "local", LocalStorageDriver)
    source = StorageConfig(
        driver="local" if owned_source else FixtureSource.name,
        root_path=str(env.root / "source"),
        base_url=None,
        options={"fixture_secret": SECRET},
    )
    target = StorageConfig(
        driver=FixtureSource.name if owned_source else "local",
        root_path=str(env.root / "target"),
    )
    config = {item.driver: item for item in (source, target)}

    async def resolve(_self, _resolver, driver_name, _scope):
        return config[driver_name]

    monkeypatch.setattr(
        module.StorageMigrationService, "_resolve_driver_config", resolve
    )

    async def platform_config(_resolver):
        return target if owned_source else source

    monkeypatch.setattr(
        "app.services.common.storage_config_resolver.StorageConfigResolver.resolve_platform_config",
        platform_config,
    )
    source_driver = storage_manager.get_driver(source)
    source_identity = {}
    if owned_source:
        from app.storage.base import StorageVisibility
        from app.storage.local_upload_receipt import LocalUploadReceipt

        receipt = LocalUploadReceipt(source_driver.root)
        upload = await source_driver.put_owned(
            KEY,
            BytesIO(PAYLOAD),
            mime_type="text/plain",
            visibility=StorageVisibility.PUBLIC,
            metadata=None,
            receipt=receipt,
        )
        source_identity = {
            "local_object_identity": receipt.snapshot(source_driver._get_cache_root())
        }
    else:
        upload = await source_driver.put(KEY, BytesIO(PAYLOAD))
    meta = {
        "description": "leave as is",
        "_storage_snapshot": build_attachment_storage_snapshot(source, "platform"),
    }
    async with env.factory() as db:
        for name in MIGRATIONS[:2] if legacy else MIGRATIONS:
            await _migration(db, name, "upgrade")
        db.add(
            Attachment(
                id=10,
                tenant_id=101,
                name="migration.txt",
                path=KEY,
                size=upload.size,
                hash=upload.hash,
                driver=source.driver,
                base_url="",
                meta=meta,
                mime_type="image/png",
                visibility="public",
                **source_identity,
            )
        )
        await db.commit()
        service = module.StorageMigrationService(db)
        request = await service.create_task(
            source.driver, target.driver, "tenant:101", 1, 99
        )
        assert request == {
            "task_id": 1,
            "status": "pending",
            "total_files": 1,
            "total_bytes": len(PAYLOAD),
        }
        if legacy:
            for name in MIGRATIONS[2:]:
                await _migration(db, name, "upgrade")
        await db.commit()
    factory = async_sessionmaker(
        env.engine,
        class_=LostCommitAck if lose_ack else AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr("app.core.database.async_session_factory", factory)
    monkeypatch.setattr("app.core.database.async_engine", env.engine)
    return SimpleNamespace(
        service=module.StorageMigrationService,
        registry=module.runtime_registry,
        source=source,
        target=target,
        original_meta=meta,
    )


async def state(env):
    async with env.factory() as db:
        task = (
            await db.execute(
                text(
                    "SELECT status, migrated_files, failed_files, recovery_token FROM px_storage_migration_tasks"
                )
            )
        ).one()
        log = (
            await db.execute(
                text(
                    "SELECT status, rollback_state, forward_token, rollback_snapshot, xmin::text FROM px_storage_migration_logs"
                )
            )
        ).one()
        ledger = (
            await db.execute(
                text("SELECT token, state FROM px_storage_migration_attempts")
            )
        ).all()
        route = (
            await db.execute(
                text("SELECT driver,path,metadata,xmin::text FROM attachments")
            )
        ).one()
        return task, log, ledger, route


async def recovery_snapshot(env):
    async with env.factory() as db:
        task = await db.execute(
            text(
                "SELECT *, xmin::text AS row_version FROM px_storage_migration_tasks ORDER BY id"
            )
        )
        logs = await db.execute(
            text(
                "SELECT *, xmin::text AS row_version FROM px_storage_migration_logs ORDER BY id"
            )
        )
        attempts = await db.execute(
            text(
                "SELECT *, xmin::text AS row_version FROM px_storage_migration_attempts ORDER BY token"
            )
        )
        attachments = await db.execute(
            text("SELECT *, xmin::text AS row_version FROM attachments ORDER BY id")
        )
        return tuple(
            [dict(row) for row in result.mappings()]
            for result in (task, logs, attempts, attachments)
        )


async def enable_plugin(db):
    plugin = load_plugin_module("storage-migration", "main").StorageMigrationPlugin()
    await plugin.on_enable(
        SimpleNamespace(
            get_db=lambda: PluginDbProxy(db, "storage-migration"),
            get_logger=lambda: get_logger(__name__),
        )
    )


async def start(env, fx):
    async with env.factory() as db:
        assert await fx.service(db).start_task(1) == {"status": "running", "task_id": 1}
        task = fx.registry.pop_background_task(1)
        assert isinstance(task, asyncio.Task)
        await asyncio.wait_for(task, timeout=8)


async def _recovery_child() -> None:
    from app.core import database

    engine = create_async_engine(
        os.environ["STORAGE_TEST_DSN"],
        poolclass=NullPool,
        connect_args={
            "server_settings": {"search_path": os.environ["STORAGE_TEST_SCHEMA"]}
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    database.async_engine = engine
    database.async_session_factory = factory
    storage_manager._drivers[FixtureSource.name] = FixtureSource
    service = load_plugin_module("storage-migration", "services.migration_service")

    if os.environ["STORAGE_TEST_EXIT_AT"] == "before_claim":
        runner = load_plugin_module(
            "storage-migration", "services.migration_service_runner"
        )

        def exit_before_claim(_asyncio, coroutine):
            coroutine.close()
            os._exit(17)

        runner._create_background_task = exit_before_claim
    else:
        forward = load_plugin_module(
            "storage-migration", "services.migration_forward_attempt"
        )

        async def wait_after_upload(*_args, **_kwargs):
            print("fixture-uploaded", flush=True)
            await asyncio.Event().wait()

        forward.record_uploaded = wait_after_upload

    async with factory() as db:
        assert await service.StorageMigrationService(db).start_task(1) == {
            "status": "running",
            "task_id": 1,
        }
    await service.runtime_registry.pop_background_task(1)


async def launch_recovery_child(env, die_at):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-c",
        "import asyncio; from tests.integration.test_storage_migration_recovery_postgresql "
        "import _recovery_child; asyncio.run(_recovery_child())",
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "STORAGE_TEST_DSN": env.engine.url.render_as_string(hide_password=False),
            "STORAGE_TEST_SCHEMA": env.schema,
            "STORAGE_TEST_EXIT_AT": die_at,
            "NOVUSAI_CLI_DISABLE_FILE_LOGGING": "1",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def recovery_scan(env, monkeypatch):
    tasks = load_plugin_module("storage-migration", "tasks")
    monkeypatch.setattr(tasks, "task_async_session", env.factory)
    return tasks.recover_forward_runs


async def test_restart_before_claim_pauses_only_managed_work_and_can_resume(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    scan = recovery_scan(env, monkeypatch)
    child = await launch_recovery_child(env, "before_claim")
    try:
        _, stderr = await asyncio.wait_for(child.communicate(), timeout=25)
        assert child.returncode == 17, stderr.decode(errors="replace")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()

    tasks, logs, attempts, attachments = await recovery_snapshot(env)
    (task,) = tasks
    abandoned_run = task["forward_run_id"]
    assert len(abandoned_run) == 32
    assert (task["status"], task["recovery_token"]) == ("running", None)
    assert [row["status"] for row in logs] == ["pending"]
    assert attempts == []
    assert not (env.root / "target" / KEY).exists()
    assert await scan() == {"paused": 0, "uncertain": 0}

    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_tasks SET updated_at=now() - interval '31 seconds' WHERE id=1"
            )
        )
        await db.execute(
            text(
                "INSERT INTO px_storage_migration_tasks "
                "(id,source_driver,target_driver,status,scope,total_files,total_bytes,"
                "migrated_files,failed_files,skipped_files,migrated_bytes,concurrency,created_by,recovery_token) "
                "SELECT 2,source_driver,target_driver,'running','tenant:202',0,0,0,0,0,0,1,created_by,"
                "repeat('a', 32) FROM px_storage_migration_tasks WHERE id=1"
            )
        )
        await db.commit()
    legacy = (await recovery_snapshot(env))[0][1]

    assert await scan() == {"paused": 1, "uncertain": 0}
    assert await scan() == {"paused": 0, "uncertain": 0}
    (recovered, legacy_after), *other_rows = await recovery_snapshot(env)
    assert (
        recovered["status"],
        recovered["recovery_token"],
        recovered["forward_run_id"],
    ) == ("paused", None, None)
    assert legacy == legacy_after
    assert other_rows == [logs, attempts, attachments]

    async with env.factory() as db:
        service = fx.service(db)
        await service._run_migration(1, abandoned_run)
        assert (await service.get_task(1))["status"] == "paused"
        assert await service.resume_task(1) == {"status": "running", "task_id": 1}
        active = fx.registry.pop_background_task(1)
        assert isinstance(active, asyncio.Task)
        await asyncio.wait_for(active, timeout=10)
        completed = await service.get_task(1)
        assert completed["status"] == "completed"
        assert completed["migrated_files"] == 1
        assert "forward_run_id" not in completed
    assert (env.root / "target" / KEY).read_bytes() == PAYLOAD
    assert [row["state"] for row in (await recovery_snapshot(env))[2]] == ["succeeded"]


async def test_restart_after_upload_waits_for_owner_and_keeps_unconfirmed_receipt(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    scan = recovery_scan(env, monkeypatch)
    child = await launch_recovery_child(env, "uploaded")

    async def wait_for_upload():
        async for line in child.stdout:
            if line.strip() == b"fixture-uploaded":
                return
        stderr = await child.stderr.read()
        raise AssertionError(
            f"Child exited before the durable upload boundary\n"
            f"{stderr.decode(errors='replace')}"
        )

    try:
        await asyncio.wait_for(wait_for_upload(), timeout=25)
        before = await recovery_snapshot(env)
        assert before[0][0]["status"] == "running"
        assert before[0][0]["recovery_token"] == before[0][0]["forward_run_id"]
        assert [row["state"] for row in before[2]] == ["prepared"]
        assert before[1][0]["status"] == "transferring"
        assert await scan() == {"paused": 0, "uncertain": 0}
        assert await recovery_snapshot(env) == before
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()

    for _ in range(20):
        result = await scan()
        if result["uncertain"]:
            break
        await asyncio.sleep(0.05)
    assert result == {"paused": 0, "uncertain": 1}
    after = await recovery_snapshot(env)
    assert after[0][0]["status"] == "migration_uncertain"
    assert (after[0][0]["recovery_token"], after[0][0]["forward_run_id"]) == (
        None,
        None,
    )
    assert before[1:] == after[1:]
    assert await scan() == {"paused": 0, "uncertain": 0}
    assert (env.root / "source" / KEY).read_bytes() == PAYLOAD
    assert (env.root / "target" / KEY).read_bytes() == PAYLOAD
    async with env.factory() as db:
        assert (await fx.service(db).resume_task(1)) == {
            "error": "Cannot resume task in 'migration_uncertain' status"
        }


async def test_lost_business_ack_keeps_single_commit_and_preserved_objects(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch, lose_ack=True, legacy=True)
    await start(env, fx)
    task, log, ledger, route = await state(env)
    assert task == ("completed", 1, 0, None)
    assert log[:2] == ("success", "none")
    assert ledger == [(log.forward_token, "succeeded")]
    async with env.factory() as db:
        snapshot = await db.scalar(
            text("SELECT snapshot FROM px_storage_migration_attempts")
        )
    assert SECRET not in repr(snapshot)
    assert route[:2] == ("local", KEY)
    sidecar = fixture_metadata_path(fx.target)
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "mime_type": "image/png",
        "visibility": "public",
        "description": "leave as is",
    }
    assert (
        (env.root / "source" / KEY).read_bytes()
        == (env.root / "target" / KEY).read_bytes()
        == PAYLOAD
    )

    handler = load_plugin_module("storage-migration", "api.handlers")
    request = Request(
        {
            "type": "http",
            "path": "/plugins/storage-migration/api/admin/tasks/1",
            "path_params": {"task_id": "1"},
            "query_string": b"",
        }
    )
    async with env.factory() as db:
        body = await handler.get_migration_task(request, SimpleNamespace(_db=db))
    assert body["status"] == "completed"
    assert body["logs"]["items"][0]["status"] == "success"
    for private in ("forward_token", "forward_snapshot", "rollback_snapshot", SECRET):
        assert private not in repr(body)


async def test_rollback_durably_restores_exact_source_and_holds_unproven_target(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    if HAS_LOCAL_RECEIPTS:
        monkeypatch.delattr(LocalStorageDriver, "put_owned")
    await start(env, fx)
    async with env.factory() as db:
        held = await fx.service(db).rollback_task(1)
        assert held["reason"] == "storage_target_verification_unavailable"
    task, log, ledger, route = await state(env)
    assert task == ("rollback_failed", 1, 0, None)
    assert log.rollback_state == "source_restored"
    assert log.rollback_snapshot["target_origin_id"] is None
    assert ledger == [(log.forward_token, "succeeded")]
    assert route[:3] == (fx.source.driver, KEY, fx.original_meta)
    assert (
        (env.root / "source" / KEY).read_bytes()
        == (env.root / "target" / KEY).read_bytes()
        == PAYLOAD
    )

    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_tasks SET status='rolling_back', recovery_token='persisted-owner'"
            )
        )
        await db.commit()
        before_enable = await recovery_snapshot(env)
        await enable_plugin(db)
        await db.commit()
        service = fx.service(db)
        status = await service.get_task(1)
        assert (status["status"], status["rollback_can_resume"]) == (
            "rolling_back",
            False,
        )
        repeated = await service.rollback_task(1)
        assert "Cannot rollback task in 'rolling_back' status" in repeated["error"]
    assert await recovery_snapshot(env) == before_enable


async def test_enable_preserves_a_live_owner_and_does_not_start_another_worker(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    service_class = fx.service
    migrate_single_file = service_class._migrate_single_file
    admitted_log_ids: list[int] = []

    async def pause_before_storage_io(self, **kwargs):
        admitted_log_ids.append(kwargs["log_id"])
        assert admitted_log_ids == [1]
        entered.set()
        await release.wait()
        return await migrate_single_file(self, **kwargs)

    monkeypatch.setattr(service_class, "_migrate_single_file", pause_before_storage_io)

    async with env.factory() as db:
        assert await service_class(db).start_task(1) == {
            "status": "running",
            "task_id": 1,
        }
    run_id = fx.registry.get_background_run_id(1)
    assert run_id
    owner_task = fx.registry.pop_background_task(1)
    assert isinstance(owner_task, asyncio.Task)
    fx.registry.register_background_task(1, run_id, owner_task)

    try:
        await asyncio.wait_for(entered.wait(), timeout=8)
        async with env.factory() as recovery_db:
            await recovery_db.execute(
                text(
                    "INSERT INTO px_storage_migration_tasks "
                    "(id,source_driver,target_driver,status,scope,total_files,"
                    "migrated_files,failed_files,skipped_files,total_bytes,"
                    "migrated_bytes,concurrency,created_by,recovery_token) "
                    "VALUES (2,:source,:target,'rollback_interrupted','tenant:202',"
                    "0,0,0,0,0,0,1,99,:token)"
                ),
                {
                    "source": fx.source.driver,
                    "target": fx.target.driver,
                    "token": "d2" * 16,
                },
            )
            await recovery_db.commit()
        before = await recovery_snapshot(env)
        assert [row["status"] for row in before[0]] == [
            "running",
            "rollback_interrupted",
        ]
        assert len(before[0][0]["recovery_token"]) == 32
        assert before[0][1]["recovery_token"] == "d2" * 16
        assert [row["status"] for row in before[1]] == ["pending"]
        assert before[2] == []
        async with env.factory() as enable_db:
            await enable_plugin(enable_db)
            await enable_db.commit()
            assert await recovery_snapshot(env) == before
            duplicate = await service_class(enable_db).start_task(1)
            assert duplicate == {"error": "Cannot start task in 'running' status"}
            await asyncio.wait_for(
                service_class(enable_db)._run_migration(1, run_id), timeout=8
            )
            assert fx.registry.get_background_run_id(1) == run_id
        assert await recovery_snapshot(env) == before

        async with env.factory() as other_db:
            with monkeypatch.context() as other_process:
                other_process.setattr(
                    fx.registry, "get_background_run_id", lambda _: None
                )
                assert (await service_class(other_db).pause_task(1))["reason"] == (
                    "storage_recovery_conflict"
                )
        assert await recovery_snapshot(env) == before

        async with env.factory() as owner_db:
            assert await service_class(owner_db).pause_task(1) == {
                "status": "paused",
                "task_id": 1,
            }
        paused = await recovery_snapshot(env)
        assert paused[0][0]["recovery_token"] == before[0][0]["recovery_token"]
        async with env.factory() as other_db:
            with monkeypatch.context() as other_process:
                other_process.setattr(
                    fx.registry, "get_background_run_id", lambda _: None
                )
                assert (await service_class(other_db).resume_task(1))["reason"] == (
                    "storage_recovery_conflict"
                )
        assert await recovery_snapshot(env) == paused
        async with env.factory() as owner_db:
            assert await service_class(owner_db).resume_task(1) == {
                "status": "running",
                "task_id": 1,
            }
        assert not owner_task.done()
    finally:
        release.set()
        await asyncio.wait_for(owner_task, timeout=8)

    tasks, logs, attempts, attachments = await recovery_snapshot(env)
    task, held = tasks
    assert held == before[0][1]
    assert (
        task["status"],
        task["migrated_files"],
        task["failed_files"],
        task["recovery_token"],
    ) == ("completed", 1, 0, None)
    (log,) = logs
    (attempt,) = attempts
    (route,) = attachments
    assert log["status"] == "success"
    assert (attempt["token"], attempt["state"]) == (log["forward_token"], "succeeded")
    assert admitted_log_ids == [1]
    assert (route["driver"], route["path"]) == ("local", KEY)
    assert (env.root / "source" / KEY).read_bytes() == PAYLOAD
    assert (env.root / "target" / KEY).read_bytes() == PAYLOAD


@pytest.mark.parametrize("owner", ["persisted-owner", None])
async def test_enable_preserves_unknown_state_and_existing_target(
    owned_storage, monkeypatch, owner
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    existing = env.root / "target" / KEY
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing unrelated bytes")
    await start(env, fx)
    task, log, ledger, route = await state(env)
    assert task == ("failed", 0, 1, None)
    assert ledger == [(log.forward_token, "failed")]
    assert route[:3] == (fx.source.driver, KEY, fx.original_meta)
    assert existing.read_bytes() == b"existing unrelated bytes"

    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_tasks SET status='running', recovery_token=:owner"
            ),
            {"owner": owner},
        )
        await db.execute(
            text("UPDATE px_storage_migration_logs SET status='commit_unknown'")
        )
        await db.execute(
            text("UPDATE px_storage_migration_attempts SET state='unknown'")
        )
        await db.commit()
        before_enable = await recovery_snapshot(env)
        await enable_plugin(db)
        await db.commit()
        assert await recovery_snapshot(env) == before_enable
        service = fx.service(db)
        status = await service.get_task(1)
        assert status["status"] == "running"
        assert (
            "Cannot resume task in 'running' status"
            in (await service.resume_task(1))["error"]
        )
        assert (
            "Cannot retry task in 'running' status"
            in (await service.retry_failed(1))["error"]
        )
        assert (await service.cancel_task(1))["reason"] == (
            "storage_migration_outcome_unknown"
        )
        assert (
            "Cannot rollback task in 'running' status"
            in (await service.rollback_task(1))["error"]
        )
        await db.commit()
    assert await recovery_snapshot(env) == before_enable
    task, log, ledger, after = await state(env)
    assert task == ("running", 0, 1, owner)
    assert log.status == "commit_unknown"
    assert ledger == [(log.forward_token, "unknown")]
    assert after == route
    assert existing.read_bytes() == b"existing unrelated bytes"


async def test_uploaded_bytes_without_receipt_stay_held_without_replay(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    forward = load_plugin_module(
        "storage-migration", "services.migration_forward_attempt"
    )
    target = env.root / "target" / KEY
    source = env.root / "source" / KEY
    sidecar = fixture_metadata_path(fx.target)
    receipts = []

    async def lose_receipt(_db, attempt, **_kwargs):
        assert target.read_bytes() == PAYLOAD
        receipts.append(attempt.token)
        raise ConnectionError("synthetic failure before persisting the upload receipt")

    monkeypatch.setattr(forward, "record_uploaded", lose_receipt)
    await start(env, fx)

    before = await recovery_snapshot(env)
    task, log, ledger, route = await state(env)
    assert task[:3] == ("migration_uncertain", 0, 0)
    assert len(task.recovery_token) == 32
    assert log.status == "transferring"
    assert ledger == [(log.forward_token, "prepared")]
    assert route[:3] == (fx.source.driver, KEY, fx.original_meta)
    assert receipts == [log.forward_token]
    retained = (source.read_bytes(), target.read_bytes(), sidecar.read_bytes())
    assert retained[:2] == (PAYLOAD, PAYLOAD)

    async with env.factory() as db:
        await enable_plugin(db)
        await db.commit()
        service = fx.service(db)
        for operation, action in (
            (service.start_task, "start"),
            (service.resume_task, "resume"),
            (service.retry_failed, "retry"),
            (service.cancel_task, "cancel"),
            (service.rollback_task, "rollback"),
        ):
            assert await operation(1) == {
                "error": f"Cannot {action} task in 'migration_uncertain' status"
            }
        assert await service.cleanup_source_files(1) == {
            "error": "Can only cleanup source files for completed tasks"
        }
        await service._run_migration(1, task.recovery_token)
    assert await recovery_snapshot(env) == before
    assert receipts == [log.forward_token]
    assert (source.read_bytes(), target.read_bytes(), sidecar.read_bytes()) == retained


@pytest.mark.skipif(
    not HAS_LOCAL_RECEIPTS, reason="Requires the owned-local storage driver interface"
)
async def test_owned_target_rollback_resumes_after_delete_response_loss(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    await start(env, fx)
    target = env.root / "target" / KEY
    target_driver = LocalStorageDriver(fx.target)
    sidecar = target_driver.owned_metadata_path(KEY)
    legacy_sidecar = target_driver._meta_path(KEY)
    assert legacy_sidecar != sidecar and not legacy_sidecar.exists()
    legacy_metadata = b'{"description":"neighboring legacy object"}'
    legacy_sidecar.write_bytes(legacy_metadata)
    async with env.factory() as db:
        row = await db.get(Attachment, 10)
        proof = row.local_object_identity
        assert set(proof["files"]) == {
            KEY,
            sidecar.relative_to(env.root / "target").as_posix(),
        }
        assert proof["cacheNamespace"] == ".cache/images"
    runtime = load_plugin_module(
        "storage-migration", "services.migration_rollback_service"
    )
    real_remove = runtime.remove_exact_pair
    seen = []

    async def lost_reply(driver, path, identity):
        seen.append(await real_remove(driver, path, identity))
        raise ConnectionError("suppressed after actual temporary unlink")

    monkeypatch.setattr(runtime, "remove_exact_pair", lost_reply)
    async with env.factory() as db:
        held = await fx.service(db).rollback_task(1)
        assert (held["reason"], seen) == ("storage_delete_failed", ["deleted"])
    task, log, _, route = await state(env)
    assert (task.status, log.rollback_state, route.driver) == (
        "rollback_failed",
        "target_deleting",
        fx.source.driver,
    )
    assert not target.exists() and not sidecar.exists()
    assert (env.root / "source" / KEY).read_bytes() == PAYLOAD
    assert legacy_sidecar.read_bytes() == legacy_metadata
    monkeypatch.setattr(runtime, "remove_exact_pair", real_remove)
    async with env.factory() as db:
        result = await fx.service(db).rollback_task(1)
        assert result == {
            "status": "pending",
            "task_id": 1,
            "reverted_files": 1,
            "target_delete_errors": 0,
        }
        restored = await db.get(Attachment, 10)
        assert (restored.driver, restored.path, restored.local_object_identity) == (
            fx.source.driver,
            KEY,
            None,
        )
    task, log, _, after = await state(env)
    assert (task.status, log.rollback_state, after) == (
        "pending",
        "target_deleted",
        route,
    )
    assert (env.root / "source" / KEY).read_bytes() == PAYLOAD
    assert legacy_sidecar.read_bytes() == legacy_metadata


@pytest.mark.skipif(
    not HAS_LOCAL_RECEIPTS, reason="Requires the owned-local storage driver interface"
)
async def test_owned_rollback_retains_replaced_target_and_holds_cleanup(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch)
    await start(env, fx)
    target = env.root / "target" / KEY
    sidecar = LocalStorageDriver(fx.target).owned_metadata_path(KEY)
    replacement = b"new unrelated target object"
    target.write_bytes(replacement)
    kept = sidecar.read_bytes()
    async with env.factory() as db:
        assert (await fx.service(db).cleanup_source_files(1))[
            "reason"
        ] == "storage_origin_unverified"
        held = await fx.service(db).rollback_task(1)
    assert held["reason"] == "storage_target_object_changed"
    task, log, _, route = await state(env)
    assert (task.status, log.rollback_state, route.driver) == (
        "rollback_failed",
        "target_deleting",
        fx.source.driver,
    )
    assert target.read_bytes() == replacement and sidecar.read_bytes() == kept
    assert (env.root / "source" / KEY).read_bytes() == PAYLOAD


@pytest.mark.skipif(
    not HAS_LOCAL_RECEIPTS, reason="Requires the owned-local storage driver interface"
)
async def test_owned_source_cleanup_requires_current_retained_bytes(
    owned_storage, monkeypatch
):
    env = owned_storage
    fx = await setup(env, monkeypatch, owned_source=True)
    await start(env, fx)
    original = env.root / "source" / KEY
    target = env.root / "target" / KEY
    sidecar = LocalStorageDriver(fx.source).owned_metadata_path(KEY)
    target.write_bytes(b"incorrect bytes")
    async with env.factory() as db:
        held = await fx.service(db).cleanup_source_files(1)
        assert (
            held["deleted_files"],
            held["errors"],
            held["cleanup_completed_at"],
        ) == (0, 1, None)
    assert original.read_bytes() == PAYLOAD and sidecar.is_file()
    target.write_bytes(PAYLOAD)
    async with env.factory() as db:
        completed = await fx.service(db).cleanup_source_files(1)
        assert (
            completed["deleted_files"],
            completed["already_absent_files"],
            completed["errors"],
            completed["cleanup_completed_at"] is not None,
        ) == (1, 0, 0, True)
    assert not original.exists() and not sidecar.exists()
    assert target.read_bytes() == PAYLOAD
