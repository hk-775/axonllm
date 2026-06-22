# Feature: project-model-access, Properties 1-6: Project model access property tests
"""Property-based tests for project model access management.

Properties covered:
  1 – Add model membership: after add, model is in allowed_models
  2 – Add model idempotence: adding same model twice produces same list as adding once
  3 – Remove model absence: after remove, model is not in allowed_models
  4 – Add-remove round trip: add then remove restores original list
  5 – List reflects mutations: GET returns exactly the expected set after operations
  6 – Gateway enforcement consistency: models not in allowed_models are rejected with 403
"""

from __future__ import annotations

import asyncio
import copy

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
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
    RateLimitConfig,
    TokenUsage,
    VirtualModelConfig,
    ProviderModelMapping,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import Router, AllProvidersExhaustedError


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def model_names() -> st.SearchStrategy[str]:
    """Generate valid model name strings (letters, digits, hyphens, min length 1)."""
    return st.text(
        alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
        min_size=1,
        max_size=30,
    ).filter(lambda s: not s.startswith("-") and not s.endswith("-"))


def model_name_lists(min_size: int = 0, max_size: int = 10) -> st.SearchStrategy[list[str]]:
    """Generate lists of unique model names."""
    return st.lists(
        model_names(),
        min_size=min_size,
        max_size=max_size,
        unique=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(admin_api: AdminAPI) -> Starlette:
    routes = create_admin_routes(admin_api)
    return Starlette(routes=routes)


def _make_admin_api(allowed_models: list[str] | None = None) -> AdminAPI:
    project = Project(project_id="proj-1", name="Test", allowed_models=allowed_models)
    return AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        projects={"proj-1": project},
    )


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()



# ===========================================================================
# Property 1: Add Model Membership
# Feature: project-model-access, Property 1
# Validates: Requirements 1.1, 1.3
# ===========================================================================


@given(
    initial_models=model_name_lists(),
    new_model=model_names(),
)
@settings(max_examples=100)
def test_add_model_membership(initial_models, new_model):
    """Property 1: After calling add-model with a valid model name on an existing
    project, the model name is present in the project's allowed_models list.

    **Validates: Requirements 1.1, 1.3**
    """
    api = _make_admin_api(allowed_models=list(initial_models))
    client = TestClient(_make_app(api), raise_server_exceptions=False)

    resp = client.post("/admin/projects/proj-1/models", json={"model": new_model})
    assert resp.status_code == 200

    data = resp.json()
    assert new_model in data["allowed_models"]

    # Also verify via GET
    get_resp = client.get("/admin/projects/proj-1/models")
    assert new_model in get_resp.json()["allowed_models"]


# ===========================================================================
# Property 2: Add Model Idempotence
# Feature: project-model-access, Property 2
# Validates: Requirements 1.2
# ===========================================================================


@given(
    initial_models=model_name_lists(),
    new_model=model_names(),
)
@settings(max_examples=100)
def test_add_model_idempotence(initial_models, new_model):
    """Property 2: Adding the same model to a project's allowed list multiple times
    produces the same list as adding it once. No duplicate entries.

    **Validates: Requirements 1.2**
    """
    api = _make_admin_api(allowed_models=list(initial_models))
    client = TestClient(_make_app(api), raise_server_exceptions=False)

    # Add once
    resp1 = client.post("/admin/projects/proj-1/models", json={"model": new_model})
    assert resp1.status_code == 200
    list_after_first = resp1.json()["allowed_models"]

    # Add again (same model)
    resp2 = client.post("/admin/projects/proj-1/models", json={"model": new_model})
    assert resp2.status_code == 200
    list_after_second = resp2.json()["allowed_models"]

    # Lists should be identical
    assert list_after_first == list_after_second

    # No duplicates
    assert list_after_second.count(new_model) == 1


# ===========================================================================
# Property 3: Remove Model Absence
# Feature: project-model-access, Property 3
# Validates: Requirements 2.1
# ===========================================================================


@given(
    initial_models=model_name_lists(min_size=1),
    index=st.integers(min_value=0),
)
@settings(max_examples=100)
def test_remove_model_absence(initial_models, index):
    """Property 3: After calling remove-model with a model name that exists in the
    project's allowed list, the model name is not present in the resulting list.

    **Validates: Requirements 2.1**
    """
    model_to_remove = initial_models[index % len(initial_models)]

    api = _make_admin_api(allowed_models=list(initial_models))
    client = TestClient(_make_app(api), raise_server_exceptions=False)

    resp = client.delete(f"/admin/projects/proj-1/models/{model_to_remove}")
    assert resp.status_code == 200

    data = resp.json()
    assert model_to_remove not in data["allowed_models"]

    # Also verify via GET
    get_resp = client.get("/admin/projects/proj-1/models")
    assert model_to_remove not in get_resp.json()["allowed_models"]


# ===========================================================================
# Property 4: Add-Remove Round Trip
# Feature: project-model-access, Property 4
# Validates: Requirements 1.1, 2.1
# ===========================================================================


