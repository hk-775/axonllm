"""One routing contract exercised through every v0.3 delivery mode."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from axonllm import (
    AsyncRouter,
    IdentityContext,
    InvalidRequestError,
    OstiariRouterAdapter,
)
from src.gateway.agent import GatewayAgent
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.router_adapter import AgentCoreRouterAdapter
from src.gateway.cache_manager import CacheManager
from src.gateway.chat.client_agent import ClientAgent
from src.gateway.chat.openai_routes import (
    OpenAICompatAPI,
    create_openai_routes,
)
from src.gateway.config_sync import ConfigSyncService
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    Project,
    RateLimitConfig,
    StreamChunk,
    TokenPricing,
    TokenUsage,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.request_validator import RequestValidator
from src.gateway.router import (
    AllProvidersExhaustedError,
    ProviderError,
    Router,
)
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_config_signing import RoutingConfigSignatureError
from src.gateway.routing_runtime import RoutingRuntime


pytestmark = pytest.mark.routing_conformance

_PROJECT_ID = "routing-conformance"
_USER_ID = "routing-conformance-user"
_SIGNING_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
_MODEL_CONFIG = {
    "models": [
        {
            "name": "chat-model",
            "description": "Conformance chat model",
            "capabilities": ["chat", "streaming", "tools"],
            "routing_strategy": "round-robin",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "openai-chat-model",
                    "fallback_order": 0,
                },
                {
                    "provider": "anthropic",
                    "model_id": "anthropic-chat-model",
                    "fallback_order": 1,
                },
            ],
        },
        {
            "name": "embedding-model",
            "description": "Conformance embedding model",
            "capabilities": ["embeddings"],
            "routing_strategy": "round-robin",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "openai-embedding-model",
                    "fallback_order": 0,
                },
                {
                    "provider": "anthropic",
                    "model_id": "anthropic-embedding-model",
                    "fallback_order": 1,
                },
            ],
        },
    ]
}
_SOURCE_SNAPSHOT = RoutingConfigSnapshot.from_registry(
    ModelRegistry.from_config(_MODEL_CONFIG, revision=7)
).with_signature(
    signing_key_arn=_SIGNING_KEY_ARN,
    signature=b"routing-conformance-signature",
)
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Return the weather for one city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]
_TOOL_CALLS = [
    {
        "id": "call-weather",
        "type": "function",
        "function": {
            "name": "get_weather",
            "arguments": '{"city":"Seattle"}',
        },
    }
]


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
            )
    return ""


class _DeterministicProviderFactory:
    """Provider transport with observable, repeatable routing outcomes."""

    available_providers = frozenset({"openai", "anthropic"})

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str]] = []
        self.closed = False

    def create(
        self,
        request: ChatCompletionRequest,
        *,
        prompt_caching_enabled: bool = False,
        spoke: Any = None,
    ):
        del prompt_caching_enabled, spoke
        prompt = _last_user_text(request.messages)

        async def invoke(mapping) -> ChatCompletionResponse:
            self.attempts.append(("chat", mapping.provider))
            if ("force fallback" in prompt and mapping.provider == "openai") or "exhaust providers" in prompt:
                raise ProviderError(
                    503,
                    mapping.provider,
                    "scripted provider failure",
                    retryable=False,
                    provider_unavailable=False,
                )

            if request.tools:
                content = None
                finish_reason = "tool_calls"
                message = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": _TOOL_CALLS,
                }
            else:
                content = f"answer:{prompt}"
                finish_reason = "stop"
                message = {
                    "role": "assistant",
                    "content": content,
                }
            return ChatCompletionResponse(
                id=f"completion-{mapping.provider}",
                choices=[
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                usage=TokenUsage(7, 3, 10),
                model=mapping.model_id,
                provider=mapping.provider,
            )

        return invoke

    def create_embeddings(self, request: EmbeddingRequest):
        async def invoke(mapping) -> EmbeddingResponse:
            self.attempts.append(("embeddings", mapping.provider))
            return EmbeddingResponse(
                id=f"embedding-{mapping.provider}",
                data=[
                    EmbeddingData(
                        index=index,
                        embedding=[float(index), float(len(value))],
                    )
                    for index, value in enumerate(request.input)
                ],
                usage=TokenUsage(4, 0, 4),
                model=mapping.model_id,
                provider=mapping.provider,
            )

        return invoke

    async def execute_streaming(
        self,
        request: ChatCompletionRequest,
        mapping,
        *,
        prompt_caching_enabled: bool = False,
        spoke: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        del request, prompt_caching_enabled, spoke
        self.attempts.append(("stream", mapping.provider))
        if mapping.provider == "openai":
            raise ProviderError(
                503,
                mapping.provider,
                "scripted stream open failure",
                retryable=False,
                provider_unavailable=False,
            )
        yield StreamChunk(
            id="stream-anthropic",
            choices=[
                {
                    "index": 0,
                    "delta": {"content": "streamed "},
                    "finish_reason": None,
                }
            ],
            model=mapping.model_id,
        )
        yield StreamChunk(
            id="stream-anthropic",
            choices=[
                {
                    "index": 0,
                    "delta": {"content": "answer"},
                    "finish_reason": "stop",
                }
            ],
            model=mapping.model_id,
            is_final=True,
            usage=TokenUsage(7, 2, 9),
        )

    def route_snapshot(self) -> list[dict[str, str]]:
        return [
            {
                "provider": provider,
                "route_id": f"{provider}-conformance",
            }
            for provider in sorted(self.available_providers)
        ]

    async def close(self) -> None:
        self.closed = True


@dataclass
class _RuntimeBundle:
    source_snapshot: RoutingConfigSnapshot
    registry: ModelRegistry
    factory: _DeterministicProviderFactory
    runtime: RoutingRuntime
    gateway: GatewayAgent
    config_sync: ConfigSyncService


class _UnavailableSnapshotSource:
    enabled = True

    def __init__(self, snapshot: RoutingConfigSnapshot) -> None:
        self.authenticated_routing_snapshot = snapshot

    async def load_model_registry_snapshot(self, **_kwargs):
        raise RoutingConfigSignatureError("scripted signature verification outage")


def _runtime_bundle() -> _RuntimeBundle:
    source_snapshot = _SOURCE_SNAPSHOT
    registry = ModelRegistry()
    source_snapshot.apply(registry)
    factory = _DeterministicProviderFactory()
    router = Router(
        registry,
        ProviderHealthTracker(),
        max_retries=0,
        base_delay=0.0,
        cooldown_seconds=0,
        available_providers=factory.available_providers,
    )
    validator = RequestValidator(registry)
    runtime = RoutingRuntime(
        router=router,
        provider_factory=factory,
        model_registry=registry,
        validator=validator,
        owns_provider_factory=True,
    )
    pricing = {
        provider: {
            model_id: TokenPricing(
                prompt_token_cost=0.001,
                completion_token_cost=0.002,
            )
            for model_id in (
                f"{provider}-chat-model",
                f"{provider}-embedding-model",
            )
        }
        for provider in factory.available_providers
    }
    cost_tracker = CostTracker(pricing)
    projects = {
        _PROJECT_ID: Project(
            project_id=_PROJECT_ID,
            name="Routing conformance",
            allowed_models=["chat-model", "embedding-model"],
        )
    }
    gateway = GatewayAgent(
        router=router,
        rate_limiter=SlidingWindowRateLimiter(RateLimitConfig()),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=cost_tracker,
        projects=projects,
        provider_fn_factory=factory,
        request_validator=validator,
        routing_runtime=runtime,
    )
    config_sync = ConfigSyncService(
        projects=projects,
        user_configs={},
        cost_tracker=cost_tracker,
        persistence=_UnavailableSnapshotSource(source_snapshot),
        model_registry=registry,
    )
    return _RuntimeBundle(
        source_snapshot=source_snapshot,
        registry=registry,
        factory=factory,
        runtime=runtime,
        gateway=gateway,
        config_sync=config_sync,
    )


def _error_observation(status_code: int) -> dict[str, Any]:
    if status_code == 404:
        category = "model_not_found"
    elif status_code < 500:
        category = "invalid_request"
    else:
        category = "provider_error"
    return {
        "error": {
            "status_code": status_code,
            "category": category,
        }
    }


def _usage_observation(value: Any) -> dict[str, int]:
    if isinstance(value, TokenUsage):
        return {
            "prompt_tokens": value.prompt_tokens,
            "completion_tokens": value.completion_tokens,
            "total_tokens": value.total_tokens,
        }
    if not isinstance(value, dict):
        value = {}
    return {
        "prompt_tokens": int(value.get("prompt_tokens", 0)),
        "completion_tokens": int(value.get("completion_tokens", 0)),
        "total_tokens": int(value.get("total_tokens", 0)),
    }


def _completion_observation(
    *,
    model: str,
    choices: list[dict[str, Any]],
    usage: Any,
) -> dict[str, Any]:
    choice = choices[0]
    message = choice["message"]
    return {
        "model": model,
        "content": message.get("content"),
        "tool_calls": message.get("tool_calls", []),
        "finish_reason": choice.get("finish_reason"),
        "usage": _usage_observation(usage),
    }


def _openai_completion_observation(payload: dict[str, Any]) -> dict[str, Any]:
    return _completion_observation(
        model=payload["model"],
        choices=payload["choices"],
        usage=payload.get("usage"),
    )


def _stream_observation(events: list[dict[str, Any]]) -> dict[str, Any]:
    content: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish_reason = None
    model = ""
    for raw_event in events:
        event = raw_event.get("data", raw_event)
        if event == "[DONE]" or not isinstance(event, dict):
            continue
        model = event.get("model") or model
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        value = delta.get("content")
        if isinstance(value, str):
            content.append(value)
        if isinstance(delta.get("tool_calls"), list):
            tool_calls.extend(delta["tool_calls"])
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
    return {
        "model": model,
        "content": "".join(content),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
    }


def _responses_observation(payload: dict[str, Any]) -> dict[str, Any]:
    content: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in payload.get("output", []):
        if item.get("type") == "message":
            content.extend(
                part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text"
            )
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    },
                }
            )
    usage = payload.get("usage") or {}
    return {
        "model": payload.get("model"),
        "content": "".join(content) or None,
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _responses_stream_observation(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    payloads = [event.get("data", event) for event in events if isinstance(event.get("data", event), dict)]
    return {
        "types": [event["type"] for event in payloads],
        "content": "".join(
            event.get("delta", "") for event in payloads if event["type"] == "response.output_text.delta"
        ),
    }


class _EmbeddedDelivery:
    name = "embedded"
    supports_responses = False

    def __init__(self) -> None:
        self.bundle = _runtime_bundle()
        self.router = AsyncRouter(
            router=self.bundle.runtime.router,
            provider_factory=self.bundle.factory,
            model_registry=self.bundle.registry,
            validator=self.bundle.runtime.validator,
            runtime=self.bundle.runtime,
        )
        self.host = _ConformanceOstiariHost(self.bundle.source_snapshot)
        self.adapter = OstiariRouterAdapter(
            self.router,
            self.host,
            trusted_signing_key_arn=_SIGNING_KEY_ARN,
        )

    async def start(self) -> None:
        await self.adapter.start()

    async def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.adapter.route(
                body.get("messages", []),
                identity=IdentityContext(
                    principal_id=_USER_ID,
                    tenant_id="routing-conformance-tenant",
                    project_id=_PROJECT_ID,
                ),
                model=body.get("model", ""),
                temperature=body.get("temperature"),
                max_tokens=body.get("max_tokens"),
                top_p=body.get("top_p"),
                stop=body.get("stop"),
                system=body.get("system"),
                tools=body.get("tools"),
                tool_choice=body.get("tool_choice"),
                preferred_provider=body.get("provider"),
            )
        except InvalidRequestError as exc:
            status_code = 404 if exc.errors and exc.errors[0].field == "model" else 400
            return _error_observation(status_code)
        except AllProvidersExhaustedError:
            return _error_observation(502)
        return {
            "model": response.model,
            "content": response.content,
            "tool_calls": list(response.tool_calls),
            "finish_reason": response.finish_reason,
            "usage": {
                "prompt_tokens": response.input_tokens,
                "completion_tokens": response.output_tokens,
                "total_tokens": (response.input_tokens + response.output_tokens),
            },
        }

    async def stream(self, body: dict[str, Any]) -> dict[str, Any]:
        stream = await self.router.chat.completions.create(
            model=body["model"],
            messages=body["messages"],
            stream=True,
            tools=body.get("tools"),
            tool_choice=body.get("tool_choice"),
        )
        assert not isinstance(stream, ChatCompletionResponse)
        events = [
            {
                "model": chunk.model,
                "choices": chunk.choices,
            }
            async for chunk in stream
        ]
        return _stream_observation(events)

    async def embeddings(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.router.embeddings.create(
            model=body["model"],
            input=body["input"],
            encoding_format=body.get("encoding_format", "float"),
            dimensions=body.get("dimensions"),
            user=body.get("user"),
        )
        return {
            "model": response.model,
            "data": [
                {
                    "index": item.index,
                    "embedding": item.embedding,
                }
                for item in response.data
            ],
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        }

    async def models(self) -> list[str]:
        return [model.name for model in await self.router.models.list()]

    async def close(self) -> None:
        await self.adapter.close()


class _ConformanceOstiariHost:
    def __init__(self, snapshot: RoutingConfigSnapshot) -> None:
        self.snapshot = snapshot

    async def load_snapshot(self):
        return self.snapshot

    async def publish_snapshot(self, config, *, expected_revision):
        del config, expected_revision
        raise AssertionError("conformance host is read-only")

    async def resolve(self, *, provider, reference):
        del provider, reference
        raise AssertionError("conformance routes are preconfigured")

    async def emit(self, event):
        del event

    async def record(self, usage):
        del usage

    async def start(self):
        return None

    async def close(self):
        return None


class _StandaloneDelivery:
    name = "standalone"
    supports_responses = True

    def __init__(self) -> None:
        self.bundle = _runtime_bundle()
        client_agent = ClientAgent(
            self.bundle.gateway,
            default_project_id=_PROJECT_ID,
            default_user_id=_USER_ID,
        )
        app = Starlette(routes=create_openai_routes(OpenAICompatAPI(client_agent)))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://routing-conformance",
        )

    async def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/chat/completions",
            json=body,
        )
        if response.status_code >= 400:
            return _error_observation(response.status_code)
        return _openai_completion_observation(response.json())

    async def stream(self, body: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        async with self.client.stream(
            "POST",
            "/v1/chat/completions",
            json={**body, "stream": True},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                events.append(json.loads(data))
        return _stream_observation(events)

    async def responses(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post("/v1/responses", json=body)
        if response.status_code >= 400:
            return _error_observation(response.status_code)
        return _responses_observation(response.json())

    async def responses_stream(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        async with self.client.stream(
            "POST",
            "/v1/responses",
            json={**body, "stream": True},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return _responses_stream_observation(events)

    async def embeddings(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post("/v1/embeddings", json=body)
        assert response.status_code == 200
        payload = response.json()
        return {
            "model": payload["model"],
            "data": [
                {
                    "index": item["index"],
                    "embedding": item["embedding"],
                }
                for item in payload["data"]
            ],
            "usage": payload["usage"],
        }

    async def models(self) -> list[str]:
        response = await self.client.get("/v1/models")
        assert response.status_code == 200
        return [model["id"] for model in response.json()["data"]]

    async def close(self) -> None:
        await self.client.aclose()
        await self.bundle.runtime.close()


class _GatewayInternalAdapter:
    """AgentCore's authenticated dispatch boundary with identity pre-resolved."""

    def __init__(self, bundle: _RuntimeBundle) -> None:
        self.bundle = bundle

    async def initialize(self) -> None:
        return None

    async def readiness(self) -> dict[str, Any]:
        return {"status": "ready", "ready": True}

    async def close(self) -> None:
        await self.bundle.runtime.close()

    async def invoke(self, payload: Any, context: Any) -> Any:
        del context
        request = dict(payload)
        action = request.pop("action")
        gateway_context = {
            "user_id": _USER_ID,
            "project_id": _PROJECT_ID,
            "roles": [],
            "scopes": [],
            "allow_legacy_project_lookup": True,
        }
        provider = request.pop("provider", None)
        if provider is not None:
            gateway_context["provider"] = provider
        if action == "list_models":
            return await self.bundle.gateway.handle_list_models(
                project_id=_PROJECT_ID,
                user_id=_USER_ID,
                allow_legacy_project_lookup=True,
            )
        if action == "embeddings":
            return await self.bundle.gateway.handle_embeddings(
                request,
                gateway_context,
            )
        if action == "chat":
            return await self.bundle.gateway.handle_chat_completion(
                request,
                gateway_context,
            )
        raise AssertionError(f"unsupported conformance action: {action}")


