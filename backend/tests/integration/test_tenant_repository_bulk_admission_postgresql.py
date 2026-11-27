"""Behavioral: tenant admission through real repositories and PostgreSQL.

Fixture mappings use a separate registry and fictional rows, so these tests do
not modify the application's model registry or touch external business systems.
"""

from __future__ import annotations

import os
from copy import deepcopy
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, synonym
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.base_repository import BaseRepository, TenantRepository
from app.core.data_permission import data_permission_ctx

_DSN = os.environ.get("TENANT_BULK_POSTGRES_DSN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _DSN, reason="Disposable PostgreSQL fixture required"),
]

_TENANT = 77
_OTHER_TENANT = 88


class _FixtureBase(DeclarativeBase):
    pass


class _RowColumns:
    __data_permission__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    org_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dept_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class _TenantRow(_RowColumns, _FixtureBase):
    __tablename__ = "bulk_admission_tenant_rows"

    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class _OwnerRow(_RowColumns, _FixtureBase):
    __tablename__ = "bulk_admission_owner_rows"

    owner_tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # This independent field is deliberately not a synonym of owner_tenant_id.
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class _AliasRow(_RowColumns, _FixtureBase):
    __tablename__ = "bulk_admission_alias_rows"

    owner_tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenant_id = synonym("owner_tenant_id")


class _TenantRows(TenantRepository):
    model = _TenantRow


class _OwnerRows(TenantRepository):
    model = _OwnerRow


class _AliasRows(TenantRepository):
    model = _AliasRow


class _GlobalRows(BaseRepository):
    model = _TenantRow


@pytest_asyncio.fixture
async def database():
    url = make_url(_DSN)
    assert (url.drivername, url.host, url.database, url.username) == (
        "postgresql+asyncpg",
        "127.0.0.1",
        "org_tree_audit",
        "audit_fixture",
    )
    schema = "tenant_bulk_" + uuid4().hex
    control = create_async_engine(url)
    context_token = data_permission_ctx.set(None)
    async with control.begin() as connection:
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"search_path": schema, "statement_timeout": "10000"}
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_FixtureBase.metadata.create_all)
        yield factory
    finally:
        data_permission_ctx.reset(context_token)
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()


async def _stored_ownership(database, model):
    owner = (
        model.owner_tenant_id if hasattr(model, "owner_tenant_id") else model.tenant_id
    )
    async with database() as reader:
        return list(
            (
                await reader.execute(select(model.label, owner).order_by(model.id))
            ).tuples()
        )


def _assert_inputs_unchanged(inputs, before, identities):
    assert inputs == before
    assert [id(row) for row in inputs] == identities


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
@pytest.mark.parametrize(
    "claims", [(88,), (None,), ("omitted",), (88, None, "omitted", 77)]
)
async def test_bulk_ownership_is_always_the_repository_tenant(
    database, repo_type, claims
):
    field = "owner_tenant_id" if repo_type is _OwnerRows else "tenant_id"
    inputs = []
    for index, claim in enumerate(claims):
        row = {"label": f"row-{index}"}
        if repo_type is _OwnerRows:
            row["tenant_id"] = 501  # An unrelated business field must survive.
        if claim != "omitted":
            row[field] = claim
        inputs.append(row)
    before, identities = deepcopy(inputs), [id(row) for row in inputs]
    async with database() as writer:
        created = await repo_type(writer, _TENANT).create_many(inputs)
        assert [getattr(row, field) for row in created] == [_TENANT] * len(inputs)
        if repo_type is _OwnerRows:
            assert [row.tenant_id for row in created] == [501] * len(inputs)
        _assert_inputs_unchanged(inputs, before, identities)
        await writer.commit()
        ids = [row.id for row in created]
    assert await _stored_ownership(database, repo_type.model) == [
        (f"row-{index}", _TENANT) for index in range(len(inputs))
    ]
    async with database() as reader:
        own, other = repo_type(reader, _TENANT), repo_type(reader, _OTHER_TENANT)
        for row_id in ids:
            assert (await own.get_by_id(row_id)).id == row_id
            assert await other.get_by_id(row_id) is None


_ALIAS_CLAIMS = [
    [("owner_tenant_id", 88), ("tenant_id", 99)],
    [("tenant_id", 99), ("owner_tenant_id", 88)],
    [("owner_tenant_id", None), ("tenant_id", 99)],
    [("tenant_id", None), ("owner_tenant_id", 88)],
    [("tenant_id", 99)],
    [("owner_tenant_id", None)],
    [],
]


