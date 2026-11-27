"""Translate only this repository's named PostgreSQL role-identity conflicts.

The caller still owns the failed transaction and must roll it back. No driver
message, SQL, parameter, role value or diagnostic detail enters the public error.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import ClassVar

from sqlalchemy.exc import IntegrityError

from app.core.i18n import _
from app.enums import ErrorCode
from app.exceptions import BusinessException


def _postgres_unique_constraint(error: IntegrityError) -> str | None:
    """Use structured asyncpg/psycopg diagnostics, never parse exception text."""
    original = error.orig
    state = (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or getattr(original, "sql_state", None)
    )
    if state != "23505":
        return None
    seen = set()
    for _attempt in range(4):
        if original is None or id(original) in seen:
            return None
        seen.add(id(original))
        name = getattr(original, "constraint_name", None) or getattr(
            getattr(original, "diag", None), "constraint_name", None
        )
        if isinstance(name, str):
            return name
        original = getattr(original, "__cause__", None)
    return None


class TenantRoleUniqueWriteMixin:
    """Keep all role write entrypoints behind the same narrow error boundary."""

    _role_unique_constraints: ClassVar[dict[str, str]]

    @contextmanager
    def _translate_role_unique_conflict(self):
        try:
            yield
        except IntegrityError as error:
            key = self._role_unique_constraints.get(_postgres_unique_constraint(error))
            if key is None:
                raise
            raise BusinessException(
                message=_(key), code=ErrorCode.DUPLICATE_ENTRY
            ) from None

    async def create(self, data):
        with self._translate_role_unique_conflict():
            return await super().create(data)

    async def create_many(self, data_list):
        with self._translate_role_unique_conflict():
            return await super().create_many(data_list)

    async def update(self, id, data):
        with self._translate_role_unique_conflict():
            return await super().update(id, data)

    async def update_many(self, ids, data):
        with self._translate_role_unique_conflict():
            return await super().update_many(ids, data)

    async def restore_by_id(self, id, delete_level=None, recycle_stage=None):
        with self._translate_role_unique_conflict():
            return await super().restore_by_id(id, delete_level, recycle_stage)

    async def batch_restore(self, ids):
        with self._translate_role_unique_conflict():
            return await super().batch_restore(ids)
