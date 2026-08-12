"""Tests for true end-to-end token streaming (#18).

Verifies the streaming path opens the provider SSE stream directly (no blocking
execute_with_fallback + second call), relays chunks, accumulates usage, runs
end-of-stream cost/audit accounting, uses provider usage when present and
estimates otherwise, and falls back across providers only before the first byte.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.gateway.agent as agent_module
from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailRule,
    Project,
    ProviderModelMapping,
    RateLimitResult,
    RequestContext,
    ResolvedPolicy,
    StreamChunk,
    TokenPricing,
    TokenUsage,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import Router
from src.gateway.security.pii_redactor import PIIRedactor


def _chunk(
    content="",
    is_final=False,
    usage=None,
    model="claude-sonnet",
    chunk_id="c",
):
    choices = [{"index": 0, "delta": {"content": content} if content else {},
                "finish_reason": "stop" if is_final else None}]
    return StreamChunk(
        id=chunk_id,
        choices=choices,
        model=model,
        is_final=is_final,
        usage=usage,
    )


class FakeHttpClient:
    """execute_streaming yields a scripted list of chunks (or raises per provider)."""

    def __init__(self, chunks_by_provider):
        self._chunks_by_provider = chunks_by_provider

    def execute_streaming(self, request, mapping, adapter, config, *, prompt_caching_enabled=False):
        return self._gen(mapping.provider)

    async def _gen(self, provider):
        item = self._chunks_by_provider[provider]
        if isinstance(item, Exception):
            raise item
        for c in item:
            if isinstance(c, Exception):
                raise c
            yield c


class FakeFactory:
    def __init__(self, providers, http_client):
        self._adapter_registry = {p: MagicMock() for p in providers}
        self._provider_configs = {p: MagicMock() for p in providers}
        self._http_client = http_client

    def create(self, request, prompt_caching_enabled=False, spoke=None):
        return AsyncMock()

    def config_for(self, provider, spoke=None):
        return self._provider_configs.get(provider)


def _rate_limiter():
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(return_value=RateLimitResult(
        allowed=True, limit=60, remaining=59, reset_at=datetime.utcnow(),
        retry_after_seconds=None))
    return rl


def _no_budget(cost_tracker):
    """Stub the pre-request budget checks so a mocked CostTracker doesn't 429."""
    from src.gateway.models import BudgetStatus
    ok = BudgetStatus(project_id="p1", current_spend=0.0, budget_limit=None,
                      alert_threshold=None, is_over_budget=False, is_alert_triggered=False)
    cost_tracker.check_budget = AsyncMock(return_value=ok)
    cost_tracker.check_user_budget = AsyncMock(return_value=ok)


def _router(chain):
    r = MagicMock(spec=Router)
    r.execute_with_fallback = AsyncMock(side_effect=AssertionError(
        "blocking call must NOT run on the true-streaming path"))
    r.get_fallback_chain = MagicMock(return_value=chain)
    r.health_tracker = MagicMock()
    r.health_tracker.is_healthy = MagicMock(return_value=True)
    r.cooldown_seconds = 60
    r._smart_strategy = None
    r._ensemble_config = None
    return r


def _agent(
    router,
    factory,
    cost_tracker=None,
    audit=None,
    projects=None,
    pii_redactor=None,
):
    pricing = {"anthropic": {"claude-sonnet": TokenPricing(
        prompt_token_cost=0.003, completion_token_cost=0.015)}}
    return GatewayAgent(
        router=router,
        rate_limiter=_rate_limiter(),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=cost_tracker or CostTracker(pricing_config=pricing),
        provider_fn_factory=factory,
        audit_trail=audit,
        projects=projects,
        pii_redactor=pii_redactor,
    )


def _req(stream=True, **overrides):
    payload = {
        "messages": [{"role": "user", "content": "Hi there"}],
        "model": "claude-sonnet",
        "stream": stream,
    }
    payload.update(overrides)
    return payload


def _ctx():
    return {"user_id": "u1", "project_id": "p1", "roles": [], "scopes": []}


def _content(chunks):
    return "".join(
        choice.get("delta", {}).get("content") or ""
        for chunk in chunks
        if isinstance(chunk.get("data"), dict)
        for choice in chunk["data"].get("choices", [])
    )


async def _drain(coro_or_gen):
    # handle_chat_completion is `async def` returning an async iterator, so await
    # it first; _stream_true (called directly) is already an async generator.
    import inspect
    gen = await coro_or_gen if inspect.iscoroutine(coro_or_gen) else coro_or_gen
    return [c async for c in gen]


