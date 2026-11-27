"""中文: 在版本生命周期副作用前识别已确定不可写的运行目录。

EN: Detect already unwritable version-mutation paths before lifecycle effects.
These observations are advisory; they do not reserve paths or make later
replacement, migration, hooks or database changes atomic.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import stat
import sysconfig
from pathlib import Path

from app.core.i18n import _
from app.plugins.dependencies import (
    is_python_requirement_satisfied,
    iter_effective_python_requirements,
)
from app.plugins.exceptions import PluginError


def _blocked(reason: str) -> PluginError:
    return PluginError(
        message=_("plugin.error.version_mutation_unavailable"),
        extra={"reason": reason},
    )


def _directory(path: Path, reason: str) -> Path:
    while not path.exists():
        parent = path.parent
        if parent == path:
            raise _blocked(reason)
        path = parent
    if not path.is_dir():
        raise _blocked(reason)
    return path


def _require_directory_write(path: Path, reason: str) -> None:
    directory = _directory(path, reason)
    if hasattr(os, "statvfs") and os.statvfs(directory).f_flag & os.ST_RDONLY:
        raise _blocked(reason)
    options = {"effective_ids": True} if os.access in os.supports_effective_ids else {}
    mode = os.W_OK | os.X_OK if os.name == "posix" else os.W_OK
    if not os.access(directory, mode, **options):
        raise _blocked(reason)


def _require_replaceable_tree(path: Path, reason: str) -> None:
    _require_directory_write(path.parent, reason)
    if not path.exists():
        return
    if not path.is_dir():
        raise _blocked(reason)
    _require_directory_write(path, reason)
    for child in path.rglob("*"):
        if child.is_dir():
            _require_directory_write(child, reason)
        elif os.name == "nt" and (
            child.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        ):
            # 中文: Windows 会拒绝删除只读文件，即使父目录可创建文件。
            # EN: Windows denies unlinking a read-only file even when its parent is writable.
            raise _blocked(reason)


def _python_install_required(requirements: list[str]) -> bool:
    needs_install = False
    for requirement in iter_effective_python_requirements(requirements):
        if requirement.url:
            raise ValueError("Direct requirement URL cannot be verified locally")
        if not is_python_requirement_satisfied(requirement):
            needs_install = True
            continue
        distribution = importlib.metadata.distribution(requirement.name)
        if not distribution.read_text("METADATA"):
            needs_install = True
            continue
        candidates = [
            item.strip()
            for item in (distribution.read_text("top_level.txt") or "").splitlines()
            if item.strip().isidentifier()
        ]
        if candidates and not any(
            importlib.util.find_spec(item) for item in candidates
        ):
            needs_install = True
    return needs_install


def require_version_mutation_writable(
    live: Path, archive: Path, *, python_requirements: list[str] | None = None
) -> None:
    """中文: 保留当前状态地拒绝确定失败；成功检查不替代真实备份或事务。

    EN: Reject known failures without changing live state. Passing this check
    does not replace the actual archive operation or transaction boundaries.
    """
    files_reason = "plugin_version_files_unwritable"
    try:
        if not live.is_dir():
            raise _blocked(files_reason)
        _require_replaceable_tree(live, files_reason)
        _require_replaceable_tree(archive, files_reason)
    except OSError as error:
        raise _blocked(files_reason) from error

    try:
        needs_install = _python_install_required(python_requirements or [])
    except Exception as error:
        raise _blocked("plugin_version_dependency_unverified") from error
    if needs_install:
        reason = "plugin_version_environment_unwritable"
        try:
            for name in ("purelib", "platlib", "scripts"):
                _require_directory_write(Path(sysconfig.get_path(name)), reason)
        except (OSError, TypeError, ValueError) as error:
            raise _blocked(reason) from error
