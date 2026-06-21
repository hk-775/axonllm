# Feature: access-aware-routing, Properties 1-3
"""Property-based tests for access-aware routing.

Properties covered:
  1 – Effective allowed models intersection
  2 – Router selection respects allowed models
  3 – Filtered models endpoint subset
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Project,
    ProviderModelMapping,
    RateLimitResult,
    RoutingStrategy,
    TokenUsage,
    VirtualModelConfig,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import AllProvidersExhaustedError, Router


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

MODEL_NAMES = ["gpt-4", "claude-3", "llama-3", "gemini-pro", "mistral-7b"]

model_list_strategy = st.lists(
    st.sampled_from(MODEL_NAMES), min_size=0, max_size=5, unique=True,
)

non_empty_model_list_strategy = st.lists(
    st.sampled_from(MODEL_NAMES), min_size=1, max_size=5, unique=True,
)


def _make_agent(project=None, user_configs=None):
    router = MagicMock(spec=Router)
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    return GatewayAgent(
        router=router,
        rate_limiter=rl,
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=CostTracker(pricing_config={}),
        projects={"proj-1": project} if project else {},
        user_configs=user_configs or {},
    )


def _build_registry(model_names: list[str]) -> ModelRegistry:
    registry = ModelRegistry()
    for name in model_names:
        registry.models[name] = VirtualModelConfig(
            name=name, description=f"{name} model",
            providers=[
                ProviderModelMapping(
                    provider=f"{name}-provider", model_id=f"{name}-id", fallback_order=1,
                ),
            ],
        )
    return registry


# ===========================================================================
# Property 1: Effective allowed models intersection
# Feature: access-aware-routing, Property 1
# Validates: Requirement 1, AC 1.1-1.5
# ===========================================================================


@given(
    project_models=st.one_of(st.none(), non_empty_model_list_strategy),
    user_models=st.one_of(st.none(), non_empty_model_list_strategy),
)
@settings(max_examples=200)
def test_effective_allowed_models_intersection(project_models, user_models):
    """Property 1: Effective allowed models intersection.

    For any combination of project and user allowed-models lists, the computed
    effective set equals their set intersection when both are set, the single
    list when only one is set, or None when neither is set.

    **Validates: Requirement 1, AC 1.1-1.5**
    """
    project = Project(
        project_id="proj-1", name="Test",
        allowed_models=project_models,
    ) if project_models is not None else Project(project_id="proj-1", name="Test")

    user_configs = {}
    if user_models is not None:
        user_configs = {"user-1": {"allowed_models": user_models}}

    agent = _make_agent(project=project, user_configs=user_configs)
    result = agent._compute_effective_allowed_models(project, "user-1")

    if project_models is not None and user_models is not None:
        expected = set(project_models) & set(user_models)
        assert result == expected
    elif project_models is not None:
        assert result == set(project_models)
    elif user_models is not None:
        assert result == set(user_models)
    else:
        assert result is None


# ===========================================================================
# Property 2: Router selection respects allowed models
# Feature: access-aware-routing, Property 2
# Validates: Requirement 2 AC 2.1, Requirement 3 AC 3.2, Requirement 6 AC 6.1-6.4
# ===========================================================================


@given(
    allowed_subset=non_empty_model_list_strategy,
    strategy=st.sampled_from(["round-robin", "weighted", "least-latency", "cost-optimized"]),
)
@settings(max_examples=200)
def test_router_selection_respects_allowed_models(allowed_subset, strategy):
    """Property 2: Router selection respects allowed models.

    For any non-empty allowed_models set passed to the Router, the Router
    either succeeds with a provider whose model is in the allowed set, or
    raises AllProvidersExhaustedError if the requested model is not allowed.

    **Validates: Requirement 2 AC 2.1, Requirement 3 AC 3.2, Requirement 6 AC 6.1-6.4**
    """
    # Build a registry with all MODEL_NAMES
    registry = ModelRegistry()
    for name in MODEL_NAMES:
        registry.models[name] = VirtualModelConfig(
            name=name, description=f"{name} model",
            providers=[
                ProviderModelMapping(
                    provider=f"{name}-provider", model_id=f"{name}-id",
                    fallback_order=1, weight=1.0,
                ),
            ],
            routing_strategy=RoutingStrategy(strategy),
        )

    health_tracker = ProviderHealthTracker()
    router = Router(
        model_registry=registry, health_tracker=health_tracker,
        max_retries=0, base_delay=0.0,
    )

    allowed_set = set(allowed_subset)

    # Pick a model to request — could be allowed or not
    for model_name in MODEL_NAMES:
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}], model=model_name,
        )

        async def provider_fn(mapping):
            return ChatCompletionResponse(
                id="resp-1",
                choices=[{"message": {"role": "assistant", "content": "hi"}}],
                usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
                model=model_name, provider=mapping.provider,
            )

        loop = asyncio.new_event_loop()
        try:
            if model_name in allowed_set:
                resp = loop.run_until_complete(
                    router.execute_with_fallback(
                        request, provider_fn, allowed_models=allowed_set,
                    )
                )
                # The response model must be in the allowed set
                assert resp.model in allowed_set, (
                    f"Router selected model {resp.model} which is not in allowed set {allowed_set}"
                )
            else:
                try:
                    loop.run_until_complete(
                        router.execute_with_fallback(
                            request, provider_fn, allowed_models=allowed_set,
                        )
                    )
                    assert False, f"Expected AllProvidersExhaustedError for model {model_name}"
                except AllProvidersExhaustedError:
                    pass  # Expected
        finally:
            loop.close()


# ===========================================================================
# Property 3: Filtered models endpoint subset
# Feature: access-aware-routing, Property 3
# Validates: Requirement 5, AC 5.2-5.4
# ===========================================================================


@given(
    project_models=st.one_of(st.none(), non_empty_model_list_strategy),
    user_models=st.one_of(st.none(), non_empty_model_list_strategy),
)
@settings(max_examples=200)
def test_filtered_models_endpoint_subset(project_models, user_models):
    """Property 3: Filtered models endpoint subset.

    For any project and user allowed-models configuration, the set of model
    names returned by handle_list_models is always a subset of the effective
    allowed models (when non-empty) and a subset of all configured models.

    **Validates: Requirement 5, AC 5.2-5.4**
    """
    all_models = set(MODEL_NAMES)
    registry = _build_registry(MODEL_NAMES)

    mock_router = MagicMock(spec=Router)
    mock_router.model_registry = registry

    project = None
    projects = {}
    if project_models is not None:
        project = Project(project_id="proj-1", name="Test", allowed_models=project_models)
        projects = {"proj-1": project}

    user_configs = {}
    if user_models is not None:
        user_configs = {"user-1": {"allowed_models": user_models}}

    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=MagicMock(spec=SlidingWindowRateLimiter),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=CostTracker(pricing_config={}),
        projects=projects,
        user_configs=user_configs,
    )

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            agent.handle_list_models(
                project_id="proj-1" if project_models is not None else None,
                user_id="user-1" if user_models is not None else None,
            )
        )
    finally:
        loop.close()

    returned_names = {m["name"] for m in result["models"]}

    # Always a subset of all configured models
    assert returned_names <= all_models, (
        f"Returned models {returned_names} not a subset of all models {all_models}"
    )

    # Compute expected effective allowed
    effective = agent._compute_effective_allowed_models(project, "user-1")
    if effective is not None:
        assert returned_names <= effective, (
            f"Returned models {returned_names} not a subset of effective allowed {effective}"
        )
    else:
        # No restrictions — all models should be returned
        assert returned_names == all_models