class TestSingleProviderCall:
    async def test_streaming_does_not_make_blocking_call(self):
        chunks = [_chunk("Hel"), _chunk("lo"),
                  _chunk("", is_final=True, usage=TokenUsage(5, 2, 7))]
        chain = [ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")]
        router = _router(chain)
        agent = _agent(router, FakeFactory(["anthropic"], FakeHttpClient({"anthropic": chunks})))

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        # The blocking path's side_effect would have raised if called.
        router.execute_with_fallback.assert_not_called()
        # Content chunks + [DONE]
        texts = [c["data"]["choices"][0]["delta"].get("content")
                 for c in out if isinstance(c.get("data"), dict) and "choices" in c["data"]]
        assert "".join(t for t in texts if t) == "Hello"
        provider_chunks = [
            chunk["data"]
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            and "choices" in chunk["data"]
        ]
        assert all(chunk["provider"] == "anthropic" for chunk in provider_chunks)
        assert out[-1] == {"data": "[DONE]"}


class TestEndOfStreamAccounting:
    async def test_cost_and_audit_recorded_from_provider_usage(self):
        chunks = [_chunk("Hi"), _chunk("", is_final=True, usage=TokenUsage(100, 40, 140))]
        chain = [ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.42)
        cost_tracker.record_usage = AsyncMock()
        _no_budget(cost_tracker)
        audit = MagicMock()
        audit.record_llm_request = AsyncMock()
        agent = _agent(_router(chain),
                       FakeFactory(["anthropic"], FakeHttpClient({"anthropic": chunks})),
                       cost_tracker=cost_tracker, audit=audit)

        await _drain(agent.handle_chat_completion(_req(), _ctx()))

        cost_tracker.calculate_cost.assert_called_once()
        args, kwargs = cost_tracker.calculate_cost.call_args
        assert args[2] == 100 and args[3] == 40      # provider-reported tokens
        cost_tracker.record_usage.assert_awaited_once()
        rec = cost_tracker.record_usage.call_args.args[0]
        assert rec.cost == 0.42 and rec.total_tokens == 140
        audit.record_llm_request.assert_awaited_once()

    async def test_finalization_failure_is_sanitized_before_done(self):
        chunks = [
            _chunk("Hi"),
            _chunk("", is_final=True, usage=TokenUsage(5, 2, 7)),
        ]
        chain = [
            ProviderModelMapping(
                provider="anthropic",
                model_id="claude-sonnet",
            )
        ]
        audit = MagicMock()
        audit.record_llm_request = AsyncMock(
            side_effect=RuntimeError("audit credential=secret-value")
        )
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            audit=audit,
        )

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        error = next(
            chunk["data"]["error"]
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            and "error" in chunk["data"]
        )
        assert error["code"] == "stream_finalization_failed"
        assert "secret-value" not in str(out)
        assert out[-1] == {"data": "[DONE]"}

    async def test_estimates_usage_when_provider_reports_none(self):
        # No usage on any chunk → gateway estimates via tiktoken.
        chunks = [_chunk("Hello world"), _chunk("!", is_final=True)]
        chain = [ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.0)
        cost_tracker.record_usage = AsyncMock()
        cost_tracker.estimate_tokens = AsyncMock(side_effect=[7, 3])  # prompt, completion
        _no_budget(cost_tracker)
        agent = _agent(_router(chain),
                       FakeFactory(["anthropic"], FakeHttpClient({"anthropic": chunks})),
                       cost_tracker=cost_tracker)

        await _drain(agent.handle_chat_completion(_req(), _ctx()))

        assert cost_tracker.estimate_tokens.await_count == 2
        rec = cost_tracker.record_usage.call_args.args[0]
        assert rec.prompt_tokens == 7 and rec.completion_tokens == 3

    async def test_stream_retains_provider_request_id(self):
        chunks = [
            _chunk("Hi", chunk_id="provider-request-123"),
            _chunk(
                "",
                is_final=True,
                usage=TokenUsage(5, 2, 7),
                chunk_id="",
            ),
        ]
        chain = [
            ProviderModelMapping(
                provider="anthropic",
                model_id="claude-sonnet",
            )
        ]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.0)
        cost_tracker.record_usage = AsyncMock()
        _no_budget(cost_tracker)
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            cost_tracker=cost_tracker,
        )

        await _drain(agent.handle_chat_completion(_req(), _ctx()))

        record = cost_tracker.record_usage.call_args.args[0]
        assert record.provider_request_id == "provider-request-123"

    async def test_final_response_id_replaces_item_id(self):
        chunks = [
            _chunk("Hi", chunk_id="output-item-123"),
            _chunk(
                "",
                is_final=True,
                usage=TokenUsage(5, 2, 7),
                chunk_id="response-456",
            ),
        ]
        chain = [
            ProviderModelMapping(
                provider="openai",
                model_id="gpt-5",
            )
        ]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.0)
        cost_tracker.record_usage = AsyncMock()
        _no_budget(cost_tracker)
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["openai"],
                FakeHttpClient({"openai": chunks}),
            ),
            cost_tracker=cost_tracker,
        )

        await _drain(agent.handle_chat_completion(_req(), _ctx()))

        record = cost_tracker.record_usage.call_args.args[0]
        assert record.provider_request_id == "response-456"

    async def test_tool_only_stream_estimation_counts_schema_and_arguments(self):
        chunks = [
            StreamChunk(
                id="provider-tool-1",
                choices=[{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"city":"Paris"}',
                            },
                        }]
                    },
                    "finish_reason": None,
                }],
                model="claude-sonnet",
            ),
            StreamChunk(
                id="",
                choices=[{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }],
                model="claude-sonnet",
                is_final=True,
            ),
        ]
        chain = [
            ProviderModelMapping(
                provider="anthropic",
                model_id="claude-sonnet",
            )
        ]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.0)
        cost_tracker.record_usage = AsyncMock()
        cost_tracker.estimate_tokens = AsyncMock(side_effect=[12, 5])
        _no_budget(cost_tracker)
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            cost_tracker=cost_tracker,
        )
        tool = {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }

        await _drain(
            agent.handle_chat_completion(
                _req(tools=[tool], tool_choice="auto"),
                _ctx(),
            )
        )

        prompt_text = cost_tracker.estimate_tokens.await_args_list[0].args[0]
        output_text = cost_tracker.estimate_tokens.await_args_list[1].args[0]
        assert '"tools"' in prompt_text
        assert "lookup" in prompt_text
        assert output_text == 'lookup{"city":"Paris"}'
        record = cost_tracker.record_usage.call_args.args[0]
        assert record.completion_tokens == 5
        assert record.provider_request_id == "provider-tool-1"


