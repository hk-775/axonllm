"""Tests for the OpenAI-compatible ingress (task #8).

Verifies /v1/chat/completions and /v1/models emit the shapes the OpenAI SDK
expects, so a base_url swap is all a client needs. Uses a fake ClientAgent so
these are fast unit tests independent of providers.
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.chat.openai_routes import OpenAICompatAPI, create_openai_routes


class _FakeClientAgent:
    """Stands in for ClientAgent — records identity, returns canned responses.

    ``chat_extra`` is merged into the non-streaming reply and ``stream_chunks``
    replaces the canned token stream, so a test can pose as a provider that
    returned a tool call.
    """

    def __init__(self, chat_extra=None, stream_chunks=None):
        self.last_call: dict = {}
        self.chat_extra = chat_extra or {}
        self.stream_chunks = stream_chunks

    def _record(self, model, user_id, project_id, smart_routing, tools, tool_choice):
        self.last_call = {"user_id": user_id, "project_id": project_id,
                          "model": model, "smart_routing": smart_routing,
                          "tools": tools, "tool_choice": tool_choice}

    async def chat(self, model, messages, temperature=None, max_tokens=None,
                   user_id=None, project_id=None, smart_routing=False,
                   tools=None, tool_choice=None):
        self._record(model, user_id, project_id, smart_routing, tools, tool_choice)
        return {
            "id": "internal-1", "model": model or "auto-selected", "provider": "test",
            "content": "hello there",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            **self.chat_extra,
        }

    async def chat_stream(self, model, messages, temperature=None, max_tokens=None,
                          user_id=None, project_id=None, smart_routing=False,
                          tools=None, tool_choice=None):
        self._record(model, user_id, project_id, smart_routing, tools, tool_choice)
        if self.stream_chunks is not None:
            for chunk in self.stream_chunks:
                yield chunk
            yield {"done": True}
            return
        for tok in ["one ", "two ", "three"]:
            yield {"id": "internal-1", "model": model, "content": tok, "is_final": False}
        yield {"done": True}

    async def list_models(self, project_id=None, user_id=None):
        return [{"name": "claude-sonnet"}, {"name": "gpt-4"}]


def _make_client(auth_method="api_key", user_id="apikey:k1", project_id="proj-b",
                 chat_extra=None, stream_chunks=None):
    from src.gateway.models import AuthMethod, RequestContext

    agent = _FakeClientAgent(chat_extra=chat_extra, stream_chunks=stream_chunks)
    api = OpenAICompatAPI(agent)

    class _CtxMiddleware(BaseHTTPMiddleware):
        """Inject an authenticated context the way AuthMiddleware would."""

        async def dispatch(self, request, call_next):
            request.state.context = RequestContext(
                user_id=user_id, project_id=project_id, roles=[], scopes=[],
                auth_method=AuthMethod(auth_method),
            )
            return await call_next(request)

    app = Starlette(routes=create_openai_routes(api))
    app.add_middleware(_CtxMiddleware)
    return TestClient(app), agent


class TestChatCompletions:
    def test_non_streaming_shape(self):
        client, _ = _make_client()
        r = client.post("/v1/chat/completions", json={
            "model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200
        d = r.json()
        assert d["object"] == "chat.completion"
        assert d["id"].startswith("chatcmpl-")
        assert d["choices"][0]["message"] == {"role": "assistant", "content": "hello there"}
        assert d["choices"][0]["finish_reason"] == "stop"
        assert d["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    def test_identity_from_context_not_body(self):
        # Body claims a different tenant; must be ignored in favor of the token.
        client, agent = _make_client(user_id="apikey:real", project_id="proj-real")
        client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "user_id": "victim", "project_id": "proj-victim",
        })
        assert agent.last_call["user_id"] == "apikey:real"
        assert agent.last_call["project_id"] == "proj-real"

    def test_anonymous_context_falls_back(self):
        client, agent = _make_client(auth_method="anonymous", user_id="anonymous", project_id="")
        client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
        })
        assert agent.last_call["user_id"] is None
        assert agent.last_call["project_id"] is None

    def test_missing_or_auto_model_triggers_smart_routing(self):
        # Empty/missing model and model="auto" now opt into smart routing rather
        # than 400 — lets standard OpenAI clients request task-aware routing.
        client, agent = _make_client()
        for body in ({"messages": [{"role": "user", "content": "x"}]},
                     {"model": "", "messages": [{"role": "user", "content": "x"}]},
                     {"model": "auto", "messages": [{"role": "user", "content": "x"}]}):
            r = client.post("/v1/chat/completions", json=body)
            assert r.status_code == 200
            assert agent.last_call["smart_routing"] is True
            assert agent.last_call["model"] == ""

    def test_missing_messages_is_400(self):
        client, _ = _make_client()
        r = client.post("/v1/chat/completions", json={"model": "m"})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"

    def test_invalid_json_is_400(self):
        client, _ = _make_client()
        r = client.post("/v1/chat/completions", content=b"{not json",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400


class TestStreaming:
    def test_sse_chunk_shape(self):
        client, _ = _make_client()
        chunks, done = [], False
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": True,
        }) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            for line in r.iter_lines():
                line = line if isinstance(line, str) else line.decode()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    done = True
                    break
                chunks.append(json.loads(data))

        assert done
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
        assert sum(1 for c in chunks if c["choices"][0]["finish_reason"] == "stop") == 1
        content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        assert content == "one two three"


_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}

_TOOL_CALL = [{
    "id": "call_abc",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
}]


class TestToolsRequestSide:
    """The route has to read tools off the body — the pipeline can't translate
    what never reached it. Asserted at the ClientAgent boundary because a
    response-only assertion passes even when tools are dropped."""

    def test_tools_and_choice_forwarded(self):
        client, agent = _make_client()
        r = client.post("/v1/chat/completions", json={
            "model": "claude-sonnet", "messages": [{"role": "user", "content": "weather?"}],
            "tools": [_WEATHER_TOOL], "tool_choice": "auto",
        })
        assert r.status_code == 200
        assert agent.last_call["tools"] == [_WEATHER_TOOL]
        assert agent.last_call["tool_choice"] == "auto"

    def test_forced_tool_choice_object_forwarded(self):
        client, agent = _make_client()
        forced = {"type": "function", "function": {"name": "get_weather"}}
        client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
            "tools": [_WEATHER_TOOL], "tool_choice": forced,
        })
        assert agent.last_call["tool_choice"] == forced

    def test_absent_tools_stay_none(self):
        client, agent = _make_client()
        client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
        })
        assert agent.last_call["tools"] is None
        assert agent.last_call["tool_choice"] is None

    def test_tools_forwarded_on_stream(self):
        client, agent = _make_client()
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
            "tools": [_WEATHER_TOOL], "tool_choice": "auto", "stream": True,
        }) as r:
            r.read()
        assert agent.last_call["tools"] == [_WEATHER_TOOL]
        assert agent.last_call["tool_choice"] == "auto"

    def test_tool_role_history_forwarded_verbatim(self):
        """Round two of a tool loop: the assistant turn with tool_calls and the
        tool result must reach the pipeline unaltered for translation."""
        client, agent = _make_client()
        history = [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": _TOOL_CALL},
            {"role": "tool", "tool_call_id": "call_abc", "content": "18C"},
        ]
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": history, "tools": [_WEATHER_TOOL],
        })
        assert r.status_code == 200


class TestToolsResponseSide:
    def test_tool_call_surfaces_with_tool_calls_finish_reason(self):
        client, _ = _make_client(chat_extra={
            "content": None, "tool_calls": _TOOL_CALL, "finish_reason": "tool_calls",
        })
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "weather?"}],
            "tools": [_WEATHER_TOOL],
        })
        assert r.status_code == 200
        choice = r.json()["choices"][0]
        assert choice["message"]["tool_calls"] == _TOOL_CALL
        assert choice["message"]["role"] == "assistant"
        # None, not "": an OpenAI client reads a string here as the final answer.
        assert choice["message"]["content"] is None
        assert choice["finish_reason"] == "tool_calls"

    def test_tool_calls_without_finish_reason_still_reported(self):
        """A provider that forwards tool_calls but no stop reason must not be
        rendered as finish_reason "stop" — that ends the caller's tool loop."""
        client, _ = _make_client(chat_extra={"content": None, "tool_calls": _TOOL_CALL})
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
        })
        assert r.json()["choices"][0]["finish_reason"] == "tool_calls"

    def test_provider_finish_reason_is_carried(self):
        client, _ = _make_client(chat_extra={"finish_reason": "length"})
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
        })
        assert r.json()["choices"][0]["finish_reason"] == "length"

    def test_plain_response_keeps_string_content_and_no_tool_calls_key(self):
        """Regression guard: the no-tools path is the overwhelming majority of
        traffic and must keep sending a string, never None."""
        client, _ = _make_client()
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
        })
        msg = r.json()["choices"][0]["message"]
        assert msg == {"role": "assistant", "content": "hello there"}
        assert "tool_calls" not in msg
        assert r.json()["choices"][0]["finish_reason"] == "stop"

    def test_empty_content_still_a_string_when_no_tool_calls(self):
        client, _ = _make_client(chat_extra={"content": None})
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
        })
        assert r.json()["choices"][0]["message"]["content"] == ""


