"""Host-owned plugin database table namespace rules. / 宿主控制的插件数据表命名空间规则。"""

from __future__ import annotations

from collections.abc import Iterable

from app.plugins.manifest_helpers import _PLUGIN_NAME_PATTERN


def get_plugin_owned_table_prefix(plugin_name: str) -> str:
    """中文: 从已验证的插件名确定唯一的宿主所有权表前缀。

    EN: Derive the single host-owned table prefix from a validated plugin name.
    """
    normalized_name = str(plugin_name or "").strip()
    if not _PLUGIN_NAME_PATTERN.fullmatch(normalized_name):
        raise ValueError("Invalid plugin name for database table ownership")
    return f"px_{normalized_name.replace('-', '_')}_"


def validate_declared_table_prefixes(
    plugin_name: str,
    declared_prefixes: Iterable[str],
) -> None:
    """中文: 拒绝任何不属于宿主生成命名空间的 manifest 表前缀。

    EN: Reject manifest prefixes outside the host-derived ownership namespace.
    """
    owned_prefix = get_plugin_owned_table_prefix(plugin_name)
    foreign_prefixes = [
        str(prefix or "").strip()
        for prefix in declared_prefixes
        if str(prefix or "").strip() != owned_prefix
    ]
    if foreign_prefixes:
        raise ValueError(
            "db_table_prefixes must match the host-owned prefix "
            f"'{owned_prefix}' for plugin '{plugin_name}'"
        )


__all__ = [
    "get_plugin_owned_table_prefix",
    "validate_declared_table_prefixes",
]