class TestUsageAfterFinalChunk:
    async def test_openai_usage_chunk_after_finish_reason(self):
        # OpenAI order: content, finish_reason chunk, THEN usage-only chunk.
        chunks = [
            _chunk("Hi"),
            _chunk("", is_final=True),                                  # finish_reason
            StreamChunk(
                id="",
                choices=[],
                model="claude-sonnet",
                is_final=True,
                usage=TokenUsage(50, 20, 70),
            ),
        ]
        chain = [ProviderModelMapping(provider="openai", model_id="gpt-4o")]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=1.0)
        cost_tracker.record_usage = AsyncMock()
        _no_budget(cost_tracker)
        agent = _agent(_router(chain),
                       FakeFactory(["openai"], FakeHttpClient({"openai": chunks})),
                       cost_tracker=cost_tracker)

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        # Usage from the trailing chunk was captured (not estimated).
        args, _ = cost_tracker.calculate_cost.call_args
        assert args[2] == 50 and args[3] == 20
        # The empty usage-only chunk was NOT emitted as a content chunk.
        emitted = [c for c in out if isinstance(c.get("data"), dict) and "choices" in c["data"]]
        assert len(emitted) == 2
        assert all(e["data"]["choices"] for e in emitted)


class TestBufferedFallbackForNonHttpProvider:
    """Regression: providers that don't stream over HttpClient (boto3 Bedrock,
    google_ai) have no HTTP config. _stream_true must fall back to a blocking
    call + simulate-stream rather than erroring 'all_providers_exhausted'.
    (Caught live: real Bedrock streaming was never exercised by the fakes.)"""

    async def test_no_http_config_falls_back_to_buffered_stream(self):
        from unittest.mock import AsyncMock, MagicMock

        from src.gateway.models import ChatCompletionResponse, TokenUsage

        chain = [ProviderModelMapping(provider="bedrock", model_id="claude-sonnet")]
        router = _router(chain)
        # The fallback path SHOULD call execute_with_fallback → return a response.
        resp = ChatCompletionResponse(
            id="r1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "1, 2, 3"}}],
            usage=TokenUsage(10, 5, 15), model="claude-sonnet", provider="bedrock")
        router.execute_with_fallback = AsyncMock(return_value=resp)

        # Factory with NO http config for bedrock (config_for → None), like the
        # real MultiProviderFactory (bedrock goes through boto3).
        factory = MagicMock()
        factory._adapter_registry = {"bedrock": MagicMock()}
        factory._provider_configs = {}
        factory._http_client = MagicMock()
        factory.config_for = MagicMock(return_value=None)   # boto3 provider → no HTTP config
        factory.create = MagicMock(return_value=AsyncMock())

        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.01)
        cost_tracker.record_usage = AsyncMock()
        _no_budget(cost_tracker)
        agent = _agent(router, factory, cost_tracker=cost_tracker)

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        # Fell back to the blocking call...
        router.execute_with_fallback.assert_awaited_once()
        # ...streamed the buffered content (not an all_providers_exhausted error)...
        text = "".join(
            c["data"]["choices"][0]["delta"].get("content", "")
            for c in out if isinstance(c.get("data"), dict) and c["data"].get("choices"))
        assert "1, 2, 3" == text
        assert all(
            chunk["data"]["provider"] == "bedrock"
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            and chunk["data"].get("choices")
        )
        assert not any(
            isinstance(c.get("data"), dict) and "error" in c["data"] for c in out)
        assert out[-1] == {"data": "[DONE]"}
        # ...and end-of-stream accounting still ran once.
        cost_tracker.record_usage.assert_awaited_once()

    async def test_finalization_failure_never_occurs_after_done(self):
        from src.gateway.models import ChatCompletionResponse

        chain = [
            ProviderModelMapping(
                provider="bedrock",
                model_id="claude-sonnet",
            )
        ]
        router = _router(chain)
        router.execute_with_fallback = AsyncMock(
            return_value=ChatCompletionResponse(
                id="r1",
                choices=[{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "buffered content",
                    },
                }],
                usage=TokenUsage(10, 5, 15),
                model="claude-sonnet",
                provider="bedrock",
            )
        )
        factory = MagicMock()
        factory._adapter_registry = {"bedrock": MagicMock()}
        factory._provider_configs = {}
        factory._http_client = MagicMock()
        factory.config_for = MagicMock(return_value=None)
        factory.create = MagicMock(return_value=AsyncMock())
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.01)
        cost_tracker.record_usage = AsyncMock(
            side_effect=RuntimeError("storage credential=secret-value")
        )
        _no_budget(cost_tracker)
        agent = _agent(router, factory, cost_tracker=cost_tracker)

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        error_index = next(
            index
            for index, chunk in enumerate(out)
            if isinstance(chunk.get("data"), dict)
            and "error" in chunk["data"]
        )
        assert out[error_index]["data"]["error"]["code"] == (
            "stream_finalization_failed"
        )
        assert "secret-value" not in str(out)
        assert out[-1] == {"data": "[DONE]"}
        assert error_index == len(out) - 2

    async def test_consumer_close_accounts_for_buffered_fallback(self):
        from src.gateway.models import ChatCompletionResponse

        chain = [
            ProviderModelMapping(
                provider="bedrock",
                model_id="claude-sonnet",
            )
        ]
        router = _router(chain)
        router.execute_with_fallback = AsyncMock(
            return_value=ChatCompletionResponse(
                id="r1",
                choices=[{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "buffered content",
                    },
                }],
                usage=TokenUsage(10, 5, 15),
                model="claude-sonnet",
                provider="bedrock",
            )
        )
        factory = MagicMock()
        factory._adapter_registry = {"bedrock": MagicMock()}
        factory._provider_configs = {}
        factory._http_client = MagicMock()
        factory.config_for = MagicMock(return_value=None)
        factory.create = MagicMock(return_value=AsyncMock())
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.01)
        cost_tracker.record_usage = AsyncMock()
        _no_budget(cost_tracker)
        agent = _agent(router, factory, cost_tracker=cost_tracker)

        stream = await agent.handle_chat_completion(_req(), _ctx())
        await anext(stream)  # rate-limit metadata
        await anext(stream)  # first buffered content chunk
        await stream.aclose()

        cost_tracker.record_usage.assert_awaited_once()
        assert cost_tracker.record_usage.call_args.args[0].status == "cancelled"


