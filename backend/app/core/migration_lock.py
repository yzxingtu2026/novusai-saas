"""Use one PostgreSQL session lock across the migration entrypoints."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def acquire_schema_migration_lock(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    # A session lock survives the commits Alembic makes while the NullPool
    # connection is open. Closing that connection releases the lock on any exit.
    connection.exec_driver_sql("SET LOCAL lock_timeout = '120s'")
    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": 8463729150381})
    connection.commit()
