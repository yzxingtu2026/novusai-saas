"""Test type: structural.

Regression for BUG-2026-11-24-STREAM-WHITESPACE-ORDER.
Parse invented Chat Completions events through the SDK converter and stream
runtime. The fixture exercises wire ordering and completion metadata.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletionChunk

from app.ai.adapters.openai_compatible.support.protocol_chat_completions_helpers import (
    convert_chat_chunk,
)
from app.ai.adapters.openai_compatible.support.usage_runtime import (
    OpenAIAdapterUsageRuntimeMixin,
)
from app.ai.runtime.query_engine import ConversationQueryEngine
from app.ai.types import ChatMessage

_FIXTURE_MODEL = "fixture-stream-model"
_EXPECTED_ANSWER = (
    "Seeds need sun, water, and patience.\n"
    "In spring, we prepare the beds and label each row. New volunteers can bring gloves "
    "and share a watering can. By summer, everyone can pick herbs, trade tips, and take "
    "a small bouquet home."
)


class _FixtureChatCompletionsAdapter(OpenAIAdapterUsageRuntimeMixin):
    """Feed synthetic wire events through the real SDK chunk model and converter."""

    wire_api = "chat_completions"

    def __init__(self, packets: list[dict]) -> None:
        self.packets = packets
        self.stream_calls = 0
        self.chat_calls = 0

    async def stream_chat(self, **kwargs):
        assert kwargs["model"] == _FIXTURE_MODEL
        self.stream_calls += 1
        for packet in self.packets:
            yield convert_chat_chunk(
                adapter=self,
                chunk=ChatCompletionChunk.model_validate(packet),
                model=_FIXTURE_MODEL,
            )

    async def chat(self, **kwargs):
        _ = kwargs
        self.chat_calls += 1
        raise AssertionError("A complete stream must not use sync rescue")


@pytest.mark.structural
@pytest.mark.asyncio
async def test_stream_preserves_whitespace_in_wire_order() -> None:
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "bug_2026_11_24_stream_whitespace_order.sse"
    ).read_bytes()
    assert sha256(fixture).hexdigest() == (
        "78e5bc86883f70d6b7890b22a934af5643164fa9374231d0bbff17bdef04e8d4"
    )
    packets = []
    done_markers = 0
    for block in fixture.replace(b"\r\n", b"\n").split(b"\n\n"):
        data = b"\n".join(
            line[5:].lstrip(b" ")
            for line in block.splitlines()
            if line.startswith(b"data:")
        )
        if data == b"[DONE]":
            done_markers += 1
        elif data:
            packets.append(json.loads(data))

    assert done_markers == 1
    assert len(packets) == 41
    expected_deltas = [
        packet["choices"][0]["delta"]["content"]
        for packet in packets
        if packet.get("choices")
        and packet["choices"][0].get("delta", {}).get("content")
    ]
    assert len(expected_deltas) == 40
    assert expected_deltas[5] == "\n"
    assert "".join(expected_deltas) == _EXPECTED_ANSWER

    adapter = _FixtureChatCompletionsAdapter(packets)
    query_engine = ConversationQueryEngine(adapter=adapter, strict_contract=False)
    chunks = await query_engine.run_stream_turn(
        messages=[
            ChatMessage(
                role="user",
                content="Write a brief update about a fictional community garden.",
            ),
        ],
        model=_FIXTURE_MODEL,
        temperature=0.0,
        max_tokens=4096,
        top_p=1.0,
        tools=None,
        tool_choice=None,
        supports_vision=False,
        supports_audio=False,
        supports_video=False,
    )

    actual_deltas = [chunk.delta for chunk in chunks if chunk.delta]
    assert actual_deltas == expected_deltas
    assert "".join(actual_deltas) == _EXPECTED_ANSWER
    assert (adapter.stream_calls, adapter.chat_calls) == (1, 0)
    terminal = chunks[-1]
    assert (
        terminal.finish_reason,
        terminal.input_tokens,
        terminal.output_tokens,
        terminal.total_tokens,
    ) == ("stop", 16, 40, 56)
    assert query_engine.turn_record.turn_outcome == "success"
