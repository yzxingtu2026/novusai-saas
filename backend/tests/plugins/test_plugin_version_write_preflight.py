"""中文: behavioral；真实文件/清单/扫描/备份，生命周期和 DB 仅记录副作用。

EN: Behavioral filesystem regressions. Lifecycle and DB record side effects;
they do not supply the filesystem or manifest decisions under test. POSIX
permission cases explicitly skip where the current identity bypasses them.
"""

from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.enums.plugin import PluginStatusEnum
from app.plugins.exceptions import PluginError
from app.plugins.loader import PluginLoader
from app.plugins.version_manager import VersionManager


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _package(root: Path, version: str, dependencies: list[str] | None = None) -> None:
    (root / "backend").mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        json.dumps(
            {
                "name": "demo-plugin",
                "version": version,
                "display_name": {"en": "Fictional write preflight"},
                "scope": "admin_only",
                "dependencies": {"python": dependencies or []},
            }
        ),
        encoding="utf-8",
    )
    (root / "backend/main.py").write_text(
        "from app.plugins.base import PluginBase\n"
        "class FictionalPlugin(PluginBase):\n"
        "    async def on_upgrade(self, ctx, old_version):\n"
        "        ctx.record(old_version)\n",
        encoding="utf-8",
    )
    (root / "version.txt").write_text(version, encoding="utf-8")


@dataclass
class VersionFixture:
    manager: VersionManager
    plugin: SimpleNamespace
    db: AsyncMock
    plugins: Path
    live: Path
    source: Path
    archived: Path
    events: list[str]


@pytest.fixture
def version_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VersionFixture:
    plugins = tmp_path / "plugins"
    live = plugins / "demo-plugin"
    source = tmp_path / "incoming/demo-plugin"
    archived = plugins / ".versions/demo-plugin/1.0.0"
    _package(live, "2.0.0")
    _package(source, "3.0.0")
    _package(archived, "1.0.0")
    plugin = SimpleNamespace(
        id=77,
        name="demo-plugin",
        version="2.0.0",
        status=PluginStatusEnum.ENABLED.value,
        manifest={"current": True},
        installed_packages=[],
        granted_capabilities=[],
        display_name="Current",
    )
    query = MagicMock()
    query.scalar_one_or_none.return_value = plugin
    db = AsyncMock()
    db.execute.return_value = query
    db.add = MagicMock()
    events: list[str] = []
    db.flush.side_effect = lambda: events.append("flush")
    state = VersionFixture(
        VersionManager(db), plugin, db, plugins, live, source, archived, events
    )
    monkeypatch.setattr("app.plugins.version_manager.PLUGINS_DIR", plugins)
    monkeypatch.setattr(
        "app.plugins.version_manager.VERSIONS_DIR", plugins / ".versions"
    )
    monkeypatch.setattr("app.plugins.loader.PLUGINS_DIR", plugins)

    class RecorderLifecycle:
        def __init__(self, _db):
            pass

        async def _disable_impl(self, plugin_id, *, preserve_plan_grants: bool) -> None:
            assert preserve_plan_grants is True
            assert plugin_id == 77
            events.append("disable")
            plugin.status = PluginStatusEnum.DISABLED.value

        async def _enable_impl(self, plugin_id):
            assert plugin_id == 77
            events.append("enable")
            plugin.status = PluginStatusEnum.ENABLED.value

        async def run_alembic_upgrade(self, name):
            assert name == "demo-plugin"
            events.append("upgrade-schema")

        async def run_alembic_downgrade(self, name):
            assert name == "demo-plugin"
            events.append("downgrade-schema")

        async def _install_python_deps(self, _name, _requirements):
            pytest.fail("No dependency installation is authorized in these fixtures")

    monkeypatch.setattr("app.plugins.lifecycle.PluginLifecycle", RecorderLifecycle)
    monkeypatch.setattr(
        "app.plugins.context_factory.create_plugin_context",
        lambda **_: SimpleNamespace(
            record=lambda old: events.append("upgrade-hook:" + old)
        ),
    )
    yield state
    from app.plugins.module_loader import unload_plugin_modules

    unload_plugin_modules("demo-plugin")


async def _mutate(state: VersionFixture, operation: str) -> None:
    if operation == "upgrade":
        await state.manager._upgrade_unlocked(77, state.source)
    else:
        await state.manager._rollback_unlocked(77, "1.0.0")


