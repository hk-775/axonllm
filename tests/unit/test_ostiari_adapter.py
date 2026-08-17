"""Production contract tests for the infrastructure-neutral Ostiari adapter."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from axonllm import (
    IdentityContext,
    OstiariAdapterNotStartedError,
    OstiariConfigurationError,
    OstiariRouterAdapter,
    OstiariRoutingModeUnavailableError,
    OstiariUsageRecordingError,
    build_ostiari_adapter,
)
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    TokenPricing,
    TokenUsage,
)
from src.gateway.request_validator import RequestValidator
from src.gateway.router import Router
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_runtime import RoutingRuntime

_REPO = Path(__file__).resolve().parents[2]
_SIGNING_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
_MODEL_CONFIG = {
    "models": [
        {
            "name": "chat-model",
            "description": "Ostiari adapter test model",
            "capabilities": ["chat", "tools"],
            "routing_strategy": "round-robin",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "provider-chat-model",
                }
            ],
        }
    ]
}


def _signed_snapshot(
    config: dict[str, Any] | None = None,
    *,
    revision: int = 1,
) -> RoutingConfigSnapshot:
    return RoutingConfigSnapshot.from_config(
        config or _MODEL_CONFIG,
        revision=revision,
    ).with_signature(
        signing_key_arn=_SIGNING_KEY_ARN,
        signature=b"verified-by-ostiari-host",
    )


class _ProviderFactory:
    available_providers = frozenset({"openai"})

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.last_request: ChatCompletionRequest | None = None
        self.configured_routes: list[dict[str, Any]] = []

    def create(self, request, **_kwargs):
        async def invoke(mapping):
            self.calls += 1
            self.last_request = request
            tool_calls = (
                [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"query":"test"}',
                        },
                    }
                ]
                if request.tools
                else []
            )
            return ChatCompletionResponse(
                id=f"provider-{self.calls}",
                choices=[
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None if tool_calls else "answer",
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": ("tool_calls" if tool_calls else "stop"),
                    }
                ],
                usage=TokenUsage(7, 3, 10),
                model=request.model,
                provider=mapping.provider,
                provider_model=mapping.model_id,
            )

        return invoke

    def configure_routes(self, routes):
        self.configured_routes = deepcopy(routes)
        return {"routes": len(routes), "providers": 1}

    def route_snapshot(self):
        return [
            {
                "route_id": route["route_id"],
                "provider": route["provider"],
                "has_credentials": bool(route.get("credentials")),
            }
            for route in self.configured_routes
        ]

    async def close(self) -> None:
        self.closed = True


class _Host:
    def __init__(self, snapshot: RoutingConfigSnapshot) -> None:
        self.snapshot = snapshot
        self.events: list[str] = []
        self.telemetry: list[dict[str, Any]] = []
        self.usage: list[Any] = []
        self.resolutions: list[tuple[str, str]] = []
        self.fail_telemetry = False
        self.fail_usage = False
        self.fail_start = False

    async def load_snapshot(self):
        self.events.append("snapshot:load")
        return self.snapshot

    async def publish_snapshot(self, config, *, expected_revision):
        self.events.append(f"snapshot:publish:{expected_revision}")
        self.snapshot = _signed_snapshot(
            dict(config),
            revision=expected_revision + 1,
        )
        return self.snapshot

    async def resolve(self, *, provider, reference):
        self.resolutions.append((provider, reference))
        return {"api_key": "resolved-secret"}

    async def emit(self, event):
        if self.fail_telemetry:
            raise RuntimeError("telemetry unavailable")
        self.telemetry.append(deepcopy(dict(event)))

    async def record(self, usage):
        if self.fail_usage:
            raise RuntimeError("usage unavailable")
        self.usage.append(usage)

    async def start(self):
        if self.fail_start:
            raise RuntimeError("secret-token-must-not-leak")
        self.events.append("host:start")

    async def close(self):
        self.events.append("host:close")


def _router(
    snapshot: RoutingConfigSnapshot | None = None,
) -> tuple[Any, _ProviderFactory]:
    active = snapshot or _signed_snapshot()
    registry = ModelRegistry()
    active.apply(registry)
    factory = _ProviderFactory()
    cost_tracker = CostTracker(
        {
            "openai": {
                "provider-chat-model": TokenPricing(
                    prompt_token_cost=0.001,
                    completion_token_cost=0.002,
                )
            }
        }
    )
    router = Router(
        registry,
        ProviderHealthTracker(),
        max_retries=0,
        cost_tracker=cost_tracker,
        available_providers=factory.available_providers,
    )
    runtime = RoutingRuntime(
        router=router,
        provider_factory=factory,
        model_registry=registry,
        validator=RequestValidator(registry),
        owns_provider_factory=True,
    )
    from axonllm import AsyncRouter

    return (
        AsyncRouter(
            router=router,
            provider_factory=factory,
            model_registry=registry,
            validator=runtime.validator,
            runtime=runtime,
        ),
        factory,
    )


def _identity() -> IdentityContext:
    return IdentityContext(
        principal_id="agent-1",
        tenant_id="tenant-1",
        project_id="project-1",
        roles=frozenset({"agent"}),
        scopes=frozenset({"model.invoke"}),
    )


def test_ostiari_adapter_import_does_not_require_host_server_or_aws() -> None:
    code = textwrap.dedent(
        """
        import importlib.abc
        import sys

        blocked = {
            "boto3",
            "botocore",
            "fastapi",
            "ostiari",
            "ostiari_gateway",
            "starlette",
            "uvicorn",
        }

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition(".")[0] in blocked:
                    raise AssertionError(f"unexpected host import: {fullname}")
                return None

        sys.meta_path.insert(0, Blocker())
        from axonllm import OstiariRouterAdapter, build_ostiari_adapter

        assert OstiariRouterAdapter.__module__ == "axonllm.ostiari"
        assert build_ostiari_adapter.__module__ == "axonllm.assemblies"
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPO)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_lifecycle_is_explicit_idempotent_and_owned_by_ostiari() -> None:
    router, factory = _router()
    host = _Host(_signed_snapshot())
    adapter = build_ostiari_adapter(
        router=router,
        host=host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )

    assert adapter.available is False
    with pytest.raises(OstiariAdapterNotStartedError):
        adapter.require()

    await adapter.start()
    await adapter.start()

    assert adapter.available is True
    assert adapter.active_snapshot.revision == 1
    assert host.events == ["host:start", "snapshot:load"]

    await adapter.close()
    await adapter.close()

    assert adapter.available is False
    assert factory.closed is True
    assert host.events[-1] == "host:close"
    with pytest.raises(OstiariAdapterNotStartedError, match="closed"):
        adapter.require()


