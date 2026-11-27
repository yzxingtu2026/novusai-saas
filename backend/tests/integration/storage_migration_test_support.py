"""Isolated Storage Migration PostgreSQL and filesystem fixtures / 存储迁移隔离夹具。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema


@pytest_asyncio.fixture
async def owned_storage(tmp_path):
    platform_dsn = os.getenv("PLATFORM_FIXTURE_POSTGRES_DSN")
    dsn = platform_dsn or os.getenv("NOVUSAI_STORAGE_MIGRATION_TEST_DSN")
    if not dsn:
        pytest.skip("Disposable storage-migration PostgreSQL fixture required")
    url = make_url(dsn)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host in {"127.0.0.1", "::1"}
    if platform_dsn:
        assert url.host == "127.0.0.1" and url.port is not None
        assert url.database in {"platform_storage", "platform_tasks"} and not url.query
    else:
        assert url.database and url.database.startswith("plugin_delivery_")
    schema = f"storage_migration_{uuid4().hex}"
    control = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    root = tmp_path / "fictional-storage"
    root.mkdir()
    try:
        async with control.begin() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT NOT rolsuper FROM pg_roles WHERE rolname = current_user"
                    )
                )
                is True
            )
            await conn.execute(CreateSchema(schema))
        yield SimpleNamespace(
            factory=async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
            root=root,
            schema=schema,
            engine=engine,
        )
    finally:
        await engine.dispose()
        async with control.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        await control.dispose()


async def create_model_tables(env, *models):
    tables = {model.__table__ for model in models}
    while True:
        required = {fk.column.table for table in tables for fk in table.foreign_keys}
        if required <= tables:
            break
        tables.update(required)
    async with env.engine.begin() as conn:
        await conn.run_sync(
            lambda bind: models[0].metadata.create_all(bind, tables=tables)
        )
