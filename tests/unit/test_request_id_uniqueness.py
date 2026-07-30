"""The gateway's request_id must stay unique per request.

Trace/span ids hash from ``UsageRecord.request_id`` (see observability/
otlp_exporter.py) and usage rows de-dupe by it (cost_tracker.load_records), so a
value that repeats across calls collapses many requests into one span and one
usage row.

The non-streaming path used to overwrite the gateway id with ``response.id``.
Provider ids are not guaranteed unique per call: all three Bedrock Mantle routes
fall back to a constant ``"mantle-response"`` when the upstream response carries
no id, so every such request hashed to the same trace and span.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionResponse,
    RateLimitResult,
    TokenPricing,
    TokenUsage,
    UsageRecord,
)
from src.gateway.observability.otlp_exporter import _id_from
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import Router

# The literal every Bedrock Mantle route falls back to when the upstream
# response has no "id" (mantle_provider.py).
MANTLE_FALLBACK_ID = "mantle-response"


def _rate_limiter():
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(return_value=RateLimitResult(
        allowed=True, limit=60, remaining=59,
        reset_at=datetime.now(timezone.utc), retry_after_seconds=None))
    return rl


def _agent(cost_tracker, response_ids, provider="bedrock-mantle", model="gpt-oss-120b"):
    """Agent whose provider returns each id in ``response_ids`` in turn."""
    ids = iter(response_ids)

    def _respond(*_args, **_kwargs):
        return ChatCompletionResponse(
            id=next(ids), model=model, provider=provider,
            choices=[{"index": 0, "message": {"role": "assistant", "content": "hi"},
                      "finish_reason": "stop"}],
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    router = MagicMock(spec=Router)
    router.execute_with_fallback = AsyncMock(side_effect=_respond)
    router._smart_strategy = None
    router._ensemble_config = None

    return GatewayAgent(
        router=router,
        rate_limiter=_rate_limiter(),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=cost_tracker,
    )


def _tracker(provider="bedrock-mantle", model="gpt-oss-120b"):
    return CostTracker(pricing_config={
        provider: {model: TokenPricing(prompt_token_cost=0.001,
                                       completion_token_cost=0.002)}})


async def _run(agent, n, model="gpt-oss-120b"):
    for i in range(n):
        await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": f"hi {i}"}], "model": model},
            {"project_id": "p1", "user_id": "alice"},
        )


class TestMantleConstantResponseId:
    """A provider id that repeats must not become the gateway's request_id."""

    @pytest.mark.asyncio
    async def test_each_request_gets_a_distinct_request_id(self):
        ct = _tracker()
        agent = _agent(ct, itertools.repeat(MANTLE_FALLBACK_ID))
        await _run(agent, 3)

        ids = [r.request_id for r in ct._records]
        assert len(ids) == 3
        assert len(set(ids)) == 3, f"request_ids collapsed: {ids}"
        assert MANTLE_FALLBACK_ID not in ids

    @pytest.mark.asyncio
    async def test_span_ids_do_not_collide(self):
        """The concrete downstream symptom: one span per request, not one total."""
        ct = _tracker()
        agent = _agent(ct, itertools.repeat(MANTLE_FALLBACK_ID))
        await _run(agent, 3)

        spans = {(_id_from(r.request_id, 16), _id_from(r.request_id, 8))
                 for r in ct._records}
        assert len(spans) == 3, "distinct requests hashed to the same trace/span id"

    @pytest.mark.asyncio
    async def test_usage_rows_survive_rehydration(self):
        """Rehydration de-dupes by request_id, so collisions lose spend."""
        ct = _tracker()
        agent = _agent(ct, itertools.repeat(MANTLE_FALLBACK_ID))
        await _run(agent, 5)
        persisted = list(ct._records)

        restarted = CostTracker(pricing_config={})
        restarted.load_records(persisted)

        assert len(restarted._records) == 5
        assert sum(r.cost for r in restarted._records) == pytest.approx(
            sum(r.cost for r in persisted))

    @pytest.mark.asyncio
    async def test_provider_id_is_preserved_for_correlation(self):
        ct = _tracker()
        agent = _agent(ct, itertools.repeat(MANTLE_FALLBACK_ID))
        await _run(agent, 1)

        assert ct._records[0].provider_request_id == MANTLE_FALLBACK_ID


class TestUniqueProviderIdsAlsoWork:
    """The fix must not regress providers that do return unique ids."""

    @pytest.mark.asyncio
    async def test_openai_style_ids_still_distinct(self):
        ct = _tracker(provider="openai", model="gpt-4")
        agent = _agent(ct, (f"chatcmpl-{i}" for i in itertools.count()),
                       provider="openai", model="gpt-4")
        await _run(agent, 3, model="gpt-4")

        assert len({r.request_id for r in ct._records}) == 3
        # The provider's own ids are kept, just not used as the key.
        assert [r.provider_request_id for r in ct._records] == [
            "chatcmpl-0", "chatcmpl-1", "chatcmpl-2"]


class TestGatewayIdShape:
    @pytest.mark.asyncio
    async def test_request_id_is_the_gateway_generated_one(self):
        """Audit and usage rows must agree: both use the gateway's id."""
        ct = _tracker()
        agent = _agent(ct, itertools.repeat(MANTLE_FALLBACK_ID))
        audit = MagicMock()
        audit.record_llm_request = AsyncMock()
        agent._audit_trail = audit
        await _run(agent, 1)

        audited = audit.record_llm_request.await_args.kwargs["request_id"]
        assert audited == ct._records[0].request_id
        assert audited.startswith("req_")


class TestTraceEventCarriesBothIds:
    def test_forwarder_event_includes_provider_request_id(self):
        from src.gateway.observability.trace_forwarder import map_usage_to_trace_event

        ev = map_usage_to_trace_event(UsageRecord(
            request_id="req_abc123", project_id="p1", user_id="alice",
            provider="bedrock-mantle", model="gpt-oss-120b",
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            cost=0.01, timestamp=datetime.now(timezone.utc),
            provider_request_id=MANTLE_FALLBACK_ID,
        ))

        assert ev["metadata"]["request_id"] == "req_abc123"
        assert ev["metadata"]["provider_request_id"] == MANTLE_FALLBACK_ID