class TestPreFirstByteFallback:
    async def test_falls_back_to_next_provider_before_first_byte(self):
        # First provider raises on open; second streams fine.
        chain = [
            ProviderModelMapping(provider="openai", model_id="gpt-4"),
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet"),
        ]
        http = FakeHttpClient({
            "openai": ConnectionError("refused"),
            "anthropic": [_chunk("ok", is_final=True, usage=TokenUsage(1, 1, 2))],
        })
        router = _router(chain)
        agent = _agent(router, FakeFactory(["openai", "anthropic"], http))

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        texts = [c["data"]["choices"][0]["delta"].get("content")
                 for c in out if isinstance(c.get("data"), dict) and "choices" in c["data"]]
        assert "".join(t for t in texts if t) == "ok"       # served by anthropic
        router.health_tracker.mark_unhealthy.assert_called()  # openai marked down

    async def test_all_providers_fail_to_open(self, caplog):
        chain = [ProviderModelMapping(provider="openai", model_id="gpt-4")]
        provider_secret = "provider-echoed-bearer-secret"
        http = FakeHttpClient({"openai": ConnectionError(provider_secret)})
        agent = _agent(_router(chain), FakeFactory(["openai"], http))

        with caplog.at_level(logging.DEBUG, logger="gateway.agent"):
            out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        err = next(c for c in out if isinstance(c.get("data"), dict) and "error" in c["data"])
        assert err["data"]["error"]["code"] == "all_providers_exhausted"
        assert provider_secret not in str(out)
        assert provider_secret not in caplog.text


