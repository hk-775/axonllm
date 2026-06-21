"""Unit tests for SessionManager."""

import pytest

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    TokenUsage,
)
from src.gateway.session_manager import MemoryClient, SessionManager


class FakeMemoryClient:
    """In-memory fake implementing the MemoryClient protocol."""

    def __init__(self):
        self.events: dict[str, list[dict]] = {}
        self.knowledge: dict[str, list[str]] = {}

    async def get_events(self, session_id: str) -> list[dict]:
        return list(self.events.get(session_id, []))

    async def store_event(self, session_id: str, event: dict) -> None:
        self.events.setdefault(session_id, []).append(event)

    async def store_knowledge(self, session_id: str, facts: list[str]) -> None:
        self.knowledge.setdefault(session_id, []).extend(facts)


def _make_request(
    model: str = "gpt-4",
    messages: list[dict] | None = None,
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=messages or [{"role": "user", "content": "hello"}],
    )


def _make_response(content: str = "Hi there!") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="gpt-4",
        provider="openai",
    )


@pytest.mark.asyncio
async def test_fake_memory_client_satisfies_protocol():
    client = FakeMemoryClient()
    assert isinstance(client, MemoryClient)


@pytest.mark.asyncio
async def test_get_conversation_history_returns_events():
    client = FakeMemoryClient()
    client.events["sess-1"] = [
        {"type": "request", "messages": [{"role": "user", "content": "hi"}]},
        {"type": "response", "choices": [{"message": {"role": "assistant", "content": "hello"}}]},
    ]
    sm = SessionManager(client)
    history = await sm.get_conversation_history("sess-1")
    assert len(history) == 2
    assert history[0]["type"] == "request"
    assert history[1]["type"] == "response"


@pytest.mark.asyncio
async def test_get_conversation_history_empty_session():
    client = FakeMemoryClient()
    sm = SessionManager(client)
    history = await sm.get_conversation_history("nonexistent")
    assert history == []


@pytest.mark.asyncio
async def test_store_exchange_stores_request_and_response_events():
    client = FakeMemoryClient()
    sm = SessionManager(client)

    request = _make_request(
        model="gpt-4",
        messages=[{"role": "user", "content": "What is Python?"}],
    )
    response = _make_response("Python is a programming language.")

    await sm.store_exchange("sess-1", request, response)

    events = client.events["sess-1"]
    assert len(events) == 2

    req_event = events[0]
    assert req_event["type"] == "request"
    assert req_event["messages"] == [{"role": "user", "content": "What is Python?"}]
    assert req_event["model"] == "gpt-4"
    assert "timestamp" in req_event

    resp_event = events[1]
    assert resp_event["type"] == "response"
    assert resp_event["choices"] == response.choices
    assert resp_event["model"] == "gpt-4"
    assert resp_event["usage"]["prompt_tokens"] == 5
    assert resp_event["usage"]["completion_tokens"] == 3
    assert resp_event["usage"]["total_tokens"] == 8
    assert "timestamp" in resp_event


@pytest.mark.asyncio
async def test_store_exchange_timestamps_match():
    client = FakeMemoryClient()
    sm = SessionManager(client)

    await sm.store_exchange("sess-1", _make_request(), _make_response())

    events = client.events["sess-1"]
    assert events[0]["timestamp"] == events[1]["timestamp"]


@pytest.mark.asyncio
async def test_store_semantic_knowledge_delegates_to_memory():
    client = FakeMemoryClient()
    sm = SessionManager(client)

    facts = ["Python was created by Guido van Rossum", "Python 3 was released in 2008"]
    await sm.store_semantic_knowledge("sess-1", facts)

    assert client.knowledge["sess-1"] == facts


@pytest.mark.asyncio
async def test_store_semantic_knowledge_empty_facts():
    client = FakeMemoryClient()
    sm = SessionManager(client)

    await sm.store_semantic_knowledge("sess-1", [])

    assert client.knowledge["sess-1"] == []


@pytest.mark.asyncio
async def test_multiple_exchanges_accumulate():
    client = FakeMemoryClient()
    sm = SessionManager(client)

    await sm.store_exchange("sess-1", _make_request(), _make_response("first"))
    await sm.store_exchange("sess-1", _make_request(), _make_response("second"))

    history = await sm.get_conversation_history("sess-1")
    assert len(history) == 4  # 2 request + 2 response events


@pytest.mark.asyncio
async def test_separate_sessions_are_isolated():
    client = FakeMemoryClient()
    sm = SessionManager(client)

    await sm.store_exchange("sess-1", _make_request(), _make_response("a"))
    await sm.store_exchange("sess-2", _make_request(), _make_response("b"))

    h1 = await sm.get_conversation_history("sess-1")
    h2 = await sm.get_conversation_history("sess-2")

    assert len(h1) == 2
    assert len(h2) == 2
    assert h1[1]["choices"] != h2[1]["choices"]