class _AgentCoreDelivery:
    name = "agentcore"
    supports_responses = True

    def __init__(self) -> None:
        self.bundle = _runtime_bundle()
        self.adapter = AgentCoreRouterAdapter(_GatewayInternalAdapter(self.bundle))

    async def _invoke(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"method": method, "path": path}
        if body is not None:
            payload["body"] = body
        try:
            return await self.adapter.invoke(payload, object())
        except AgentCoreAdapterError as exc:
            return _error_observation(exc.status_code)

    async def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._invoke(
            "POST",
            "/v1/chat/completions",
            body,
        )
        if "error" in response:
            return response
        return _openai_completion_observation(response)

    async def stream(self, body: dict[str, Any]) -> dict[str, Any]:
        stream = await self._invoke(
            "POST",
            "/v1/chat/completions",
            {**body, "stream": True},
        )
        assert hasattr(stream, "__aiter__")
        events = [event async for event in stream]
        return _stream_observation(events)

    async def responses(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._invoke("POST", "/v1/responses", body)
        if response.get("error") is not None:
            return response
        return _responses_observation(response)

    async def responses_stream(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        stream = await self._invoke(
            "POST",
            "/v1/responses",
            {**body, "stream": True},
        )
        assert hasattr(stream, "__aiter__")
        events = [event async for event in stream]
        return _responses_stream_observation(events)

    async def embeddings(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._invoke("POST", "/v1/embeddings", body)
        assert "error" not in response
        return {
            "model": response["model"],
            "data": [
                {
                    "index": item["index"],
                    "embedding": item["embedding"],
                }
                for item in response["data"]
            ],
            "usage": response["usage"],
        }

    async def models(self) -> list[str]:
        response = await self._invoke("GET", "/v1/models")
        return [model["id"] for model in response["data"]]

    async def close(self) -> None:
        await self.adapter.close()


@pytest.fixture(
    params=(
        _EmbeddedDelivery,
        _StandaloneDelivery,
        _AgentCoreDelivery,
    ),
    ids=("embedded", "standalone", "agentcore"),
)
async def delivery(request):
    instance = request.param()
    start = getattr(instance, "start", None)
    if start is not None:
        await start()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture(
    params=(_StandaloneDelivery, _AgentCoreDelivery),
    ids=("standalone", "agentcore"),
)
async def responses_delivery(request):
    instance = request.param()
    try:
        yield instance
    finally:
        await instance.close()


async def test_modes_adopt_the_same_versioned_signed_snapshot(delivery) -> None:
    source = delivery.bundle.source_snapshot
    active = delivery.bundle.runtime.config_snapshot()

    assert source.is_signed is True
    assert active.revision == source.revision == 7
    assert active.sha256 == source.sha256
    assert await delivery.models() == ["chat-model", "embedding-model"]


async def test_signature_outage_keeps_last_known_good_routes(delivery) -> None:
    before = delivery.bundle.runtime.config_snapshot()

    assert await delivery.bundle.config_sync.refresh_routing_if_stale() is False
    assert delivery.bundle.config_sync.routing_config_status == {
        "status": "degraded",
        "revision": 7,
        "sha256": delivery.bundle.source_snapshot.sha256,
        "signed": True,
        "error": "signature_verification_failed",
    }

    result = await delivery.chat(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "still available"}],
        }
    )
    after = delivery.bundle.runtime.config_snapshot()

    assert result["content"] == "answer:still available"
    assert after.revision == before.revision
    assert after.sha256 == before.sha256


