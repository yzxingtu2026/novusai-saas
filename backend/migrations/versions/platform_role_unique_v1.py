"""Enforce live tenant role identities without changing historical role rows.

Revision ID: platform_role_unique_v1
Revises: platform_epoch_v1

Preflight under a table lock and leave historical rows intact on conflicts.
锁表检查历史重复，遇到冲突时保留所有原记录。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

revision = "platform_role_unique_v1"
down_revision = "platform_epoch_v1"
branch_labels = None
depends_on = None

_INDEXES = (
    ("tenant_user_roles", "name", "uq_tenant_user_roles_live_name"),
    ("tenant_user_roles", "code", "uq_tenant_user_roles_live_code"),
    ("tenant_admin_roles", "name", "uq_tenant_admin_roles_flat_name"),
    ("tenant_admin_roles", "code", "uq_tenant_admin_roles_flat_code"),
)
_FLAT = (
    "is_deleted = false AND type = 'role' AND parent_id IS NULL "
    "AND path IS NULL AND leader_id IS NULL AND level = 1"
)


def _predicate(table):
    conditions = [table.c.is_deleted.is_(False)]
    if table.name == "tenant_admin_roles":
        conditions.extend(
            (
                table.c.type == "role",
                table.c.parent_id.is_(None),
                table.c.path.is_(None),
                table.c.leader_id.is_(None),
                table.c.level == 1,
            )
        )
    return sa.and_(*conditions)


def upgrade():
    bind = op.get_bind()
    if (
        bind.dialect.name != "postgresql"
        or not bind.in_transaction()
        or getattr(bind.connection.dbapi_connection, "autocommit", None) is not False
    ):
        raise RuntimeError("tenant_role_unique_postgresql_transaction_required")
    prior_timeout = bind.scalar(sa.text("SHOW lock_timeout"))
    milliseconds = bind.scalar(
        sa.text("SELECT setting::int FROM pg_settings WHERE name = 'lock_timeout'")
    )
    effective = min(milliseconds, 5000) if milliseconds else 5000
    bind.execute(
        sa.text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": str(effective) + "ms"},
    )
    try:
        # Block concurrent INSERT/UPDATE/DELETE before all conflict reads and
        # keep that authority until the migration owner commits its DDL.
        bind.execute(
            sa.text(
                "LOCK TABLE tenant_user_roles, tenant_admin_roles "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig, "pgcode", None
        )
        if state in {"55P03", "57014"}:
            raise RuntimeError("tenant_role_unique_lock_unavailable") from None
        raise

    metadata = sa.MetaData()
    tables = {
        name: sa.Table(name, metadata, autoload_with=bind)
        for name in ("tenant_user_roles", "tenant_admin_roles")
    }
    required = {"tenant_id", "name", "code", "is_deleted"}
    for name, table in tables.items():
        expected = required | (
            {"type", "parent_id", "path", "leader_id", "level"}
            if name == "tenant_admin_roles"
            else set()
        )
        if not expected.issubset(table.c.keys()):
            raise RuntimeError("tenant_role_unique_relation_incomplete")
    # Complete every conflict preflight before creating even the first index.
    # No names/codes/IDs or provider/database error text enter the failure reason.
    for name, field, _index in _INDEXES:
        table = tables[name]
        duplicate = (
            sa.select(sa.literal(1))
            .where(_predicate(table))
            .group_by(table.c.tenant_id, table.c[field])
            .having(sa.func.count() > 1)
            .limit(1)
        )
        if bind.scalar(duplicate) is not None:
            raise RuntimeError("tenant_role_unique_historical_conflict")
    for name, field, index in _INDEXES:
        op.create_index(
            index,
            name,
            ["tenant_id", field],
            unique=True,
            postgresql_where=sa.text(
                _FLAT if name == "tenant_admin_roles" else "is_deleted = false"
            ),
        )
    bind.execute(
        sa.text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": prior_timeout},
    )


def downgrade():
    for name, _field, index in reversed(_INDEXES):
        op.drop_index(index, table_name=name)
