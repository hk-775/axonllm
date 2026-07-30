"""simulate_streaming must carry tool calls, not just text.

This is the buffered path: every provider that can't open a real SSE stream
(boto3 Bedrock, google_ai, or any provider whose stream fails to open) reaches
the client through here. It extracted `message.content` only, and a tool call
carries no text — so a streaming request with tools produced a single empty
chunk and the tool call was discarded between the provider and the client.
"""

from __future__ import annotations

from src.gateway.models import ChatCompletionResponse, TokenUsage
from src.gateway.streaming import simulate_streaming

_TOOL_CALL = [{
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
}]


def _response(content=None, tool_calls=None, finish_reason="stop"):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"index": 0, "message": message, "finish_reason": finish_reason}],
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="m",
        provider="p",
    )


def _deltas(chunks):
    return [c.choices[0]["delta"] for c in chunks]


class TestBareToolCall:
    """content=None + tool_calls — the normal shape of a tool call."""

    def test_tool_call_is_emitted(self):
        chunks = simulate_streaming(_response(None, _TOOL_CALL, "tool_calls"))
        assert len(chunks) == 1
        assert _deltas(chunks)[0]["tool_calls"] == _TOOL_CALL

    def test_final_and_finish_reason_carried(self):
        chunks = simulate_streaming(_response(None, _TOOL_CALL, "tool_calls"))
        assert chunks[-1].is_final is True
        assert chunks[-1].choices[0]["finish_reason"] == "tool_calls"

    def test_arguments_not_split_across_chunks(self):
        """The arguments are a JSON string; word-splitting them would emit
        fragments no client can parse until reassembled."""
        chunks = simulate_streaming(_response(None, _TOOL_CALL, "tool_calls"))
        args = [d["tool_calls"][0]["function"]["arguments"]
                for d in _deltas(chunks) if d.get("tool_calls")]
        assert args == ['{"city":"Paris"}']

    def test_id_and_model_still_propagate(self):
        chunks = simulate_streaming(_response(None, _TOOL_CALL, "tool_calls"))
        assert all(c.id == "resp-1" and c.model == "m" for c in chunks)


class TestTextPlusToolCall:
    """A model may emit prose and a tool call in the same turn."""

    def test_text_is_chunked_and_tool_call_attached_to_last(self):
        chunks = simulate_streaming(_response("Let me check.", _TOOL_CALL, "tool_calls"))
        deltas = _deltas(chunks)
        assert "".join(d.get("content", "") for d in deltas) == "Let me check."
        assert deltas[-1]["tool_calls"] == _TOOL_CALL
        # Exactly once — a client appending each delta would otherwise call twice.
        assert sum(1 for d in deltas if d.get("tool_calls")) == 1

    def test_finish_reason_only_on_final_chunk(self):
        chunks = simulate_streaming(_response("Let me check.", _TOOL_CALL, "tool_calls"))
        reasons = [c.choices[0].get("finish_reason") for c in chunks]
        assert reasons[-1] == "tool_calls"
        assert all(r is None for r in reasons[:-1])


class TestNoToolCallRegression:
    """The text-only path is the overwhelming majority of traffic."""

    def test_plain_text_unchanged_and_no_tool_calls_key(self):
        chunks = simulate_streaming(_response("hello world"))
        deltas = _deltas(chunks)
        assert "".join(d["content"] for d in deltas) == "hello world"
        assert not any("tool_calls" in d for d in deltas)

    def test_empty_content_still_sends_content_key(self):
        """Clients read delta["content"] unconditionally; dropping the key for an
        empty response would KeyError in code that worked before."""
        chunks = simulate_streaming(_response(""))
        assert len(chunks) == 1
        assert chunks[0].choices[0]["delta"] == {"content": ""}
        assert chunks[0].is_final is True

    def test_none_content_treated_as_empty(self):
        chunks = simulate_streaming(_response(None))
        assert chunks[0].choices[0]["delta"] == {"content": ""}

    def test_missing_message_does_not_crash(self):
        resp = ChatCompletionResponse(
            id="r", choices=[{"index": 0}],
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model="m", provider="p")
        chunks = simulate_streaming(resp)
        assert chunks[0].choices[0]["delta"] == {"content": ""}

    def test_no_choices_does_not_crash(self):
        resp = ChatCompletionResponse(
            id="r", choices=[],
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model="m", provider="p")
        chunks = simulate_streaming(resp)
        assert chunks[0].choices[0]["delta"] == {"content": ""}
        assert chunks[0].is_final is True

    def test_text_reconstruction_preserved_with_finish_reason_added(self):
        """Adding finish_reason to the last chunk must not disturb the text."""
        chunks = simulate_streaming(_response("  a  b ", None, "length"))
        assert "".join(_deltas(chunks)[i]["content"] for i in range(len(chunks))) == "  a  b "
        assert chunks[-1].choices[0]["finish_reason"] == "length"
