"""Stable embedded API over the AxonLLM routing data plane."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

import yaml

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelSummary,
    ProviderModelMapping,
    StreamChunk,
    ValidationError,
)
from src.gateway.router import AllProvidersExhaustedError, ProviderError
from src.gateway.routing import NoHealthyProviderError
from src.gateway.streaming import simulate_streaming

if TYPE_CHECKING:
    from src.gateway.model_registry import ModelRegistry
    from src.gateway.multi_provider_factory import MultiProviderFactory
    from src.gateway.request_validator import RequestValidator
    from src.gateway.router import Router


class InvalidRequestError(ValueError):
    """Raised when an embedded completion request fails validation."""

    def __init__(self, errors: Sequence[ValidationError]) -> None:
        self.errors = tuple(errors)
        detail = "; ".join(
            f"{error.field}: {error.message}" for error in self.errors
        )
        super().__init__(detail or "Invalid request")


class RouterClosedError(RuntimeError):
    """Raised when a closed embedded router is used."""


class _Completions:
    def __init__(self, owner: AsyncRouter) -> None:
        self._owner = owner

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: Literal[False] = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> ChatCompletionResponse: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: Literal[True],
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> AsyncIterator[StreamChunk]: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[StreamChunk]: ...

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[StreamChunk]:
        """Create a routed chat completion.

        When ``stream`` is true, awaiting this method returns an async iterator.
        Provider fallback is allowed only before the first chunk is yielded.
        """
        request = ChatCompletionRequest(
            model=model,
            messages=messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
        )
        return await self._owner._complete(
            request,
            preferred_provider=preferred_provider,
        )


class _Chat:
    def __init__(self, owner: AsyncRouter) -> None:
        self.completions = _Completions(owner)


class _Models:
    def __init__(self, owner: AsyncRouter) -> None:
        self._owner = owner

    async def list(self) -> list[ModelSummary]:
        """List models that have at least one enabled provider mapping."""
        self._owner._ensure_open()
        summaries: list[ModelSummary] = []
        for model in self._owner._model_registry.list_models():
            mappings = self._owner._router.available_mappings(model.name)
            if not mappings:
                continue
            summaries.append(
                ModelSummary(
                    name=model.name,
                    description=model.description,
                    providers=sorted(
                        {mapping.provider for mapping in mappings}
                    ),
                    capabilities=list(model.capabilities or []),
                    routing_strategy=model.routing_strategy.value,
                )
            )
        return summaries


class AsyncRouter:
    """Embeddable asynchronous AxonLLM multi-provider router.

    ``from_files`` is the local/bootstrap constructor. Production deployments
    should provide these files from a versioned control-plane snapshot.
    Constructing this class does not initialize the AxonLLM admin, identity,
    query, or persistence services.
    """

    def __init__(
        self,
        *,
        router: Router,
        provider_factory: MultiProviderFactory,
        model_registry: ModelRegistry,
        validator: RequestValidator,
    ) -> None:
        self._router = router
        self._provider_factory = provider_factory
        self._model_registry = model_registry
        self._validator = validator
        self._closed = False
        self.chat = _Chat(self)
        self.models = _Models(self)

    @classmethod
    def from_files(
        cls,
        *,
        models: str | Path,
        providers: str | Path,
        pricing: str | Path | None = None,
        enabled_providers: Iterable[str] | None = None,
        bedrock_region: str = "us-east-1",
        max_retries: int = 2,
        base_delay: float = 0.5,
        cooldown_seconds: int = 60,
        require_priced_mappings: bool = False,
    ) -> AsyncRouter:
        """Build a router from strictly validated local configuration files."""
        from src.gateway.config_loader import load_pricing_config
        from src.gateway.cost_tracker import CostTracker
        from src.gateway.health_tracker import ProviderHealthTracker
        from src.gateway.model_registry import ModelRegistry
        from src.gateway.multi_provider_factory import MultiProviderFactory
        from src.gateway.provider_loader import load_provider_routes
        from src.gateway.request_validator import RequestValidator
        from src.gateway.router import Router

        model_document = yaml.safe_load(
            Path(models).read_text(encoding="utf-8")
        )
        if not isinstance(model_document, dict):
            raise ValueError(
                "model configuration must contain a YAML object"
            )
        registry = ModelRegistry.from_config(model_document)
        pricing_config = (
            load_pricing_config(str(pricing)) if pricing is not None else {}
        )
        cost_tracker = CostTracker(pricing_config)
        routes = load_provider_routes(str(providers))
        provider_set = (
            frozenset(enabled_providers)
            if enabled_providers is not None
            else None
        )
        factory = MultiProviderFactory(
            bedrock_region=bedrock_region,
            enabled_providers=provider_set,
            provider_routes=routes,
        )
        try:
            router = Router(
                registry,
                ProviderHealthTracker(),
                max_retries=max_retries,
                base_delay=base_delay,
                cooldown_seconds=cooldown_seconds,
                cost_tracker=cost_tracker,
                available_providers=factory.available_providers,
                require_priced_mappings=require_priced_mappings,
            )
            validator = RequestValidator(registry)
        except BaseException:
            factory.close_credential_providers()
            raise
        return cls(
            router=router,
            provider_factory=factory,
            model_registry=registry,
            validator=validator,
        )

    @property
    def available_providers(self) -> frozenset[str]:
        """Providers with an enabled route in this router process."""
        self._ensure_open()
        return self._provider_factory.available_providers

    def route_snapshot(self) -> list[dict]:
        """Return a secret-free snapshot of this process's concrete routes."""
        self._ensure_open()
        return self._provider_factory.route_snapshot()

    async def close(self) -> None:
        """Release HTTP sessions and refreshable credential providers."""
        if self._closed:
            return
        self._closed = True
        await self._provider_factory.close()

    async def __aenter__(self) -> AsyncRouter:
        self._ensure_open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RouterClosedError("AxonLLM router is closed")

    async def _complete(
        self,
        request: ChatCompletionRequest,
        *,
        preferred_provider: str | None,
    ) -> ChatCompletionResponse | AsyncIterator[StreamChunk]:
        self._ensure_open()
        errors = self._validator.validate(request)
        if errors:
            raise InvalidRequestError(errors)
        if request.stream:
            return self._stream(
                request,
                preferred_provider=preferred_provider,
            )
        provider_fn = self._provider_factory.create(request)
        return await self._router.execute_with_fallback(
            request,
            provider_fn,
            preferred_provider=preferred_provider,
        )

    def _stream_chain(
        self,
        model: str,
        preferred_provider: str | None,
    ) -> list[ProviderModelMapping]:
        mappings = self._router.available_mappings(model)
        if preferred_provider:
            return sorted(
                mappings,
                key=lambda mapping: (
                    mapping.provider != preferred_provider,
                    mapping.fallback_order,
                ),
            )

        strategy = self._router._get_strategy(model)
        try:
            initial = strategy.select(
                mappings,
                self._router.health_tracker,
            )
        except NoHealthyProviderError:
            return sorted(mappings, key=lambda item: item.fallback_order)
        return [
            initial,
            *sorted(
                [
                    mapping
                    for mapping in mappings
                    if mapping is not initial
                ],
                key=lambda item: item.fallback_order,
            ),
        ]

    async def _stream(
        self,
        request: ChatCompletionRequest,
        *,
        preferred_provider: str | None,
    ) -> AsyncIterator[StreamChunk]:
        attempts: list[dict] = []
        for mapping in self._stream_chain(
            request.model,
            preferred_provider,
        ):
            if not self._router.health_tracker.is_healthy(mapping.provider):
                attempts.append(
                    {
                        "provider": mapping.provider,
                        "status_code": 0,
                        "message": "skipped (unhealthy)",
                    }
                )
                continue

            stream = self._provider_factory.execute_streaming(
                request,
                mapping,
            )
            try:
                first = await stream.__anext__()
            except StopAsyncIteration:
                return
            except ProviderError as exc:
                attempts.append(
                    {
                        "provider": mapping.provider,
                        "status_code": exc.status_code,
                        "message": exc.message,
                        **(
                            {"route_id": exc.route_id}
                            if exc.route_id
                            else {}
                        ),
                    }
                )
                if exc.provider_unavailable is not False:
                    self._router.health_tracker.mark_unhealthy(
                        mapping.provider,
                        self._router.cooldown_seconds,
                    )
                continue

            first.model = request.model
            yield first
            try:
                async for chunk in stream:
                    chunk.model = request.model
                    yield chunk
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    await close()
            return

        # Some providers, including the boto3 Bedrock transport, do not expose
        # native SSE. Preserve streaming semantics by routing one normal
        # completion through the same fallback engine and chunking the result.
        buffered_request = replace(request, stream=False)
        provider_fn = self._provider_factory.create(buffered_request)
        try:
            response = await self._router.execute_with_fallback(
                buffered_request,
                provider_fn,
                preferred_provider=preferred_provider,
            )
        except AllProvidersExhaustedError as exc:
            raise AllProvidersExhaustedError(
                [*attempts, *exc.attempts]
            ) from exc
        for chunk in simulate_streaming(response):
            yield chunk