@pytest.mark.behavioral
@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
async def test_actual_archive_path_failure_keeps_current_runtime_and_bytes(
    version_fixture: VersionFixture, operation: str
) -> None:
    state = version_fixture
    current_archive = state.plugins / ".versions/demo-plugin/2.0.0"
    current_archive.write_bytes(b"FICTIONAL-PATH-COLLISION-MUST-REMAIN")
    before_files, before_row = _files(state.plugins), deepcopy(vars(state.plugin))

    with pytest.raises(PluginError) as error:
        await _mutate(state, operation)

    assert error.value.extra["reason"] == "plugin_version_files_unwritable"
    assert vars(state.plugin) == before_row
    assert _files(state.plugins) == before_files
    assert state.events == []
    assert state.db.execute.await_count == 1
    state.db.add.assert_not_called()


@pytest.mark.behavioral
@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
@pytest.mark.parametrize("location", ["plugins", "nested", "archive"])
async def test_real_posix_write_denial_precedes_disable_and_hooks(
    version_fixture: VersionFixture, operation: str, location: str
) -> None:
    if os.name != "posix":
        pytest.skip("Real POSIX directory permissions require a non-root Linux fixture")
    state = version_fixture
    directory = {
        "plugins": state.plugins,
        "nested": state.live / "backend",
        "archive": state.archived.parent,
    }[location]
    previous_mode = directory.stat().st_mode
    directory.chmod(0o555)
    try:
        if os.access(directory, os.W_OK):
            pytest.skip("Current identity bypasses POSIX write denial")
        with pytest.raises(PermissionError):
            (directory / "must-not-create").open("xb")
        before_files, before_row = _files(state.plugins), deepcopy(vars(state.plugin))
        with pytest.raises(PluginError) as error:
            await _mutate(state, operation)
        assert error.value.extra["reason"] == "plugin_version_files_unwritable"
        assert state.events == []
        assert vars(state.plugin) == before_row
        assert _files(state.plugins) == before_files
        assert state.db.execute.await_count == 1
        state.db.add.assert_not_called()
    finally:
        directory.chmod(previous_mode)


@pytest.mark.behavioral
@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
async def test_real_windows_readonly_file_precedes_disable_and_hooks(
    version_fixture: VersionFixture, operation: str
) -> None:
    if os.name != "nt":
        pytest.skip("Real Windows read-only file attributes require Windows")
    state = version_fixture
    readonly_file = state.live / "backend/main.py"
    readonly_file.chmod(stat.S_IREAD)
    try:
        with pytest.raises(PermissionError):
            readonly_file.unlink()
        before_files, before_row = _files(state.plugins), deepcopy(vars(state.plugin))
        with pytest.raises(PluginError) as error:
            await _mutate(state, operation)
        assert error.value.extra["reason"] == "plugin_version_files_unwritable"
        assert state.events == []
        assert vars(state.plugin) == before_row
        assert _files(state.plugins) == before_files
        assert state.db.execute.await_count == 1
        state.db.add.assert_not_called()
    finally:
        for root in (state.plugins, state.source):
            for path in root.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.behavioral
