"""Router with retry, exponential backoff, and fallback logic."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from src.gateway.config import DEFAULT_CONFIG, RetryConfig
from src.gateway.ensemble import EnsembleStrategy
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EnsembleDecision,
    EnsemblePreset,
    PanelMemberResult,
    ProviderModelMapping,
    RoutingStrategy,
    SmartRoutingDecision,
    UsageRecord,
)
from src.gateway.routing import (
    CostOptimizedStrategy,
    LeastLatencyStrategy,
    NoHealthyProviderError,
    RoundRobinStrategy,
    RoutingStrategyBase,
    WeightedStrategy,
)

if TYPE_CHECKING:
    from src.gateway.cost_tracker import CostTracker
    from src.gateway.ensemble_config import EnsembleConfig
    from src.gateway.smart_routing import SmartRoutingStrategy


class ProviderError(Exception):
    """Error raised by a provider call, carrying HTTP-style status code."""

    def __init__(self, status_code: int, provider: str, message: str) -> None:
        self.status_code = status_code
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {status_code}: {message}")


class AllProvidersExhaustedError(Exception):
    """Raised when every provider in the fallback chain has failed."""

    def __init__(self, attempts: list[dict]) -> None:
        self.attempts = attempts
        summary = "; ".join(
            f"{a['provider']}({a['status_code']}): {a['message']}" for a in attempts
        )
        super().__init__(f"All providers exhausted: {summary}")


class EnsembleAccessError(Exception):
    """Raised when a panel member or judge model is not in the allowed set.

    Carries the disallowed model identifier for a 403-style access error.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"Model '{model}' is not in the allowed models list")


class EnsembleCostCeilingError(Exception):
    """Raised when the estimated ensemble cost exceeds the preset's ceiling.

    Carries both the estimated cost and the configured ceiling.
    """

    def __init__(self, estimated: float, ceiling: float) -> None:
        self.estimated = estimated
        self.ceiling = ceiling
        super().__init__(
            f"Estimated ensemble cost {estimated} exceeds ceiling {ceiling}"
        )


class EnsembleNoSurvivorsError(Exception):
    """Raised when no panel member returned a usable response.

    Carries the partial :class:`EnsembleDecision` for observability.
    """

    def __init__(self, decision: EnsembleDecision) -> None:
        self.decision = decision
        super().__init__(decision.error or "no panel responses available")


class EnsembleQuorumError(Exception):
    """Raised when survivors are below quorum under the error fallback policy.

    Carries the partial :class:`EnsembleDecision` for observability.
    """

    def __init__(self, decision: EnsembleDecision) -> None:
        self.decision = decision
        super().__init__(decision.error or "quorum not met")


class EnsembleSynthesisError(Exception):
    """Raised when the judge synthesis call fails after fallback.

    Carries the partial :class:`EnsembleDecision` (with survivors preserved)
    for observability.
    """

    def __init__(self, decision: EnsembleDecision) -> None:
        self.decision = decision
        super().__init__(decision.error or "judge synthesis failed")


# Module-level aliases kept for backward compatibility with tests.
RETRYABLE_STATUS_CODES = set(DEFAULT_CONFIG.retry.retryable_status_codes)
NON_RETRYABLE_STATUS_CODES = set(DEFAULT_CONFIG.retry.non_retryable_status_codes)