@given(
    initial_models=model_name_lists(),
    new_model=model_names(),
)
@settings(max_examples=100)
def test_add_remove_round_trip(initial_models, new_model):
    """Property 4: For any model added to a project's allowed list, removing that
    same model restores the list to its original state (minus the added model).

    **Validates: Requirements 1.1, 2.1**
    """
    assume(new_model not in initial_models)

    original = list(initial_models)
    api = _make_admin_api(allowed_models=list(initial_models))
    client = TestClient(_make_app(api), raise_server_exceptions=False)

    # Add
    add_resp = client.post("/admin/projects/proj-1/models", json={"model": new_model})
    assert add_resp.status_code == 200
    assert new_model in add_resp.json()["allowed_models"]

    # Remove
    remove_resp = client.delete(f"/admin/projects/proj-1/models/{new_model}")
    assert remove_resp.status_code == 200

    restored = remove_resp.json()["allowed_models"]
    assert set(restored) == set(original)
    assert new_model not in restored



# ===========================================================================
# Property 5: List Reflects Mutations
# Feature: project-model-access, Property 5
# Validates: Requirements 3.1
# ===========================================================================


@st.composite
def add_remove_operations(draw):
    """Generate a sequence of add/remove operations and the expected final set."""
    initial = draw(model_name_lists(max_size=5))
    expected = set(initial)

    # Generate a sequence of operations
    num_ops = draw(st.integers(min_value=1, max_value=15))
    ops = []
    for _ in range(num_ops):
        action = draw(st.sampled_from(["add", "remove"]))
        model = draw(model_names())
        ops.append((action, model))
        if action == "add":
            expected.add(model)
        elif action == "remove":
            expected.discard(model)

    return initial, ops, expected


@given(data=add_remove_operations())
@settings(max_examples=100)
def test_list_reflects_mutations(data):
    """Property 5: After any sequence of add and remove operations, the GET endpoint
    returns exactly the set of models that should be present based on the operations.

    **Validates: Requirements 3.1**
    """
    initial, ops, expected = data

    api = _make_admin_api(allowed_models=list(initial))
    client = TestClient(_make_app(api), raise_server_exceptions=False)

    for action, model in ops:
        if action == "add":
            client.post("/admin/projects/proj-1/models", json={"model": model})
        elif action == "remove":
            client.delete(f"/admin/projects/proj-1/models/{model}")

    get_resp = client.get("/admin/projects/proj-1/models")
    assert get_resp.status_code == 200
    actual = set(get_resp.json()["allowed_models"])
    assert actual == expected, (
        f"Expected {expected}, got {actual}"
    )


# ===========================================================================
# Property 6: Gateway Enforcement Consistency
# Feature: project-model-access, Property 6
# Validates: Requirements 5.1, 5.2
# ===========================================================================


@given(
    allowed=model_name_lists(min_size=1, max_size=5),
    requested_model=model_names(),
)
@settings(max_examples=100)
def test_gateway_enforcement_consistency(allowed, requested_model):
    """Property 6: For any model not in a project's allowed_models list, the
    GatewayAgent returns a 403 model_not_allowed error. For any model in the list,
    the GatewayAgent does not return a model_not_allowed error.

    **Validates: Requirements 5.1, 5.2**
    """
    project = Project(
        project_id="proj-1",
        name="Test",
        allowed_models=list(allowed),
    )

    # Build a minimal registry with the requested model so routing doesn't fail
    registry = ModelRegistry()
    registry.models[requested_model] = VirtualModelConfig(
        name=requested_model,
        description="test",
        providers=[
            ProviderModelMapping(provider="openai", model_id="gpt-4", fallback_order=1),
        ],
    )

    health_tracker = ProviderHealthTracker()
    router = Router(
        model_registry=registry,
        health_tracker=health_tracker,
        max_retries=0,
        base_delay=0.0,
        cooldown_seconds=60,
    )

    agent = GatewayAgent(
        router=router,
        rate_limiter=SlidingWindowRateLimiter(RateLimitConfig()),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=CostTracker(pricing_config={}),
        projects={"proj-1": project},
    )

    request_data = {
        "model": requested_model,
        "messages": [{"role": "user", "content": "hello"}],
    }
    context = {
        "user_id": "user-1",
        "project_id": "proj-1",
    }

    try:
        result = _run(agent.handle_chat_completion(request_data, context))
    except (NotImplementedError, AllProvidersExhaustedError):
        # These errors mean the request passed the model access check
        # but failed at the provider call stage — that's expected in tests
        # without a real provider. This only happens for allowed models.
        assert requested_model in allowed, (
            f"Model '{requested_model}' is NOT in allowed list {allowed} "
            f"but passed the model access check"
        )
        return

    if requested_model not in allowed:
        # Should be rejected with 403 model_not_allowed
        assert isinstance(result, dict)
        assert result["status_code"] == 403
        assert result["error"]["code"] == "model_not_allowed"
    else:
        # Should NOT be a model_not_allowed error
        # (may fail for other reasons like provider not available, which is fine)
        if isinstance(result, dict) and "error" in result:
            assert result.get("error", {}).get("code") != "model_not_allowed", (
                f"Model '{requested_model}' is in allowed list {allowed} "
                f"but was rejected with model_not_allowed"
            )
