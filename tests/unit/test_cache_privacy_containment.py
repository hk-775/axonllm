"""Adversarial tests for request-level cache privacy containment."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailRule,
    Project,
    RateLimitResult,
    ResolvedPolicy,
    TokenUsage,
)
from src.gateway.quota_enforcer import QuotaEnforcer
from src.gateway.security.pii_redactor import PIIRedactor
from src.gateway.semantic_cache import SemanticCache


PROJECT_ID = "proj-cache-privacy"
MODEL = "test-model"


def _response(content: str, response_id: str = "resp-1") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=response_id,
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        usage=TokenUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
        model=MODEL,
        provider="test-provider",
    )


class _AllowRateLimiter:
    async def check_rate_limit(
        self,
        user_id: str,
        project_id: str,
    ) -> RateLimitResult:
        return RateLimitResult(
            allowed=True,
            limit=1_000,
            remaining=999,
            reset_at=datetime.now(timezone.utc),
            retry_after_seconds=None,
        )


class _PolicyResolver:
    def __init__(self, policy: ResolvedPolicy) -> None:
        self.policy = policy

    async def resolve(self, project_id: str) -> ResolvedPolicy:
        return self.policy


class _EchoRouter:
    """Returns request-specific content so a cache hit is visible."""

    def __init__(self) -> None:
        self.requests: list[ChatCompletionRequest] = []
        self._smart_strategy = None

    def get_fallback_chain(self, model: str) -> list:
        return []

    async def execute_with_fallback(
        self,
        request: ChatCompletionRequest,
        provider_fn,
        **kwargs,
    ) -> ChatCompletionResponse:
        self.requests.append(request)
        prompt = request.messages[0].get("content", "")
        content = (
            "Contact [EMAIL_1]"
            if "[EMAIL_1]" in prompt
            else f"live response {len(self.requests)}"
        )
        return _response(content, response_id=f"resp-{len(self.requests)}")

    async def smart_route(
        self,
        request: ChatCompletionRequest,
        provider_fn,
        prompt: str,
        **kwargs,
    ):
        self.requests.append(request)
        decision = SimpleNamespace(
            task_type="general",
            confidence=1.0,
            selected_model=MODEL,
            benchmark_score=1.0,
            candidates_considered=[MODEL],
            used_fallback=False,
            cost_quality_tradeoff="balanced",
        )
        return (
            _response(
                f"live response {len(self.requests)}",
                response_id=f"resp-{len(self.requests)}",
            ),
            decision,
        )


class _Embedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0, 0.0]


def _make_agent(
    *,
    semantic_cache: SemanticCache | None = None,
    pii_reinject: bool = True,
) -> tuple[GatewayAgent, _EchoRouter, CacheManager]:
    router = _EchoRouter()
    cache = CacheManager()
    project = Project(
        project_id=PROJECT_ID,
        name="Cache Privacy",
        cache_enabled=True,
        cache_ttl_seconds=300,
        semantic_cache_enabled=semantic_cache is not None,
    )
    policy = ResolvedPolicy(
        pii_redaction_enabled=True,
        pii_redact_types=["email"],
        pii_reinject=pii_reinject,
    )
    agent = GatewayAgent(
        router=router,
        rate_limiter=_AllowRateLimiter(),
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache,
        cost_tracker=CostTracker(pricing_config={}),
        projects={PROJECT_ID: project},
        quota_enforcer=QuotaEnforcer(),
        policy_resolver=_PolicyResolver(policy),
        pii_redactor=PIIRedactor(),
        semantic_cache=semantic_cache,
    )
    return agent, router, cache


def _request(content: str, **overrides) -> dict:
    request = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
    }
    request.update(overrides)
    return request


def _context(user_id: str) -> dict:
    return {"project_id": PROJECT_ID, "user_id": user_id}


def _tenant_context(
    user_id: str,
    tenant_id: str,
    *,
    semantic_cache_enabled: bool = False,
) -> dict:
    project = Project(
        project_id=PROJECT_ID,
        tenant_id=tenant_id,
        name=f"Cache Privacy {tenant_id}",
        cache_enabled=True,
        cache_ttl_seconds=300,
        semantic_cache_enabled=semantic_cache_enabled,
    )
    return {
        "project_id": PROJECT_ID,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "authorized_project": project,
    }


def _spy_exact_cache(cache: CacheManager) -> None:
    cache.get = AsyncMock(wraps=cache.get)
    cache.put = AsyncMock(wraps=cache.put)


def _spy_semantic_cache(cache: SemanticCache) -> None:
    cache.get = AsyncMock(wraps=cache.get)
    cache.put = AsyncMock(wraps=cache.put)


async def test_reversible_pii_cannot_read_or_write_poisoned_exact_cache():
    agent, router, cache = _make_agent()
    normalized_prompt = "Contact [EMAIL_1] about the invoice"
    normalized = ChatCompletionRequest(
        model=MODEL,
        messages=[{"role": "user", "content": normalized_prompt}],
    )
    poisoned_key = cache.compute_cache_key(normalized, PROJECT_ID)
    await cache.put(
        poisoned_key,
        _response("Contact alice@example.com", response_id="alice-cache"),
        ttl_seconds=300,
    )
    _spy_exact_cache(cache)

    result = await agent.handle_chat_completion(
        _request("Contact bob@example.com about the invoice"),
        _context("bob"),
    )

    assert result["choices"][0]["message"]["content"] == "Contact bob@example.com"
    assert "alice@example.com" not in result["choices"][0]["message"]["content"]
    assert len(router.requests) == 1
    assert router.requests[0].messages[0]["content"] == normalized_prompt
    cache.get.assert_not_awaited()
    cache.put.assert_not_awaited()


async def test_reversible_pii_cannot_read_or_write_poisoned_semantic_cache():
    embedder = _Embedder()
    semantic = SemanticCache(embedder)
    agent, router, exact = _make_agent(semantic_cache=semantic)
    alice_normalized = ChatCompletionRequest(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": "Please email [EMAIL_1] about the invoice",
        }],
    )
    await semantic.put(
        alice_normalized,
        PROJECT_ID,
        _response("Contact alice@example.com", response_id="alice-semantic"),
        ttl_seconds=300,
    )
    embedder.calls.clear()
    _spy_exact_cache(exact)
    _spy_semantic_cache(semantic)

    result = await agent.handle_chat_completion(
        _request("Could you email bob@example.com about the invoice"),
        _context("bob"),
    )

    assert result["choices"][0]["message"]["content"] == "Contact bob@example.com"
    assert "alice@example.com" not in result["choices"][0]["message"]["content"]
    assert len(router.requests) == 1
    assert embedder.calls == []
    exact.get.assert_not_awaited()
    exact.put.assert_not_awaited()
    semantic.get.assert_not_awaited()
    semantic.put.assert_not_awaited()


async def test_irreversible_pii_also_bypasses_shared_cache():
    agent, router, exact = _make_agent(pii_reinject=False)
    _spy_exact_cache(exact)

    await agent.handle_chat_completion(
        _request("Contact alice@example.com about the invoice"),
        _context("alice"),
    )
    await agent.handle_chat_completion(
        _request("Contact bob@example.com about the invoice"),
        _context("bob"),
    )

    assert len(router.requests) == 2
    exact.get.assert_not_awaited()
    exact.put.assert_not_awaited()


async def test_stream_request_bypasses_exact_and_semantic_cache():
    embedder = _Embedder()
    semantic = SemanticCache(embedder)
    agent, router, exact = _make_agent(semantic_cache=semantic)
    data = _request("Explain the refund policy", stream=True)
    cache_request = ChatCompletionRequest(
        model=MODEL,
        messages=data["messages"],
        stream=True,
    )
    await exact.put(
        exact.compute_cache_key(cache_request, PROJECT_ID),
        _response("poisoned buffered response"),
        ttl_seconds=300,
    )
    _spy_exact_cache(exact)
    _spy_semantic_cache(semantic)

    result = await agent.handle_chat_completion(data, _context("stream-user"))
    chunks = [chunk async for chunk in result]
    text = "".join(
        choice.get("delta", {}).get("content", "")
        for chunk in chunks
        if isinstance(chunk.get("data"), dict)
        for choice in chunk["data"].get("choices", [])
    )

    assert text == "live response 1"
    assert len(router.requests) == 1
    assert embedder.calls == []
    exact.get.assert_not_awaited()
    exact.put.assert_not_awaited()
    semantic.get.assert_not_awaited()
    semantic.put.assert_not_awaited()


_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_invoice",
        "parameters": {"type": "object", "properties": {}},
    },
}


@pytest.mark.parametrize(
    "cache_sensitive_fields",
    [
        pytest.param({"tools": [_TOOL]}, id="tools"),
        pytest.param({"tools": []}, id="empty-tools-field"),
        pytest.param({"tool_choice": "auto"}, id="tool-choice"),
    ],
)
async def test_tool_fields_bypass_exact_and_semantic_cache(
    cache_sensitive_fields,
):
    embedder = _Embedder()
    semantic = SemanticCache(embedder)
    agent, router, exact = _make_agent(semantic_cache=semantic)
    data = _request("Explain the refund policy", **cache_sensitive_fields)
    cache_request = ChatCompletionRequest(
        model=MODEL,
        messages=data["messages"],
        tools=data.get("tools"),
        tool_choice=data.get("tool_choice"),
    )
    await exact.put(
        exact.compute_cache_key(cache_request, PROJECT_ID),
        _response("poisoned tool response"),
        ttl_seconds=300,
    )
    _spy_exact_cache(exact)
    _spy_semantic_cache(semantic)

    first = await agent.handle_chat_completion(data, _context("tool-user"))
    second = await agent.handle_chat_completion(data, _context("tool-user"))

    assert first["choices"][0]["message"]["content"] == "live response 1"
    assert second["choices"][0]["message"]["content"] == "live response 2"
    assert len(router.requests) == 2
    assert embedder.calls == []
    exact.get.assert_not_awaited()
    exact.put.assert_not_awaited()
    semantic.get.assert_not_awaited()
    semantic.put.assert_not_awaited()


@pytest.mark.parametrize(
    ("request_fields", "context_fields"),
    [
        pytest.param(
            {"model": ""},
            {"smart_routing": True},
            id="smart-routing",
        ),
        pytest.param(
            {},
            {"provider": "forced-provider"},
            id="forced-provider",
        ),
        pytest.param(
            {},
            {"data_residency_zone": "eu"},
            id="data-residency",
        ),
        pytest.param(
            {},
            {"preferred_region": "eu-west-1"},
            id="preferred-region",
        ),
    ],
)
async def test_policy_sensitive_routing_context_bypasses_cache(
    request_fields,
    context_fields,
):
    embedder = _Embedder()
    semantic = SemanticCache(embedder)
    agent, router, exact = _make_agent(semantic_cache=semantic)
    if context_fields.get("smart_routing"):
        router._smart_strategy = SimpleNamespace(classifier=None)
    _spy_exact_cache(exact)
    _spy_semantic_cache(semantic)
    data = _request("Explain the refund policy", **request_fields)
    context = {**_context("policy-user"), **context_fields}

    await agent.handle_chat_completion(data, context)
    await agent.handle_chat_completion(data, context)

    assert len(router.requests) == 2
    assert embedder.calls == []
    exact.get.assert_not_awaited()
    exact.put.assert_not_awaited()
    semantic.get.assert_not_awaited()
    semantic.put.assert_not_awaited()


async def test_ordinary_buffered_non_pii_request_still_uses_exact_cache():
    agent, router, exact = _make_agent()
    data = _request("Explain the refund policy")

    first = await agent.handle_chat_completion(data, _context("ordinary-user"))
    second = await agent.handle_chat_completion(data, _context("ordinary-user"))

    assert first.get("is_cached") is not True
    assert second["is_cached"] is True
    assert second["choices"][0]["message"]["content"] == "live response 1"
    assert len(router.requests) == 1
    assert len(exact._cache) == 1


async def test_exact_cache_isolated_for_same_project_id_across_tenants():
    agent, router, _ = _make_agent()
    data = _request("Explain the refund policy")
    tenant_a = _tenant_context("user-a", "tenant-a")
    tenant_b = _tenant_context("user-b", "tenant-b")

    first_a = await agent.handle_chat_completion(data, tenant_a)
    first_b = await agent.handle_chat_completion(data, tenant_b)
    second_a = await agent.handle_chat_completion(data, tenant_a)
    second_b = await agent.handle_chat_completion(data, tenant_b)

    assert first_a["choices"][0]["message"]["content"] == "live response 1"
    assert first_b["choices"][0]["message"]["content"] == "live response 2"
    assert second_a["choices"][0]["message"]["content"] == "live response 1"
    assert second_b["choices"][0]["message"]["content"] == "live response 2"
    assert second_a["is_cached"] is True
    assert second_b["is_cached"] is True
    assert len(router.requests) == 2


async def test_semantic_cache_isolated_for_same_project_id_across_tenants():
    semantic = SemanticCache(_Embedder())
    agent, router, _ = _make_agent(semantic_cache=semantic)
    tenant_a = _tenant_context(
        "user-a",
        "tenant-a",
        semantic_cache_enabled=True,
    )
    tenant_b = _tenant_context(
        "user-b",
        "tenant-b",
        semantic_cache_enabled=True,
    )

    first_a = await agent.handle_chat_completion(
        _request("Explain the refund policy for annual plans"),
        tenant_a,
    )
    first_b = await agent.handle_chat_completion(
        _request("Describe the refund policy for annual plans"),
        tenant_b,
    )
    second_b = await agent.handle_chat_completion(
        _request("Tell me about the refund policy for annual plans"),
        tenant_b,
    )

    assert first_a["choices"][0]["message"]["content"] == "live response 1"
    assert first_b["choices"][0]["message"]["content"] == "live response 2"
    assert second_b["choices"][0]["message"]["content"] == "live response 2"
    assert second_b["cache_type"] == "semantic"
    assert len(router.requests) == 2


async def test_cache_hit_rechecks_current_output_policy_and_is_audited():
    agent, router, _ = _make_agent()
    audit = SimpleNamespace(record_llm_request=AsyncMock())
    agent._audit_trail = audit
    data = _request("Explain the refund policy")

    first = await agent.handle_chat_completion(data, _context("ordinary-user"))
    agent._projects[PROJECT_ID].guardrail_rules = [
        GuardrailRule(
            name="new_cache_response_rule",
            rule_type="keyword_block",
            pattern="live response 1",
            action="block",
            applies_to="response",
        )
    ]
    second = await agent.handle_chat_completion(data, _context("ordinary-user"))

    assert first["choices"][0]["message"]["content"] == "live response 1"
    assert second["is_cached"] is True
    assert "live response 1" not in second["choices"][0]["message"]["content"]
    assert "new_cache_response_rule" in second["choices"][0]["message"]["content"]
    assert len(router.requests) == 1
    assert audit.record_llm_request.await_count == 2


async def test_exact_cache_isolated_by_top_level_system_instruction():
    agent, router, _ = _make_agent()
    prompt = "Explain the refund policy"

    first = await agent.handle_chat_completion(
        _request(prompt, system="Answer for customers."),
        _context("ordinary-user"),
    )
    second = await agent.handle_chat_completion(
        _request(prompt, system="Answer for support agents."),
        _context("ordinary-user"),
    )

    assert first["choices"][0]["message"]["content"] == "live response 1"
    assert second["choices"][0]["message"]["content"] == "live response 2"
    assert len(router.requests) == 2


@pytest.mark.parametrize(
    "context_fields",
    [
        pytest.param(
            {"system": "Answer for customers."},
            id="top-level-system",
        ),
        pytest.param(
            {
                "messages": [
                    {"role": "system", "content": "Answer for customers."},
                    {"role": "user", "content": "Explain the refund policy"},
                ]
            },
            id="system-message",
        ),
        pytest.param(
            {
                "messages": [
                    {"role": "assistant", "content": "Earlier context."},
                    {"role": "user", "content": "Explain the refund policy"},
                ]
            },
            id="assistant-context",
        ),
    ],
)
async def test_semantic_cache_skips_system_and_conversation_context(
    context_fields,
):
    embedder = _Embedder()
    semantic = SemanticCache(embedder)
    agent, router, _ = _make_agent(semantic_cache=semantic)
    data = _request("Explain the refund policy", **context_fields)

    await agent.handle_chat_completion(data, _context("ordinary-user"))
    await agent.handle_chat_completion(data, _context("ordinary-user"))

    # Exact caching may still serve an identical contextual request, but the
    # semantic layer must never embed or reuse it.
    assert semantic.stats.skipped >= 1
    assert embedder.calls == []
    assert len(router.requests) == 1