@pytest.mark.asyncio
async def test_start_rejects_unsigned_or_untrusted_configuration() -> None:
    unsigned = RoutingConfigSnapshot.from_config(_MODEL_CONFIG, revision=1)
    router, _ = _router()
    host = _Host(unsigned)
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )

    with pytest.raises(OstiariConfigurationError, match="must be signed"):
        await adapter.start()
    assert host.events == ["host:start", "snapshot:load", "host:close"]

    wrong_key = _signed_snapshot()
    wrong_key = RoutingConfigSnapshot.from_config(
        wrong_key.config,
        revision=wrong_key.revision,
    ).with_signature(
        signing_key_arn=("arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        signature=b"wrong-key",
    )
    router, _ = _router()
    host = _Host(wrong_key)
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )

    with pytest.raises(
        OstiariConfigurationError,
        match="unexpected signing key",
    ):
        await adapter.start()


@pytest.mark.asyncio
async def test_startup_error_state_does_not_expose_host_exception_text() -> None:
    router, _ = _router()
    host = _Host(_signed_snapshot())
    host.fail_start = True
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )

    with pytest.raises(RuntimeError, match="secret-token"):
        await adapter.start()

    assert adapter.error == "RuntimeError"
    assert "secret-token" not in adapter.error


@pytest.mark.asyncio
async def test_configuration_refresh_rejects_rollback_and_equivocation() -> None:
    router, _ = _router()
    host = _Host(_signed_snapshot())
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()

    updated = deepcopy(_MODEL_CONFIG)
    updated["models"].append(
        {
            "name": "second-model",
            "description": "second",
            "capabilities": ["chat"],
            "routing_strategy": "round-robin",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "provider-chat-model",
                }
            ],
        }
    )
    host.snapshot = _signed_snapshot(updated, revision=2)
    await adapter.refresh_configuration()

    assert adapter.knows_model("second-model") is True
    assert adapter.active_snapshot.revision == 2

    host.snapshot = _signed_snapshot(_MODEL_CONFIG, revision=1)
    with pytest.raises(OstiariConfigurationError, match="rollback"):
        await adapter.refresh_configuration()
    assert adapter.active_snapshot.revision == 2

    host.snapshot = _signed_snapshot(_MODEL_CONFIG, revision=2)
    with pytest.raises(OstiariConfigurationError, match="equivocation"):
        await adapter.refresh_configuration()
    assert adapter.knows_model("second-model") is True
    await adapter.close()