class TestPiiReinjectOverStream:
    async def test_reinjects_tokens_across_real_chunks(self):
        from src.gateway.security.pii_redactor import PIIRedactor
        from src.gateway.models import ResolvedPolicy

        redactor = PIIRedactor()
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        _, mapping = redactor.redact("mail a@b.com", policy)   # builds [EMAIL_1] -> a@b.com

        # Provider streams the token split across chunk boundaries.
        chunks = [_chunk("see [EMAIL"), _chunk("_1] now"),
                  _chunk("", is_final=True, usage=TokenUsage(3, 3, 6))]
        chain = [ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")]
        agent = _agent(_router(chain),
                       FakeFactory(["anthropic"], FakeHttpClient({"anthropic": chunks})))
        agent._pii_redactor = redactor

        # Inject the mapping by calling _stream_true through the public path:
        # simplest is to drive _stream_true directly with the mapping.
        from src.gateway.models import ChatCompletionRequest, RequestContext
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "x"}],
                                    model="claude-sonnet", stream=True)
        gen = agent._stream_true(
            req, _ctx(), RequestContext(user_id="u", project_id="p", roles=[], scopes=[]),
            False, None, policy, mapping, "req_x", 0.0,
            preferred_provider=None, effective_allowed=None, smart_routing_decision=None,
        )
        out = await _drain(gen)
        texts = [c["data"]["choices"][0]["delta"].get("content")
                 for c in out if isinstance(c.get("data"), dict) and "choices" in c["data"]]
        assert "a@b.com" in "".join(t for t in texts if t)   # reinjected across boundary
        assert "[EMAIL_1]" not in "".join(t for t in texts if t)