async def test_writable_upgrade_archives_before_disable_and_runs_real_upgrade_hook(
    version_fixture: VersionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = version_fixture
    before_live, incoming = _files(state.live), _files(state.source)
    real_archive = state.manager.archive_version

    def archive(name, version):
        state.events.append("archive")
        return real_archive(name, version)

    monkeypatch.setattr(state.manager, "archive_version", archive)
    await state.manager._upgrade_unlocked(77, state.source)
    assert state.events == [
        "archive",
        "disable",
        "upgrade-schema",
        "flush",
        "upgrade-hook:2.0.0",
        "enable",
    ]
    assert _files(state.plugins / ".versions/demo-plugin/2.0.0") == before_live
    assert _files(state.live) == incoming
    assert state.plugin.version == "3.0.0"
    assert state.plugin.status == PluginStatusEnum.ENABLED.value
    assert (
        state.plugin.manifest
        == PluginLoader().load_manifest("demo-plugin").model_dump()
    )
    added = state.db.add.call_args.args[0]
    assert (added.plugin_id, added.version, added.status) == (77, "3.0.0", "active")


@pytest.mark.behavioral
async def test_missing_dependency_with_blocked_environment_precedes_archive_and_disable(
    version_fixture: VersionFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = version_fixture
    manifest = json.loads((state.source / "plugin.yaml").read_text(encoding="utf-8"))
    manifest["dependencies"]["python"] = ["novusai-fictional-missing-preflight==1.0.0"]
    (state.source / "plugin.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    blocked = tmp_path / "not-a-site-packages-directory"
    blocked.write_bytes(b"FICTIONAL-ENVIRONMENT-PATH-COLLISION")
    monkeypatch.setattr("sysconfig.get_path", lambda *_args, **_kwargs: str(blocked))
    before_files, before_row = _files(state.plugins), deepcopy(vars(state.plugin))

    with pytest.raises(PluginError) as error:
        await state.manager._upgrade_unlocked(77, state.source)

    assert error.value.extra["reason"] == "plugin_version_environment_unwritable"
    assert state.events == []
    assert vars(state.plugin) == before_row
    assert _files(state.plugins) == before_files
    assert state.db.execute.await_count == 1
    state.db.add.assert_not_called()


@pytest.mark.behavioral
def test_satisfied_installed_requirement_does_not_need_writable_environment(
    version_fixture: VersionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.plugins.version_mutation_preflight import require_version_mutation_writable

    def unexpected_environment_probe(*_args, **_kwargs):
        pytest.fail("Satisfied dependencies must not require environment writes")

    state = version_fixture
    monkeypatch.setattr("sysconfig.get_path", unexpected_environment_probe)
    before = _files(state.plugins)
    require_version_mutation_writable(
        state.live,
        state.plugins / ".versions/demo-plugin/2.0.0",
        python_requirements=["pytest>=0"],
    )
    assert _files(state.plugins) == before
    assert state.events == []


@pytest.mark.behavioral
def test_unverifiable_requirement_rejects_without_filesystem_mutation(
    version_fixture: VersionFixture,
) -> None:
    from app.plugins.version_mutation_preflight import require_version_mutation_writable

    state = version_fixture
    before = _files(state.plugins)
    with pytest.raises(PluginError) as error:
        require_version_mutation_writable(
            state.live,
            state.plugins / ".versions/demo-plugin/2.0.0",
            python_requirements=["%%%invalid requirement%%%"],
        )
    assert error.value.extra["reason"] == "plugin_version_dependency_unverified"
    assert _files(state.plugins) == before
    assert state.events == []


@pytest.mark.behavioral
@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
async def test_actual_archive_copy_failure_precedes_disable(
    version_fixture: VersionFixture, operation: str
) -> None:
    if os.name != "posix":
        pytest.skip("Real POSIX file read denial requires a non-root Linux fixture")
    state = version_fixture
    unreadable = state.live / "version.txt"
    before_files = _files(state.live)
    unreadable.chmod(0)
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("Current identity bypasses POSIX file read denial")
        with pytest.raises(PermissionError):
            unreadable.read_bytes()
        before_row = deepcopy(vars(state.plugin))
        with pytest.raises(PluginError) as error:
            await _mutate(state, operation)
        assert error.value.extra["reason"] == "plugin_version_archive_failed"
        assert vars(state.plugin) == before_row
        assert state.events == []
        assert state.db.execute.await_count == 1
        state.db.add.assert_not_called()
    finally:
        unreadable.chmod(0o600)
    assert _files(state.live) == before_files


@pytest.mark.structural
def test_readonly_mount_flag_is_a_rejection_branch_only(
    version_fixture: VersionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中文: structural；模拟标志仅覆盖分支，不代表真实 Linux 只读挂载验收。

    EN: Structural branch coverage only; the flag double is not real Linux
    read-only mount acceptance.
    """
    from app.plugins.version_mutation_preflight import require_version_mutation_writable

    monkeypatch.setattr(
        os, "statvfs", lambda _path: SimpleNamespace(f_flag=1), raising=False
    )
    monkeypatch.setattr(os, "ST_RDONLY", 1, raising=False)
    state = version_fixture
    before = _files(state.plugins)
    with pytest.raises(PluginError) as error:
        require_version_mutation_writable(
            state.live, state.plugins / ".versions/demo-plugin/2.0.0"
        )
    assert error.value.extra["reason"] == "plugin_version_files_unwritable"
    assert _files(state.plugins) == before
    assert state.events == []
