"""ClientAgent tool pass-through — the layer between the HTTP route and the pipeline.

ClientAgent flattens the pipeline's OpenAI-shaped response into a small dict for
the route to render. That summary listed `content` and nothing else, so a tool
call — which carries no text — vanished at exactly this point even though the
pipeline had translated it correctly. Both directions are asserted at the
gateway boundary: the request_data actually handed down, and the dict handed back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.gateway.chat.client_agent import ClientAgent

_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}

_TOOL_CALL = [{
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
}]


class _CapturingGateway:
    """Records request_data and returns a canned OpenAI-shaped response."""

    def __init__(self, choice=None, stream_chunks=None):
        self.requests: list[dict] = []
        self.choice = choice or {"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}
        self.stream_chunks = stream_chunks
        self.cost_tracker = SimpleNamespace(_records=[])
        self._user_configs = {}

    async def handle_chat_completion(self, request_data, context):
        self.requests.append(request_data)
        if request_data.get("stream"):
            return self._stream()
        return {"id": "x", "model": "m", "provider": "p",
                "choices": [self.choice], "usage": {}}

    async def _stream(self):
        for chunk in (self.stream_chunks or []):
            yield chunk
        yield {"data": "[DONE]"}


def _collect(agen):
    async def run():
        return [c async for c in agen]
    return asyncio.run(run())


class TestRequestSide:
    def test_tools_and_choice_reach_the_pipeline(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}],
                            tools=[_TOOL], tool_choice="auto"))
        assert gw.requests[-1]["tools"] == [_TOOL]
        assert gw.requests[-1]["tool_choice"] == "auto"

    def test_no_tools_means_no_keys(self):
        """An explicit `tools: null` is not the same request as no tools, and a
        `tools: []` payload is rejected by some providers outright."""
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}]))
        assert "tools" not in gw.requests[-1]
        assert "tool_choice" not in gw.requests[-1]

    def test_empty_tool_list_omitted(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}], tools=[]))
        assert "tools" not in gw.requests[-1]

    def test_tool_choice_without_tools_omitted(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}], tool_choice="auto"))
        assert "tool_choice" not in gw.requests[-1]

    def test_tools_reach_the_pipeline_on_stream(self):
        gw = _CapturingGateway(stream_chunks=[])
        ca = ClientAgent(gw)
        _collect(ca.chat_stream("m", [{"role": "user", "content": "hi"}],
                                tools=[_TOOL], tool_choice="auto"))
        assert gw.requests[-1]["tools"] == [_TOOL]
        assert gw.requests[-1]["tool_choice"] == "auto"
        assert gw.requests[-1]["stream"] is True


class TestResponseSide:
    def test_tool_calls_and_finish_reason_survive_the_flattening(self):
        gw = _CapturingGateway(choice={
            "message": {"role": "assistant", "content": None, "tool_calls": _TOOL_CALL},
            "finish_reason": "tool_calls",
        })
        ca = ClientAgent(gw)
        result = asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}], tools=[_TOOL]))
        assert result["tool_calls"] == _TOOL_CALL
        assert result["finish_reason"] == "tool_calls"
        assert result["content"] is None

    def test_plain_response_gains_no_tool_keys(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        result = asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}]))
        assert "tool_calls" not in result
        assert result["content"] == "ok"
        assert result["finish_reason"] == "stop"

    def test_absent_finish_reason_not_invented(self):
        """The route decides the fallback; inventing "stop" here would hide a
        provider that reported nothing."""
        gw = _CapturingGateway(choice={"message": {"content": "ok"}})
        ca = ClientAgent(gw)
        result = asyncio.run(ca.chat("m", [{"role": "user", "content": "hi"}]))
        assert "finish_reason" not in result


class TestStreamResponseSide:
    def test_delta_tool_calls_surface(self):
        gw = _CapturingGateway(stream_chunks=[
            {"data": {"id": "i", "model": "m", "choices": [
                {"delta": {"tool_calls": _TOOL_CALL}, "finish_reason": None}]}},
            {"data": {"id": "i", "model": "m", "is_final": True, "choices": [
                {"delta": {}, "finish_reason": "tool_calls"}]}},
        ])
        ca = ClientAgent(gw)
        chunks = _collect(ca.chat_stream("m", [{"role": "user", "content": "hi"}],
                                         tools=[_TOOL]))
        assert any(c.get("tool_calls") == _TOOL_CALL for c in chunks)
        assert any(c.get("finish_reason") == "tool_calls" for c in chunks)

    def test_text_stream_unchanged(self):
        gw = _CapturingGateway(stream_chunks=[
            {"data": {"id": "i", "model": "m", "choices": [
                {"delta": {"content": "hi "}, "finish_reason": None}]}},
            {"data": {"id": "i", "model": "m", "choices": [
                {"delta": {"content": "there"}, "finish_reason": "stop"}]}},
        ])
        ca = ClientAgent(gw)
        chunks = _collect(ca.chat_stream("m", [{"role": "user", "content": "hi"}]))
        assert "".join(c.get("content", "") for c in chunks) == "hi there"
        assert not any("tool_calls" in c for c in chunks)

    def test_null_delta_content_does_not_crash(self):
        """OpenAI sends content: null on a tool-call delta; `.get("content", "")`
        returns None there, not "", and the route concatenates it."""
        gw = _CapturingGateway(stream_chunks=[
            {"data": {"id": "i", "model": "m", "choices": [
                {"delta": {"content": None, "tool_calls": _TOOL_CALL},
                 "finish_reason": None}]}},
        ])
        ca = ClientAgent(gw)
        chunks = _collect(ca.chat_stream("m", [{"role": "user", "content": "hi"}]))
        assert chunks[0]["content"] == ""
        assert chunks[0]["tool_calls"] == _TOOL_CALL