@pytest.mark.asyncio
async def test_publish_binds_expected_revision_and_exact_candidate() -> None:
    router, _ = _router()
    host = _Host(_signed_snapshot())
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()
    updated = deepcopy(_MODEL_CONFIG)
    updated["models"][0]["description"] = "published"

    published = await adapter.publish_configuration(
        updated,
        expected_revision=1,
    )

    assert published.revision == 2
    assert adapter.model_registry_config()["models"][0]["description"] == ("published")
    with pytest.raises(
        OstiariConfigurationError,
        match="revision changed",
    ):
        await adapter.publish_configuration(
            updated,
            expected_revision=1,
        )
    assert host.events.count("snapshot:publish:1") == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_provider_routes_use_opaque_credential_references() -> None:
    router, factory = _router()
    host = _Host(_signed_snapshot())
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()

    with pytest.raises(
        OstiariConfigurationError,
        match="credential_reference",
    ):
        await adapter.configure_provider_routes(
            [
                {
                    "route_id": "openai:inline",
                    "provider": "openai",
                    "credentials": {"api_key": "plaintext"},
                }
            ]
        )

    supplied = [
        {
            "route_id": "openai:primary",
            "provider": "openai",
            "endpoint": "https://openai.example",
            "credential_reference": "vault://openai/primary",
        }
    ]
    original = deepcopy(supplied)
    result = await adapter.configure_provider_routes(supplied)

    assert supplied == original
    assert result == {"routes": 1, "providers": 1}
    assert host.resolutions == [("openai", "vault://openai/primary")]
    assert factory.configured_routes[0]["credentials"] == {"api_key": "resolved-secret"}
    assert adapter.provider_route_snapshot() == [
        {
            "route_id": "openai:primary",
            "provider": "openai",
            "has_credentials": True,
        }
    ]
    assert "resolved-secret" not in repr(adapter.provider_route_snapshot())
    await adapter.close()


@pytest.mark.asyncio
async def test_route_records_identity_usage_and_secret_free_telemetry() -> None:
    router, factory = _router()
    host = _Host(_signed_snapshot())
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()
    result = await adapter.route(
        [{"role": "user", "content": "hello"}],
        identity=_identity(),
        model="chat-model",
        system=[{"type": "text", "text": "Be concise."}],
        session_id="session-1",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert result.content is None
    assert result.model == "chat-model"
    assert result.provider == "openai"
    assert result.input_tokens == 7
    assert result.output_tokens == 3
    assert result.cost == pytest.approx(0.000013)
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0]["function"]["name"] == "lookup"
    assert factory.calls == 1
    assert factory.last_request is not None
    assert factory.last_request.messages[0] == {
        "role": "system",
        "content": "Be concise.",
    }

    assert len(host.usage) == 1
    usage = host.usage[0]
    assert usage.tenant_id == "tenant-1"
    assert usage.project_id == "project-1"
    assert usage.user_id == "agent-1"
    assert usage.request_id == result.request_id
    assert usage.provider_request_id == result.provider_request_id

    assert len(host.telemetry) == 1
    event = host.telemetry[0]
    assert event["status"] == "success"
    assert event["session_id"] == "session-1"
    assert event["tenant_id"] == "tenant-1"
    assert "messages" not in event
    assert "credentials" not in event
    assert "resolved-secret" not in repr(event)
    await adapter.close()


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_fail_completed_request() -> None:
    router, factory = _router()
    host = _Host(_signed_snapshot())
    host.fail_telemetry = True
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()

    result = await adapter.route(
        [{"role": "user", "content": "hello"}],
        identity=_identity(),
        model="chat-model",
    )

    assert result.content == "answer"
    assert factory.calls == 1
    assert len(host.usage) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_usage_failure_preserves_result_without_provider_retry() -> None:
    router, factory = _router()
    host = _Host(_signed_snapshot())
    host.fail_usage = True
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()

    with pytest.raises(OstiariUsageRecordingError) as failure:
        await adapter.route(
            [{"role": "user", "content": "hello"}],
            identity=_identity(),
            model="chat-model",
        )

    assert failure.value.result.content == "answer"
    assert factory.calls == 1
    assert host.telemetry[0]["status"] == "accounting_failed"
    await adapter.close()


@pytest.mark.asyncio
async def test_unconfigured_smart_mode_fails_without_provider_call() -> None:
    router, factory = _router()
    host = _Host(_signed_snapshot())
    adapter = OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=_SIGNING_KEY_ARN,
    )
    await adapter.start()

    with pytest.raises(
        OstiariRoutingModeUnavailableError,
        match="smart routing",
    ):
        await adapter.route(
            [{"role": "user", "content": "choose a model"}],
            identity=_identity(),
            smart=True,
        )

    assert factory.calls == 0
    assert host.telemetry[0]["status"] == "failed"
    await adapter.close()
