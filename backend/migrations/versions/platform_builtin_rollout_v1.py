"""Update the installation snapshots for three bundled plugin changes.

Revision ID: platform_builtin_rollout_v1
Revises: platform_shared_v1
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "platform_builtin_rollout_v1"
down_revision = "platform_shared_v1"
branch_labels = None
depends_on = None

# SHA-256 of the canonical normalized manifest before and after this release.
# Exact matches leave externally maintained copies and custom manifests intact.
_MANIFEST_DIGESTS = {
    "storage-billing": (
        "c68a7ba145464d7ec7bdbd12e26ac4178cd91f5dcd1ec87462377ddeca27c88a",
        "b23a9770adb11f98dfddb05a0e19e9f55292dbe7d9c1e7b13a3d4a77ee5d6b4b",
    ),
    "weather-widget": (
        "f876eb82f7953b2809250ac024520eba8ae083bea80a8f48b462e6cbced7f0ad",
        "35ab3099891eec4a1ff9e76e8ff40e613653bc545bf3bc932c55006c9f438c74",
    ),
    "storage-migration": (
        "b749bd5b094cd75c248a67f28dae2ea5c470b8aae13a2706ab849ed873de0246",
        "60937a8683a723617cb970ef6ddc9fd2e56106e24aeff8a8fe1b6b2a5768e7f0",
    ),
}
_GRANTED_CAPABILITIES = {
    "storage-billing": (
        ["db:own_tables", "platform:read", "http:outbound", "config:write"],
        [
            "db:own_tables",
            "platform:read",
            "storage:credential_use",
            "config:read",
            "http:outbound",
            "config:write",
        ],
        {"db:own_tables", "platform:read"},
    ),
    "weather-widget": (
        ["http:outbound"],
        ["http:outbound", "config:read"],
        {"http:outbound"},
    ),
}
_RECOVERY_TASK = {
    "name": "recover_forward_runs",
    "handler": "tasks.recover_forward_runs",
    "display_name": {
        "zh-CN": "存储迁移中断检查",
        "en": "Storage Migration Interruption Check",
    },
    "schedule_type": "interval",
    "cron_expression": None,
    "interval_seconds": 60,
    "queue": "scheduled",
    "description": {
        "zh-CN": "检查已失去执行进程的迁移任务，标记为暂停或结果待核对。",
        "en": (
            "Mark forward migrations whose execution process is gone "
            "as paused or unconfirmed."
        ),
    },
}
_plugins = sa.table(
    "plugins",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("manifest", sa.JSON),
    sa.column("granted_capabilities", sa.JSON),
    sa.column("install_source", sa.String),
    sa.column("is_deleted", sa.Boolean),
)


def _digest(manifest: dict) -> str:
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply(*, forward: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _plugins.c.id,
            _plugins.c.name,
            _plugins.c.manifest,
            _plugins.c.granted_capabilities,
        )
        .where(
            _plugins.c.name.in_(tuple(_MANIFEST_DIGESTS)),
            _plugins.c.install_source == "local",
            _plugins.c.is_deleted.is_(False),
        )
        .with_for_update()
    ).mappings()
    for row in rows:
        name = row["name"]
        manifest = row["manifest"]
        grants = row["granted_capabilities"]
        before, after = _MANIFEST_DIGESTS[name]
        if not isinstance(manifest, dict) or _digest(manifest) != (
            before if forward else after
        ):
            continue

        if name == "storage-migration":
            manifest["extensions"]["tasks"] = [_RECOVERY_TASK] if forward else []
        else:
            previous, current, required = _GRANTED_CAPABILITIES[name]
            manifest["capabilities"] = current if forward else previous
            added = [capability for capability in current if capability not in previous]
            if not isinstance(grants, list) or not all(isinstance(cap, str) for cap in grants):
                continue
            if forward and required.issubset(grants):
                grants = list(dict.fromkeys([*grants, *added]))
            elif not forward:
                grants = [capability for capability in grants if capability not in added]

        bind.execute(
            _plugins.update()
            .where(_plugins.c.id == row["id"])
            .values(manifest=manifest, granted_capabilities=grants)
        )


def upgrade() -> None:
    _apply(forward=True)


def downgrade() -> None:
    _apply(forward=False)
