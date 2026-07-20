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
    """Stands in for ClientAgent — records identity, returns canned responses."""

    def __init__(self):
        self.last_call: dict = {}

    async def chat(self, model, messages, temperature=None, max_tokens=None,
                   user_id=None, project_id=None):
        self.last_call = {"user_id": user_id, "project_id": project_id, "model": model}
        return {
            "id": "internal-1", "model": model, "provider": "test",
            "content": "hello there",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    async def chat_stream(self, model, messages, temperature=None, max_tokens=None,
                          user_id=None, project_id=None):
        self.last_call = {"user_id": user_id, "project_id": project_id, "model": model}
        for tok in ["one ", "two ", "three"]:
            yield {"id": "internal-1", "model": model, "content": tok, "is_final": False}
        yield {"done": True}

    async def list_models(self, project_id=None, user_id=None):
        return [{"name": "claude-sonnet"}, {"name": "gpt-4"}]


def _make_client(auth_method="api_key", user_id="apikey:k1", project_id="proj-b"):
    from src.gateway.models import AuthMethod, RequestContext

    agent = _FakeClientAgent()
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

    def test_missing_model_is_400(self):
        client, _ = _make_client()
        r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"

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


class TestModels:
    def test_list_models_shape(self):
        client, _ = _make_client()
        r = client.get("/v1/models")
        assert r.status_code == 200
        d = r.json()
        assert d["object"] == "list"
        assert {m["id"] for m in d["data"]} == {"claude-sonnet", "gpt-4"}
        assert all(m["object"] == "model" and m["owned_by"] == "axonllm" for m in d["data"])
