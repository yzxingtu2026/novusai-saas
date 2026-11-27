"""Behavioral: real role repositories and PostgreSQL uniqueness boundaries.

Only fictional rows in an explicitly selected, owned loopback fixture are used.
The tests never access production, authenticate users or dispatch business work.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

import app.models  # noqa: F401 -- register actual relationship targets.
from app.core.base_model import Base
from app.enums import ErrorCode
from app.exceptions import BusinessException
from app.models.auth.permission import Permission
from app.models.auth.tenant_admin_role import TenantAdminRole
from app.models.auth.tenant_user_role import TenantUserRole
from app.models.tenant.tenant_admin import TenantAdmin
from app.models.tenant.tenant_user import TenantUser
from app.repositories.tenant.tenant_permission_role_repository import (
    TenantPermissionRoleRepository,
)
from app.repositories.tenant.tenant_user_role_repository import TenantUserRoleRepository

pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("TENANT_ROLE_POSTGRES_DSN")
_INDEXES = {
    "user": ("uq_tenant_user_roles_live_name", "uq_tenant_user_roles_live_code"),
    "admin": ("uq_tenant_admin_roles_flat_name", "uq_tenant_admin_roles_flat_code"),
}


def _tables():
    selected = {
        model.__table__
        for model in (
            Permission,
            TenantAdminRole,
            TenantUserRole,
            TenantAdmin,
            TenantUser,
        )
    }
    selected |= {
        table
        for table in Base.metadata.tables.values()
        if table.name
        in {"tenant_admin_role_permissions", "tenant_user_role_permissions"}
    }
    while True:
        dependencies = {
            key.column.table for table in selected for key in table.foreign_keys
        }
        if dependencies <= selected:
            return list(selected)
        selected |= dependencies


@pytest_asyncio.fixture
async def database():
    if not _DSN:
        pytest.skip("Explicit owned TENANT_ROLE_POSTGRES_DSN is required")
    try:
        url = make_url(_DSN)
    except Exception:
        pytest.fail("Invalid disposable PostgreSQL DSN (value withheld)", pytrace=False)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "::1"}
        or not (url.database and url.username)
        or url.query
    ):
        pytest.fail("Role checks require disposable loopback PostgreSQL", pytrace=False)
    schema = "role_unique_" + uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as db:
        assert await db.scalar(text("SELECT current_database()")) == url.database
        assert (
            await db.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
            is False
        )
        await db.execute(CreateSchema(schema))
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"search_path": schema, "statement_timeout": "10000"}
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with engine.begin() as db:
            await db.run_sync(
                lambda connection: Base.metadata.create_all(
                    connection, tables=_tables()
                )
            )
        yield factory
    finally:
        await engine.dispose()
        async with control.begin() as db:
            await db.execute(DropSchema(schema, cascade=True))
        await control.dispose()


def _model(domain):
    return TenantUserRole if domain == "user" else TenantAdminRole


def _repo(db, domain, tenant_id=77):
    cls = (
        TenantUserRoleRepository if domain == "user" else TenantPermissionRoleRepository
    )
    return cls(db, tenant_id)


async def _wait_for_conflict_or_completion(factory, task, pid):
    """Observe a real PostgreSQL wait; a missing constraint completes too early."""
    async with factory() as observer:
        for _ in range(100):
            if task.done():
                return
            row = (
                await observer.execute(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE pid=:pid AND cardinality(pg_blocking_pids(pid)) > 0"
                    ),
                    {"pid": pid},
                )
            ).one_or_none()
            if row and row[0] == "Lock":
                return
            await asyncio.sleep(0.02)
    raise AssertionError(
        "Second role write neither completed nor waited for unique key"
    )


@pytest.mark.parametrize("domain", ["user", "admin"])
@pytest.mark.parametrize("field", ["name", "code"])
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_concurrent_writes_reject_duplicate_after_both_preflights_pass(
    database, domain, field, operation
):
    if operation == "update":
        async with database() as seed:
            for number in (1, 2):
                await _repo(seed, domain).create(
                    {
                        "id": number,
                        "name": f"original-{number}",
                        "code": f"code-{number}",
                    }
                )
            await seed.commit()
    first, second = database(), database()
    task = None
    try:
        repo_a, repo_b = _repo(first, domain), _repo(second, domain)
        check = "name_exists" if field == "name" else "code_exists"
        assert await getattr(repo_a, check)("shared-value") is False
        assert await getattr(repo_b, check)("shared-value") is False
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))

        async def write(repo, number):
            if operation == "update":
                return await repo.update(number, {field: "shared-value"})
            values = {"name": f"new-{number}", "code": f"new-code-{number}"}
            values[field] = "shared-value"
            return await repo.create(values)

        await write(repo_a, 1)

        async def contender():
            try:
                await write(repo_b, 2)
                await second.commit()
                return "committed"
            except BusinessException as error:
                assert second.is_active is False
                await second.rollback()
                return error.code

        task = asyncio.create_task(contender())
        await _wait_for_conflict_or_completion(database, task, second_pid)
        await first.commit()
        assert await asyncio.wait_for(task, 5) == ErrorCode.DUPLICATE_ENTRY
        async with database() as check_db:
            model = _model(domain)
            count = await check_db.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == 77,
                    model.is_deleted.is_(False),
                    getattr(model, field) == "shared-value",
                )
            )
            assert count == 1
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await first.rollback()
        await second.rollback()
        await first.close()
        await second.close()


@pytest.mark.parametrize("domain", ["user", "admin"])
async def test_direct_database_write_is_constrained_and_tenants_are_independent(
    database, domain
):
    model = _model(domain)
    async with database() as db:
        for tenant_id in (77, 88):
            await _repo(db, domain, tenant_id).create({"name": "same", "code": "same"})
        await db.commit()
    async with database() as db:
        db.add(model(tenant_id=77, name="same", code="different"))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(model)) == 2


@pytest.mark.parametrize("domain", ["user", "admin"])
@pytest.mark.parametrize("field", ["name", "code"])
async def test_soft_delete_allows_reuse_but_restore_reports_safe_conflict(
    database, domain, field
):
    async with database() as db:
        repo = _repo(db, domain)
        old = await repo.create({"name": "old", "code": "old"})
        old_id = old.id
        assert await repo.delete(old_id) is True
        values = {"name": "replacement", "code": "replacement"}
        values[field] = "old"
        replacement = await repo.create(values)
        replacement_id = replacement.id
        await db.commit()
    async with database() as db:
        with pytest.raises(BusinessException) as caught:
            await _repo(db, domain).restore_by_id(old_id)
        assert caught.value.code == ErrorCode.DUPLICATE_ENTRY
        assert db.is_active is False
        await db.rollback()
        old = await _repo(db, domain).get_by_id(old_id, include_deleted=True)
        replacement = await _repo(db, domain).get_by_id(replacement_id)
        assert (old.is_deleted, replacement.is_deleted) == (True, False)


async def test_flat_permission_role_uniqueness_does_not_restrict_org_nodes(database):
    async with database() as db:
        parent = await _repo(db, "admin").create({"name": "same", "code": "same"})
        db.add(
            TenantAdmin(
                id=500,
                tenant_id=77,
                username="fictional-leader",
                email="role-leader@example.invalid",
                password_hash="fictional-test-value",
            )
        )
        await db.flush()
        db.add_all(
            [
                TenantAdminRole(
                    tenant_id=77, name="same", code="same", type="department"
                ),
                TenantAdminRole(tenant_id=77, name="same", code="same", path="/999/"),
                TenantAdminRole(tenant_id=77, name="same", code="same", level=2),
                TenantAdminRole(
                    tenant_id=77, name="same", code="same", parent_id=parent.id
                ),
                TenantAdminRole(tenant_id=77, name="same", code="same", leader_id=500),
            ]
        )
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(TenantAdminRole)) == 6


@pytest.mark.parametrize("domain", ["user", "admin"])
async def test_existing_legacy_duplicates_do_not_break_boolean_preflight(
    database, domain
):
    model = _model(domain)
    async with database() as db:
        model_indexes = {index.name: index for index in model.__table__.indexes}
        connection = await db.connection()
        for name in _INDEXES[domain]:
            await connection.run_sync(
                lambda sync, idx=model_indexes[name]: idx.drop(sync)
            )
        db.add_all(
            [
                model(tenant_id=77, name="duplicate", code="duplicate"),
                model(tenant_id=77, name="duplicate", code="duplicate"),
            ]
        )
        await db.commit()
        repo = _repo(db, domain)
        assert await repo.name_exists("duplicate") is True
        assert await repo.code_exists("duplicate") is True
        assert await repo.name_exists("absent") is False
        assert await repo.code_exists("absent") is False


@pytest.mark.parametrize("domain", ["user", "admin"])
@pytest.mark.parametrize("operation", ["create_many", "update_many", "batch_restore"])
async def test_bulk_writes_use_the_same_safe_conflict_boundary(
    database, domain, operation
):
    async with database() as db:
        repo = _repo(db, domain)
        existing = await repo.create({"name": "taken", "code": "taken"})
        other = await repo.create({"name": "other", "code": "other"})
        other_id = other.id
        if operation == "batch_restore":
            assert await repo.delete(other_id) is True
            # The conflicting old identity is legal only while this row is deleted.
            other.name = existing.name
            await db.flush()
        await db.commit()
    async with database() as db:
        repo = _repo(db, domain)
        with pytest.raises(BusinessException) as caught:
            if operation == "create_many":
                await repo.create_many(
                    [{"tenant_id": 77, "name": "taken", "code": "x"}]
                )
            elif operation == "update_many":
                await repo.update_many([other_id], {"name": "taken"})
            else:
                await repo.batch_restore([other_id])
        assert caught.value.code == ErrorCode.DUPLICATE_ENTRY
        await db.rollback()
        model = _model(domain)
        assert await db.scalar(select(func.count()).select_from(model)) == 2


@pytest.mark.parametrize("domain", ["user", "admin"])
async def test_unrelated_integrity_errors_are_not_reported_as_role_duplicates(
    database, domain
):
    async with database() as db:
        repo = _repo(db, domain)
        await repo.create({"id": 50, "name": "original", "code": "original"})
        await db.commit()
    async with database() as db:
        repo = _repo(db, domain)
        with pytest.raises(IntegrityError):
            await repo.create({"id": 50, "name": "different", "code": "different"})
        await db.rollback()
        assert (await repo.get_by_id(50)).name == "original"


@pytest.mark.parametrize("domain", ["user", "admin"])
async def test_restore_retains_tenant_scope_and_recycle_filters(database, domain):
    async with database() as db:
        repo = _repo(db, domain)
        old = await repo.create({"name": "old", "code": "old"})
        old_id = old.id
        assert await repo.delete(old_id) is True
        await db.commit()
        assert await _repo(db, domain, tenant_id=88).restore_by_id(old_id) is None
        assert await repo.restore_by_id(old_id, delete_level="mismatch") is None
        assert await repo.restore_by_id(old_id, recycle_stage="mismatch") is None
        assert await _repo(db, domain, tenant_id=88).batch_restore([old_id]) == 0
        assert await repo.batch_restore([old_id]) == 1
        await db.commit()
        restored = await repo.get_by_id(old_id)
        assert (restored.tenant_id, restored.is_deleted, restored.name) == (
            77,
            False,
            "old",
        )


@pytest.mark.parametrize("domain", ["user", "admin"])
async def test_pending_other_role_constraint_is_not_misattributed(database, domain):
    other = "admin" if domain == "user" else "user"
    other_model = _model(other)
    async with database() as db:
        await _repo(db, other).create({"name": "other-original", "code": "original"})
        await db.commit()
        db.add(other_model(tenant_id=77, name="other-original", code="duplicate"))
        with pytest.raises(IntegrityError):
            await _repo(db, domain).create({"name": "unrelated", "code": "unrelated"})
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(_model(domain))) == 0
        assert await db.scalar(select(func.count()).select_from(other_model)) == 1


def _migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/platform_role_unique_v1.py"
    )
    spec = importlib.util.spec_from_file_location("fixture_role_unique_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _without_indexes(db):
    connection = await db.connection()
    for model in (TenantUserRole, TenantAdminRole):
        for index in model.__table__.indexes:
            if index.name in {*_INDEXES["user"], *_INDEXES["admin"]}:
                await connection.run_sync(lambda sync, idx=index: idx.drop(sync))


async def _run_migration(db, method):
    module = _migration()
    connection = await db.connection()

    def invoke(sync):
        module.op = Operations(MigrationContext.configure(sync))
        getattr(module, method)()

    await connection.run_sync(invoke)


async def _index_names(db):
    return set(
        await db.scalars(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() "
                "AND indexname IN ('uq_tenant_user_roles_live_name', "
                "'uq_tenant_user_roles_live_code', 'uq_tenant_admin_roles_flat_name', "
                "'uq_tenant_admin_roles_flat_code')"
            )
        )
    )


async def test_actual_migration_upgrade_downgrade_and_reupgrade_preserve_rows(database):
    async with database() as db:
        for domain in ("user", "admin"):
            await _repo(db, domain).create({"name": "unchanged", "code": "unchanged"})
        await _without_indexes(db)
        await db.commit()
        await db.execute(text("SET LOCAL lock_timeout = '120ms'"))
        await _run_migration(db, "upgrade")
        assert await db.scalar(text("SHOW lock_timeout")) == "120ms"
        assert await _index_names(db) == {*_INDEXES["user"], *_INDEXES["admin"]}
        await db.commit()
        for model in (TenantUserRole, TenantAdminRole):
            assert (await db.execute(select(model.name, model.code))).all() == [
                ("unchanged", "unchanged")
            ]
        await _run_migration(db, "downgrade")
        assert await _index_names(db) == set()
        await db.commit()
        await _run_migration(db, "upgrade")
        assert await _index_names(db) == {*_INDEXES["user"], *_INDEXES["admin"]}
        await db.commit()


@pytest.mark.parametrize("domain", ["user", "admin"])
@pytest.mark.parametrize("field", ["name", "code"])
async def test_migration_refuses_historical_duplicates_before_any_index_ddl(
    database, domain, field
):
    model = _model(domain)
    async with database() as db:
        await _without_indexes(db)
        for number in (1, 2):
            values = {
                "tenant_id": 77,
                "name": f"name-{number}",
                "code": f"code-{number}",
            }
            values[field] = "private-duplicate-value"
            db.add(model(**values))
        await db.commit()
        before = (
            await db.execute(select(model.id, model.name, model.code, model.is_deleted))
        ).all()
        with pytest.raises(
            RuntimeError, match="^tenant_role_unique_historical_conflict$"
        ):
            await _run_migration(db, "upgrade")
        assert await _index_names(db) == set()
        await db.rollback()
        after = (
            await db.execute(select(model.id, model.name, model.code, model.is_deleted))
        ).all()
        assert before == after
        assert await _index_names(db) == set()


async def test_migration_ignores_deleted_and_nonflat_role_duplicates(database):
    async with database() as db:
        await _without_indexes(db)
        for model in (TenantUserRole, TenantAdminRole):
            for _number in (1, 2):
                db.add(
                    model(tenant_id=77, name="deleted", code="deleted", is_deleted=True)
                )
        for _number in (1, 2):
            db.add(
                TenantAdminRole(tenant_id=77, name="org", code="org", type="department")
            )
        await db.commit()
        await _run_migration(db, "upgrade")
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(TenantUserRole)) == 2
        assert await db.scalar(select(func.count()).select_from(TenantAdminRole)) == 4
        assert await _index_names(db) == {*_INDEXES["user"], *_INDEXES["admin"]}


async def test_migration_lock_wait_preserves_shorter_timeout_and_has_safe_reason(
    database,
):
    async with database() as seed:
        await _without_indexes(seed)
        await seed.commit()
    blocker, migrator = database(), database()
    try:
        await blocker.execute(
            text("LOCK TABLE tenant_user_roles IN ROW EXCLUSIVE MODE")
        )
        await migrator.execute(text("SET LOCAL lock_timeout = '80ms'"))
        with pytest.raises(RuntimeError, match="^tenant_role_unique_lock_unavailable$"):
            await _run_migration(migrator, "upgrade")
        await migrator.rollback()
        assert await _index_names(migrator) == set()
    finally:
        await migrator.rollback()
        await blocker.rollback()
        await migrator.close()
        await blocker.close()


async def test_migration_denies_sqlalchemy_logical_transaction_under_dbapi_autocommit(
    database,
):
    engine = database.kw["bind"]
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.exec_driver_sql("SELECT 1")

        def attempt(sync):
            assert sync.in_transaction() is True
            assert sync.connection.dbapi_connection.autocommit is True
            module = _migration()
            module.op = Operations(MigrationContext.configure(sync))
            with pytest.raises(
                RuntimeError,
                match="^tenant_role_unique_postgresql_transaction_required$",
            ):
                module.upgrade()

        await connection.run_sync(attempt)
    async with database() as db:
        assert await _index_names(db) == {*_INDEXES["user"], *_INDEXES["admin"]}