class TestStreamingOutputPolicy:
    async def test_blocked_pattern_split_across_chunks_never_leaks(self):
        rules = [
            GuardrailRule(
                name="block_secret",
                rule_type="keyword_block",
                pattern="secret_data",
                action="block",
                applies_to="response",
            )
        ]
        project = Project(project_id="p1", name="P1", guardrail_rules=rules)
        chunks = [
            _chunk("prefix secret_"),
            _chunk("data suffix", is_final=True, usage=TokenUsage(4, 4, 8)),
        ]
        chain = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            projects={"p1": project},
        )

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        content = _content(out)
        assert "secret_data" not in content
        assert "prefix" not in content
        assert "block_secret" in content
        assert out[-1] == {"data": "[DONE]"}

    async def test_generated_pii_split_across_chunks_is_redacted(self):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True,
            pii_redact_types=["email"],
            pii_reinject=False,
        )
        redactor = PIIRedactor()
        chunks = [
            _chunk("Contact generated@"),
            _chunk(
                "example.com now",
                is_final=True,
                usage=TokenUsage(4, 4, 8),
            ),
        ]
        chain = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            pii_redactor=redactor,
        )
        _, mapping = redactor.redact("no pii here", policy)
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet",
            stream=True,
        )

        out = await _drain(
            agent._stream_true(
                request,
                _ctx(),
                RequestContext(
                    user_id="u1",
                    project_id="p1",
                    roles=[],
                    scopes=[],
                ),
                False,
                None,
                policy,
                mapping,
                "req_pii",
                time.perf_counter(),
                preferred_provider=None,
                effective_allowed=None,
                smart_routing_decision=None,
            )
        )

        content = _content(out)
        assert "generated@example.com" not in content
        assert "[EMAIL_1]" in content

    async def test_pii_in_partial_tool_arguments_is_redacted(self):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True,
            pii_redact_types=["email"],
            pii_reinject=False,
        )
        redactor = PIIRedactor()
        chunks = [
            StreamChunk(
                id="c",
                choices=[{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "notify",
                                "arguments": '{"email":"generated@',
                            },
                        }]
                    },
                    "finish_reason": None,
                }],
                model="claude-sonnet",
            ),
            StreamChunk(
                id="c",
                choices=[{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {
                                "arguments": 'example.com"}',
                            },
                        }]
                    },
                    "finish_reason": "tool_calls",
                }],
                model="claude-sonnet",
                is_final=True,
                usage=TokenUsage(4, 4, 8),
            ),
        ]
        chain = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            pii_redactor=redactor,
        )
        _, mapping = redactor.redact("no pii here", policy)
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet",
            stream=True,
            tools=[{"type": "function", "function": {"name": "notify"}}],
        )

        out = await _drain(
            agent._stream_true(
                request,
                _ctx(),
                RequestContext(
                    user_id="u1",
                    project_id="p1",
                    roles=[],
                    scopes=[],
                ),
                False,
                None,
                policy,
                mapping,
                "req_tool_pii",
                time.perf_counter(),
                preferred_provider=None,
                effective_allowed=None,
                smart_routing_decision=None,
            )
        )

        calls = [
            call
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            for choice in chunk["data"].get("choices", [])
            for call in choice.get("delta", {}).get("tool_calls", [])
        ]
        arguments = calls[0]["function"]["arguments"]
        assert "generated@example.com" not in arguments
        assert "[EMAIL_1]" in arguments

    async def test_configured_detector_failure_withholds_entire_output(self):
        class FailingDetector:
            async def detect(self, text, active_types):
                raise RuntimeError("comprehend credential detail")

        policy = ResolvedPolicy(
            pii_redaction_enabled=True,
            pii_redact_types=["email"],
            pii_ner_enabled=True,
            pii_ner_types=["name"],
            pii_reinject=False,
        )
        redactor = PIIRedactor(entity_detector=FailingDetector())
        chunks = [
            _chunk("Alice generated@example.com"),
            _chunk("", is_final=True, usage=TokenUsage(4, 4, 8)),
        ]
        chain = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            pii_redactor=redactor,
        )
        _, mapping = await redactor.redact_messages_async(
            [{"role": "user", "content": "hello"}],
            policy,
        )
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet",
            stream=True,
        )

        out = await _drain(
            agent._stream_true(
                request,
                _ctx(),
                RequestContext(
                    user_id="u1",
                    project_id="p1",
                    roles=[],
                    scopes=[],
                ),
                False,
                None,
                policy,
                mapping,
                "req_ner",
                time.perf_counter(),
                preferred_provider=None,
                effective_allowed=None,
                smart_routing_decision=None,
            )
        )

        assert "Alice" not in _content(out)
        error = next(
            chunk["data"]["error"]
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            and "error" in chunk["data"]
        )
        assert error["code"] == "output_policy_failed"
        assert "credential" not in error["message"]


