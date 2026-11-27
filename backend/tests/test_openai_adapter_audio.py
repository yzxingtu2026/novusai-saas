"""Tests for OpenAI adapter native audio (input_audio) conversion. / 测试

Covers:
- Audio attachment with HTTP URL: mock returns wav/mp3 bytes → content contains input_audio with correct format.
- Audio attachment with data URL → input_audio block.
- Failure path (fetch fails or no URL): content is text hint [Audio: ...].
- supports_audio=False: always text hint.
- url is null or url key missing: fallback to text hint."""

from __future__ import annotations

import asyncio
import base64
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from app.ai.adapters.openai_adapter import SUPPORTS_NATIVE_AUDIO, OpenAIAdapter
from app.ai.adapters.openai_compatible.support import audio_inputs
from app.ai.types import ChatMessage

MEDIA_HOST = "audio.fixture.invalid"
MEDIA_URL = f"https://{MEDIA_HOST}:8443/sample.wav?tag=one&tag=two"
# Only the in-memory MockTransport receives this address.
MEDIA_ADDRESS = "93.184.216.34"


@pytest.fixture
async def adapter():
    instance = OpenAIAdapter(api_key="fixture-key")
    try:
        yield instance
    finally:
        await instance.client.close()


@pytest.fixture
def audio_http(monkeypatch):
    real_client = httpx.AsyncClient

    def serve(response, *, address=MEDIA_ADDRESS):
        options = []
        requests = []

        def receive(request):
            requests.append(request)
            return response

        def create_client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(receive), **kwargs)

        async def resolve(host, port, *args, **kwargs):
            assert host == MEDIA_HOST
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, port))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(httpx, "AsyncClient", create_client)
        return options, requests

    return serve


def _make_audio_message(
    url: str,
    mime_type: str = "audio/wav",
    name: str = "test.wav",
) -> ChatMessage:
    return ChatMessage(
        role="user",
        content="",
        attachments=[
            {"type": "audio", "url": url, "mime_type": mime_type, "name": name},
        ],
    )


@pytest.mark.behavioral
@pytest.mark.parametrize(
    ("mime_type", "expected_format"),
    [("audio/wav", "wav"), ("audio/mpeg", "mp3")],
)
@pytest.mark.asyncio
async def test_convert_messages_audio_http_url_yields_input_audio(
    adapter: OpenAIAdapter, audio_http, mime_type: str, expected_format: str
) -> None:
    raw = b"fixture-audio-bytes"
    options, requests = audio_http(httpx.Response(200, content=raw))

    result = await adapter._convert_messages(
        [_make_audio_message(MEDIA_URL, mime_type=mime_type)],
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )

    assert len(result) == 1
    parts = [part for part in result[0]["content"] if part["type"] == "input_audio"]
    assert parts == [
        {
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(raw).decode("ascii"),
                "format": expected_format,
            },
        }
    ]
    assert options == [
        {
            "timeout": audio_inputs.AUDIO_FETCH_TIMEOUT_SEC,
            "trust_env": False,
            "follow_redirects": False,
        }
    ]
    (request,) = requests
    assert request.method == "GET"
    assert request.url == httpx.URL(
        f"https://{MEDIA_ADDRESS}:8443/sample.wav?tag=one&tag=two"
    )
    assert request.headers["host"] == f"{MEDIA_HOST}:8443"
    assert request.extensions["sni_hostname"] == MEDIA_HOST


@pytest.mark.asyncio
async def test_convert_messages_audio_data_url_yields_input_audio(
    adapter: OpenAIAdapter,
) -> None:
    """Data URL (data:audio/...;base64,...) → input_audio without HTTP. / 说明"""
    raw = b"small-audio"
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:audio/wav;base64,{b64}"
    messages = [_make_audio_message(data_url, mime_type="audio/wav")]
    result = await adapter._convert_messages(
        messages,
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )
    parts = [p for p in result[0]["content"] if p.get("type") == "input_audio"]
    assert len(parts) == 1
    assert base64.b64decode(parts[0]["input_audio"]["data"]) == raw
    assert parts[0]["input_audio"]["format"] == "wav"


@pytest.mark.behavioral
@pytest.mark.parametrize("status", [302, 502], ids=("redirect", "upstream-error"))
@pytest.mark.asyncio
async def test_convert_messages_audio_fetch_failure_fallback_to_text(
    adapter: OpenAIAdapter, audio_http, status: int
) -> None:
    _, requests = audio_http(httpx.Response(status, headers={"location": "/next.wav"}))
    result = await adapter._convert_messages(
        [_make_audio_message(MEDIA_URL)],
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )
    content = result[0]["content"]
    assert [part for part in content if part["type"] == "input_audio"] == []
    hints = [part["text"] for part in content if part["type"] == "text"]
    assert hints == ["[Audio: test.wav]"]
    (request,) = requests
    assert request.url.path == "/sample.wav"


@pytest.mark.behavioral
@pytest.mark.asyncio
async def test_audio_fetch_keeps_nonpublic_addresses_out_of_transport(
    adapter: OpenAIAdapter, audio_http
) -> None:
    options, requests = audio_http(
        httpx.Response(200, content=b"unused"), address="127.0.0.1"
    )

    assert await adapter._fetch_audio_bytes(MEDIA_URL) is None
    assert (options, requests) == ([], [])