async def test_chat_completion_model_content_and_usage_conform(delivery) -> None:
    result = await delivery.chat(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert result == {
        "model": "chat-model",
        "content": "answer:hello",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
    }


async def test_pre_first_byte_fallback_order_conforms(delivery) -> None:
    result = await delivery.chat(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "force fallback"}],
        }
    )

    assert result["content"] == "answer:force fallback"
    assert delivery.bundle.factory.attempts == [
        ("chat", "openai"),
        ("chat", "anthropic"),
    ]


async def test_tool_call_transport_and_finish_reason_conform(delivery) -> None:
    result = await delivery.chat(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "weather"}],
            "tools": _TOOLS,
            "tool_choice": {
                "type": "function",
                "function": {"name": "get_weather"},
            },
        }
    )

    assert result["content"] is None
    assert result["finish_reason"] == "tool_calls"
    assert result["tool_calls"] == _TOOL_CALLS


async def test_streaming_model_content_finish_and_fallback_conform(
    delivery,
) -> None:
    result = await delivery.stream(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "stream"}],
        }
    )

    assert result == {
        "model": "chat-model",
        "content": "streamed answer",
        "tool_calls": [],
        "finish_reason": "stop",
    }
    assert delivery.bundle.factory.attempts == [
        ("stream", "openai"),
        ("stream", "anthropic"),
    ]