class TestFinishReasonNormalization:
    """OpenAI defines four finish_reason values and typed SDK clients
    deserialize the field into an enum, so a raw provider reason is a client-side
    validation error. Forwarding the provider's value (rather than the old
    hardcoded "stop") is what exposed this."""

    def test_anthropic_end_turn_becomes_stop(self):
        client, _ = _make_client(chat_extra={"finish_reason": "end_turn"})
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}]})
        assert r.json()["choices"][0]["finish_reason"] == "stop"

    def test_provider_reasons_all_map_into_spec(self):
        raw_to_expected = {
            "end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
            "max_tokens": "length", "content_filtered": "content_filter",
            "guardrail_intervened": "content_filter",
            "STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
            "RECITATION": "content_filter", "BLOCKLIST": "content_filter",
            "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter",
            "COMPLETE": "stop", "MAX_TOKENS_REACHED": "length",
            "ERROR_TOXIC": "content_filter",
            "completed": "stop", "incomplete": "length",
            # Already-legal values pass through untouched.
            "stop": "stop", "length": "length", "content_filter": "content_filter",
            "tool_calls": "tool_calls",
        }
        for raw, expected in raw_to_expected.items():
            client, _ = _make_client(chat_extra={"finish_reason": raw})
            r = client.post("/v1/chat/completions", json={
                "model": "m", "messages": [{"role": "user", "content": "x"}]})
            got = r.json()["choices"][0]["finish_reason"]
            assert got == expected, f"{raw!r} -> {got!r}, expected {expected!r}"

    def test_unknown_reason_falls_back_to_stop(self):
        client, _ = _make_client(chat_extra={"finish_reason": "something_new"})
        r = client.post("/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}]})
        assert r.json()["choices"][0]["finish_reason"] == "stop"

    def test_tool_calls_override_any_provider_reason(self):
        """Mantle's Responses route reports lifecycle status ("completed") even
        when a tool was called; tool_calls present must win regardless."""
        for raw in ("end_turn", "completed", "STOP", None, "stop"):
            client, _ = _make_client(chat_extra={
                "content": None, "tool_calls": _TOOL_CALL, "finish_reason": raw})
            r = client.post("/v1/chat/completions", json={
                "model": "m", "messages": [{"role": "user", "content": "x"}],
                "tools": [_WEATHER_TOOL]})
            assert r.json()["choices"][0]["finish_reason"] == "tool_calls", raw

    def test_non_string_reason_does_not_crash(self):
        for raw in (0, 1, [], {}, True):
            client, _ = _make_client(chat_extra={"finish_reason": raw})
            r = client.post("/v1/chat/completions", json={
                "model": "m", "messages": [{"role": "user", "content": "x"}]})
            assert r.status_code == 200
            assert r.json()["choices"][0]["finish_reason"] == "stop"

    def test_every_emitted_reason_is_in_spec(self):
        """Guards the mapping against drift: any literal an adapter can assign to
        finish_reason must normalize to one of OpenAI's four values."""
        from src.gateway.chat.openai_routes import (
            _FINISH_REASONS,
            _VALID_FINISH_REASONS,
            _finish_reason,
        )

        assert set(_FINISH_REASONS.values()) <= _VALID_FINISH_REASONS
        assert not set(_FINISH_REASONS) & _VALID_FINISH_REASONS, (
            "a legal value must not also be remapped")
        for raw in list(_FINISH_REASONS) + list(_VALID_FINISH_REASONS):
            assert _finish_reason(raw, False) in _VALID_FINISH_REASONS

    def test_streaming_normalizes_too(self):
        client, _ = _make_client(stream_chunks=[
            {"id": "i", "model": "m", "content": "hi", "finish_reason": "end_turn",
             "is_final": True},
        ])
        reasons = []
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True,
        }) as r:
            for line in r.iter_lines():
                line = line if isinstance(line, str) else line.decode()
                if not line.startswith("data: ") or line[6:] == "[DONE]":
                    continue
                fr = json.loads(line[6:])["choices"][0]["finish_reason"]
                if fr:
                    reasons.append(fr)
        assert reasons == ["stop"]

    def test_streaming_tool_call_without_finish_reason_reports_tool_calls(self):
        """A provider that streams a tool-call delta but never labels the stop
        must still end the stream with tool_calls, or the loop halts."""
        client, _ = _make_client(stream_chunks=[
            {"id": "i", "model": "m", "content": "", "tool_calls": _TOOL_CALL},
        ])
        reasons = []
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
            "tools": [_WEATHER_TOOL], "stream": True,
        }) as r:
            for line in r.iter_lines():
                line = line if isinstance(line, str) else line.decode()
                if not line.startswith("data: ") or line[6:] == "[DONE]":
                    continue
                fr = json.loads(line[6:])["choices"][0]["finish_reason"]
                if fr:
                    reasons.append(fr)
        assert reasons == ["tool_calls"]