@pytest.mark.behavioral
@pytest.mark.asyncio
async def test_audio_fetch_stops_reading_when_stream_crosses_size_limit(
    adapter: OpenAIAdapter, audio_http, monkeypatch
) -> None:
    read = []

    class AudioStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for segment in (b"12", b"34", b"56"):
                read.append(segment)
                yield segment

    monkeypatch.setattr(audio_inputs, "AUDIO_MAX_BYTES", 3)
    _, requests = audio_http(httpx.Response(200, stream=AudioStream()))

    assert await adapter._fetch_audio_bytes(MEDIA_URL) is None
    assert read == [b"12", b"34"]
    assert len(requests) == 1


@pytest.mark.behavioral
@pytest.mark.parametrize(
    "payload", ["YQ==!", "YWJjZGU="], ids=("invalid", "over-limit")
)
@pytest.mark.asyncio
async def test_audio_data_url_validates_bytes_before_building_input(
    adapter: OpenAIAdapter, monkeypatch, payload: str
) -> None:
    monkeypatch.setattr(audio_inputs, "AUDIO_MAX_BYTES", 4)
    assert await adapter._fetch_audio_bytes(f"data:audio/wav;base64,{payload}") is None


@pytest.mark.asyncio
async def test_convert_messages_audio_no_url_fallback_to_text(
    adapter: OpenAIAdapter,
) -> None:
    """Audio attachment with empty URL → text hint. / 说明"""
    msg = ChatMessage(
        role="user",
        content="",
        attachments=[
            {"type": "audio", "url": "", "mime_type": "audio/wav", "name": "x.wav"}
        ],
    )
    result = await adapter._convert_messages(
        [msg],
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )
    content = result[0]["content"]
    text_parts = [
        p for p in content if p.get("type") == "text" and "[Audio:" in p.get("text", "")
    ]
    assert len(text_parts) == 1
    assert "x.wav" in text_parts[0]["text"] or "uploaded audio" in text_parts[0]["text"]


@pytest.mark.asyncio
async def test_convert_messages_audio_url_null_fallback_to_text(
    adapter: OpenAIAdapter,
) -> None:
    """Audio attachment with url=null → text hint (no HTTP fetch). / 说明"""
    msg = ChatMessage(
        role="user",
        content="",
        attachments=[
            {"type": "audio", "url": None, "mime_type": "audio/wav", "name": "a.wav"}
        ],
    )
    result = await adapter._convert_messages(
        [msg],
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )
    content = result[0]["content"]
    text_parts = [
        p for p in content if p.get("type") == "text" and "[Audio:" in p.get("text", "")
    ]
    assert len(text_parts) == 1
    input_audio_parts = [p for p in content if p.get("type") == "input_audio"]
    assert len(input_audio_parts) == 0


@pytest.mark.asyncio
async def test_convert_messages_audio_url_key_missing_fallback_to_text(
    adapter: OpenAIAdapter,
) -> None:
    """Audio attachment without 'url' key → text hint (att.get('url', '') yields ''). / 获取/返回"""
    msg = ChatMessage(
        role="user",
        content="",
        attachments=[{"type": "audio", "mime_type": "audio/wav", "name": "b.wav"}],
    )
    result = await adapter._convert_messages(
        [msg],
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )
    content = result[0]["content"]
    text_parts = [
        p for p in content if p.get("type") == "text" and "[Audio:" in p.get("text", "")
    ]
    assert len(text_parts) == 1
    input_audio_parts = [p for p in content if p.get("type") == "input_audio"]
    assert len(input_audio_parts) == 0


@pytest.mark.asyncio
async def test_convert_messages_audio_supports_audio_false_yields_text(
    adapter: OpenAIAdapter, monkeypatch
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(adapter, "_fetch_audio_bytes", fetch)
    result = await adapter._convert_messages(
        [_make_audio_message(MEDIA_URL)],
        supports_vision=True,
        supports_audio=False,
        supports_video=False,
    )
    content = result[0]["content"]
    assert [part for part in content if part["type"] == "input_audio"] == []
    assert [part["text"] for part in content if part["type"] == "text"] == [
        "[Audio: test.wav]"
    ]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_convert_messages_audio_uses_payload_helper(
    adapter: OpenAIAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio conversion should delegate to the shared payload helper. / 说明"""
    called: dict[str, object] = {}

    async def _fake_fetch_audio_bytes(_url: str) -> bytes | None:
        return b"payload-bytes"

    def _fake_build_input_audio_part(
        audio_bytes: bytes,
        mime_type: str | None,
        *,
        audio_mime_to_openai_format: dict[str, str] | None = None,
    ) -> dict[str, object]:
        called["audio_bytes"] = audio_bytes
        called["mime_type"] = mime_type
        called["mapping"] = dict(audio_mime_to_openai_format or {})
        return {"type": "input_audio", "input_audio": {"data": "YWJj", "format": "wav"}}

    monkeypatch.setattr(
        adapter, "_fetch_audio_bytes", AsyncMock(side_effect=_fake_fetch_audio_bytes)
    )
    monkeypatch.setattr(
        "app.ai.adapters.openai_compatible.support.chat_multimodal_messages.build_input_audio_part",
        _fake_build_input_audio_part,
    )

    messages = [
        _make_audio_message("https://example.com/audio.wav", mime_type="audio/wav")
    ]
    result = await adapter._convert_messages(
        messages,
        supports_vision=True,
        supports_audio=True,
        supports_video=False,
    )

    parts = [p for p in result[0]["content"] if p.get("type") == "input_audio"]
    assert len(parts) == 1
    assert parts[0]["input_audio"]["format"] == "wav"
    assert called["audio_bytes"] == b"payload-bytes"
    assert called["mime_type"] == "audio/wav"


def test_supports_native_audio_constant() -> None:
    """SUPPORTS_NATIVE_AUDIO is True so native audio can be used when enabled. / 说明"""
    assert SUPPORTS_NATIVE_AUDIO is True
