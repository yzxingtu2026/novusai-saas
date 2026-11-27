"""Behavioral: actual tenant sorting routes/repositories and owned PostgreSQL.

Only fictional rows in an explicit loopback fixture are changed. Authentication
and exact route permission are fixture inputs; production auth is not exercised.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import Boolean, Integer, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateSchema, DropSchema

import app.models  # noqa: F401 -- register actual relationship targets.
from app.api.tenant.organization import TenantOrganizationController
from app.core.base_model import Base
from app.core.base_repository import BaseRepository, TenantRepository
from app.core.data_permission import data_permission_ctx
from app.core.deps import get_current_active_tenant_admin, get_db
from app.models.org.tenant_org_node import TenantOrgNode

_DSN = os.environ.get("TENANT_SORTING_POSTGRES_DSN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _DSN, reason="Disposable PostgreSQL fixture required"),
]

_TENANT = 77
_OTHER_TENANT = 88


class _FixtureBase(DeclarativeBase):
    pass


class _SortColumns:
    __data_permission__ = True
    __sortable__ = {
        "field": "sort_order",
        "step": 1000,
        "scope_fields": ["tenant_id", "parent_id"],
    }

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class _TenantRow(_SortColumns, _FixtureBase):
    __tablename__ = "sorting_tenant_rows"

    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class _OwnerRow(_SortColumns, _FixtureBase):
    __tablename__ = "sorting_owner_rows"
    __sortable__ = {
        "field": "sort_order",
        "step": 1000,
        "scope_fields": ["owner_tenant_id", "parent_id"],
    }

    owner_tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Independent business field, deliberately different from canonical ownership.
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, default=501)


class _TenantRows(TenantRepository):
    model = _TenantRow


class _OwnerRows(TenantRepository):
    model = _OwnerRow


class _GlobalRows(BaseRepository):
    model = _TenantRow


def _actual_tables():
    selected = {TenantOrgNode.__table__}
    while True:
        dependencies = {
            key.column.table for table in selected for key in table.foreign_keys
        }
        if dependencies <= selected:
            return list(selected)
        selected |= dependencies


@pytest_asyncio.fixture
async def database():
    url = make_url(_DSN)
    assert (url.drivername, url.host, url.database, url.username) == (
        "postgresql+asyncpg",
        "127.0.0.1",
        "org_tree_audit",
        "audit_fixture",
    )
    schema = "tenant_sorting_" + uuid4().hex
    control = create_async_engine(url)
    token = data_permission_ctx.set(None)
    async with control.begin() as connection:
        role = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            )
        ).one()
        assert tuple(role) == (False, False)
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
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=_actual_tables())
            )
        async with factory() as writer:
            for model in (_TenantRow, _OwnerRow):
                owner_field = "owner_tenant_id" if model is _OwnerRow else "tenant_id"
                for row_id, owner, parent, rank, creator, deleted in (
                    (1, 77, None, 111, 301, False),
                    (2, 77, None, 222, 302, False),
                    (3, 77, 10, 333, 301, False),
                    (4, 88, None, 9000, 301, False),
                    (5, None, None, 8000, 301, False),
                    (6, 77, None, 7000, 301, True),
                    (7, 77, 20, 444, 302, False),
                    (8, 77, 10, 555, 301, False),
                ):
                    writer.add(
                        model(
                            id=row_id,
                            **{owner_field: owner},
                            parent_id=parent,
                            sort_order=rank,
                            created_by=creator,
                            is_deleted=deleted,
                        )
                    )
            for node_id, owner, parent, rank in (
                (10, 77, None, 101),
                (11, 77, None, 202),
                (80, 88, None, 8080),
            ):
                writer.add(
                    TenantOrgNode(
                        id=node_id,
                        tenant_id=owner,
                        parent_id=parent,
                        sort_order=rank,
                        name=f"fictional-node-{node_id}",
                        code=f"fixture-{node_id}",
                    )
                )
            await writer.flush()
            writer.add(
                TenantOrgNode(
                    id=12,
                    tenant_id=77,
                    parent_id=10,
                    sort_order=303,
                    name="fictional-child",
                    code="fixture-12",
                )
            )
            await writer.commit()
        yield factory
    finally:
        data_permission_ctx.reset(token)
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()


async def _ranks(database, model=_TenantRow):
    """Read committed state with an independent session, including deleted rows."""
    async with database() as reader:
        return dict((await reader.execute(select(model.id, model.sort_order))).all())


@asynccontextmanager
async def _owner_client(database):
    app = FastAPI()
    app.include_router(TenantOrganizationController.get_router(), prefix="/tenant")

    async def db_fixture():
        async with database() as session:
            yield session

    async def identity_fixture():
        return SimpleNamespace(id=500, tenant_id=77, is_active=True, is_owner=True)

    @app.middleware("http")
    async def permission_fixture(request: Request, call_next):
        request.state.user_permissions = (
            set()
            if request.headers.get("X-Fixture-Permission") == "deny"
            else {"organization:reorder"}
        )
        return await call_next(request)

    app.dependency_overrides[get_db] = db_fixture
    app.dependency_overrides[get_current_active_tenant_admin] = identity_fixture
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.local"
    ) as client:
        yield client


@pytest.mark.parametrize(
    "ids,expected_count,own_rank", [([80], 0, 101), ([10, 80, 11], 2, 1000)]
)
async def test_owner_reorder_http_cannot_write_another_tenant(
    database, ids, expected_count, own_rank
):
    async with _owner_client(database) as client:
        response = await client.put(
            "/tenant/organization/reorder", json={"ids": ids, "parent_id": None}
        )
    assert response.status_code == 200
    assert response.json()["data"] == {"updated_count": expected_count}
    expected = {10: own_rank, 11: 3000 if expected_count else 202, 12: 303, 80: 8080}
    assert await _ranks(database, TenantOrgNode) == expected


async def test_owner_reorder_http_honors_sibling_scope_and_exact_permission(database):
    async with _owner_client(database) as client:
        denied = await client.put(
            "/tenant/organization/reorder",
            json={"ids": [10]},
            headers={"X-Fixture-Permission": "deny"},
        )
        assert denied.status_code == 403
        assert await _ranks(database, TenantOrgNode) == {
            10: 101,
            11: 202,
            12: 303,
            80: 8080,
        }
        allowed = await client.put(
            "/tenant/organization/reorder",
            json={"ids": [12, 11, 10], "parent_id": None, "tenant_id": 88},
        )
    assert allowed.status_code == 200
    assert allowed.json()["data"] == {"updated_count": 2}
    assert await _ranks(database, TenantOrgNode) == {
        10: 3000,
        11: 2000,
        12: 303,
        80: 8080,
    }


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
async def test_mixed_sort_ids_filter_foreign_platform_deleted_and_missing(
    database, repo_type
):
    before = await _ranks(database, repo_type.model)
    async with database() as writer:
        count = await repo_type(writer, 77).batch_update_sort_order(
            [4, 2, 5, 1, 6, 999]
        )
        await writer.commit()
    assert count == 2
    assert await _ranks(database, repo_type.model) == {**before, 2: 2000, 1: 4000}


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
@pytest.mark.parametrize(
    "parent,expected", [(None, {1: 3000, 2: 2000}), (10, {3: 1000, 8: 4000})]
)
async def test_explicit_parent_scope_preserves_other_siblings(
    database, repo_type, parent, expected
):
    before = await _ranks(database, repo_type.model)
    async with database() as writer:
        count = await repo_type(writer, 77).batch_update_sort_order(
            [3, 2, 1, 8], parent_id=parent
        )
        await writer.commit()
    assert count == 2
    assert await _ranks(database, repo_type.model) == {**before, **expected}


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
async def test_caller_tenant_filter_cannot_replace_repository_ownership(
    database, repo_type
):
    before = await _ranks(database, repo_type.model)
    field = "owner_tenant_id" if repo_type is _OwnerRows else "tenant_id"
    async with database() as writer:
        count = await repo_type(writer, 77).batch_update_sort_order(
            [4, 1], **{field: 88}
        )
        await writer.commit()
    assert count == 1
    assert await _ranks(database, repo_type.model) == {**before, 1: 2000}


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_sorting_write_and_next_rank_share_data_permission_scope(
    database, repo_type, operation
):
    before = await _ranks(database, repo_type.model)
    token = data_permission_ctx.set({"scope_mode": "self", "current_user_id": 301})
    try:
        async with database() as writer:
            repo = repo_type(writer, 77)
            if operation == "read":
                assert await repo.get_next_sort_order(parent_id=None) == 1111
            else:
                count = await repo.batch_update_sort_order([2, 4, 3, 1], parent_id=None)
                await writer.commit()
    finally:
        data_permission_ctx.reset(token)
    if operation == "read":
        assert await _ranks(database, repo_type.model) == before
    else:
        assert count == 1
        assert await _ranks(database, repo_type.model) == {**before, 1: 4000}


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
@pytest.mark.parametrize(
    "filters,expected",
    [({}, 1555), ({"parent_id": None}, 1222), ({"parent_id": 10}, 1555)],
)
async def test_next_rank_ignores_other_tenants_and_deleted_rows(
    database, repo_type, filters, expected
):
    async with database() as reader:
        assert await repo_type(reader, 77).get_next_sort_order(**filters) == expected


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows])
async def test_platform_none_scope_is_exact_and_does_not_become_global(
    database, repo_type
):
    before = await _ranks(database, repo_type.model)
    async with database() as writer:
        repo = repo_type(writer, None)
        assert await repo.get_next_sort_order() == 9000
        count = await repo.batch_update_sort_order([4, 1, 5])
        await writer.commit()
    assert count == 1
    assert await _ranks(database, repo_type.model) == {**before, 5: 3000}


async def test_base_repository_remains_global_but_honors_declared_parent_scope(
    database,
):
    before = await _ranks(database)
    async with database() as writer:
        repo = _GlobalRows(writer)
        assert await repo.get_next_sort_order() == 10000
        count = await repo.batch_update_sort_order([4, 1, 3, 5, 6], parent_id=None)
        await writer.commit()
    assert count == 3
    assert await _ranks(database) == {**before, 4: 1000, 1: 2000, 5: 4000}


@pytest.mark.parametrize("repo_type", [_TenantRows, _OwnerRows, _GlobalRows])
async def test_valid_sorting_empty_ids_duplicate_rank_and_rollback_semantics(
    database, repo_type
):
    before = await _ranks(database, repo_type.model)
    async with database() as writer:
        repo = repo_type(writer) if repo_type is _GlobalRows else repo_type(writer, 77)
        assert await repo.batch_update_sort_order([]) == 0
        # The existing CASE mapping uses the last duplicate's original position.
        assert await repo.batch_update_sort_order([2, 1, 2], parent_id=None) == 2
        persisted_in_transaction = dict(
            (await writer.execute(select(repo.model.id, repo.model.sort_order))).all()
        )
        assert persisted_in_transaction == {**before, 1: 2000, 2: 3000}
        await writer.rollback()
    assert await _ranks(database, repo_type.model) == before
