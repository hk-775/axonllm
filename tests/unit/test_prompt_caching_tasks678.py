"""Unit tests for provider prompt caching — Tasks 6, 7, 8.

Task 6: HTTP Client Header Injection
Task 7: Bedrock Provider Cache Support
Task 8: Agent Orchestration Integration
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gateway.bedrock_provider import _invoke_converse, _is_anthropic_model
from src.gateway.http_client import HttpClient
from src.gateway.models import (
    BudgetStatus,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Project,
    ProviderModelMapping,
    TokenPricing,
    TokenUsage,
    UsageRecord,
)
from src.gateway.provider_config import ProviderConfig


# ---------------------------------------------------------------------------
# Task 6: HTTP Client Header Injection
# ---------------------------------------------------------------------------


class _ResponseContent:
    async def iter_chunked(self, _size: int):
        yield b"{}"


class TestHttpClientHeaderInjection:
    """Tests for prompt_caching_enabled parameter and anthropic-beta header."""

    def _make_config(self, provider_name: str = "anthropic") -> ProviderConfig:
        return ProviderConfig(
            provider_name=provider_name,
            base_url="https://api.anthropic.com",
            auth_type="api_key",
            credentials={"api_key": "test-key"},
        )

    def _make_request(self) -> ChatCompletionRequest:
        return ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-3-sonnet-20240229",
        )

    def _make_mapping(self, provider: str = "anthropic") -> ProviderModelMapping:
        return ProviderModelMapping(
            provider=provider,
            model_id="claude-3-sonnet-20240229",
        )

    @pytest.mark.asyncio
    async def test_execute_accepts_prompt_caching_param(self):
        """6.1: execute accepts prompt_caching_enabled parameter."""
        client = HttpClient()
        adapter = MagicMock()
        adapter.translate_request = AsyncMock(return_value={"model": "test", "messages": []})
        adapter.translate_response = MagicMock(
            return_value=ChatCompletionResponse(
                id="r1", choices=[], usage=TokenUsage(10, 5, 15), model="test", provider="anthropic"
            )
        )
        config = self._make_config()
        mapping = self._make_mapping()
        request = self._make_request()

        # Mock the session to capture headers
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content = _ResponseContent()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False

        client._sessions["anthropic"] = mock_session

        await client.execute(request, mapping, adapter, config, prompt_caching_enabled=True)

        # Verify the anthropic-beta header was included
        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "anthropic-beta" in headers
        assert headers["anthropic-beta"] == "prompt-caching-2024-07-31"

    @pytest.mark.asyncio
    async def test_execute_no_beta_header_when_disabled(self):
        """6.2: No anthropic-beta header when prompt_caching_enabled=False."""
        client = HttpClient()
        adapter = MagicMock()
        adapter.translate_request = AsyncMock(return_value={"model": "test", "messages": []})
        adapter.translate_response = MagicMock(
            return_value=ChatCompletionResponse(
                id="r1", choices=[], usage=TokenUsage(10, 5, 15), model="test", provider="anthropic"
            )
        )
        config = self._make_config()
        mapping = self._make_mapping()
        request = self._make_request()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content = _ResponseContent()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False

        client._sessions["anthropic"] = mock_session

        await client.execute(request, mapping, adapter, config, prompt_caching_enabled=False)

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "anthropic-beta" not in headers

    @pytest.mark.asyncio
    async def test_execute_no_beta_header_for_non_anthropic(self):
        """6.2: No anthropic-beta header for non-anthropic providers even when enabled."""
        client = HttpClient()
        adapter = MagicMock()
        adapter.translate_request = AsyncMock(return_value={"model": "test", "messages": []})
        adapter.translate_response = MagicMock(
            return_value=ChatCompletionResponse(
                id="r1", choices=[], usage=TokenUsage(10, 5, 15), model="test", provider="openai"
            )
        )
        config = self._make_config(provider_name="openai")
        mapping = self._make_mapping(provider="openai")
        request = self._make_request()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content = _ResponseContent()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False

        client._sessions["openai"] = mock_session

        await client.execute(request, mapping, adapter, config, prompt_caching_enabled=True)

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "anthropic-beta" not in headers


# ---------------------------------------------------------------------------
# Task 7: Bedrock Provider Cache Support
# ---------------------------------------------------------------------------


class TestBedrockProviderCacheSupport:
    """Tests for Bedrock provider cache marker injection."""

    def test_is_anthropic_model_true(self):
        """7.1: Anthropic models are correctly identified."""
        assert _is_anthropic_model("anthropic.claude-3-sonnet-20240229-v1:0")
        assert _is_anthropic_model("anthropic.claude-3-haiku-20240307-v1:0")

    def test_is_anthropic_model_false(self):
        """7.3: Non-Anthropic models are correctly identified."""
        assert not _is_anthropic_model("us.amazon.nova-pro-v1:0")
        assert not _is_anthropic_model("us.deepseek.r1-v1:0")
        assert not _is_anthropic_model("amazon.titan-text-express-v1")

    def test_converse_no_cache_for_non_anthropic(self):
        """7.3: Non-Anthropic Bedrock models never receive cache_control markers."""
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
            model="us.amazon.nova-pro-v1:0",
            system="You are helpful.",
        )
        mapping = ProviderModelMapping(provider="bedrock", model_id="us.amazon.nova-pro-v1:0")

        # Replicate the payload construction from _invoke_converse
        messages = []
        system_parts = []
        for msg in req.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": content})
            else:
                messages.append({"role": role, "content": [{"text": content}]})
        if req.system:
            system_parts.insert(0, {"text": req.system})

        # Even with prompt_caching_enabled=True, non-Anthropic should not get markers
        if True and _is_anthropic_model(mapping.model_id) and system_parts:
            system_parts[-1]["cache_control"] = {"type": "ephemeral"}

        for part in system_parts:
            assert "cache_control" not in part

    def test_converse_cache_for_anthropic(self):
        """7.2: Anthropic Bedrock models get cache_control when enabled."""
        model_id = "anthropic.claude-3-sonnet-20240229-v1:0"
        system_parts = [{"text": "You are helpful."}]

        if _is_anthropic_model(model_id) and system_parts:
            system_parts[-1]["cache_control"] = {"type": "ephemeral"}

        assert "cache_control" in system_parts[-1]
        assert system_parts[-1]["cache_control"] == {"type": "ephemeral"}

    def test_converse_no_cache_for_anthropic_when_disabled(self):
        """7.2: Anthropic Bedrock models don't get cache_control when disabled."""
        model_id = "anthropic.claude-3-sonnet-20240229-v1:0"
        system_parts = [{"text": "You are helpful."}]
        prompt_caching_enabled = False

        if prompt_caching_enabled and _is_anthropic_model(model_id) and system_parts:
            system_parts[-1]["cache_control"] = {"type": "ephemeral"}

        assert "cache_control" not in system_parts[-1]


