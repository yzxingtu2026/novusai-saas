"""中文: 每次 Celery 调用独占异步引擎。 EN: A fresh async engine for every notification task invocation."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def run_notification_task(work):
    """中文: 不复用已关闭事件循环的连接池。 EN: Never reuse a connection pool from a closed event loop."""

    async def run():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            sessions = async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            )
            return await work(sessions)
        finally:
            try:
                await engine.dispose()
            except Exception:
                logger.warning("Notification task engine cleanup unavailable")

    return asyncio.run(run())
