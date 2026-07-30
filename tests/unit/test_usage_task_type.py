"""Regression tests for #B8 — the task type stamped on a UsageRecord.

The bug: `UserEfficiencyProfile.dominant_task_type` was a hardcoded "general".
The literal was only the visible half. The real gap was that `UsageRecord` had
no `task_type` at all: the classifier ran per request, its result was used to
route, and then it was thrown away. By profile-build time there was nothing to
aggregate, so the constant was standing in for data that never existed.

These tests cover the two halves the mode in `semantic_efficiency` depends on
(the mode itself is pinned in test_semantic_efficiency.py):

- the field survives a DynamoDB round trip, and a row written *before* the field
  existed reads back as "" rather than as a task type
- the agent populates it on both UsageRecord construction sites (the main path
  and the streaming path), from the smart decision when there is one and from
  the classifier otherwise

The distinction that has to hold everywhere: "" means "not classified" and is
NOT the same as the classifier having returned "general". Collapsing the two
recreates the original bug for historical data, which is the population most
likely to be affected by it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.gateway.agent import GatewayAgent, extract_last_user_prompt
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionResponse,
    ProviderModelMapping,
    RateLimitResult,
    SmartRoutingDecision,
    StreamChunk,
    TokenPricing,
    TokenUsage,
    UsageRecord,
)
from src.gateway.persistence import DynamoPersistence
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import Router
from src.gateway.task_classifier import TaskClassifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_iter(items):
    for item in items:
        yield item


def _make_record(task_type: str = "math") -> UsageRecord:
    return UsageRecord(
        request_id="req-1",
        project_id="proj-1",
        user_id="alice",
        provider="bedrock",
        model="claude-sonnet",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost=0.01,
        timestamp=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        task_type=task_type,
    )


def _make_response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"index": 0, "message": {"role": "assistant", "content": "24"}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet",
        provider="anthropic",
    )


def _make_decision(task_type: str = "coding") -> SmartRoutingDecision:
    return SmartRoutingDecision(
        task_type=task_type,
        confidence=0.85,
        selected_model="claude-sonnet",
        benchmark_score=90.0,
        candidates_considered=[],
        used_fallback=False,
        cost_quality_tradeoff=0.3,
    )


@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=True, limit=60, remaining=59,
            reset_at=datetime.now(timezone.utc), retry_after_seconds=None,
        )
    )
    return rl


@pytest.fixture
def cost_tracker():
    return CostTracker(pricing_config={
        "anthropic": {
            "claude-sonnet": TokenPricing(
                prompt_token_cost=0.003, completion_token_cost=0.015
            ),
        }
    })


def _build_agent(cost_tracker, rate_limiter, *, with_classifier: bool) -> GatewayAgent:
    """An agent whose router either has a real classifier attached or none.

    `with_classifier=False` is the realistic shape for a deployment that never
    enabled smart routing — the aggregate has to degrade to "" there, not guess.
    """
    router = MagicMock(spec=Router)
    if with_classifier:
        strategy = MagicMock()
        strategy.classifier = TaskClassifier()
        router._smart_strategy = strategy
    else:
        router._smart_strategy = None
    response = _make_response()
    router.execute_with_fallback = AsyncMock(return_value=response)
    router.model_registry = MagicMock()

    factory = MagicMock()
    factory.create = MagicMock(return_value=AsyncMock(return_value=response))

    return GatewayAgent(
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=cost_tracker,
        provider_fn_factory=factory,
    )


# ---------------------------------------------------------------------------
# Persistence round trip
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    def test_task_type_survives_a_round_trip(self):
        item = DynamoPersistence.serialize_usage_record(_make_record("math"))
        assert item["task_type"] == "math"
        assert DynamoPersistence.deserialize_usage_record(item).task_type == "math"

    def test_a_row_written_before_the_field_existed_reads_as_empty(self):
        """The migration case: no `task_type` key at all.

        Reading it back as "general" would be the original bug, moved one layer
        down and applied to every historical row.
        """
        item = DynamoPersistence.serialize_usage_record(_make_record("math"))
        del item["task_type"]

        record = DynamoPersistence.deserialize_usage_record(item)
        assert record.task_type == ""
        assert record.task_type != "general"

    def test_unclassified_records_round_trip_as_unclassified(self):
        item = DynamoPersistence.serialize_usage_record(_make_record(""))
        assert DynamoPersistence.deserialize_usage_record(item).task_type == ""

    def test_default_is_empty_not_general(self):
        """Pinned on the model itself, since every other default flows from it."""
        assert UsageRecord.__dataclass_fields__["task_type"].default == ""


# ---------------------------------------------------------------------------
# Prompt extraction (shared by routing and classification)
# ---------------------------------------------------------------------------


class TestExtractLastUserPrompt:
    """Moved to module scope so `_stream_true` can reach it; behaviour unchanged.

    Pinned here because usage classification now depends on it agreeing with
    what smart routing considered "the prompt" for the same request.
    """

    def test_plain_string_content(self):
        assert extract_last_user_prompt(
            [{"role": "user", "content": "what is 4!"}]
        ) == "what is 4!"

    def test_last_user_message_wins(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        assert extract_last_user_prompt(messages) == "second"

    def test_skips_tool_results_and_null_assistant_turns(self):
        """Mid-tool-loop shape: the final message is not the user's prompt."""
        messages = [
            {"role": "user", "content": "what is the weather"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
        ]
        assert extract_last_user_prompt(messages) == "what is the weather"

    def test_content_blocks_are_joined(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "solve"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "4!"},
        ]}]
        assert extract_last_user_prompt(messages) == "solve 4!"

    def test_no_user_message_is_empty_not_an_error(self):
        assert extract_last_user_prompt([{"role": "system", "content": "hi"}]) == ""

    def test_none_and_empty_are_empty(self):
        assert extract_last_user_prompt(None) == ""
        assert extract_last_user_prompt([]) == ""

    def test_malformed_entries_are_skipped(self):
        assert extract_last_user_prompt(["not a dict", None]) == ""


