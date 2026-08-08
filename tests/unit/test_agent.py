"""Unit tests for the Gateway Agent orchestration logic."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gateway.agent import GatewayAgent, _error_response, create_gateway_agent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    BudgetStatus,
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailResult,
    GuardrailRule,
    Project,
    ProviderModelMapping,
    RateLimitConfig,
    RateLimitResult,
    RequestContext,
    TokenPricing,
    TokenUsage,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import AllProvidersExhaustedError, Router
from src.gateway.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_response(
    content: str = "Hello!",
    model: str = "gpt-4",
    provider: str = "openai",
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"index": 0, "message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _base_request_data() -> dict:
    return {
        "messages": [{"role": "user", "content": "Hi"}],
        "model": "gpt-4",
    }


def _base_context() -> dict:
    return {
        "user_id": "user-1",
        "project_id": "proj-1",
        "roles": ["developer"],
        "scopes": ["chat"],
    }


def _make_project(**overrides) -> Project:
    defaults = dict(
        project_id="proj-1",
        name="Test Project",
    )
    defaults.update(overrides)
    return Project(**defaults)


@pytest.fixture
def mock_router():
    router = MagicMock(spec=Router)
    response = _make_response()
    router.execute_with_fallback = AsyncMock(return_value=response)
    return router


@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=True, limit=60, remaining=59,
            reset_at=datetime.utcnow(), retry_after_seconds=None,
        )
    )
    return rl


@pytest.fixture
def guardrail_engine():
    return GuardrailEngine()


@pytest.fixture
def cache_manager():
    return CacheManager()


@pytest.fixture
def cost_tracker():
    pricing = {
        "openai": {
            "gpt-4": TokenPricing(prompt_token_cost=0.01, completion_token_cost=0.03),
        }
    }
    return CostTracker(pricing_config=pricing)


@pytest.fixture
def agent(mock_router, mock_rate_limiter, guardrail_engine, cache_manager, cost_tracker):
    return GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_non_streaming_request(agent, mock_router):
    """Successful non-streaming request returns a well-formed response dict."""
    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert isinstance(result, dict)
    assert result["id"] == "resp-1"
    assert result["model"] == "gpt-4"
    assert result["provider"] == "openai"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5
    assert result["choices"][0]["message"]["content"] == "Hello!"
    mock_router.execute_with_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_rejection(agent, mock_rate_limiter):
    """Rate limit exceeded returns a 429-style error dict."""
    mock_rate_limiter.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=False, limit=60, remaining=0,
            reset_at=datetime.utcnow(), retry_after_seconds=30,
        )
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert result["status_code"] == 429
    assert result["error"]["type"] == "rate_limit_error"
    assert "30" in result["error"]["message"]


@pytest.mark.asyncio
async def test_request_guardrail_violation(mock_router, mock_rate_limiter, cache_manager, cost_tracker):
    """Request guardrail violation returns a 400-style error dict."""
    rules = [
        GuardrailRule(
            name="block_badword",
            rule_type="keyword_block",
            pattern="badword",
            action="block",
            applies_to="request",
        )
    ]
    project = _make_project(guardrail_rules=rules)
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        projects={"proj-1": project},
    )

    data = {"messages": [{"role": "user", "content": "say badword"}], "model": "gpt-4"}
    result = await agent.handle_chat_completion(data, _base_context())

    assert result["status_code"] == 400
    assert result["error"]["type"] == "content_policy_violation"
    assert "block_badword" in result["error"]["message"]


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_response(mock_router, mock_rate_limiter, cost_tracker):
    """Cache hit returns the cached response without calling the router."""
    project = _make_project(cache_enabled=True, cache_ttl_seconds=300)
    cm = CacheManager()
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cm,
        cost_tracker=cost_tracker,
        projects={"proj-1": project},
    )

    req_data = _base_request_data()
    ctx = _base_context()

    # Pre-populate cache
    request = ChatCompletionRequest(
        messages=req_data["messages"], model=req_data["model"]
    )
    cache_key = cm.compute_cache_key(request, "proj-1")
    cached_resp = _make_response(content="cached answer")
    await cm.put(cache_key, cached_resp, ttl_seconds=300)

    result = await agent.handle_chat_completion(req_data, ctx)

    assert result["is_cached"] is True
    assert result["choices"][0]["message"]["content"] == "cached answer"
    mock_router.execute_with_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_exceeded_rejects_request(mock_router, mock_rate_limiter, cache_manager):
    """When project budget is exceeded, the request is rejected with 429 before routing."""
    pricing = {
        "openai": {
            "gpt-4": TokenPricing(prompt_token_cost=0.01, completion_token_cost=0.03),
        }
    }
    ct = CostTracker(pricing_config=pricing, budgets={"proj-1": {"budget_limit": 0.0001, "alert_threshold": 0.00005}})
    project = _make_project(budget_limit=0.0001, alert_threshold=0.00005)
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=ct,
        projects={"proj-1": project},
    )

    # Seed a usage record so the project is over budget
    from src.gateway.models import UsageRecord
    await ct.record_usage(UsageRecord(
        request_id="r-1", project_id="proj-1", user_id="user-1",
        provider="openai", model="gpt-4",
        prompt_tokens=100, completion_tokens=100, total_tokens=200,
        cost=1.0, timestamp=datetime.utcnow(),
    ))

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert result["status_code"] == 429
    assert result["error"]["type"] == "budget_exceeded"
    assert "proj-1" in result["error"]["message"]
    mock_router.execute_with_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_guardrail_replaces_content(mock_router, mock_rate_limiter, cache_manager, cost_tracker):
    """Response guardrail violation replaces the response content."""
    # The mock router returns a response containing "secret_data"
    mock_router.execute_with_fallback = AsyncMock(
        return_value=_make_response(content="Here is secret_data for you")
    )
    rules = [
        GuardrailRule(
            name="block_secrets",
            rule_type="keyword_block",
            pattern="secret_data",
            action="block",
            applies_to="response",
        )
    ]
    project = _make_project(guardrail_rules=rules)
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        projects={"proj-1": project},
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    # Content should be replaced with the guardrail message
    content = result["choices"][0]["message"]["content"]
    assert "secret_data" not in content
    assert "block_secrets" in content
    assert "Response modified by guardrail" in result.get("warnings", [])


@pytest.mark.asyncio
async def test_all_providers_exhausted(mock_router, mock_rate_limiter, guardrail_engine, cache_manager, cost_tracker):
    """When all providers fail, a 502 error is returned."""
    mock_router.execute_with_fallback = AsyncMock(
        side_effect=AllProvidersExhaustedError(
            [{"provider": "openai", "status_code": 500, "message": "Internal error"}]
        )
    )
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert result["status_code"] == 502
    assert result["error"]["type"] == "provider_error"


@pytest.mark.asyncio
async def test_streaming_yields_sse_chunks(agent):
    """Streaming mode returns an async generator yielding SSE dicts with a [DONE] marker."""
    data = _base_request_data()
    data["stream"] = True

    result = await agent.handle_chat_completion(data, _base_context())

    # result should be an async generator
    chunks = []
    async for chunk in result:
        chunks.append(chunk)

    # Filter out rate limit header metadata chunks
    data_chunks = [c for c in chunks if "_rate_limit_headers" not in c]

    # Last chunk should be the DONE marker
    assert data_chunks[-1]["data"] == "[DONE]"
    # All preceding chunks should have data dicts with content
    for c in data_chunks[:-1]:
        assert "data" in c
        assert isinstance(c["data"], dict)


@pytest.mark.asyncio
async def test_session_storage_called_when_session_id_present(
    mock_router, mock_rate_limiter, guardrail_engine, cache_manager, cost_tracker
):
    """When session_id is in context, session_manager.store_exchange is called."""
    mock_sm = MagicMock(spec=SessionManager)
    mock_sm.store_exchange = AsyncMock()

    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        session_manager=mock_sm,
    )

    ctx = _base_context()
    ctx["session_id"] = "sess-abc"

    await agent.handle_chat_completion(_base_request_data(), ctx)

    mock_sm.store_exchange.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_gateway_agent_factory():
    """create_gateway_agent wires up and returns a GatewayAgent."""
    router = MagicMock(spec=Router)
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    ge = GuardrailEngine()
    cm = CacheManager()
    ct = CostTracker(pricing_config={})

    agent = create_gateway_agent(router, rl, ge, cm, ct)

    assert isinstance(agent, GatewayAgent)
    assert agent.router is router

# ---------------------------------------------------------------------------
# Task 14.2 — list_models and health_check
# ---------------------------------------------------------------------------

from src.gateway.models import ModelConfig, RoutingStrategy
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry


def _build_registry_with_models() -> ModelRegistry:
    """Build a ModelRegistry pre-loaded with two models."""
    registry = ModelRegistry()
    registry.models = {
        "gpt-4": ModelConfig(
            name="gpt-4",
            description="GPT-4 class model",
            providers=[
                ProviderModelMapping(provider="openai", model_id="gpt-4-turbo", weight=0.7),
                ProviderModelMapping(provider="azure_openai", model_id="gpt-4-turbo-2024", weight=0.3),
            ],
            routing_strategy=RoutingStrategy.WEIGHTED,
            capabilities=["chat", "streaming", "function_calling"],
        ),
        "claude-3": ModelConfig(
            name="claude-3",
            description="Claude 3 class model",
            providers=[
                ProviderModelMapping(provider="anthropic", model_id="claude-3-sonnet"),
            ],
            routing_strategy=RoutingStrategy.COST_OPTIMIZED,
            capabilities=["chat", "streaming"],
        ),
    }
    return registry


@pytest.mark.asyncio
async def test_handle_list_models_returns_all_models(mock_rate_limiter, cache_manager, cost_tracker):
    """handle_list_models returns correct model info for all registered models."""
    registry = _build_registry_with_models()
    health_tracker = ProviderHealthTracker()

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_list_models()

    assert "models" in result
    models = result["models"]
    assert len(models) == 2

    names = {m["name"] for m in models}
    assert names == {"gpt-4", "claude-3"}

    gpt4 = next(m for m in models if m["name"] == "gpt-4")
    assert gpt4["description"] == "GPT-4 class model"
    assert gpt4["providers"] == ["openai", "azure_openai"]
    assert gpt4["capabilities"] == ["chat", "streaming", "function_calling"]
    assert gpt4["routing_strategy"] == "weighted"

    claude = next(m for m in models if m["name"] == "claude-3")
    assert claude["providers"] == ["anthropic"]
    assert claude["routing_strategy"] == "cost-optimized"


@pytest.mark.asyncio
async def test_handle_list_models_empty_registry(mock_rate_limiter, cache_manager, cost_tracker):
    """handle_list_models returns empty list when no models are registered."""
    registry = ModelRegistry()
    health_tracker = ProviderHealthTracker()

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_list_models()

    assert result == {"models": []}


@pytest.mark.asyncio
async def test_handle_list_models_hides_unavailable_provider_only_models(
    mock_rate_limiter,
    cache_manager,
    cost_tracker,
):
    registry = _build_registry_with_models()
    router = Router(
        registry,
        ProviderHealthTracker(),
        available_providers=frozenset({"openai"}),
    )
    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_list_models()

    assert result["models"] == [
        {
            "name": "gpt-4",
            "description": "GPT-4 class model",
            "providers": ["openai"],
            "capabilities": [
                "chat",
                "streaming",
                "function_calling",
            ],
            "routing_strategy": "weighted",
        }
    ]


@pytest.mark.asyncio
async def test_handle_health_check_all_healthy(mock_rate_limiter, cache_manager, cost_tracker):
    """health_check returns 'healthy' for all providers when none are in cooldown."""
    registry = _build_registry_with_models()
    health_tracker = ProviderHealthTracker()

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_health_check()

    assert result["status"] == "ok"
    assert result["providers"]["openai"] == "healthy"
    assert result["providers"]["azure_openai"] == "healthy"
    assert result["providers"]["anthropic"] == "healthy"


@pytest.mark.asyncio
async def test_handle_health_check_with_unhealthy_provider(mock_rate_limiter, cache_manager, cost_tracker):
    """health_check reports 'unhealthy' for providers in cooldown."""
    registry = _build_registry_with_models()
    health_tracker = ProviderHealthTracker()
    health_tracker.mark_unhealthy("openai", cooldown_seconds=300)

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_health_check()

    assert result["status"] == "ok"
    assert result["providers"]["openai"] == "unhealthy"
    assert result["providers"]["azure_openai"] == "healthy"
    assert result["providers"]["anthropic"] == "healthy"


@pytest.mark.asyncio
async def test_list_models_entrypoint_without_agent():
    """list_models entrypoint returns error when agent is not initialised."""
    from src.gateway import agent as agent_module

    original = agent_module._agent
    try:
        agent_module._agent = None
        result = await agent_module.list_models()
        assert result["status_code"] == 500
        assert result["error"]["type"] == "server_error"
    finally:
        agent_module._agent = original


@pytest.mark.asyncio
async def test_health_check_entrypoint_without_agent():
    """health_check entrypoint returns error when agent is not initialised."""
    from src.gateway import agent as agent_module

    original = agent_module._agent
    try:
        agent_module._agent = None
        result = await agent_module.health_check()
        assert result["status_code"] == 500
        assert result["error"]["type"] == "server_error"
    finally:
        agent_module._agent = original



# ---------------------------------------------------------------------------
# Cache write-back and semantic cache
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Returns a fixed vector per prompt; every unlisted prompt gets the same one.

    A shared default means unlisted prompts score 1.0 against each other, which
    is the useful setting for testing the *guards* — it isolates them from the
    similarity threshold.
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self.vectors.get(text, [1.0, 0.0, 0.0])


def _cache_agent(
    mock_router, mock_rate_limiter, cost_tracker, project, *, embedder=None
):
    from src.gateway.semantic_cache import SemanticCache

    cm = CacheManager()
    sc = SemanticCache(embedder) if embedder is not None else None
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cm,
        cost_tracker=cost_tracker,
        projects={project.project_id: project},
        semantic_cache=sc,
    )
    return agent, cm, sc


@pytest.mark.asyncio
async def test_a_repeated_request_is_served_from_the_cache(
    mock_router, mock_rate_limiter, cost_tracker
):
    """Regression: nothing ever called cache_manager.put.

    The read at step 9 existed from the start, but no code path wrote to the
    cache, so a project with cache_enabled=True paid for every repeat and
    reported is_cached=False forever. Green tests either pre-populated the cache
    by hand or asserted only on a miss, so nothing noticed.
    """
    project = _make_project(cache_enabled=True, cache_ttl_seconds=300)
    agent, cm, _ = _cache_agent(mock_router, mock_rate_limiter, cost_tracker, project)

    first = await agent.handle_chat_completion(_base_request_data(), _base_context())
    second = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert first.get("is_cached") is not True
    assert second["is_cached"] is True
    assert mock_router.execute_with_fallback.await_count == 1


@pytest.mark.asyncio
async def test_caching_stays_off_for_a_project_that_did_not_ask_for_it(
    mock_router, mock_rate_limiter, cost_tracker
):
    project = _make_project(cache_enabled=False)
    agent, cm, _ = _cache_agent(mock_router, mock_rate_limiter, cost_tracker, project)

    await agent.handle_chat_completion(_base_request_data(), _base_context())
    await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert mock_router.execute_with_fallback.await_count == 2


@pytest.mark.asyncio
async def test_a_blocked_response_is_not_cached(
    mock_router, mock_rate_limiter, cost_tracker
):
    """Otherwise one guardrail block becomes a block on every matching request.

    The stored content would be the refusal notice, served to later callers with
    no violation of their own to explain it — and it would bypass the guardrail
    engine entirely, since a cache hit returns at step 9.
    """
    mock_router.execute_with_fallback = AsyncMock(
        return_value=_make_response(content="here is the badword")
    )
    rules = [
        GuardrailRule(
            name="block_badword",
            rule_type="keyword_block",
            pattern="badword",
            action="block",
            applies_to="response",
        )
    ]
    project = _make_project(cache_enabled=True, guardrail_rules=rules)
    agent, cm, _ = _cache_agent(mock_router, mock_rate_limiter, cost_tracker, project)

    await agent.handle_chat_completion(_base_request_data(), _base_context())
    assert len(cm._cache) == 0


@pytest.mark.asyncio
async def test_a_streaming_request_does_not_populate_the_cache(
    mock_router, mock_rate_limiter, cost_tracker
):
    """The write sits after the streaming return, so a stream never reaches it."""
    project = _make_project(cache_enabled=True)
    agent, cm, _ = _cache_agent(mock_router, mock_rate_limiter, cost_tracker, project)

    data = {**_base_request_data(), "stream": True}
    result = await agent.handle_chat_completion(data, _base_context())
    # Drain the generator so the streaming path runs to completion.
    if hasattr(result, "__aiter__"):
        async for _ in result:
            pass
    assert len(cm._cache) == 0


@pytest.mark.asyncio
async def test_a_reworded_question_hits_the_semantic_cache(
    mock_router, mock_rate_limiter, cost_tracker
):
    project = _make_project(cache_enabled=True, semantic_cache_enabled=True)
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project,
        embedder=_FakeEmbedder(),
    )

    first = {"messages": [{"role": "user", "content": "what is our refund policy"}],
             "model": "gpt-4"}
    reworded = {"messages": [{"role": "user", "content": "what's the refund policy"}],
                "model": "gpt-4"}

    await agent.handle_chat_completion(first, _base_context())
    result = await agent.handle_chat_completion(reworded, _base_context())

    assert result["is_cached"] is True
    assert result["cache_type"] == "semantic"
    assert mock_router.execute_with_fallback.await_count == 1


@pytest.mark.asyncio
async def test_a_semantic_hit_is_labelled_differently_from_an_exact_hit(
    mock_router, mock_rate_limiter, cost_tracker
):
    """An exact hit is the answer to this question; a semantic hit is the answer
    to one judged equivalent. A caller has to be able to tell them apart."""
    project = _make_project(cache_enabled=True, semantic_cache_enabled=True)
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project,
        embedder=_FakeEmbedder(),
    )

    data = {"messages": [{"role": "user", "content": "what is our refund policy"}],
            "model": "gpt-4"}
    await agent.handle_chat_completion(data, _base_context())
    exact = await agent.handle_chat_completion(data, _base_context())

    assert exact["is_cached"] is True
    assert "cache_type" not in exact


@pytest.mark.asyncio
async def test_the_semantic_cache_is_not_consulted_when_the_project_opts_out(
    mock_router, mock_rate_limiter, cost_tracker
):
    """cache_enabled must not imply semantic_cache_enabled — opting into exact
    matching is not opting into answering a different question."""
    embedder = _FakeEmbedder()
    project = _make_project(cache_enabled=True, semantic_cache_enabled=False)
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project, embedder=embedder,
    )

    first = {"messages": [{"role": "user", "content": "what is our refund policy"}],
             "model": "gpt-4"}
    reworded = {"messages": [{"role": "user", "content": "what's the refund policy"}],
                "model": "gpt-4"}

    await agent.handle_chat_completion(first, _base_context())
    result = await agent.handle_chat_completion(reworded, _base_context())

    assert result.get("is_cached") is not True
    assert embedder.calls == []
    assert mock_router.execute_with_fallback.await_count == 2


@pytest.mark.asyncio
async def test_an_exact_repeat_never_pays_for_an_embedding(
    mock_router, mock_rate_limiter, cost_tracker
):
    """The semantic lookup runs only after the exact key misses, so the cheap
    path stays cheap."""
    embedder = _FakeEmbedder()
    project = _make_project(cache_enabled=True, semantic_cache_enabled=True)
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project, embedder=embedder,
    )

    data = {"messages": [{"role": "user", "content": "what is our refund policy"}],
            "model": "gpt-4"}
    await agent.handle_chat_completion(data, _base_context())
    calls_after_write = len(embedder.calls)
    await agent.handle_chat_completion(data, _base_context())

    assert len(embedder.calls) == calls_after_write


@pytest.mark.asyncio
async def test_differing_numbers_are_not_served_a_semantic_hit(
    mock_router, mock_rate_limiter, cost_tracker
):
    """End to end through the agent: the embedder gives both prompts the same
    vector, so similarity is 1.0 and only the literal guard prevents the wrong
    answer being returned."""
    project = _make_project(cache_enabled=True, semantic_cache_enabled=True)
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project,
        embedder=_FakeEmbedder(),
    )

    await agent.handle_chat_completion(
        {"messages": [{"role": "user", "content": "what is 17 times 23"}], "model": "gpt-4"},
        _base_context(),
    )
    result = await agent.handle_chat_completion(
        {"messages": [{"role": "user", "content": "what is 17 times 24"}], "model": "gpt-4"},
        _base_context(),
    )

    assert result.get("is_cached") is not True
    assert mock_router.execute_with_fallback.await_count == 2
    assert sc.stats.rejected_by_literals == 1


@pytest.mark.asyncio
async def test_a_project_threshold_is_honoured_over_the_gateway_default(
    mock_router, mock_rate_limiter, cost_tracker
):
    embedder = _FakeEmbedder(vectors={
        "what is our refund policy": [1.0, 0.0],
        "what is our shipping policy": [1.0, 0.55],  # cos ~= 0.876
    })
    # 0.85 rather than 0.90: the score has to sit *between* the project value and
    # the gateway default, or the assertion below passes whether or not the
    # project value was read. When the default moved 0.95 -> 0.90 this test kept
    # passing while testing nothing.
    project = _make_project(
        cache_enabled=True, semantic_cache_enabled=True, semantic_cache_threshold=0.85,
    )
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project, embedder=embedder,
    )

    await agent.handle_chat_completion(
        {"messages": [{"role": "user", "content": "what is our refund policy"}], "model": "gpt-4"},
        _base_context(),
    )
    result = await agent.handle_chat_completion(
        {"messages": [{"role": "user", "content": "what is our shipping policy"}], "model": "gpt-4"},
        _base_context(),
    )

    # 0.876 clears the project's 0.85 but not the 0.90 default, so a hit here
    # proves the project value reached the lookup.
    assert result["is_cached"] is True
    assert result["cache_type"] == "semantic"


@pytest.mark.asyncio
async def test_an_embedding_outage_does_not_fail_the_request(
    mock_router, mock_rate_limiter, cost_tracker
):
    class _Broken:
        async def embed(self, text):
            raise RuntimeError("bedrock is down")

    project = _make_project(cache_enabled=True, semantic_cache_enabled=True)
    agent, cm, sc = _cache_agent(
        mock_router, mock_rate_limiter, cost_tracker, project, embedder=_Broken(),
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())
    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert "error" not in result


@pytest.mark.asyncio
async def test_a_gateway_with_no_semantic_cache_still_caches_exactly(
    mock_router, mock_rate_limiter, cost_tracker
):
    """semantic_cache=None is the default for every existing caller."""
    project = _make_project(cache_enabled=True, semantic_cache_enabled=True)
    agent, cm, sc = _cache_agent(mock_router, mock_rate_limiter, cost_tracker, project)
    assert sc is None

    await agent.handle_chat_completion(_base_request_data(), _base_context())
    second = await agent.handle_chat_completion(_base_request_data(), _base_context())
    assert second["is_cached"] is True