@pytest.mark.parametrize("operation", ["create", "create_many"])
@pytest.mark.parametrize("claims", _ALIAS_CLAIMS)
async def test_mapped_tenant_synonym_cannot_override_repository_ownership(
    database, operation, claims
):
    inputs = [dict([("label", "alias-row"), *claims])]
    before, identities = deepcopy(inputs), [id(row) for row in inputs]
    async with database() as writer:
        repo = _AliasRows(writer, _TENANT)
        created = (
            [await repo.create(inputs[0])]
            if operation == "create"
            else await repo.create_many(inputs)
        )
        assert [(row.owner_tenant_id, row.tenant_id) for row in created] == [
            (_TENANT, _TENANT)
        ]
        _assert_inputs_unchanged(inputs, before, identities)
        await writer.commit()
    assert await _stored_ownership(database, _AliasRow) == [("alias-row", _TENANT)]


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows, _AliasRows])
@pytest.mark.parametrize("operation", ["create", "create_many"])
async def test_explicit_platform_scope_keeps_single_create_none_semantics(
    database, repo_type, operation
):
    field = "owner_tenant_id" if repo_type is not _TenantRows else "tenant_id"
    row = {"label": "platform-row", field: 88}
    if repo_type is _OwnerRows:
        row["tenant_id"] = 501
    if repo_type is _AliasRows:
        row["tenant_id"] = 99
    inputs = [row]
    before, identities = deepcopy(inputs), [id(item) for item in inputs]
    async with database() as writer:
        repo = repo_type(writer, None)
        created = (
            [await repo.create(inputs[0])]
            if operation == "create"
            else await repo.create_many(inputs)
        )
        assert [getattr(item, field) for item in created] == [None]
        if repo_type is _OwnerRows:
            assert created[0].tenant_id == 501
        _assert_inputs_unchanged(inputs, before, identities)
        await writer.commit()
    assert await _stored_ownership(database, repo_type.model) == [
        ("platform-row", None)
    ]


async def test_bulk_preserves_data_permission_defaults_and_explicit_values(database):
    inputs = [
        {"label": "defaults", "tenant_id": 88},
        {
            "label": "explicit",
            "tenant_id": 88,
            "created_by": 401,
            "org_node_id": 402,
            "dept_id": 403,
        },
        {
            "label": "nulls",
            "tenant_id": None,
            "created_by": None,
            "org_node_id": None,
            "dept_id": None,
        },
    ]
    before, identities = deepcopy(inputs), [id(row) for row in inputs]
    context_token = data_permission_ctx.set(
        {"current_user_id": 301, "primary_org_id": 302, "primary_department_id": 303}
    )
    try:
        async with database() as writer:
            await _TenantRows(writer, _TENANT).create_many(inputs)
            _assert_inputs_unchanged(inputs, before, identities)
            await writer.commit()
    finally:
        data_permission_ctx.reset(context_token)
    async with database() as reader:
        stored = list(
            (
                await reader.execute(
                    select(
                        _TenantRow.label,
                        _TenantRow.tenant_id,
                        _TenantRow.created_by,
                        _TenantRow.org_node_id,
                        _TenantRow.dept_id,
                    ).order_by(_TenantRow.id)
                )
            ).tuples()
        )
    assert stored == [
        ("defaults", _TENANT, 301, 302, 303),
        ("explicit", _TENANT, 401, 402, 403),
        ("nulls", _TENANT, None, None, None),
    ]


async def test_bulk_flush_remains_in_the_callers_rollback_transaction(database):
    async with database() as writer:
        created = await _TenantRows(writer, _TENANT).create_many(
            [{"label": "rollback-a", "tenant_id": 88}, {"label": "rollback-b"}]
        )
        created_ids = [row.id for row in created]
        assert writer.in_transaction()
        assert await _stored_ownership(database, _TenantRow) == []
        local = list(
            (
                await writer.execute(
                    select(_TenantRow.label, _TenantRow.tenant_id).order_by(
                        _TenantRow.id
                    )
                )
            ).tuples()
        )
        await writer.rollback()
        assert not writer.in_transaction()
    assert await _stored_ownership(database, _TenantRow) == []
    assert local == [("rollback-a", _TENANT), ("rollback-b", _TENANT)]
    assert len(set(created_ids)) == 2


async def test_unscoped_base_repository_retains_caller_ownership(database):
    inputs = [
        {"label": "foreign", "tenant_id": 88},
        {"label": "null", "tenant_id": None},
        {"label": "missing"},
    ]
    before, identities = deepcopy(inputs), [id(row) for row in inputs]
    async with database() as writer:
        await _GlobalRows(writer).create_many(inputs)
        _assert_inputs_unchanged(inputs, before, identities)
        await writer.commit()
    assert await _stored_ownership(database, _TenantRow) == [
        ("foreign", 88),
        ("null", None),
        ("missing", None),
    ]


async def test_empty_bulk_does_not_start_a_transaction(database):
    async with database() as writer:
        assert not writer.in_transaction()
        assert await _TenantRows(writer, _TENANT).create_many([]) == []
        assert not writer.in_transaction()
    assert await _stored_ownership(database, _TenantRow) == []


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
async def test_single_create_owns_row_without_mutating_independent_fields(
    database, repo_type
):
    field = "owner_tenant_id" if repo_type is _OwnerRows else "tenant_id"
    inputs = [{"label": "single-row", field: _OTHER_TENANT}]
    if repo_type is _OwnerRows:
        inputs[0]["tenant_id"] = 501
    before, identities = deepcopy(inputs), [id(row) for row in inputs]
    async with database() as writer:
        created = await repo_type(writer, _TENANT).create(inputs[0])
        assert getattr(created, field) == _TENANT
        if repo_type is _OwnerRows:
            assert created.tenant_id == 501
        _assert_inputs_unchanged(inputs, before, identities)
        await writer.commit()
    assert await _stored_ownership(database, repo_type.model) == [
        ("single-row", _TENANT)
    ]
    if repo_type is _OwnerRows:
        async with database() as reader:
            assert list(
                (
                    await reader.execute(
                        select(_OwnerRow.owner_tenant_id, _OwnerRow.tenant_id)
                    )
                ).tuples()
            ) == [(_TENANT, 501)]