async def test_embeddings_order_model_and_usage_conform(delivery) -> None:
    result = await delivery.embeddings(
        {
            "model": "embedding-model",
            "input": ["a", "three"],
        }
    )

    assert result == {
        "model": "embedding-model",
        "data": [
            {"index": 0, "embedding": [0.0, 1.0]},
            {"index": 1, "embedding": [1.0, 5.0]},
        ],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


async def test_unknown_model_error_conforms(delivery) -> None:
    result = await delivery.chat(
        {
            "model": "missing-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert result == _error_observation(404)


async def test_provider_exhaustion_error_conforms(delivery) -> None:
    result = await delivery.chat(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "exhaust providers"}],
        }
    )

    assert result == _error_observation(502)
    assert delivery.bundle.factory.attempts == [
        ("chat", "openai"),
        ("chat", "anthropic"),
    ]


async def test_responses_translation_conforms(responses_delivery) -> None:
    result = await responses_delivery.responses(
        {
            "model": "chat-model",
            "input": "hello",
            "instructions": "Be concise.",
        }
    )

    assert result == {
        "model": "chat-model",
        "content": "answer:hello",
        "tool_calls": [],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
    }


async def test_responses_stream_event_contract_conforms(
    responses_delivery,
) -> None:
    result = await responses_delivery.responses_stream(
        {
            "model": "chat-model",
            "input": "stream",
        }
    )

    assert result["content"] == "streamed answer"
    assert result["types"][0:2] == [
        "response.created",
        "response.in_progress",
    ]
    assert result["types"][-1] == "response.completed"
