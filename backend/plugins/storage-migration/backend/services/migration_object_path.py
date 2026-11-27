"""Validate persisted object-relative paths / 校验持久化的相对对象路径。"""

from __future__ import annotations


def relative_parts(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("/")) if type(path) is str else ()
    if not parts or any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or any(char in "\\:" or ord(char) < 32 or ord(char) == 127 for char in part)
        for part in parts
    ):
        raise ValueError("storage_migration_path_invalid")
    return parts