class Router:
    """Selects provider/model and manages fallback chains with retry logic."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        health_tracker: ProviderHealthTracker,
        max_retries: int = DEFAULT_CONFIG.retry.max_retries,
        base_delay: float = DEFAULT_CONFIG.retry.base_delay,
        cooldown_seconds: int = DEFAULT_CONFIG.retry.cooldown_seconds,
        retry_config: RetryConfig | None = None,
        smart_strategy: SmartRoutingStrategy | None = None,
        ensemble_config: "EnsembleConfig | None" = None,
        cost_tracker: "CostTracker | None" = None,
    ) -> None:
        self.model_registry = model_registry
        self.health_tracker = health_tracker

        # Strategy map: instantiated once, reused across requests
        self._strategies: dict[RoutingStrategy, RoutingStrategyBase] = {
            RoutingStrategy.ROUND_ROBIN: RoundRobinStrategy(),
            RoutingStrategy.WEIGHTED: WeightedStrategy(),
            RoutingStrategy.LEAST_LATENCY: LeastLatencyStrategy(),
            RoutingStrategy.COST_OPTIMIZED: CostOptimizedStrategy(),
        }

        # Smart routing strategy (optional)
        self._smart_strategy = smart_strategy
        if smart_strategy is not None:
            self._strategies[RoutingStrategy.SMART] = smart_strategy

        # Ensemble routing wiring (optional)
        self._ensemble_config = ensemble_config
        self._cost_tracker = cost_tracker

        if retry_config is not None:
            self.max_retries = retry_config.max_retries
            self.base_delay = retry_config.base_delay
            self.cooldown_seconds = retry_config.cooldown_seconds
            self._retryable = retry_config.retryable_status_codes
            self._non_retryable = retry_config.non_retryable_status_codes
        else:
            self.max_retries = max_retries
            self.base_delay = base_delay
            self.cooldown_seconds = cooldown_seconds
            self._retryable = DEFAULT_CONFIG.retry.retryable_status_codes
            self._non_retryable = DEFAULT_CONFIG.retry.non_retryable_status_codes

    def _get_strategy(self, model: str) -> RoutingStrategyBase:
        """Look up the routing strategy for a model."""
        config = self.model_registry.models[model]
        return self._strategies[config.routing_strategy]

    def get_fallback_chain(self, model: str) -> list[ProviderModelMapping]:
        """Return ordered fallback chain for a model."""
        mappings = self.model_registry.resolve(model)
        return sorted(mappings, key=lambda m: m.fallback_order)

    async def smart_route(
        self,
        request: ChatCompletionRequest,
        provider_fn_factory,
        prompt: str,
        allowed_models: set[str] | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[ChatCompletionResponse, SmartRoutingDecision]:
        """Smart routing: select model, then execute with fallback.

        1. Call smart_strategy.select_model() to pick the best model
        2. Update request.model to the selected model
        3. Create provider_fn for the selected model
        4. Call execute_with_fallback() with the selected model
        5. If all providers for that model fail, fall back to the default model
        6. Return response + decision metadata
        """
        if self._smart_strategy is None:
            raise RuntimeError("Smart routing strategy not configured")

        decision = await self._smart_strategy.select_model(
            prompt, allowed_models, project_id, user_id,
        )
        request.model = decision.selected_model
        provider_fn = provider_fn_factory.create(request)
        try:
            response = await self.execute_with_fallback(
                request, provider_fn, allowed_models=allowed_models,
            )
            return response, decision
        except AllProvidersExhaustedError:
            default_model = self._smart_strategy.default_model
            if decision.selected_model == default_model:
                raise
            request.model = default_model
            provider_fn = provider_fn_factory.create(request)
            decision = SmartRoutingDecision(
                task_type=decision.task_type,
                confidence=decision.confidence,
                selected_model=default_model,
                benchmark_score=0.0,
                candidates_considered=decision.candidates_considered,
                used_fallback=True,
                cost_quality_tradeoff=decision.cost_quality_tradeoff,
            )
            response = await self.execute_with_fallback(
                request, provider_fn, allowed_models=allowed_models,
            )
            return response, decision

    # ------------------------------------------------------------------
    # Ensemble helpers
    # ------------------------------------------------------------------

    def _clone_request(
        self,
        request: ChatCompletionRequest,
        model: str | None = None,
        messages: list[dict] | None = None,
        stream: bool = False,
    ) -> ChatCompletionRequest:
        """Return a copy of ``request`` with overridden model/messages/stream.

        Used to build per-panel-member and judge requests from the original
        ensemble request without mutating it. ``stream`` defaults to ``False``
        because panel members are never streamed.
        """
        overrides: dict = {"stream": stream}
        if model is not None:
            overrides["model"] = model
        if messages is not None:
            overrides["messages"] = messages
        return dataclasses.replace(request, **overrides)

    async def _record_member_cost(
        self,
        response: ChatCompletionResponse,
        model: str,
        project_id: str | None,
        user_id: str | None,
    ) -> float:
        """Calculate and record usage for one successful underlying call.

        Mirrors the agent's single-call cost tracking: computes the cost via
        ``calculate_cost`` (using the response provider and token usage) and
        records exactly one :class:`UsageRecord`. Returns the computed cost, or
        ``0.0`` when no cost tracker is configured.
        """
        if self._cost_tracker is None:
            return 0.0

        cost = self._cost_tracker.calculate_cost(
            response.provider,
            response.model,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            cached_tokens=response.usage.cached_tokens,
            cache_creation_tokens=response.usage.cache_creation_tokens,
        )
        usage_record = UsageRecord(
            request_id=response.id,
            project_id=project_id,
            user_id=user_id,
            provider=response.provider,
            model=model,  # model name, not provider's versioned name
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cost=cost,
            timestamp=datetime.datetime.utcnow(),
            cached_tokens=response.usage.cached_tokens,
            cache_creation_tokens=response.usage.cache_creation_tokens,
        )
        await self._cost_tracker.record_usage(usage_record)
        return cost

    def _build_decision(
        self,
        preset: EnsemblePreset,
        results: list[PanelMemberResult],
        cost_multiplier: float,
        panel_cost: float,
    ) -> EnsembleDecision:
        """Partition panel results and populate an :class:`EnsembleDecision`.

        Survivors are results with ``status == "succeeded"``; failures carry a
        ``{"model", "reason"}`` entry. The judge cost is added later by
        ``ensemble_route`` once the judge is invoked.
        """
        survivors = [r for r in results if r.status == "succeeded"]
        failures = [r for r in results if r.status != "succeeded"]
        succeeded_count = len(survivors)
        return EnsembleDecision(
            preset_name=preset.name,
            panel_members=[r.model for r in results],
            judge_model=preset.judge,
            succeeded=[r.model for r in survivors],
            failed=[
                {"model": r.model, "reason": r.failure_reason} for r in failures
            ],
            quorum_met=succeeded_count >= preset.quorum,
            succeeded_count=succeeded_count,
            quorum_threshold=preset.quorum,
            total_cost=panel_cost,
            cost_multiplier=cost_multiplier,
        )

    async def ensemble_route(
        self,
        request: ChatCompletionRequest,
        provider_fn_factory,
        prompt: str,
        preset: EnsemblePreset,
        allowed_models: set[str] | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        per_call_cost_estimate: float = 0.0,
    ) -> tuple[ChatCompletionResponse, EnsembleDecision]:
        """Scatter-gather-synthesize across the preset's panel and judge.

        Steps:
        1. Access control: every panel member + judge must be in allowed_models.
        2. Cost ceiling: estimated (N+1)*per_call must not exceed preset.cost_ceiling.
        3. Scatter: asyncio.gather over panel, each via execute_with_fallback under
           a 60s asyncio.wait_for timeout; member failures tolerated.
        4. Gather: collect survivors, record per-member success/failure + cost.
        5. Quorum: 0 survivors → always error; >= quorum → synthesize; else apply
           fallback_policy (best-single ranked survivor, or error).
        6. Synthesize: build synthesis prompt, call judge via execute_with_fallback.
        7. Return (response, EnsembleDecision).
        """
        n = len(preset.panel)
        cost_multiplier = EnsembleStrategy.estimate_cost_multiplier(n)

        # --- Step 1: Access control over ALL panel + judge models (pre-dispatch) ---
        if allowed_models is not None:
            for model in [*preset.panel, preset.judge]:
                if model not in allowed_models:
                    raise EnsembleAccessError(model)

        # --- Step 2: Cost ceiling (pre-dispatch, no usage recorded) ---
        estimated_cost = (n + 1) * per_call_cost_estimate
        if preset.cost_ceiling is not None and estimated_cost > preset.cost_ceiling:
            raise EnsembleCostCeilingError(estimated_cost, preset.cost_ceiling)

        # --- Step 3: Scatter ---
        async def _dispatch(model: str) -> PanelMemberResult:
            member_req = self._clone_request(request, model=model, stream=False)
            provider_fn = provider_fn_factory.create(member_req)
            start = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    self.execute_with_fallback(
                        member_req, provider_fn, allowed_models=allowed_models,
                    ),
                    timeout=EnsembleStrategy.PER_MEMBER_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return PanelMemberResult(
                    model, "failed", failure_reason="timeout (60s)"
                )
            except AllProvidersExhaustedError as exc:
                return PanelMemberResult(model, "failed", failure_reason=str(exc))
            cost = await self._record_member_cost(
                resp, request.model, project_id, user_id
            )
            return PanelMemberResult(
                model,
                "succeeded",
                response=resp,
                cost=cost,
                latency_ms=(time.monotonic() - start) * 1000,
            )

        results = await asyncio.gather(*[_dispatch(m) for m in preset.panel])

        # --- Step 4: Gather ---
        survivors = [r for r in results if r.status == "succeeded"]
        panel_cost = sum(r.cost for r in survivors)
        decision = self._build_decision(preset, results, cost_multiplier, panel_cost)

        # --- Step 5: Quorum ---
        if len(survivors) == 0:
            decision.error = "no panel responses available"
            raise EnsembleNoSurvivorsError(decision)
        if not EnsembleStrategy.evaluate_quorum(len(survivors), preset.quorum):
            if preset.fallback_policy == "best-single":
                best = EnsembleStrategy.rank_survivors(
                    survivors, preset.ranking_criteria
                )[0]
                decision.fallback_used = True
                return best.response, decision
            decision.error = f"quorum not met: {len(survivors)} < {preset.quorum}"
            raise EnsembleQuorumError(decision)

        # --- Step 6: Synthesize (survivors only) ---
        synth_messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, prompt, preset.ranking_criteria,
        )
        judge_req = self._clone_request(
            request, model=preset.judge, messages=synth_messages,
        )
        provider_fn = provider_fn_factory.create(judge_req)
        try:
            final = await self.execute_with_fallback(
                judge_req, provider_fn, allowed_models=allowed_models,
            )
        except AllProvidersExhaustedError as exc:
            decision.error = f"judge synthesis failed: {exc}"
            raise EnsembleSynthesisError(decision)

        judge_cost = await self._record_member_cost(
            final, preset.judge, project_id, user_id
        )
        decision.judge_invoked = True
        decision.total_cost = panel_cost + judge_cost
        return final, decision

    async def execute_with_fallback(
        self,
        request: ChatCompletionRequest,
        provider_fn: Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]],
        preferred_provider: str | None = None,
        allowed_models: set[str] | None = None,
    ) -> ChatCompletionResponse:
        """Execute request with strategy-based initial selection, retry, and fallback.

        If preferred_provider is set, skip strategy and use that provider directly.
        If allowed_models is set, filter out providers for models not in the set.
        """
        mappings = self.model_registry.resolve(request.model)

        # Filter by allowed models when the access list is provided
        if allowed_models is not None and request.model not in allowed_models:
            raise AllProvidersExhaustedError([{
                "provider": "none",
                "status_code": 403,
                "message": f"Model '{request.model}' is not in the allowed models list",
            }])

        attempts: list[dict] = []

        if preferred_provider:
            # Find the mapping for the preferred provider
            preferred = [m for m in mappings if m.provider == preferred_provider]
            if preferred:
                result = await self._try_provider(preferred[0], provider_fn, attempts)
                if result is not None:
                    return result
                # Preferred failed — fall through to remaining providers
                remaining = [m for m in mappings if m.provider != preferred_provider]
            else:
                remaining = list(mappings)

            for mapping in sorted(remaining, key=lambda m: m.fallback_order):
                if not self.health_tracker.is_healthy(mapping.provider):
                    attempts.append({"provider": mapping.provider, "status_code": 0, "message": "skipped (unhealthy)"})
                    continue
                result = await self._try_provider(mapping, provider_fn, attempts)
                if result is not None:
                    return result
            raise AllProvidersExhaustedError(attempts)

        # --- Step 1: Use strategy to pick the initial provider ---
        try:
            strategy = self._get_strategy(request.model)
            initial = strategy.select(mappings, self.health_tracker)
        except NoHealthyProviderError:
            # All providers unhealthy — build attempts from all mappings
            for m in sorted(mappings, key=lambda m: m.fallback_order):
                attempts.append(
                    {
                        "provider": m.provider,
                        "status_code": 0,
                        "message": "skipped (unhealthy)",
                    }
                )
            raise AllProvidersExhaustedError(attempts)

        # --- Step 2: Try the initial provider with retry logic ---
        result = await self._try_provider(initial, provider_fn, attempts)
        if result is not None:
            return result

        # --- Step 3: Build fallback chain from remaining providers ---
        fallback = sorted(
            [m for m in mappings if m.provider != initial.provider],
            key=lambda m: m.fallback_order,
        )

        for mapping in fallback:
            if not self.health_tracker.is_healthy(mapping.provider):
                attempts.append(
                    {
                        "provider": mapping.provider,
                        "status_code": 0,
                        "message": "skipped (unhealthy)",
                    }
                )
                continue

            result = await self._try_provider(mapping, provider_fn, attempts)
            if result is not None:
                return result

        raise AllProvidersExhaustedError(attempts)

    async def _try_provider(
        self,
        mapping: ProviderModelMapping,
        provider_fn: Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]],
        attempts: list[dict],
    ) -> ChatCompletionResponse | None:
        """Try a single provider with retry logic. Returns response or None on failure."""
        last_error: ProviderError | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return await provider_fn(mapping)
            except ProviderError as exc:
                last_error = exc

                if exc.status_code in self._non_retryable:
                    attempts.append(
                        {
                            "provider": mapping.provider,
                            "status_code": exc.status_code,
                            "message": exc.message,
                        }
                    )
                    return None

                if exc.status_code in self._retryable:
                    if attempt < self.max_retries:
                        delay = self.base_delay * (2 ** attempt)
                        await asyncio.sleep(delay)
                else:
                    attempts.append(
                        {
                            "provider": mapping.provider,
                            "status_code": exc.status_code,
                            "message": exc.message,
                        }
                    )
                    return None
        else:
            if last_error is not None:
                self.health_tracker.mark_unhealthy(
                    mapping.provider, self.cooldown_seconds
                )
                attempts.append(
                    {
                        "provider": mapping.provider,
                        "status_code": last_error.status_code,
                        "message": last_error.message,
                    }
                )

        return None