class TestStreamingTools:
    def test_tool_call_delta_and_finish_reason(self):
        client, _ = _make_client(stream_chunks=[
            {"id": "i", "model": "m", "content": "", "tool_calls": _TOOL_CALL},
            {"id": "i", "model": "m", "content": "", "finish_reason": "tool_calls",
             "is_final": True},
        ])
        chunks = []
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}],
            "tools": [_WEATHER_TOOL], "stream": True,
        }) as r:
            for line in r.iter_lines():
                line = line if isinstance(line, str) else line.decode()
                if not line.startswith("data: ") or line[6:] == "[DONE]":
                    continue
                chunks.append(json.loads(line[6:]))

        deltas = [c["choices"][0]["delta"] for c in chunks]
        assert any(d.get("tool_calls") == _TOOL_CALL for d in deltas)
        # The tool-call delta carries no text.
        tc_delta = next(d for d in deltas if d.get("tool_calls"))
        assert tc_delta["content"] is None
        finals = [c["choices"][0]["finish_reason"] for c in chunks
                  if c["choices"][0]["finish_reason"]]
        assert finals == ["tool_calls"]

    def test_plain_stream_still_finishes_stop(self):
        client, _ = _make_client()
        reasons, content = [], ""
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True,
        }) as r:
            for line in r.iter_lines():
                line = line if isinstance(line, str) else line.decode()
                if not line.startswith("data: ") or line[6:] == "[DONE]":
                    continue
                c = json.loads(line[6:])
                content += c["choices"][0]["delta"].get("content") or ""
                if c["choices"][0]["finish_reason"]:
                    reasons.append(c["choices"][0]["finish_reason"])
        assert reasons == ["stop"]
        assert content == "one two three"


class TestModels:
    def test_list_models_shape(self):
        client, _ = _make_client()
        r = client.get("/v1/models")
        assert r.status_code == 200
        d = r.json()
        assert d["object"] == "list"
        assert {m["id"] for m in d["data"]} == {"claude-sonnet", "gpt-4"}
        assert all(m["object"] == "model" and m["owned_by"] == "axonllm" for m in d["data"])