class TestStreamingFailureAndCancellation:
    async def test_midstream_provider_failure_does_not_leak_buffered_output(self):
        rules = [
            GuardrailRule(
                name="inspect_output",
                rule_type="keyword_block",
                pattern="never-match",
                action="block",
                applies_to="response",
            )
        ]
        project = Project(project_id="p1", name="P1", guardrail_rules=rules)
        chunks = [
            _chunk("withheld provider content"),
            RuntimeError("provider credential=secret-value"),
        ]
        chain = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.0)
        cost_tracker.record_usage = AsyncMock()
        cost_tracker.estimate_tokens = AsyncMock(side_effect=[3, 4])
        _no_budget(cost_tracker)
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            cost_tracker=cost_tracker,
            projects={"p1": project},
        )

        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))

        assert "withheld provider content" not in _content(out)
        error = next(
            chunk["data"]["error"]
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            and "error" in chunk["data"]
        )
        assert error == {
            "type": "stream_error",
            "message": "The provider stream failed.",
            "code": "provider_stream_failed",
        }
        assert "secret-value" not in str(out)
        cost_tracker.record_usage.assert_awaited_once()
        assert cost_tracker.record_usage.call_args.args[0].status == "error"

    async def test_consumer_close_records_cancelled_usage_once(self):
        chunks = [_chunk("first"), _chunk("second")]
        chain = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        cost_tracker = MagicMock(spec=CostTracker)
        cost_tracker.calculate_cost = MagicMock(return_value=0.0)
        cost_tracker.record_usage = AsyncMock()
        cost_tracker.estimate_tokens = AsyncMock(side_effect=[2, 1])
        _no_budget(cost_tracker)
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
            cost_tracker=cost_tracker,
        )
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet",
            stream=True,
        )
        stream = agent._stream_true(
            request,
            _ctx(),
            RequestContext(
                user_id="u1",
                project_id="p1",
                roles=[],
                scopes=[],
            ),
            False,
            None,
            None,
            None,
            "req_cancel",
            time.perf_counter(),
            preferred_provider=None,
            effective_allowed=None,
            smart_routing_decision=None,
        )

        first = await anext(stream)
        assert _content([first]) == "first"
        await stream.aclose()

        cost_tracker.record_usage.assert_awaited_once()
        record = cost_tracker.record_usage.call_args.args[0]
        assert record.status == "cancelled"
        assert record.request_id == "req_cancel"


class TestSimulateFallbackWhenNoFactory:
    async def test_no_factory_uses_simulate_path(self):
        # Without a provider_fn_factory, _can_stream_true() is False → the agent
        # keeps the blocking+simulate behavior (execute_with_fallback runs).
        from src.gateway.models import ChatCompletionResponse
        resp = ChatCompletionResponse(
            id="r", choices=[{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            usage=TokenUsage(1, 1, 2), model="claude-sonnet", provider="anthropic")
        router = MagicMock(spec=Router)
        router.execute_with_fallback = AsyncMock(return_value=resp)
        router._smart_strategy = None
        router._ensemble_config = None
        pricing = {"anthropic": {"claude-sonnet": TokenPricing(
            prompt_token_cost=0.003, completion_token_cost=0.015)}}
        agent = GatewayAgent(
            router=router, rate_limiter=_rate_limiter(), guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(), cost_tracker=CostTracker(pricing_config=pricing),
        )
        out = await _drain(agent.handle_chat_completion(_req(), _ctx()))
        router.execute_with_fallback.assert_awaited_once()   # blocking path used
        assert out[-1]["data"] == "[DONE]"


class TestStreamOutputBounds:
    async def test_true_stream_limit_applies_without_policy_buffer(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(agent_module, "_MAX_STREAM_OUTPUT_BYTES", 1)
        chain = [
            ProviderModelMapping(
                provider="anthropic",
                model_id="claude-sonnet",
            )
        ]
        chunks = [
            _chunk("provider output"),
            _chunk("", is_final=True),
        ]
        agent = _agent(
            _router(chain),
            FakeFactory(
                ["anthropic"],
                FakeHttpClient({"anthropic": chunks}),
            ),
        )

        out = await _drain(
            agent.handle_chat_completion(_req(), _ctx())
        )

        assert "provider output" not in _content(out)
        error = next(
            chunk["data"]["error"]
            for chunk in out
            if isinstance(chunk.get("data"), dict)
            and "error" in chunk["data"]
        )
        assert error["code"] == "provider_stream_failed"

    async def test_simulated_stream_limit_is_checked_before_output(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(agent_module, "_MAX_STREAM_OUTPUT_BYTES", 1)
        response = ChatCompletionResponse(
            id="response-1",
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "provider output",
                    },
                }
            ],
            usage=TokenUsage(1, 1, 2),
            model="claude-sonnet",
            provider="anthropic",
        )
        agent = _agent(_router([]), factory=None)

        out = [
            chunk
            async for chunk in agent._stream_response(response)
        ]

        assert "provider output" not in _content(out)
        assert out[0]["data"]["error"]["code"] == (
            "response_stream_failed"
        )
