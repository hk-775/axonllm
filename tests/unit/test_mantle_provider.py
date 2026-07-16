"""Unit tests for Bedrock Mantle model→API routing."""

import asyncio

import pytest

import src.gateway.mantle_provider as mp
from src.gateway.mantle_provider import (
    _is_anthropic_model,
    _is_unsupported_route_error,
    _prefers_responses_api,
    create_mantle_provider_fn,
)
from src.gateway.models import ChatCompletionRequest, ProviderModelMapping
from src.gateway.router import ProviderError


def _run(coro):
    return asyncio.run(coro)


class TestRouteSelection:
    def test_anthropic_prefix(self):
        assert _is_anthropic_model("anthropic.claude-sonnet-5")
        assert not _is_anthropic_model("openai.gpt-5.6-sol")

    def test_frontier_gpt_prefers_responses(self):
        assert _prefers_responses_api("openai.gpt-5.6-sol")
        assert _prefers_responses_api("openai.gpt-4.1")
        assert _prefers_responses_api("openai.o3")

    def test_open_weight_does_not_prefer_responses(self):
        # gpt-oss, deepseek, qwen route via chat completions
        assert not _prefers_responses_api("openai.gpt-oss-120b")
        assert not _prefers_responses_api("deepseek.v3.1")
        assert not _prefers_responses_api("qwen.qwen3-32b")


class TestUnsupportedRouteDetection:
    def test_detects_does_not_support(self):
        exc = ProviderError(400, "bedrock-mantle", "The model 'x' does not support the '/v1/responses' API")
        assert _is_unsupported_route_error(exc)

    def test_detects_isnt_supported_on_route(self):
        exc = ProviderError(400, "bedrock-mantle", "model `x` isn't supported on this route")
        assert _is_unsupported_route_error(exc)

    def test_ignores_other_errors(self):
        assert not _is_unsupported_route_error(ProviderError(404, "bedrock-mantle", "does not exist"))
        assert not _is_unsupported_route_error(ProviderError(400, "bedrock-mantle", "bad request"))
        assert not _is_unsupported_route_error(ProviderError(429, "bedrock-mantle", "does not support"))


def _make_provider(monkeypatch, calls):
    """Build a provider_fn while recording which API path each call takes."""
    # Avoid real AWS session/credential resolution.
    monkeypatch.setattr(mp.boto3, "Session", lambda: type("S", (), {"get_credentials": lambda self: None})())

    async def fake_responses(creds, endpoint, region, request, mapping):
        calls.append("responses")
        raise ProviderError(400, "bedrock-mantle", f"The model '{mapping.model_id}' does not support the '/v1/responses' API")

    async def fake_chat(creds, endpoint, region, request, mapping):
        calls.append("chat")
        return "CHAT_OK"

    async def fake_messages(creds, endpoint, region, request, mapping):
        calls.append("messages")
        return "MSG_OK"

    monkeypatch.setattr(mp, "_invoke_responses_api", fake_responses)
    monkeypatch.setattr(mp, "_invoke_chat_completions_api", fake_chat)
    monkeypatch.setattr(mp, "_invoke_messages_api", fake_messages)

    factory = create_mantle_provider_fn(region="us-east-1")
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
    return factory(request)


class TestDispatch:
    def test_anthropic_uses_messages(self, monkeypatch):
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="anthropic.claude-sonnet-5")))
        assert result == "MSG_OK"
        assert calls == ["messages"]

    def test_open_weight_uses_chat_directly(self, monkeypatch):
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="qwen.qwen3-32b")))
        assert result == "CHAT_OK"
        assert calls == ["chat"]  # never tried responses

    def test_gpt_oss_falls_back_from_responses_to_chat(self, monkeypatch):
        # openai.gpt-oss-* is not in the responses-preferring set, so it should
        # go straight to chat completions.
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="openai.gpt-oss-120b")))
        assert result == "CHAT_OK"
        assert calls == ["chat"]

    def test_frontier_gpt_falls_back_to_chat_on_unsupported_route(self, monkeypatch):
        # openai.gpt-5.x prefers responses; our fake responses raises the
        # unsupported-route error, so it must fall back to chat.
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="openai.gpt-5.6-sol")))
        assert result == "CHAT_OK"
        assert calls == ["responses", "chat"]


class TestFallbackDoesNotMaskRealErrors:
    def test_non_route_error_propagates(self, monkeypatch):
        monkeypatch.setattr(mp.boto3, "Session", lambda: type("S", (), {"get_credentials": lambda self: None})())

        async def fake_responses(creds, endpoint, region, request, mapping):
            raise ProviderError(429, "bedrock-mantle", "rate limited")

        monkeypatch.setattr(mp, "_invoke_responses_api", fake_responses)
        factory = create_mantle_provider_fn(region="us-east-1")
        request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        fn = factory(request)
        with pytest.raises(ProviderError) as ei:
            _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="openai.gpt-5.6-sol")))
        assert ei.value.status_code == 429
