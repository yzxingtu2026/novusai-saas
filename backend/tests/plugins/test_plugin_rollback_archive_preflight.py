"""中文: Test type: behavioral；BUG-2026-11-06-PLUGIN-ROLLBACK-PREFLIGHT。

EN: Test type: behavioral; BUG-2026-11-06-PLUGIN-ROLLBACK-PREFLIGHT.
中文: 使用真实清单解析、临时文件备份/替换；仅记录生命周期与数据库传输，禁止真实迁移。
EN: Real manifest parsing and temporary archive/replacement; lifecycle and DB
transport are recorders, so no real migration runs. Neither supplies manifest
decisions or filesystem outcomes asserted by these tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.enums.plugin import PluginStatusEnum
from app.plugins.exceptions import PluginError, PluginManifestError, PluginNotFoundError
from app.plugins.loader import PluginLoader
from app.plugins.version_manager import VersionManager

pytestmark = pytest.mark.behavioral


def _manifest(name: str = "demo-plugin", version: str = "1.0.0") -> str:
    return json.dumps(
        {
            "name": name,
            "version": version,
            "display_name": {"en": "Fixture"},
            "scope": "admin_only",
        }
    )


def _files(directory: Path) -> dict[str, bytes]:
    return {
        file.relative_to(directory).as_posix(): file.read_bytes()
        for file in directory.rglob("*")
        if file.is_file()
    }


@dataclass
class RollbackFixture:
    manager: VersionManager
    db: AsyncMock
    plugin: SimpleNamespace
    version_row: SimpleNamespace
    plugins: Path
    live: Path
    target: Path
    events: list[str]
    downgrade_fails: bool = False


@pytest.fixture
def rollback_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RollbackFixture:
    plugins = tmp_path / "plugins"
    live = plugins / "demo-plugin"
    target = plugins / ".versions/demo-plugin/1.0.0"
    live.mkdir(parents=True)
    target.mkdir(parents=True)
    (live / "plugin.yaml").write_text(_manifest(version="2.0.0"), encoding="utf-8")
    (live / "current.txt").write_text("current-live-data", encoding="utf-8")
    (live / "migration.py").write_text("current-migration-script", encoding="utf-8")
    (target / "plugin.yaml").write_text(_manifest(), encoding="utf-8")
    (target / "restored.txt").write_text("restored-version-data", encoding="utf-8")
    plugin = SimpleNamespace(
        id=77,
        name="demo-plugin",
        version="2.0.0",
        status=PluginStatusEnum.ENABLED.value,
        manifest={"current": True},
        installed_packages=["current-dependency"],
    )
    version_row = SimpleNamespace(status="archived", manifest={}, rolled_back_at=None)
    plugin_query, version_query = MagicMock(), MagicMock()
    plugin_query.scalar_one_or_none.return_value = plugin
    version_query.scalar_one_or_none.return_value = version_row
    db = AsyncMock()
    db.execute.side_effect = [plugin_query, MagicMock(), version_query]
    db.add = MagicMock()
    manager = VersionManager(db)
    state = RollbackFixture(manager, db, plugin, version_row, plugins, live, target, [])
    db.flush.side_effect = lambda: state.events.append("flush-version-records")
    monkeypatch.setattr("app.plugins.version_manager.PLUGINS_DIR", plugins)
    monkeypatch.setattr(
        "app.plugins.version_manager.VERSIONS_DIR", plugins / ".versions"
    )
    monkeypatch.setattr("app.plugins.loader.PLUGINS_DIR", plugins)
    real_archive = manager.archive_version

    def archive_current(name: str, version: str) -> Path:
        state.events.append("archive-current-files")
        return real_archive(name, version)

    monkeypatch.setattr(manager, "archive_version", archive_current)

    class RecorderLifecycle:
        def __init__(self, _db: object) -> None:
            pass

        async def _disable_impl(self, plugin_id: int, *, preserve_plan_grants: bool) -> None:
            assert preserve_plan_grants is True
            assert plugin_id == 77
            state.events.append("disable-current-runtime")

        async def run_alembic_downgrade(self, plugin_name: str) -> None:
            assert plugin_name == "demo-plugin"
            assert (live / "migration.py").read_text(
                encoding="utf-8"
            ) == "current-migration-script"
            assert (live / "current.txt").read_text(
                encoding="utf-8"
            ) == "current-live-data"
            assert not (live / "restored.txt").exists()
            state.events.append("downgrade-current-schema")
            if state.downgrade_fails:
                raise RuntimeError("recorded downgrade failure")

        async def _enable_impl(self, plugin_id: int) -> None:
            assert plugin_id == 77
            state.events.append("enable-runtime")

    monkeypatch.setattr("app.plugins.lifecycle.PluginLifecycle", RecorderLifecycle)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_target", "expected_error"),
    [
        ("yaml", PluginManifestError),
        ("schema", PluginManifestError),
        ("name", PluginError),
        ("version", PluginError),
        ("manifest-missing", PluginNotFoundError),
        ("archive-missing", PluginNotFoundError),
    ],
)
async def test_invalid_archive_is_rejected_before_any_lifecycle_or_archive_write(
    rollback_fixture: RollbackFixture,
    invalid_target: str,
    expected_error: type[PluginError],
) -> None:
    state = rollback_fixture
    manifest = state.target / "plugin.yaml"
    if invalid_target == "yaml":
        manifest.write_text("name: [\n", encoding="utf-8")
    elif invalid_target == "schema":
        manifest.write_text("name: demo-plugin\n", encoding="utf-8")
    elif invalid_target == "name":
        manifest.write_text(_manifest(name="other-plugin"), encoding="utf-8")
    elif invalid_target == "version":
        manifest.write_text(_manifest(version="0.9.0"), encoding="utf-8")
    else:
        manifest.unlink()
        if invalid_target == "archive-missing":
            (state.target / "restored.txt").unlink()
            state.target.rmdir()
    before = _files(state.plugins)

    with pytest.raises(expected_error):
        await state.manager._rollback_unlocked(77, "1.0.0")

    assert state.events == []
    assert _files(state.plugins) == before
    assert state.plugin.version == "2.0.0"
    assert state.plugin.manifest == {"current": True}
    assert state.plugin.installed_packages == ["current-dependency"]
    assert state.db.execute.await_count == 1
    assert state.db.flush.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_valid_archive_preserves_downgrade_before_replacement_order(
    rollback_fixture: RollbackFixture, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    state = rollback_fixture
    if not enabled:
        state.plugin.status = PluginStatusEnum.DISABLED.value
    before_live = _files(state.live)
    target_files = _files(state.target)

    def record_unload(name: str) -> None:
        assert name == "demo-plugin"
        assert _files(state.live) == target_files
        state.events.append("unload-after-restoration")

    monkeypatch.setattr(
        "app.plugins.module_loader.unload_plugin_modules", record_unload
    )
    await state.manager._rollback_unlocked(77, "1.0.0")

    expected = [
        "archive-current-files",
        "downgrade-current-schema",
        "unload-after-restoration",
        "flush-version-records",
    ]
    if enabled:
        expected = [
            expected[0],
            "disable-current-runtime",
            *expected[1:],
            "enable-runtime",
        ]
    assert state.events == expected
    assert _files(state.plugins / ".versions/demo-plugin/2.0.0") == before_live
    assert _files(state.live) == target_files
    restored = PluginLoader().load_manifest("demo-plugin")
    assert state.plugin.version == "1.0.0"
    assert state.plugin.manifest == restored.model_dump()
    assert state.plugin.installed_packages == []
    assert state.version_row.status == "active"
    assert state.version_row.manifest == restored.model_dump()
    assert state.version_row.rolled_back_at.tzinfo is None
    assert state.db.execute.await_count == 3
    assert state.db.flush.await_count == 1


@pytest.mark.asyncio
async def test_downgrade_failure_keeps_live_files_and_reenables_current_runtime(
    rollback_fixture: RollbackFixture,
) -> None:
    state = rollback_fixture
    state.downgrade_fails = True
    before_live = _files(state.live)

    with pytest.raises(PluginError, match="alembic downgrade failed"):
        await state.manager._rollback_unlocked(77, "1.0.0")

    assert state.events == [
        "archive-current-files",
        "disable-current-runtime",
        "downgrade-current-schema",
        "enable-runtime",
    ]
    assert _files(state.live) == before_live
    assert _files(state.plugins / ".versions/demo-plugin/2.0.0") == before_live
    assert state.plugin.version == "2.0.0"
    assert state.plugin.manifest == {"current": True}
    assert state.db.flush.await_count == 0