# ---------------------------------------------------------------------------
# _classify_for_usage
# ---------------------------------------------------------------------------


class TestClassifyForUsage:
    def test_prefers_the_smart_decision(self, cost_tracker, mock_rate_limiter):
        """Re-classifying could disagree with the model actually selected."""
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=True)

        # The prompt classifies as math; the decision says coding. The decision wins.
        assert agent._classify_for_usage(
            "what is 4!", _make_decision("coding")
        ) == "coding"

    def test_classifies_when_there_is_no_decision(self, cost_tracker, mock_rate_limiter):
        """Most requests never go through smart routing.

        Without this fallback the per-user aggregate would describe only the
        auto-select minority and silently omit everyone else.
        """
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=True)

        assert agent._classify_for_usage("what is 4!") == "math"

    def test_no_classifier_configured_yields_empty(self, cost_tracker, mock_rate_limiter):
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=False)

        assert agent._classify_for_usage("what is 4!") == ""

    def test_empty_prompt_yields_empty(self, cost_tracker, mock_rate_limiter):
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=True)

        assert agent._classify_for_usage("") == ""

    def test_a_raising_classifier_does_not_break_the_request(
        self, cost_tracker, mock_rate_limiter
    ):
        """This runs after the response is in hand.

        A classifier failure must cost one record's reporting detail, not turn a
        served request into an error.
        """
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=True)
        agent.router._smart_strategy.classifier.classify = MagicMock(
            side_effect=RuntimeError("boom")
        )

        assert agent._classify_for_usage("what is 4!") == ""

    def test_a_decision_with_no_task_type_falls_back_to_the_prompt(
        self, cost_tracker, mock_rate_limiter
    ):
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=True)

        assert agent._classify_for_usage(
            "what is 4!", _make_decision(task_type="")
        ) == "math"


# ---------------------------------------------------------------------------
# End-to-end: the record the agent actually writes
# ---------------------------------------------------------------------------


class TestRecordedUsageCarriesTaskType:
    @pytest.mark.asyncio
    async def test_non_smart_request_records_a_classified_task_type(
        self, cost_tracker, mock_rate_limiter
    ):
        """The path #B8's aggregate reads from — an ordinary explicit-model call."""
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=True)

        await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "what is 4!"}],
             "model": "claude-sonnet"},
            {"user_id": "alice", "project_id": "proj-1",
             "roles": ["developer"], "scopes": ["chat"]},
        )

        assert [r.task_type for r in cost_tracker._records] == ["math"]

    @pytest.mark.asyncio
    async def test_without_a_classifier_the_record_is_unclassified(
        self, cost_tracker, mock_rate_limiter
    ):
        agent = _build_agent(cost_tracker, mock_rate_limiter, with_classifier=False)

        await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "what is 4!"}],
             "model": "claude-sonnet"},
            {"user_id": "alice", "project_id": "proj-1",
             "roles": ["developer"], "scopes": ["chat"]},
        )

        assert [r.task_type for r in cost_tracker._records] == [""]

    @pytest.mark.asyncio
    async def test_the_streaming_path_records_a_task_type_too(
        self, cost_tracker, mock_rate_limiter
    ):
        """The second UsageRecord construction site.

        `_stream_true` accounts for the stream in `_finalize_stream`, a separate
        method that never sees the smart decision — so the value is classified in
        `_stream_true` and threaded down. Without this test the streaming half of
        every deployment would keep writing unclassified records while the
        blocking half looked fixed.
        """
        chain = [ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")]
        chunks = [
            StreamChunk(id="c", choices=[{"index": 0, "delta": {"content": "24"}}],
                        model="claude-sonnet"),
            StreamChunk(id="c", choices=[{"index": 0, "delta": {},
                                          "finish_reason": "stop"}],
                        model="claude-sonnet", is_final=True,
                        usage=TokenUsage(5, 2, 7)),
        ]

        router = MagicMock(spec=Router)
        strategy = MagicMock()
        strategy.classifier = TaskClassifier()
        router._smart_strategy = strategy
        router._ensemble_config = None
        router.get_fallback_chain = MagicMock(return_value=chain)
        router.health_tracker = MagicMock()
        router.health_tracker.is_healthy = MagicMock(return_value=True)
        router.cooldown_seconds = 60
        router.execute_with_fallback = AsyncMock(
            side_effect=AssertionError("must not make a blocking call")
        )

        http_client = MagicMock()
        http_client.execute_streaming = MagicMock(
            side_effect=lambda *a, **k: _async_iter(chunks)
        )
        factory = MagicMock()
        factory._adapter_registry = {"anthropic": MagicMock()}
        factory._http_client = http_client
        factory.config_for = MagicMock(return_value=MagicMock())
        factory.create = MagicMock(return_value=AsyncMock())

        agent = GatewayAgent(
            router=router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
            provider_fn_factory=factory,
        )

        gen = await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "what is 4!"}],
             "model": "claude-sonnet", "stream": True},
            {"user_id": "alice", "project_id": "proj-1",
             "roles": ["developer"], "scopes": ["chat"]},
        )
        async for _ in gen:
            pass

        assert [r.task_type for r in cost_tracker._records] == ["math"]
