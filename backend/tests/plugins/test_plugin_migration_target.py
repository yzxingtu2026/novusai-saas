"""中文: structural；使用真实 Alembic 图验证插件自有迁移终点。

EN: Structural: real Alembic graphs and exact plugin-owned target admission.
No SQL executor, migration command or database result is mocked. These graph
checks do not claim actual database/public-lifecycle upgrade acceptance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from app.plugins.exceptions import PluginInstallError
from app.plugins.migration_target import resolve_plugin_upgrade_target

pytestmark = pytest.mark.structural
_REASON = "plugin_migration_target_unavailable"


def _write(directory, filename, revision, parent=None, dependencies=None, branch=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        f"revision={revision!r}\ndown_revision={parent!r}\n"
        f"depends_on={dependencies!r}\nbranch_labels={branch!r}\n"
        "def upgrade():\n pass\ndef downgrade():\n pass\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def graph_root(tmp_path):
    host = tmp_path / "migrations" / "versions"
    own = tmp_path / "plugins" / "demo-plugin" / "backend" / "migrations" / "versions"
    host.mkdir(parents=True)
    own.mkdir(parents=True)
    return host, own


def _target(graph_root):
    host, own = graph_root
    return resolve_plugin_upgrade_target(
        "demo-plugin",
        plugins_dir=own.parents[3],
        version_locations=[str(host), str(own)],
    )


def test_combined_graph_includes_current_storage_migration_revision():
    backend = Path(__file__).resolve().parents[2]
    locations = [backend / "migrations" / "versions"] + sorted(
        (backend / "plugins").glob("*/backend/migrations/versions")
    )
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("version_locations", "\n".join(map(str, locations)))
    graph = ScriptDirectory.from_config(cfg)
    assert graph.get_revision("sm_006_forward_recovery").down_revision == (
        "sm_005_forward_attempt"
    )
    assert "sm_006_forward_recovery" in graph.get_heads()


def test_branch_label_head_can_be_ambiguous_while_owned_target_is_unique(graph_root):
    host, own = graph_root
    _write(host, "base.py", "host_base")
    _write(own, "one.py", "own_one", "host_base", branch=("plugin_demo_plugin",))
    _write(host, "host_left.py", "host_left", "own_one")
    _write(own, "own_right.py", "own_right", "own_one")
    cfg = Config()
    cfg.set_main_option("script_location", str(host.parent))
    cfg.set_main_option("path_separator", "newline")
    cfg.set_main_option("version_locations", "\n".join(map(str, graph_root)))
    graph = ScriptDirectory.from_config(cfg)
    with pytest.raises(CommandError, match="Multiple head revisions"):
        graph.get_revision("plugin_demo_plugin@head")
    assert _target(graph_root) == "own_right"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("novusdoc", "novusdoc_003_folder_tag_owner"),
        ("storage-billing", "sb_003_period_fields"),
        ("storage-migration", "sm_006_forward_recovery"),
    ],
)
def test_builtin_migration_roots_have_exact_owned_targets(name, expected):
    backend = Path(__file__).resolve().parents[2]
    locations = [backend / "migrations" / "versions"] + sorted(
        (backend / "plugins").glob("*/backend/migrations/versions")
    )
    assert (
        resolve_plugin_upgrade_target(
            name,
            plugins_dir=backend / "plugins",
            version_locations=list(map(str, locations)),
        )
        == expected
    )


def test_external_parent_and_dependency_remain_in_complete_graph(graph_root):
    host, own = graph_root
    _write(host, "base.py", "host_base")
    _write(host, "dependency.py", "host_dependency", "host_base")
    _write(own, "one.py", "own_one", "host_base", branch=("plugin_demo_plugin",))
    _write(own, "two.py", "own_two", "own_one", "host_dependency")
    _write(host, "merge.py", "host_merge", ("own_one", "host_dependency"))
    assert _target(graph_root) == "own_two"


def test_internal_dependency_counts_as_owned_edge(graph_root):
    host, own = graph_root
    _write(host, "base.py", "host_base")
    _write(own, "one.py", "own_one", "host_base")
    _write(own, "two.py", "own_two", "host_base", "own_one")
    assert _target(graph_root) == "own_two"


@pytest.mark.parametrize("via_dependency", [False, True])
def test_interleaved_external_chain_has_one_owned_maximal_revision(
    graph_root, via_dependency
):
    host, own = graph_root
    _write(own, "one.py", "own_one")
    _write(host, "between.py", "host_between", "own_one")
    _write(
        own,
        "two.py",
        "own_two",
        None if via_dependency else "host_between",
        "host_between" if via_dependency else None,
    )
    assert _target(graph_root) == "own_two"


def test_owned_merge_has_one_leaf(graph_root):
    _host, own = graph_root
    _write(own, "one.py", "one")
    _write(own, "two.py", "two", "one")
    _write(own, "three.py", "three", "one")
    _write(own, "merge.py", "merge", ("two", "three"))
    assert _target(graph_root) == "merge"


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "two_leaves",
        "duplicate",
        "missing_parent",
        "missing_dependency",
        "cycle",
        "no_revision",
        "dynamic_revision",
    ],
)
def test_invalid_graphs_fail_closed_with_fixed_safe_reason(graph_root, case):
    _host, own = graph_root
    if case == "two_leaves":
        _write(own, "one.py", "one")
        _write(own, "two.py", "two")
    elif case == "duplicate":
        _write(own, "one.py", "same")
        _write(own, "two.py", "same")
    elif case == "missing_parent":
        _write(own, "one.py", "one", "PRIVATE-MISSING-PARENT")
    elif case == "missing_dependency":
        _write(own, "one.py", "one", dependencies="PRIVATE-MISSING-DEPENDENCY")
    elif case == "cycle":
        _write(own, "one.py", "one", "two")
        _write(own, "two.py", "two", "one")
    elif case in {"no_revision", "dynamic_revision"}:
        (own / "invalid.py").write_text(
            "revision='x'+'PRIVATE'\n" if case == "dynamic_revision" else "PRIVATE=1\n"
        )
    with pytest.raises(PluginInstallError) as error:
        _target(graph_root)
    assert error.value.data == {"reason": _REASON}
    assert "PRIVATE" not in str(error.value)
    assert str(error.value) != _REASON
    assert error.value.__cause__ is None


def test_missing_full_graph_member_is_not_ignored(graph_root):
    host, own = graph_root
    _write(own, "one.py", "one")
    _write(host, "bad.py", "host_bad", "PRIVATE-MISSING")
    with pytest.raises(PluginInstallError) as error:
        _target(graph_root)
    assert error.value.data == {"reason": _REASON}


def test_missing_owned_graph_location_is_not_guessed(graph_root):
    host, own = graph_root
    _write(own, "one.py", "one")
    with pytest.raises(PluginInstallError) as error:
        resolve_plugin_upgrade_target(
            "demo-plugin", plugins_dir=own.parents[3], version_locations=[str(host)]
        )
    assert error.value.data == {"reason": _REASON}


@pytest.mark.parametrize(
    "name", ["../demo-plugin", "Demo", "demo/plugin", " demo-plugin", "demo-plugin "]
)
def test_plugin_name_uses_host_ownership_rules(graph_root, name):
    host, own = graph_root
    _write(own, "one.py", "one")
    with pytest.raises(PluginInstallError) as error:
        resolve_plugin_upgrade_target(
            name, plugins_dir=own.parents[3], version_locations=[str(host), str(own)]
        )
    assert error.value.data == {"reason": _REASON}


def test_external_version_directory_cannot_supply_ownership(graph_root, tmp_path):
    host, own = graph_root
    _write(own, "one.py", "one")
    foreign = tmp_path.parent / (tmp_path.name + "-foreign")
    try:
        _write(foreign, "external.py", "external")
        with pytest.raises(PluginInstallError) as error:
            resolve_plugin_upgrade_target(
                "demo-plugin",
                plugins_dir=own.parents[3],
                version_locations=[str(host), str(own), str(foreign)],
            )
        assert error.value.data == {"reason": _REASON}
    finally:
        (foreign / "external.py").unlink()
        foreign.rmdir()