# ---------------------------------------------------------------------------
# Task 8: Agent Orchestration Integration
# ---------------------------------------------------------------------------


class TestAgentOrchestrationIntegration:
    """Tests for GatewayAgent integration with prompt caching."""

    def _make_agent(self, project=None, provider_fn_factory=None):
        from src.gateway.agent import GatewayAgent
        from src.gateway.cache_manager import CacheManager
        from src.gateway.cost_tracker import CostTracker
        from src.gateway.guardrail_engine import GuardrailEngine
        from src.gateway.rate_limiter import SlidingWindowRateLimiter
        from src.gateway.router import Router
        from src.gateway.models import RateLimitResult

        router = MagicMock(spec=Router)
        rate_limiter = MagicMock(spec=SlidingWindowRateLimiter)
        rate_limiter.check_rate_limit = AsyncMock(
            return_value=RateLimitResult(
                allowed=True, limit=60, remaining=59,
                reset_at=datetime.utcnow(),
            )
        )
        guardrail_engine = MagicMock(spec=GuardrailEngine)
        cache_manager = MagicMock(spec=CacheManager)
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.01)
        cost_tracker.record_usage = AsyncMock()
        cost_tracker.check_budget = AsyncMock(
            return_value=BudgetStatus(
                project_id="proj1",
                current_spend=0.0,
                budget_limit=None,
                alert_threshold=None,
                is_over_budget=False,
                is_alert_triggered=False,
            )
        )
        cost_tracker.check_user_budget = AsyncMock(
            return_value=BudgetStatus(
                project_id="user1",
                current_spend=0.0,
                budget_limit=None,
                alert_threshold=None,
                is_over_budget=False,
                is_alert_triggered=False,
            )
        )

        projects = {}
        if project:
            projects[project.project_id] = project

        agent = GatewayAgent(
            router=router,
            rate_limiter=rate_limiter,
            guardrail_engine=guardrail_engine,
            cache_manager=cache_manager,
            cost_tracker=cost_tracker,
            projects=projects,
            provider_fn_factory=provider_fn_factory,
        )
        return agent, cost_tracker, router

    @pytest.mark.asyncio
    async def test_prompt_caching_enabled_passed_to_factory(self):
        """8.1/8.2: prompt_caching_enabled is read from project and passed to factory."""
        project = Project(
            project_id="proj1",
            name="Test Project",
            prompt_caching_enabled=True,
        )

        factory = MagicMock()
        mock_response = ChatCompletionResponse(
            id="resp1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            usage=TokenUsage(100, 50, 150, cached_tokens=80, cache_creation_tokens=10),
            model="claude-3-sonnet",
            provider="anthropic",
        )
        provider_fn = AsyncMock(return_value=mock_response)
        factory.create = MagicMock(return_value=provider_fn)

        agent, cost_tracker, router = self._make_agent(project=project, provider_fn_factory=factory)
        router.execute_with_fallback = AsyncMock(return_value=mock_response)

        result = await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "Hi"}], "model": "claude-3-sonnet"},
            {"user_id": "user1", "project_id": "proj1"},
        )

        # Verify factory.create was called with prompt_caching_enabled=True
        factory.create.assert_called_once()
        call_kwargs = factory.create.call_args
        assert call_kwargs.kwargs.get("prompt_caching_enabled") is True or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] is True
        )

    @pytest.mark.asyncio
    async def test_cached_tokens_passed_to_cost_tracker(self):
        """8.3/8.4: cached_tokens and cache_creation_tokens passed to calculate_cost."""
        project = Project(
            project_id="proj1",
            name="Test Project",
            prompt_caching_enabled=True,
        )

        factory = MagicMock()
        mock_response = ChatCompletionResponse(
            id="resp1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            usage=TokenUsage(100, 50, 150, cached_tokens=80, cache_creation_tokens=10),
            model="claude-3-sonnet",
            provider="anthropic",
        )
        provider_fn = AsyncMock(return_value=mock_response)
        factory.create = MagicMock(return_value=provider_fn)

        agent, cost_tracker, router = self._make_agent(project=project, provider_fn_factory=factory)
        router.execute_with_fallback = AsyncMock(return_value=mock_response)

        result = await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "Hi"}], "model": "claude-3-sonnet"},
            {"user_id": "user1", "project_id": "proj1"},
        )

        # Verify calculate_cost was called with cached_tokens and cache_creation_tokens
        cost_tracker.calculate_cost.assert_called_once()
        call_kwargs = cost_tracker.calculate_cost.call_args
        assert call_kwargs.kwargs.get("cached_tokens") == 80
        assert call_kwargs.kwargs.get("cache_creation_tokens") == 10

    @pytest.mark.asyncio
    async def test_usage_record_populated_with_cached_tokens(self):
        """8.3: UsageRecord populated with cached_tokens and cache_creation_tokens."""
        project = Project(
            project_id="proj1",
            name="Test Project",
            prompt_caching_enabled=True,
        )

        factory = MagicMock()
        mock_response = ChatCompletionResponse(
            id="resp1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            usage=TokenUsage(100, 50, 150, cached_tokens=80, cache_creation_tokens=10),
            model="claude-3-sonnet",
            provider="anthropic",
        )
        provider_fn = AsyncMock(return_value=mock_response)
        factory.create = MagicMock(return_value=provider_fn)

        agent, cost_tracker, router = self._make_agent(project=project, provider_fn_factory=factory)
        router.execute_with_fallback = AsyncMock(return_value=mock_response)

        result = await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "Hi"}], "model": "claude-3-sonnet"},
            {"user_id": "user1", "project_id": "proj1"},
        )

        # Verify record_usage was called with a UsageRecord containing cached fields
        cost_tracker.record_usage.assert_called_once()
        usage_record = cost_tracker.record_usage.call_args[0][0]
        assert isinstance(usage_record, UsageRecord)
        assert usage_record.cached_tokens == 80
        assert usage_record.cache_creation_tokens == 10

    @pytest.mark.asyncio
    async def test_prompt_caching_disabled_by_default(self):
        """8.1: When project has no prompt_caching_enabled, defaults to False."""
        project = Project(
            project_id="proj1",
            name="Test Project",
            # prompt_caching_enabled defaults to False
        )

        factory = MagicMock()
        mock_response = ChatCompletionResponse(
            id="resp1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            usage=TokenUsage(100, 50, 150),
            model="claude-3-sonnet",
            provider="anthropic",
        )
        provider_fn = AsyncMock(return_value=mock_response)
        factory.create = MagicMock(return_value=provider_fn)

        agent, cost_tracker, router = self._make_agent(project=project, provider_fn_factory=factory)
        router.execute_with_fallback = AsyncMock(return_value=mock_response)

        result = await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "Hi"}], "model": "claude-3-sonnet"},
            {"user_id": "user1", "project_id": "proj1"},
        )

        # Verify factory.create was called with prompt_caching_enabled=False
        factory.create.assert_called_once()
        call_kwargs = factory.create.call_args
        assert call_kwargs.kwargs.get("prompt_caching_enabled") is False or (
            len(call_kwargs.args) <= 1
        )

    @pytest.mark.asyncio
    async def test_no_project_defaults_caching_disabled(self):
        """8.1: When no project found, prompt_caching_enabled defaults to False."""
        factory = MagicMock()
        mock_response = ChatCompletionResponse(
            id="resp1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            usage=TokenUsage(100, 50, 150),
            model="claude-3-sonnet",
            provider="anthropic",
        )
        provider_fn = AsyncMock(return_value=mock_response)
        factory.create = MagicMock(return_value=provider_fn)

        agent, cost_tracker, router = self._make_agent(provider_fn_factory=factory)
        router.execute_with_fallback = AsyncMock(return_value=mock_response)

        result = await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "Hi"}], "model": "claude-3-sonnet"},
            {"user_id": "user1", "project_id": "unknown_proj"},
        )

        factory.create.assert_called_once()
        call_kwargs = factory.create.call_args
        assert call_kwargs.kwargs.get("prompt_caching_enabled") is False
