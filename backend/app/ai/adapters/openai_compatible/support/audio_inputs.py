"""Audio input helpers for OpenAI-compatible multimodal adapters."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from app.core.egress_url_policy import EgressPolicyError, request_public_http
from app.core.logging import LogManager

logger = LogManager.get_logger("ai")

AUDIO_MIME_TO_OPENAI_FORMAT: dict[str, str] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "m4a",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
    "audio/mpeg3": "mpeg",
    "audio/mpg": "mpeg",
}

AUDIO_FETCH_TIMEOUT_SEC: float = 30.0
AUDIO_MAX_BYTES: int = 25 * 1024 * 1024


def resolve_audio_format(
    mime_type: str | None,
    *,
    audio_mime_to_openai_format: dict[str, str] | None = None,
) -> str:
    """Resolve an audio MIME type to the OpenAI input_audio format label."""
    normalized_mime = str(mime_type or "").strip().lower()
    formats = audio_mime_to_openai_format or AUDIO_MIME_TO_OPENAI_FORMAT
    return formats.get(normalized_mime) or "mpeg"


def build_input_audio_part(
    audio_bytes: bytes,
    mime_type: str | None,
    *,
    audio_mime_to_openai_format: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build input_audio part payload for chat.completions messages."""
    b64_str = base64.b64encode(audio_bytes).decode("ascii")
    return {
        "type": "input_audio",
        "input_audio": {
            "data": b64_str,
            "format": resolve_audio_format(
                mime_type,
                audio_mime_to_openai_format=audio_mime_to_openai_format,
            ),
        },
    }


async def fetch_audio_bytes(
    url: str,
    *,
    extract_data_url_payload_fn: Callable[[str], str | None],
) -> bytes | None:
    """Resolve an audio URL to bytes for native input_audio blocks."""
    if not url or not url.strip():
        return None

    normalized_url = url.strip()
    if normalized_url.startswith("data:audio"):
        payload = extract_data_url_payload_fn(normalized_url)
        max_encoded_bytes = ((AUDIO_MAX_BYTES + 2) // 3) * 4
        if payload is None or len(payload) > max_encoded_bytes:
            return None
        try:
            decoded = base64.b64decode(payload, validate=True)
        except ValueError:
            logger.warning("Audio data URL has invalid base64")
            return None
        return decoded if len(decoded) <= AUDIO_MAX_BYTES else None

    try:
        # Attachment URLs use the public egress policy regardless of provider settings.
        response = await request_public_http(
            "GET",
            normalized_url,
            timeout=AUDIO_FETCH_TIMEOUT_SEC,
            deadline_seconds=AUDIO_FETCH_TIMEOUT_SEC,
            max_response_bytes=AUDIO_MAX_BYTES,
        )
    except EgressPolicyError as exc:
        logger.warning("Audio fetch refused by egress policy: {}", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audio fetch failed: {}", type(exc).__name__)
        return None

    if not response.is_success:
        logger.warning("Audio fetch returned HTTP {}", response.status_code)
        return None
    return response.content


__all__ = [
    "AUDIO_FETCH_TIMEOUT_SEC",
    "AUDIO_MAX_BYTES",
    "AUDIO_MIME_TO_OPENAI_FORMAT",
    "build_input_audio_part",
    "fetch_audio_bytes",
    "resolve_audio_format",
]
