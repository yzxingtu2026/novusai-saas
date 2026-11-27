"""Shared stream cleanup helpers for OpenAI-compatible protocol flows."""

from __future__ import annotations

from typing import Any

from openai import AsyncStream

from app.core.logging import LogManager

logger = LogManager.get_logger("ai")


async def aclose_openai_stream(stream: Any) -> None:
    """Close upstream SDK streams once a terminal SSE event has been emitted."""
    close = (
        stream.close
        if isinstance(stream, AsyncStream)
        else getattr(stream, "aclose", None)
    )
    if close is None:
        return
    try:
        await close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("OpenAI upstream stream close (ignored): {}", type(exc).__name__)


__all__ = ["aclose_openai_stream"]
