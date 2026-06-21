# Feature: litellm-service, Property 29: Session memory round-trip
"""Property-based tests for SessionManager.

Properties covered:
  29 – Session memory round-trip
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    TokenUsage,
)
from src.gateway.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeMemoryClient:
    """In-memory fake implementing the MemoryClient protocol."""

    def __init__(self):
        self.events: dict[str, list[dict]] = {}

    async def get_events(self, session_id: str) -> list[dict]:
        return list(self.events.get(session_id, []))

    async def store_event(self, session_id: str, event: dict) -> None:
        self.events.setdefault(session_id, []).append(event)

    async def store_knowledge(self, session_id: str, facts: list[str]) -> None:
        pass  # not needed for this property


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=30,
).filter(lambda s: len(s.strip()) > 0)

_message_content = st.text(min_size=1, max_size=100).filter(lambda s: len(s.strip()) > 0)
_model_strategy = st.sampled_from(["gpt-4", "claude-3", "gemini-pro", "command-r"])
_token_count = st.integers(min_value=1, max_value=5000)


# ===========================================================================
# Property 29: Session memory round-trip
# Feature: litellm-service, Property 29: Session memory round-trip
# ===========================================================================


@given(
    session_id=_safe_text,
    user_content=_message_content,
    assistant_content=_message_content,
    model=_model_strategy,
    prompt_tokens=_token_count,
    completion_tokens=_token_count,
)
@settings(max_examples=100)
def test_session_memory_round_trip(
    session_id,
    user_content,
    assistant_content,
    model,
    prompt_tokens,
    completion_tokens,
):
    """Property 29: Session memory round-trip.

    For any session_id and any request/response exchange stored,
    retrieving history includes the stored exchange.

    **Validates: Requirements 11.2, 11.3**
    """
    client = FakeMemoryClient()
    sm = SessionManager(client)

    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": user_content}],
        model=model,
    )
    response = ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": assistant_content}}],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=model,
        provider="openai",
    )

    # Store the exchange
    _run(sm.store_exchange(session_id, request, response))

    # Retrieve history
    history = _run(sm.get_conversation_history(session_id))

    # Must contain at least the request and response events
    assert len(history) >= 2

    # Find the request event
    req_events = [e for e in history if e.get("type") == "request"]
    assert len(req_events) >= 1
    req_event = req_events[-1]
    assert req_event["messages"] == request.messages
    assert req_event["model"] == model

    # Find the response event
    resp_events = [e for e in history if e.get("type") == "response"]
    assert len(resp_events) >= 1
    resp_event = resp_events[-1]
    assert resp_event["choices"] == response.choices
    assert resp_event["model"] == model
    assert resp_event["usage"]["prompt_tokens"] == prompt_tokens
    assert resp_event["usage"]["completion_tokens"] == completion_tokens
    assert resp_event["usage"]["total_tokens"] == prompt_tokens + completion_tokens
