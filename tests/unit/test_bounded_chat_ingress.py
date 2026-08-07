"""Adversarial coverage for bounded chat request bodies."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from src.gateway.chat.openai_routes import OpenAICompatAPI, create_openai_routes
from src.gateway.chat.request_body import JSONBodyError, read_json_object
from src.gateway.chat.routes import ChatAPI, create_chat_routes


class _FakeClientAgent:
    async def chat(self, model, messages, **kwargs):
        return {
            "id": "internal-1",
            "model": model or "auto-selected",
            "provider": "test",
            "content": "ok",
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    async def chat_stream(self, model, messages, **kwargs):
        yield {
            "id": "internal-1",
            "model": model or "auto-selected",
            "content": "ok",
        }
        yield {"done": True}

    async def list_models(self, **kwargs):
        return []

    async def get_available_users(self):
        return []


def _client(max_bytes: int = 256) -> TestClient:
    agent = _FakeClientAgent()
    native = ChatAPI(agent, max_request_bytes=max_bytes)
    openai = OpenAICompatAPI(agent, max_request_bytes=max_bytes)
    return TestClient(
        Starlette(
            routes=[
                *create_chat_routes(native),
                *create_openai_routes(openai),
            ]
        )
    )


def _chunked(raw: bytes, size: int = 17) -> Iterator[bytes]:
    for start in range(0, len(raw), size):
        yield raw[start : start + size]


_VALID_BODY = {
    "model": "m",
    "messages": [{"role": "user", "content": "hello"}],
}


@pytest.mark.parametrize(
    "path",
    ["/api/chat", "/api/chat/stream", "/v1/chat/completions"],
)
def test_valid_json_object_is_accepted_on_every_chat_route(path):
    response = _client().post(path, json=_VALID_BODY)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/api/chat", "/api/chat/stream", "/v1/chat/completions"],
)
def test_json_array_is_rejected_before_route_shape_access(path):
    response = _client().post(path, json=[_VALID_BODY])
    assert response.status_code == 400
    assert "JSON object" in response.json()["error"]["message"]


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_declared_oversize_body_is_rejected_with_413(path):
    response = _client(max_bytes=80).post(
        path,
        json={
            **_VALID_BODY,
            "messages": [{"role": "user", "content": "x" * 200}],
        },
    )
    assert response.status_code == 413
    assert "80-byte limit" in response.json()["error"]["message"]


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_chunked_oversize_body_without_content_length_is_rejected(path):
    raw = json.dumps(
        {
            **_VALID_BODY,
            "messages": [{"role": "user", "content": "x" * 200}],
        }
    ).encode()
    response = _client(max_bytes=100).post(
        path,
        content=_chunked(raw),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_chunked_body_under_limit_remains_supported():
    raw = json.dumps(_VALID_BODY).encode()
    response = _client().post(
        "/api/chat",
        content=_chunked(raw, size=3),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "auto_fields"),
    [
        ("/api/chat", {"context": {"smart_routing": True}}),
        ("/v1/chat/completions", {"model": "auto"}),
    ],
)
def test_auto_routing_does_not_bypass_resource_validation(path, auto_fields):
    response = _client().post(
        path,
        json={
            **_VALID_BODY,
            **auto_fields,
            "max_tokens": 1_000_000,
        },
    )
    assert response.status_code == 400
    assert "max_tokens" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"{not-json", "application/json"),
        (json.dumps(_VALID_BODY).encode(), "text/plain"),
        (b'{"temperature": NaN}', "application/json"),
    ],
)
def test_malformed_or_unsupported_content_is_stable_400(body, content_type):
    response = _client().post(
        "/api/chat",
        content=body,
        headers={"content-type": content_type},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


@pytest.mark.parametrize(
    "body",
    [
        (
            b'{"model":"m","model":"other",'
            b'"messages":[{"role":"user","content":"hello"}]}'
        ),
        (
            b'{"model":"m","messages":[{"role":"user","content":"hello"}],'
            b'"tools":[{"type":"function","function":{"name":"a","name":"b"}}]}'
        ),
        (
            b'{"model":"m","messages":[{"role":"user","content":"hello"}],'
            b'"unused":1e9999}'
        ),
    ],
)
def test_ambiguous_or_non_finite_json_is_rejected(body):
    response = _client().post(
        "/api/chat",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid JSON in request body"


def test_native_stream_does_not_expose_internal_exception_text():
    class _FailingAgent(_FakeClientAgent):
        async def chat_stream(self, model, messages, **kwargs):
            if False:
                yield {}
            raise RuntimeError("provider-secret-and-internal-hostname")

    app = Starlette(
        routes=create_chat_routes(ChatAPI(_FailingAgent()))
    )
    response = TestClient(app).post("/api/chat/stream", json=_VALID_BODY)

    assert response.status_code == 200
    assert "Internal server error" in response.text
    assert "provider-secret-and-internal-hostname" not in response.text


def _streaming_request(
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
) -> tuple[Request, list[int]]:
    events = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": idx < len(chunks) - 1,
        }
        for idx, chunk in enumerate(chunks)
    ]
    receive_calls: list[int] = []

    async def receive():
        receive_calls.append(1)
        return events.pop(0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("test", 1),
        "server": ("testserver", 80),
    }
    return Request(scope, receive), receive_calls


@pytest.mark.asyncio
async def test_stream_reader_stops_as_soon_as_chunks_cross_limit():
    request, calls = _streaming_request(
        [b"{" + b"x" * 59, b"x" * 60, b"x" * 1000],
        [(b"content-type", b"application/json")],
    )
    with pytest.raises(JSONBodyError) as raised:
        await read_json_object(request, max_bytes=100)
    assert raised.value.status_code == 413
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_lying_content_length_is_rejected_after_bounded_read():
    request, _ = _streaming_request(
        [json.dumps(_VALID_BODY).encode()],
        [
            (b"content-type", b"application/json"),
            (b"content-length", b"1"),
        ],
    )
    with pytest.raises(JSONBodyError) as raised:
        await read_json_object(request, max_bytes=256)
    assert raised.value.status_code == 400
    assert "does not match" in raised.value.message
