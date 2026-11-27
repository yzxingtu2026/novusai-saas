"""中文: 按插件源码所有权解析迁移终点，避免继承的宿主分支标签。

EN: Resolve a plugin-owned migration tip without inherited host branch labels.
"""

from __future__ import annotations

import ast
import stat
import warnings
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.i18n import _
from app.plugins.exceptions import PluginInstallError
from app.plugins.table_ownership import get_plugin_owned_table_prefix

_REASON = "plugin_migration_target_unavailable"


def _owned_path(path: Path, root: Path, *, directory: bool) -> Path:
    """中文: 先拒绝链接或重解析路径组件，再解析插件所有权路径。

    EN: Reject linked/reparse components before resolving an owned path.
    """
    relative = path.relative_to(root)
    for item in (
        root,
        *(
            root.joinpath(*relative.parts[:i])
            for i in range(1, len(relative.parts) + 1)
        ),
    ):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(_REASON)
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if directory != resolved.is_dir() or (not directory and not resolved.is_file()):
        raise ValueError(_REASON)
    return resolved


def _declaration(source: bytes) -> tuple[str, tuple[str, ...]]:
    values = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {
                "revision",
                "down_revision",
                "depends_on",
            }:
                if target.id in values or node.value is None:
                    raise ValueError(_REASON)
                values[target.id] = ast.literal_eval(node.value)
    revision = values.get("revision")
    if type(revision) is not str or not revision or revision.strip() != revision:
        raise ValueError(_REASON)
    references = []
    for field in ("down_revision", "depends_on"):
        value = values.get(field)
        if value is None:
            continue
        items = (value,) if type(value) is str else value
        if not isinstance(items, (tuple, list)) or any(
            type(item) is not str or not item or item.strip() != item for item in items
        ):
            raise ValueError(_REASON)
        references.extend(items)
    return revision, tuple(references)


def resolve_plugin_upgrade_target(
    plugin_name: str,
    *,
    plugins_dir: Path,
    version_locations: Sequence[str],
) -> str:
    """中文: 只选插件自有的唯一终点，依赖仍在完整迁移图中验证。

    EN: Select one exact owned leaf, validating the complete graph's dependencies.
    Host merges inherit plugin branch labels. They must not become candidates
    for this plugin's upgrade, while legitimate external parents/dependencies
    remain resolved by the full Alembic graph.
    """
    try:
        if type(plugin_name) is not str or plugin_name.strip() != plugin_name:
            raise ValueError(_REASON)
        get_plugin_owned_table_prefix(plugin_name)
        root = Path(plugins_dir).absolute()
        versions = _owned_path(
            root / plugin_name / "backend" / "migrations" / "versions",
            root,
            directory=True,
        )
        owned: dict[str, tuple[Path, tuple[str, ...]]] = {}
        for path in sorted(versions.glob("*.py")):
            if path.name == "__init__.py":
                continue
            exact = _owned_path(path, root, directory=False)
            revision, references = _declaration(exact.read_bytes())
            if revision in owned:
                raise ValueError(_REASON)
            owned[revision] = (exact, references)
        if not owned:
            raise ValueError(_REASON)
        locations = [
            _owned_path(Path(path).absolute(), root.parent, directory=True)
            for path in version_locations
        ]
        if versions not in locations:
            raise ValueError(_REASON)
        config = Config()
        config.set_main_option("script_location", str(root.parent / "migrations"))
        config.set_main_option("path_separator", "newline")
        config.set_main_option("version_locations", "\n".join(map(str, locations)))
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            graph = ScriptDirectory.from_config(config)
            # 中文: 强制检查完整图，不能忽略其他分支的缺失依赖或循环。
            # EN: Validate the complete graph, including missing edges and cycles.
            list(graph.walk_revisions())
            parents = set()
            for revision, (path, references) in owned.items():
                script = graph.get_revision(revision)
                if script is None or Path(script.path).resolve(strict=True) != path:
                    raise ValueError(_REASON)
                for reference in references:
                    parent = graph.get_revision(reference)
                    if parent is None:
                        raise ValueError(_REASON)
                # 中文: 两个自有修订之间可有外部节点，必须遍历完整祖先及依赖。
                # EN: Traverse all ancestors/dependencies across external nodes.
                parents.update(
                    ancestor.revision
                    for ancestor in graph.iterate_revisions(
                        revision, "base", implicit_base=True
                    )
                    if ancestor.revision in owned and ancestor.revision != revision
                )
            leaves = set(owned) - parents
            if len(leaves) != 1:
                raise ValueError(_REASON)
            return leaves.pop()
    except Exception:
        # 中文: 不公开文件路径、源码表达式或 Alembic 原始异常。
        # EN: Do not expose paths, source expressions or raw Alembic errors.
        raise PluginInstallError(
            message=_("plugin.error.install_failed"), data={"reason": _REASON}
        ) from None
