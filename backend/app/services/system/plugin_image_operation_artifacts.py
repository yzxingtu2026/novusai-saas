"""中文: 离线发布包的保留和精确树身份。 EN: Retained artifacts and exact tree identity.

中文: 不构建镜像，不写插件目录。 EN: Never build images or mutate installed plugins.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

import yaml

from app.core.config import settings
from app.plugins.manifest import PluginManifest
from app.plugins.package_security import validate_plugin_zip_archive
from app.services.system.plugin_image_operation_contract import (
    TreeEntry,
    canonical_sha,
    strict_json,
    tree_sha,
)


class ArtifactError(ValueError):
    """中文: 仅固定机器原因。 EN: Contains fixed machine reasons only."""


def _ordinary(path: Path, *, directory: bool) -> os.stat_result:
    observed = path.lstat()
    if stat.S_ISLNK(observed.st_mode) or getattr(
        observed, "st_file_attributes", 0
    ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ArtifactError("plugin_image_artifact_link")
    allowed = stat.S_ISDIR if directory else stat.S_ISREG
    if not allowed(observed.st_mode):
        raise ArtifactError("plugin_image_artifact_type")
    return observed


def _identity(observed: os.stat_result, *, cross_handle: bool = False) -> tuple:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        None if os.name == "nt" and cross_handle else observed.st_ctime_ns,
        getattr(observed, "st_birthtime_ns", None),
    )


def file_digest(path: Path) -> str:
    """中文: 拒绝链接与读取期间改变。 EN: Refuse links and changes during reads."""
    before = _ordinary(path, directory=False)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        # 中文: Windows 3.13 stat/fstat的ctime语义不同；同来源前后比较仍检查完整ctime。
        # EN: Windows 3.13 stat/fstat ctime differs; same-source comparisons retain full ctime.
        if _identity(opened, cross_handle=True) != _identity(before, cross_handle=True):
            raise ArtifactError("plugin_image_artifact_changed")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        if _identity(os.fstat(stream.fileno())) != _identity(opened):
            raise ArtifactError("plugin_image_artifact_changed")
    if _identity(path.lstat()) != _identity(before):
        raise ArtifactError("plugin_image_artifact_changed")
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    """中文: 包含全部目录与文件，禁止链接。 EN: Hash every directory/file, no links."""
    _ordinary(root, directory=True)
    rows: list[tuple] = []
    total_size = 0

    def walk(directory: Path):
        nonlocal total_size
        before = _ordinary(directory, directory=True)
        if len(directory.relative_to(root).parts) > 40:
            raise ArtifactError("plugin_image_artifact_size")
        for child in sorted(directory.iterdir()):
            if len(rows) >= settings.PLUGIN_MAX_ARCHIVE_FILES:
                raise ArtifactError("plugin_image_artifact_size")
            observed = child.lstat()
            relative = child.relative_to(root).as_posix()
            if stat.S_ISDIR(observed.st_mode):
                _ordinary(child, directory=True)
                rows.append((relative, "directory"))
                walk(child)
            else:
                _ordinary(child, directory=False)
                total_size += observed.st_size
                if (
                    observed.st_size > settings.PLUGIN_MAX_ARCHIVE_SINGLE_FILE_SIZE
                    or total_size > settings.PLUGIN_MAX_UNCOMPRESSED_SIZE
                ):
                    raise ArtifactError("plugin_image_artifact_size")
                rows.append((relative, "file", observed.st_size, file_digest(child)))
        if _identity(directory.lstat()) != _identity(before):
            raise ArtifactError("plugin_image_artifact_changed")

    walk(root)
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def archive_identity(path: Path, expected_sha: str) -> tuple[str, str, str, str]:
    """Measure the bounded ZIP from its manifest root / 从清单根核对有界 ZIP。"""
    original = _ordinary(path, directory=False)
    if original.st_size > settings.PLUGIN_MAX_PACKAGE_SIZE:
        raise ArtifactError("plugin_image_artifact_size")
    if file_digest(path) != expected_sha:
        raise ArtifactError("plugin_image_archive_mismatch")
    validate_plugin_zip_archive(path)
    with zipfile.ZipFile(path) as archive:
        names = {}
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename.rstrip("/"))
            if name.as_posix() != entry.filename.rstrip("/") or name in names:
                raise ArtifactError("plugin_image_archive_path")
            if (
                name.is_absolute()
                or any(char in entry.filename for char in "\\:\x00")
                or any(part in {".", ".."} for part in name.parts)
            ):
                raise ArtifactError("plugin_image_archive_path")
            names[name] = entry
        manifests = [
            name
            for name, entry in names.items()
            if name.name == "plugin.yaml"
            and len(name.parts) <= 2
            and not entry.is_dir()
        ]
        if len(manifests) != 1:
            raise ArtifactError("plugin_image_archive_manifest")
        root = manifests[0].parent
        if names[manifests[0]].file_size > 1_048_576:
            raise ArtifactError("plugin_image_archive_manifest")
        manifest = PluginManifest.model_validate(
            yaml.safe_load(archive.read(names[manifests[0]]))
        )
        files = {}
        directories = set()
        for name, entry in names.items():
            if name == root and entry.is_dir():
                continue
            relative = name.relative_to(root)
            directories.update(parent for parent in relative.parents if parent.parts)
            if entry.is_dir():
                directories.add(relative)
            else:
                with archive.open(entry) as contents:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: contents.read(1024 * 1024), b""):
                        hasher.update(chunk)
                files[relative] = TreeEntry(
                    path=relative.as_posix(),
                    kind="file",
                    size=entry.file_size,
                    sha256=hasher.hexdigest(),
                )
        if set(files) & directories:
            raise ArtifactError("plugin_image_archive_path")
        if manifest.icon != "icon.png":
            manifest.icon = "icon.png" if PurePosixPath("icon.png") in files else ""
        entries = [
            TreeEntry(path=path.as_posix(), kind="directory", size=0, sha256=None)
            for path in directories
        ] + list(files.values())
    if (
        _identity(path.lstat()) != _identity(original)
        or file_digest(path) != expected_sha
    ):
        raise ArtifactError("plugin_image_archive_changed")
    return (
        manifest.name,
        manifest.version,
        canonical_sha(manifest.model_dump()),
        tree_sha(entries),
    )


def private_json(path: Path, expected_sha: str):
    """Read one exact private evidence file / 读取一份精确绑定的私密证据文件。"""
    if not path.is_absolute():
        raise ArtifactError("plugin_image_evidence_path")
    for parent in path.parents:
        _ordinary(parent, directory=True)
    before = _ordinary(path, directory=False)
    if not 0 < before.st_size <= 4_000_000 or (
        os.name != "nt" and before.st_mode & 0o077
    ):
        raise ArtifactError("plugin_image_evidence_access")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(4_000_001)
    if (
        _identity(opened, cross_handle=True) != _identity(before, cross_handle=True)
        or _identity(path.lstat()) != _identity(before)
        or len(raw) != before.st_size
        or hashlib.sha256(raw).hexdigest() != expected_sha
    ):
        raise ArtifactError("plugin_image_evidence_changed")
    return strict_json(raw)
