"""Gateway Agent entrypoint — orchestrates the full chat completion request flow."""

from __future__ import annotations

import time
import warnings
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator

from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    BudgetStatus,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EnsemblePreset,
    Project,
    RequestContext,
    ResolvedPolicy,
    StreamChunk,
    UsageRecord,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.request_validator import RequestValidator
from src.gateway.router import (
    AllProvidersExhaustedError,
    EnsembleAccessError,
    EnsembleCostCeilingError,
    EnsembleNoSurvivorsError,
    EnsembleQuorumError,
    EnsembleSynthesisError,
    Router,
)
from src.gateway.session_manager import SessionManager
from src.gateway.smart_routing import NoCandidateModelsError
from src.gateway.streaming import simulate_streaming

if TYPE_CHECKING:
    from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
    from src.gateway.multi_region.region_router import RegionRouter
    from src.gateway.provider_fn_factory import ProviderFnFactory
    from src.gateway.quota_enforcer import QuotaEnforcer
    from src.gateway.observability.trace_forwarder import TraceForwarder
    from src.gateway.security.audit_trail import AuditTrail
    from src.gateway.security.event_dispatcher import EventDispatcher
    from src.gateway.security.injection_detector import PromptInjectionDetector
    from src.gateway.security.pii_redactor import PIIRedactor, RedactionMapping


# ---------------------------------------------------------------------------
# Stub for BedrockAgentCoreApp (since we can't import the real SDK)
# ---------------------------------------------------------------------------


class BedrockAgentCoreApp:
    """Stub that mirrors the real BedrockAgentCoreApp decorator API."""

    def __init__(self) -> None:
        self._entrypoints: dict[str, Any] = {}

    def entrypoint(self, name: str):
        """Decorator that registers an async function as a named entrypoint."""

        def decorator(fn):
            self._entrypoints[name] = fn
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


class GatewayError(Exception):
    """Structured error raised during request processing."""

    def __init__(self, status_code: int, error_type: str, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "error": {
                "type": self.error_type,
                "message": self.message,
            }
        }
        if self.code:
            d["error"]["code"] = self.code
        return d


def _error_response(status_code: int, error_type: str, message: str, code: str | None = None) -> dict:
    """Build a JSON-style error dict matching the spec error format."""
    d: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
        },
        "status_code": status_code,
    }
    if code:
        d["error"]["code"] = code
    return d


# ---------------------------------------------------------------------------
# GatewayAgent — orchestration class
# ---------------------------------------------------------------------------


class GatewayAgent:
    """Orchestrates the full chat completion request flow.

    All dependencies are injected via the constructor for testability.
    """

    def __init__(
        self,
        router: Router,
        rate_limiter: SlidingWindowRateLimiter,
        guardrail_engine: GuardrailEngine,
        cache_manager: CacheManager,
        cost_tracker: CostTracker,
        session_manager: SessionManager | None = None,
        projects: dict[str, Project] | None = None,
        provider_fn_factory: ProviderFnFactory | None = None,
        user_configs: dict[str, dict] | None = None,
        request_validator: RequestValidator | None = None,
        smart_routing_enabled: bool = False,
        quota_enforcer: QuotaEnforcer | None = None,
        policy_resolver: PolicyHierarchyResolver | None = None,
        pii_redactor: PIIRedactor | None = None,
        injection_detector: PromptInjectionDetector | None = None,
        audit_trail: AuditTrail | None = None,
        event_dispatcher: EventDispatcher | None = None,
        region_router: RegionRouter | None = None,
        trace_forwarder: TraceForwarder | None = None,
    ) -> None:
        self.router = router
        self.rate_limiter = rate_limiter
        self.guardrail_engine = guardrail_engine
        self.cache_manager = cache_manager
        self.cost_tracker = cost_tracker
        self.session_manager = session_manager
        self._projects: dict[str, Project] = projects or {}
        self.provider_fn_factory = provider_fn_factory
        self._user_configs: dict[str, dict] = user_configs or {}
        self.request_validator = request_validator
        self._smart_routing_enabled = smart_routing_enabled
        self._quota_enforcer = quota_enforcer
        self._policy_resolver = policy_resolver
        self._pii_redactor = pii_redactor
        self._injection_detector = injection_detector
        self._audit_trail = audit_trail
        self._event_dispatcher = event_dispatcher
        self._region_router = region_router
        self._trace_forwarder = trace_forwarder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_chat_completion(
        self,
        request_data: dict,
        context: dict,
    ) -> dict | AsyncIterator[dict]:
        """Main entrypoint that orchestrates the full request flow.

        Returns either a response dict (non-streaming) or an async generator
        of SSE-formatted dicts (streaming).
        """
        # 1. Parse request
        request = self._parse_request(request_data)

        # 2. Extract context
        req_ctx = self._extract_context(context)
        project = self._projects.get(req_ctx.project_id)

        # 2.5. Request validation (before rate limiting)
        # Skip model validation for smart routing (model will be selected later)
        is_smart_routing = self._is_smart_routing_request(request, context)
        is_ensemble, ensemble_preset_name, ensemble_err = self._is_ensemble_request(request, context)
        # Ensemble requests defer concrete model selection to the preset, so skip
        # single-model validation/access checks exactly as smart routing does.
        skip_model_checks = is_smart_routing or is_ensemble
        if self.request_validator is not None and not skip_model_checks:
            validation_errors = self.request_validator.validate(request)
            if validation_errors:
                first_error = validation_errors[0]
                # Determine status code and error code based on error type
                if first_error.field == "model":
                    return _error_response(
                        404, "not_found", first_error.message, code="model_not_found"
                    )
                elif "role" in first_error.field and "missing" not in first_error.message.lower():
                    return _error_response(
                        400, "invalid_request", first_error.message, code="invalid_role"
                    )
                elif "missing" in first_error.message.lower():
                    return _error_response(
                        400, "invalid_request", first_error.message, code="invalid_message_format"
                    )
                elif first_error.field == "messages" and "token" in first_error.message.lower():
                    return _error_response(
                        400, "invalid_request", first_error.message, code="token_limit_exceeded"
                    )
                else:
                    return _error_response(
                        400, "invalid_request", first_error.message, code="invalid_message_format"
                    )

        # 2.7. Policy-hierarchy quota enforcement
        resolved_policy = None
        if self._quota_enforcer is not None and self._policy_resolver is not None:
            resolved_policy = await self._policy_resolver.resolve(req_ctx.project_id)
            estimated_cost = self._estimate_request_cost(request)
            quota_decision = await self._quota_enforcer.enforce_all(
                project_id=req_ctx.project_id,
                model=request.model or "",
                provider=context.get("provider"),
                max_tokens=request.max_tokens,
                estimated_cost=estimated_cost,
                policy=resolved_policy,
            )
            if not quota_decision.allowed:
                return _error_response(
                    429,
                    "quota_exceeded",
                    quota_decision.reason,
                    code=f"quota_{quota_decision.limit_type}",
                )

            # Apply the policy's max_tokens ceiling. This also bounds requests
            # that omit max_tokens entirely — otherwise an unbounded (streaming)
            # response could exhaust resources or amplify cost on shared
            # provider credentials.
            request.max_tokens = self._quota_enforcer.cap_max_tokens(
                request.max_tokens, resolved_policy
            )

        # 2.8. Prompt injection detection
        request_id = f"req_{__import__('uuid').uuid4().hex[:12]}"
        pii_mapping = None

        if self._injection_detector is not None:
            injection_result = self._injection_detector.analyze_messages(request.messages or [])
            if injection_result.score > 0:
                if self._audit_trail is not None:
                    await self._audit_trail.record_injection_event(
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        request_id=request_id,
                        threat_level=injection_result.threat_level.value,
                        patterns=injection_result.detected_patterns,
                        blocked=injection_result.should_block,
                    )
                if self._event_dispatcher is not None:
                    await self._event_dispatcher.dispatch_injection_event(
                        event_id=request_id,
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        threat_level=injection_result.threat_level.value,
                        patterns=injection_result.detected_patterns,
                        blocked=injection_result.should_block,
                    )
                if injection_result.should_block:
                    return _error_response(
                        400,
                        "content_policy_violation",
                        f"Request blocked: prompt injection detected "
                        f"(threat_level={injection_result.threat_level.value}, "
                        f"score={injection_result.score:.2f})",
                        code="injection_blocked",
                    )

        # 2.9. PII redaction
        if self._pii_redactor is not None:
            effective_policy = resolved_policy or ResolvedPolicy()
            redacted_messages, pii_mapping = self._pii_redactor.redact_messages(
                request.messages or [], effective_policy
            )
            if pii_mapping.redacted_count > 0:
                request = ChatCompletionRequest(
                    messages=redacted_messages,
                    model=request.model,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    top_p=request.top_p,
                    stop=request.stop,
                    stream=request.stream,
                    system=request.system,
                )
                if self._audit_trail is not None:
                    redacted_types = list(pii_mapping._counters.keys())
                    await self._audit_trail.record_pii_redaction(
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        request_id=request_id,
                        redacted_types=redacted_types,
                        count=pii_mapping.redacted_count,
                    )
                if self._event_dispatcher is not None:
                    await self._event_dispatcher.dispatch_pii_event(
                        event_id=request_id,
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        redacted_types=list(pii_mapping._counters.keys()),
                        count=pii_mapping.redacted_count,
                    )

        # 3. Rate limit check
        rate_result = await self.rate_limiter.check_rate_limit(
            req_ctx.user_id, req_ctx.project_id
        )

        # Build rate limit headers from the result
        _rate_limit_headers = {
            "X-RateLimit-Limit": str(rate_result.limit),
            "X-RateLimit-Remaining": str(rate_result.remaining),
            "X-RateLimit-Reset": str(int(rate_result.reset_at.timestamp())),
        }

        if not rate_result.allowed:
            if rate_result.retry_after_seconds is not None:
                _rate_limit_headers["Retry-After"] = str(rate_result.retry_after_seconds)
            resp = _error_response(
                429,
                "rate_limit_error",
                f"Rate limit exceeded. Retry after {rate_result.retry_after_seconds}s.",
                code="rate_limit_exceeded",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 4. Project model access check (skip for smart routing — model will be selected later)
        if not skip_model_checks and project and project.allowed_models and request.model not in project.allowed_models:
            resp = _error_response(
                403,
                "forbidden",
                f"Model '{request.model}' is not allowed for project '{req_ctx.project_id}'.",
                code="model_not_allowed",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 5. User model access check (skip for smart routing)
        user_config = self._user_configs.get(req_ctx.user_id, {})
        user_allowed = user_config.get("allowed_models")
        if not skip_model_checks and user_allowed and request.model not in user_allowed:
            resp = _error_response(
                403,
                "forbidden",
                f"Model '{request.model}' is not allowed for user '{req_ctx.user_id}'.",
                code="model_not_allowed",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 6. Project budget check
        if project:
            budget_status = await self.cost_tracker.check_budget(req_ctx.project_id)
            if budget_status.is_over_budget:
                resp = _error_response(
                    429,
                    "budget_exceeded",
                    f"Project '{req_ctx.project_id}' has exceeded its budget.",
                    code="budget_exceeded",
                )
                resp["_rate_limit_headers"] = _rate_limit_headers
                return resp

        # 7. User budget check
        user_budget = await self.cost_tracker.check_user_budget(req_ctx.user_id)
        if user_budget.is_over_budget:
            resp = _error_response(
                429,
                "budget_exceeded",
                f"User '{req_ctx.user_id}' has exceeded their budget.",
                code="budget_exceeded",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 8. Request guardrails
        if project and project.guardrail_rules:
            guard_result = self.guardrail_engine.evaluate_request(
                request, project.guardrail_rules
            )
            if not guard_result.passed:
                resp = _error_response(
                    400,
                    "content_policy_violation",
                    guard_result.message or "Request blocked by guardrail.",
                    code="guardrail_violation",
                )
                resp["_rate_limit_headers"] = _rate_limit_headers
                return resp

        # 9. Cache check
        if project and project.cache_enabled:
            cache_key = self.cache_manager.compute_cache_key(request, req_ctx.project_id)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                result = self._response_to_dict(cached, is_cached=True)
                result["_rate_limit_headers"] = _rate_limit_headers
                return result

        # 9.5. Region routing — check spoke availability and data residency
        region_decision = None
        if self._region_router is not None:
            data_zone = context.get("data_residency_zone")
            region_decision = self._region_router.route(
                model=request.model or None,
                data_residency_zone=data_zone,
                preferred_region=context.get("preferred_region"),
            )
            if region_decision is None:
                resp = _error_response(
                    503,
                    "service_unavailable",
                    "No available region for this request"
                    + (f" (data_residency_zone={data_zone})" if data_zone else ""),
                    code="no_available_region",
                )
                resp["_rate_limit_headers"] = _rate_limit_headers
                return resp

        # 10. Route and execute
        _request_start = time.perf_counter()
        try:
            prompt_caching_enabled = project.prompt_caching_enabled if project else False

            if self.provider_fn_factory is not None:
                provider_fn = self.provider_fn_factory.create(
                    request, prompt_caching_enabled=prompt_caching_enabled,
                )
            else:
                provider_fn = self._make_provider_fn()

            # Compute effective allowed models from project + user access lists
            effective_allowed = self._compute_effective_allowed_models(project, req_ctx.user_id)

            # Extract prompt from last user message (shared by smart + ensemble)
            prompt = ""
            if request.messages:
                last_msg = request.messages[-1]
                prompt = last_msg.get("content", "") if isinstance(last_msg, dict) else ""

            # Check if smart routing / ensemble routing should be used
            smart_routing_decision = None
            ensemble_decision = None
            ensemble_unavailable = False

            ensemble_config = getattr(self.router, "_ensemble_config", None)
            take_ensemble_path = is_ensemble and ensemble_config is not None and ensemble_config.is_configured

            if is_ensemble and not take_ensemble_path:
                # Backward-compat: detected as ensemble but not configured →
                # fall through to the normal/smart path and note unavailability.
                ensemble_unavailable = True

            if take_ensemble_path:
                # Malformed invocation (e.g. "ensemble:" with empty name).
                if ensemble_err:
                    resp = _error_response(
                        400, "invalid_request", ensemble_err,
                        code="ensemble_preset_missing",
                    )
                    resp["_rate_limit_headers"] = _rate_limit_headers
                    return resp

                # Resolve preset (named or default).
                if ensemble_preset_name is not None:
                    preset = ensemble_config.get_preset(ensemble_preset_name)
                    if preset is None:
                        resp = _error_response(
                            404, "not_found",
                            f"Ensemble preset '{ensemble_preset_name}' not found",
                            code="ensemble_preset_not_found",
                        )
                        resp["_rate_limit_headers"] = _rate_limit_headers
                        return resp
                else:
                    preset = ensemble_config.default_preset()
                    if preset is None:
                        resp = _error_response(
                            400, "invalid_request",
                            "No default ensemble preset configured",
                            code="ensemble_no_default",
                        )
                        resp["_rate_limit_headers"] = _rate_limit_headers
                        return resp

                # Budget pre-check on the estimated (N+1) * per_call cost
                # before any dispatch.
                per_call = self._estimate_per_call_cost(request, preset)
                estimated = (len(preset.panel) + 1) * per_call
                if estimated > 0:
                    if project:
                        proj_budget = await self.cost_tracker.check_budget(req_ctx.project_id)
                        if (
                            proj_budget.budget_limit is not None
                            and proj_budget.current_spend + estimated > proj_budget.budget_limit
                        ):
                            resp = _error_response(
                                429, "budget_exceeded",
                                f"Estimated ensemble cost {estimated:.4f} would exceed "
                                f"project '{req_ctx.project_id}' budget.",
                                code="budget_exceeded",
                            )
                            resp["_rate_limit_headers"] = _rate_limit_headers
                            return resp
                    usr_budget = await self.cost_tracker.check_user_budget(req_ctx.user_id)
                    if (
                        usr_budget.budget_limit is not None
                        and usr_budget.current_spend + estimated > usr_budget.budget_limit
                    ):
                        resp = _error_response(
                            429, "budget_exceeded",
                            f"Estimated ensemble cost {estimated:.4f} would exceed "
                            f"user '{req_ctx.user_id}' budget.",
                            code="budget_exceeded",
                        )
                        resp["_rate_limit_headers"] = _rate_limit_headers
                        return resp

                # Streaming ensemble request: defer to the streaming
                # generator instead of running the non-streaming path. The
                # panel phase still runs to completion inside the generator
                # (output is withheld until the judge / best-single result is
                # ready), so nothing is streamed during panel dispatch.
                if request.stream:
                    return self._stream_ensemble_response(
                        request,
                        prompt,
                        preset,
                        effective_allowed,
                        req_ctx.project_id,
                        req_ctx.user_id,
                        per_call,
                        _rate_limit_headers,
                    )

                response, ensemble_decision = await self.router.ensemble_route(
                    request,
                    self.provider_fn_factory,
                    prompt,
                    preset,
                    allowed_models=effective_allowed,
                    project_id=req_ctx.project_id,
                    user_id=req_ctx.user_id,
                    per_call_cost_estimate=per_call,
                )
            elif self._is_smart_routing_request(request, context):
                response, smart_routing_decision = await self.router.smart_route(
                    request,
                    self.provider_fn_factory,
                    prompt,
                    allowed_models=effective_allowed,
                    project_id=req_ctx.project_id,
                    user_id=req_ctx.user_id,
                )
            else:
                response = await self.router.execute_with_fallback(
                    request, provider_fn,
                    preferred_provider=context.get("provider"),
                    allowed_models=effective_allowed,
                )
        except EnsembleAccessError as exc:
            resp = _error_response(
                403, "forbidden",
                f"Model '{exc.model}' is not allowed",
                code="model_not_allowed",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleCostCeilingError as exc:
            resp = _error_response(
                400, "invalid_request", str(exc),
                code="ensemble_cost_ceiling",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleNoSurvivorsError as exc:
            resp = _error_response(
                502, "provider_error",
                str(exc),
                code="ensemble_no_survivors",
            )
            resp["ensemble"] = self._ensemble_metadata(exc.decision)
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleQuorumError as exc:
            resp = _error_response(
                502, "provider_error",
                str(exc),
                code="ensemble_quorum_not_met",
            )
            resp["ensemble"] = self._ensemble_metadata(exc.decision)
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleSynthesisError as exc:
            resp = _error_response(
                502, "provider_error",
                str(exc),
                code="ensemble_synthesis_failed",
            )
            resp["ensemble"] = self._ensemble_metadata(exc.decision)
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except AllProvidersExhaustedError as exc:
            resp = _error_response(
                502,
                "provider_error",
                str(exc),
                code="all_providers_exhausted",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except NoCandidateModelsError as exc:
            resp = _error_response(
                502,
                "provider_error",
                str(exc),
                code="no_candidate_models",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 11. Response guardrails
        if project and project.guardrail_rules:
            resp_guard = self.guardrail_engine.evaluate_response(
                response, project.guardrail_rules
            )
            if not resp_guard.passed:
                response = self._replace_response_content(
                    response,
                    resp_guard.message or "Response blocked by guardrail.",
                )

        # 11.5. PII re-injection into response
        if pii_mapping is not None and pii_mapping.redacted_count > 0 and self._pii_redactor is not None:
            for i, choice in enumerate(response.choices):
                msg = choice.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    reinjected = self._pii_redactor.reinject_response(content, pii_mapping)
                    response.choices[i] = {
                        **choice,
                        "message": {**msg, "content": reinjected},
                    }

        # 11.6. Audit trail — record LLM request
        if self._audit_trail is not None:
            await self._audit_trail.record_llm_request(
                user_id=req_ctx.user_id,
                project_id=req_ctx.project_id,
                request_id=request_id,
                model=response.model,
                provider=response.provider,
                message_count=len(request.messages or []),
                pii_redacted_count=pii_mapping.redacted_count if pii_mapping else 0,
                injection_score=0.0,
            )

        # 12. Cost tracking
        # Ensemble routing records per-call usage internally via the router's
        # cost tracker; skip the normal post-response recording to avoid
        # double-counting.
        if ensemble_decision is None:
            cost = self.cost_tracker.calculate_cost(
                response.provider,
                response.model,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                cached_tokens=response.usage.cached_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
            )
            request_id = response.id
            _latency_ms = (time.perf_counter() - _request_start) * 1000
            usage_record = UsageRecord(
                request_id=request_id,
                project_id=req_ctx.project_id,
                user_id=req_ctx.user_id,
                provider=response.provider,
                model=request.model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cost=cost,
                timestamp=datetime.now(timezone.utc),
                cached_tokens=response.usage.cached_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
                latency_ms=_latency_ms,
                status="success",
            )
            await self.cost_tracker.record_usage(usage_record)

            # Forward the trace to an embedding Ostiari (best-effort; never blocks
            # or fails the request). forward() swallows and logs its own errors,
            # and is a no-op when Ostiari isn't detected.
            if self._trace_forwarder is not None:
                await self._trace_forwarder.forward(usage_record)

            # Record spend in quota enforcer for hierarchy budget tracking
            if self._quota_enforcer is not None:
                budget_limit = resolved_policy.budget_limit if resolved_policy else None
                await self._quota_enforcer.record_spend(req_ctx.project_id, cost, budget_limit=budget_limit)

        # 13. Budget status for streaming (already enforced pre-request)
        budget_status: BudgetStatus | None = None

        # 14. Session storage
        session_id = context.get("session_id")
        if session_id and self.session_manager:
            await self.session_manager.store_exchange(session_id, request, response)

        # 15. Streaming support
        if request.stream:
            if self.provider_fn_factory is not None and response.provider != "google_ai":
                return self._stream_response_real(
                    request, response, budget_status, _rate_limit_headers,
                    prompt_caching_enabled=prompt_caching_enabled,
                    pii_mapping=pii_mapping,
                )
            return self._stream_response(response, budget_status, _rate_limit_headers, pii_mapping=pii_mapping)

        # 16. Non-streaming return
        result = self._response_to_dict(response)
        if smart_routing_decision is not None:
            result["smart_routing"] = {
                "task_type": smart_routing_decision.task_type,
                "confidence": smart_routing_decision.confidence,
                "selected_model": smart_routing_decision.selected_model,
                "benchmark_score": smart_routing_decision.benchmark_score,
                "candidates": smart_routing_decision.candidates_considered,
                "used_fallback": smart_routing_decision.used_fallback,
                "cost_quality_tradeoff": smart_routing_decision.cost_quality_tradeoff,
            }
        if ensemble_decision is not None:
            result["ensemble"] = self._ensemble_metadata(ensemble_decision)
        if ensemble_unavailable:
            result["ensemble_unavailable"] = True
        if region_decision is not None:
            result["region"] = {
                "spoke": region_decision.target_spoke.region,
                "reason": region_decision.reason,
            }
        result["_rate_limit_headers"] = _rate_limit_headers
        return result

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream_response(
        self,
        response: ChatCompletionResponse,
        budget_status: BudgetStatus | None = None,
        rate_limit_headers: dict[str, str] | None = None,
        pii_mapping: RedactionMapping | None = None,
    ) -> AsyncIterator[dict]:
        """Async generator that yields SSE-formatted chunks.

        Uses simulated streaming (chunking the complete response) since the
        provider call already returned a full response.
        """
        # Yield rate limit headers as first metadata chunk if present
        if rate_limit_headers:
            yield {"_rate_limit_headers": rate_limit_headers}
        try:
            pii_buffer: dict = {"pending": ""}
            chunks = simulate_streaming(response)
            for chunk in chunks:
                chunk_dict = self._chunk_to_dict(chunk)
                if pii_mapping and pii_mapping.redacted_count > 0:
                    chunk_dict = self._reinject_chunk_pii(chunk_dict, pii_mapping, pii_buffer)
                yield {"data": chunk_dict}
        except Exception as exc:
            # On error during streaming, yield error event then DONE
            yield {"data": {"error": {"type": "stream_error", "message": str(exc)}}}
        finally:
            done_data: dict[str, Any] = {"data": "[DONE]"}
            if budget_status and budget_status.is_over_budget:
                done_data["budget_exceeded"] = True
            yield done_data

    async def _stream_ensemble_response(
        self,
        request: ChatCompletionRequest,
        prompt: str,
        preset: EnsemblePreset,
        effective_allowed: list[str] | None,
        project_id: str,
        user_id: str,
        per_call: float,
        rate_limit_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[dict]:
        """Async generator for streaming ensemble requests.

        The panel phase cannot be streamed (Req 10.1): all output is withheld
        until ``ensemble_route`` completes the scatter-gather-synthesize flow
        and returns the final response. ``ensemble_route`` already produces the
        judge's synthesized output when quorum is met, or the highest-ranked
        survivor when the best-single fallback applies, so we stream only that
        final response (Req 10.2, 10.3). When the quorum is not met under an
        error policy (or there are zero survivors), the stream terminates with
        an error chunk and no synthesized content (Req 10.4).
        """
        # Yield rate limit headers as first metadata chunk if present.
        if rate_limit_headers:
            yield {"_rate_limit_headers": rate_limit_headers}

        try:
            response, _decision = await self.router.ensemble_route(
                request,
                self.provider_fn_factory,
                prompt,
                preset,
                allowed_models=effective_allowed,
                project_id=project_id,
                user_id=user_id,
                per_call_cost_estimate=per_call,
            )
        except (
            EnsembleAccessError,
            EnsembleCostCeilingError,
            EnsembleNoSurvivorsError,
            EnsembleQuorumError,
            EnsembleSynthesisError,
        ) as exc:
            # Pre-dispatch rejection (access / cost ceiling), below quorum
            # under error policy, 0 survivors, or judge failure: terminate the
            # stream with an error chunk and no synthesized content (Req 10.4).
            yield {"data": {"error": {"type": "ensemble_error", "message": str(exc)}}}
            yield {"data": "[DONE]"}
            return

        # Quorum met (judge output) or best-single survivor: stream only the
        # final response incrementally via simulated streaming. Panel/survivor
        # responses are never streamed (Req 10.2, 10.3).
        try:
            chunks = simulate_streaming(response)
            for chunk in chunks:
                yield {"data": self._chunk_to_dict(chunk)}
        except Exception as exc:
            yield {"data": {"error": {"type": "stream_error", "message": str(exc)}}}
        finally:
            yield {"data": "[DONE]"}

    async def _stream_response_real(
        self,
        request: ChatCompletionRequest,
        response: ChatCompletionResponse,
        budget_status: BudgetStatus | None = None,
        rate_limit_headers: dict[str, str] | None = None,
        prompt_caching_enabled: bool = False,
        pii_mapping: RedactionMapping | None = None,
    ) -> AsyncIterator[dict]:
        """Async generator that yields real SSE chunks from the provider.

        Uses ``ProviderFnFactory``'s underlying ``HttpClient.execute_streaming``
        to obtain a real SSE stream from the provider, rather than simulating
        streaming from a complete response.

        Falls back to simulated streaming if the real SSE call fails.
        """
        assert self.provider_fn_factory is not None  # caller guarantees this

        factory = self.provider_fn_factory
        adapter = factory._adapter_registry.get(response.provider)
        config = factory._provider_configs.get(response.provider)

        if adapter is None or config is None:
            # Fall back to simulated streaming if we can't resolve provider info
            async for chunk_dict in self._stream_response(response, budget_status, rate_limit_headers, pii_mapping=pii_mapping):
                yield chunk_dict
            return

        # Resolve the mapping from the router's fallback chain
        chain = self.router.get_fallback_chain(request.model)
        mapping = None
        for m in chain:
            if m.provider == response.provider:
                mapping = m
                break

        if mapping is None:
            async for chunk_dict in self._stream_response(response, budget_status, rate_limit_headers, pii_mapping=pii_mapping):
                yield chunk_dict
            return

        # Yield rate limit headers as first metadata chunk if present
        if rate_limit_headers:
            yield {"_rate_limit_headers": rate_limit_headers}

        try:
            pii_buffer: dict = {"pending": ""}
            stream = factory._http_client.execute_streaming(
                request, mapping, adapter, config,
                prompt_caching_enabled=prompt_caching_enabled,
            )
            async for chunk in stream:
                chunk_dict = self._chunk_to_dict(chunk)
                if pii_mapping and pii_mapping.redacted_count > 0:
                    chunk_dict = self._reinject_chunk_pii(chunk_dict, pii_mapping, pii_buffer)
                yield {"data": chunk_dict}
                if chunk.is_final:
                    break
        except Exception as exc:
            yield {"data": {"error": {"type": "stream_error", "message": str(exc)}}}
        finally:
            done_data: dict[str, Any] = {"data": "[DONE]"}
            if budget_status and budget_status.is_over_budget:
                done_data["budget_exceeded"] = True
            yield done_data

    # ------------------------------------------------------------------
    # Smart Routing
    # ------------------------------------------------------------------

    def _is_smart_routing_request(self, request: ChatCompletionRequest, context: dict) -> bool:
        """Detect when smart routing should be used.

        Returns True when:
        - The context has "smart_routing": True flag (from Routing Explorer), OR
        - The request model is empty/not specified (auto-select mode)

        Also requires that smart routing is actually configured on the router.
        """
        if not hasattr(self.router, '_smart_strategy') or self.router._smart_strategy is None:
            return False
        if context.get("smart_routing") is True:
            return True
        if not request.model or request.model.strip() == "":
            return True
        return False

    def _is_ensemble_request(
        self, request: ChatCompletionRequest, context: dict
    ) -> tuple[bool, str | None, str | None]:
        """Detect when ensemble routing should be used.

        Returns ``(is_ensemble, preset_name, error)`` where:
        - ``is_ensemble`` is True when the request targets ensemble routing
        - ``preset_name`` is the named preset, or None for the default preset
        - ``error`` describes a malformed invocation (e.g. missing preset name)

        Triggers on ``model == "ensemble"`` (default preset),
        ``model == "ensemble:<name>"`` (named preset), and
        ``context["ensemble"] is True`` (default preset). The model value takes
        precedence over the context flag. Returns ``(False, None, None)`` when
        ensemble routing is not configured on the router.
        """
        if getattr(self.router, "_ensemble_config", None) is None:
            return (False, None, None)
        model = request.model or ""
        if model == "ensemble":
            return (True, None, None)            # default preset
        if model.startswith("ensemble:"):
            name = model[len("ensemble:"):]
            if name == "":
                return (True, None, "missing preset name")
            return (True, name, None)
        if context.get("ensemble") is True:
            return (True, None, None)
        return (False, None, None)

    def _estimate_per_call_cost(
        self, request: ChatCompletionRequest, preset: EnsemblePreset
    ) -> float:
        """Conservative per-call cost estimate for the ensemble budget pre-check.

        Estimates a single underlying call's cost from the request's prompt size
        and a nominal completion-token count, priced using the judge model's
        resolved provider pricing. The caller multiplies this by ``(N + 1)`` to
        bound the total ensemble cost before any dispatch.

        Returns ``0.0`` when pricing cannot be resolved, in which case the
        budget pre-check becomes a no-op beyond the existing budget checks.
        """
        # Estimate prompt tokens from message content length (~4 chars/token).
        prompt_chars = 0
        for msg in request.messages or []:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_chars += len(content)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        # Nominal completion budget for the estimate.
        estimated_completion_tokens = request.max_tokens or 256

        # Price using the judge model's resolved provider, if available.
        chain = self.router.get_fallback_chain(preset.judge)
        if not chain:
            return 0.0
        mapping = chain[0]
        try:
            return self.cost_tracker.calculate_cost(
                mapping.provider,
                mapping.model_id,
                estimated_prompt_tokens,
                estimated_completion_tokens,
            )
        except Exception:
            return 0.0

    @staticmethod
    def _ensemble_metadata(decision) -> dict:
        """Build the ``ensemble`` metadata block from an ``EnsembleDecision``."""
        return {
            "preset": decision.preset_name,
            "panel": decision.panel_members,
            "judge": decision.judge_model,
            "succeeded": decision.succeeded,
            "failed": decision.failed,
            "quorum_met": decision.quorum_met,
            "succeeded_count": decision.succeeded_count,
            "quorum_threshold": decision.quorum_threshold,
            "total_cost": decision.total_cost,
            "cost_multiplier": decision.cost_multiplier,
            "fallback_used": decision.fallback_used,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_request_cost(self, request: ChatCompletionRequest) -> float:
        """Estimate the cost of a request before execution for quota pre-check.

        Uses a rough token estimate from message content and the model's pricing.
        Returns 0.0 if pricing cannot be resolved (quota budget check becomes no-op).
        """
        prompt_chars = 0
        for msg in request.messages or []:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_chars += len(content)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        estimated_completion_tokens = request.max_tokens or 256

        try:
            chain = self.router.get_fallback_chain(request.model or "")
        except (KeyError, Exception):
            return 0.0
        if not chain:
            return 0.0
        mapping = chain[0]
        try:
            return self.cost_tracker.calculate_cost(
                mapping.provider,
                mapping.model_id,
                estimated_prompt_tokens,
                estimated_completion_tokens,
            )
        except Exception:
            return 0.0

    def _compute_effective_allowed_models(
        self, project: Project | None, user_id: str
    ) -> set[str] | None:
        """Compute the effective allowed-models set from project and user access lists.

        Returns the intersection when both are set, the single list when only one
        is set, or ``None`` when neither is set (meaning all models are permitted).
        """
        project_allowed: set[str] | None = None
        if project and project.allowed_models:
            project_allowed = set(project.allowed_models)

        user_config = self._user_configs.get(user_id, {})
        user_allowed_list = user_config.get("allowed_models")
        user_allowed: set[str] | None = None
        if user_allowed_list:
            user_allowed = set(user_allowed_list)

        if project_allowed is not None and user_allowed is not None:
            return project_allowed & user_allowed
        if project_allowed is not None:
            return project_allowed
        if user_allowed is not None:
            return user_allowed
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_request(self, data: dict) -> ChatCompletionRequest:
        """Parse raw dict into ChatCompletionRequest."""
        return ChatCompletionRequest(
            messages=data.get("messages", []),
            model=data.get("model", ""),
            temperature=data.get("temperature"),
            max_tokens=data.get("max_tokens"),
            top_p=data.get("top_p"),
            stop=data.get("stop"),
            stream=data.get("stream", False),
            system=data.get("system"),
        )

    def _extract_context(self, context: dict) -> RequestContext:
        """Extract RequestContext from the context dict."""
        return RequestContext(
            user_id=context.get("user_id", ""),
            project_id=context.get("project_id", ""),
            roles=context.get("roles", []),
            scopes=context.get("scopes", []),
        )

    def _make_provider_fn(self):
        """Return a provider function compatible with Router.execute_with_fallback.

        .. deprecated::
            Use ``ProviderFnFactory`` instead. This method is retained for
            backward compatibility with tests that do not supply a factory.
        """
        warnings.warn(
            "_make_provider_fn is deprecated; use ProviderFnFactory instead",
            DeprecationWarning,
            stacklevel=2,
        )

        async def _noop(mapping):
            raise NotImplementedError("provider_fn must be supplied by caller")
        return _noop

    def _response_to_dict(
        self, response: ChatCompletionResponse, is_cached: bool = False
    ) -> dict:
        """Convert ChatCompletionResponse to a plain dict."""
        d: dict[str, Any] = {
            "id": response.id,
            "choices": response.choices,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "model": response.model,
            "provider": response.provider,
        }
        if response.warnings:
            d["warnings"] = response.warnings
        if is_cached:
            d["is_cached"] = True
        return d

    def _chunk_to_dict(self, chunk: StreamChunk) -> dict:
        """Convert StreamChunk to a plain dict."""
        return {
            "id": chunk.id,
            "choices": chunk.choices,
            "model": chunk.model,
            "is_final": chunk.is_final,
        }

    def _reinject_chunk_pii(
        self, chunk_dict: dict, mapping: RedactionMapping, buffer: dict
    ) -> dict:
        """Re-inject PII tokens in a streaming chunk's delta content.

        Uses a buffer to handle tokens split across chunk boundaries.
        Buffer holds {"pending": str} — text that might be a partial token.
        """
        choices = chunk_dict.get("choices", [])
        new_choices = []
        for choice in choices:
            delta = choice.get("delta", {})
            content = delta.get("content", "")
            if isinstance(content, str) and content:
                text = buffer.get("pending", "") + content
                # Check if text ends with a potential partial token (starts with [ but no closing ])
                last_open = text.rfind("[")
                if last_open != -1 and "]" not in text[last_open:]:
                    buffer["pending"] = text[last_open:]
                    text = text[:last_open]
                else:
                    buffer["pending"] = ""
                reinjected = mapping.reinject(text)
                new_choices.append({**choice, "delta": {**delta, "content": reinjected}})
            else:
                new_choices.append(choice)
        return {**chunk_dict, "choices": new_choices}

    def _replace_response_content(
        self, response: ChatCompletionResponse, message: str
    ) -> ChatCompletionResponse:
        """Return a copy of the response with content replaced by a policy message."""
        replaced_choices = []
        for choice in response.choices:
            new_choice = dict(choice)
            new_choice["message"] = {"role": "assistant", "content": message}
            replaced_choices.append(new_choice)
        return ChatCompletionResponse(
            id=response.id,
            choices=replaced_choices,
            usage=response.usage,
            model=response.model,
            provider=response.provider,
            warnings=response.warnings + ["Response modified by guardrail"],
        )

    async def handle_list_models(self, project_id: str | None = None, user_id: str | None = None) -> dict:
        """Return all models with descriptions and capabilities.

        If project_id is provided and the project has allowed_models,
        only those models are returned. If user_id is provided and the
        user has allowed_models, further filter to the intersection.
        """
        models = self.router.model_registry.list_models()
        if project_id:
            project = self._projects.get(project_id)
            if project and project.allowed_models:
                allowed = set(project.allowed_models)
                models = [m for m in models if m.name in allowed]

        if user_id:
            user_config = self._user_configs.get(user_id, {})
            user_allowed = user_config.get("allowed_models")
            if user_allowed:
                allowed_set = set(user_allowed)
                models = [m for m in models if m.name in allowed_set]

        return {
            "models": [
                {
                    "name": m.name,
                    "description": m.description,
                    "providers": [p.provider for p in m.providers],
                    "capabilities": m.capabilities or [],
                    "routing_strategy": m.routing_strategy.value,
                }
                for m in models
            ]
        }

    async def handle_health_check(self) -> dict:
        """Return service status and per-provider health."""
        models = self.router.model_registry.list_models()
        providers: set[str] = set()
        for m in models:
            for p in m.providers:
                providers.add(p.provider)

        provider_health: dict[str, str] = {}
        for provider in sorted(providers):
            provider_health[provider] = (
                "healthy" if self.router.health_tracker.is_healthy(provider) else "unhealthy"
            )

        return {
            "status": "ok",
            "providers": provider_health,
        }



# ---------------------------------------------------------------------------
# App instance and entrypoint registration
# ---------------------------------------------------------------------------

app = BedrockAgentCoreApp()

# Singleton agent — wired up by create_gateway_agent()
_agent: GatewayAgent | None = None


@app.entrypoint("chat_completions")
async def chat_completions(request_data: dict, context: dict) -> dict | AsyncIterator[dict]:
    """Main chat completions entrypoint registered with BedrockAgentCoreApp."""
    if _agent is None:
        return _error_response(500, "server_error", "Gateway agent not initialised.")
    return await _agent.handle_chat_completion(request_data, context)

@app.entrypoint("list_models")
async def list_models() -> dict:
    """List all available models."""
    if _agent is None:
        return _error_response(500, "server_error", "Gateway agent not initialised.")
    return await _agent.handle_list_models()


@app.entrypoint("health_check")
async def health_check() -> dict:
    """Health check with per-provider status."""
    if _agent is None:
        return _error_response(500, "server_error", "Gateway agent not initialised.")
    return await _agent.handle_health_check()



# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_gateway_agent(
    router: Router,
    rate_limiter: SlidingWindowRateLimiter,
    guardrail_engine: GuardrailEngine,
    cache_manager: CacheManager,
    cost_tracker: CostTracker,
    session_manager: SessionManager | None = None,
    projects: dict[str, Project] | None = None,
    provider_fn_factory: ProviderFnFactory | None = None,
    user_configs: dict[str, dict] | None = None,
    request_validator: RequestValidator | None = None,
    quota_enforcer: QuotaEnforcer | None = None,
    policy_resolver: PolicyHierarchyResolver | None = None,
    pii_redactor: PIIRedactor | None = None,
    injection_detector: PromptInjectionDetector | None = None,
    audit_trail: AuditTrail | None = None,
    event_dispatcher: EventDispatcher | None = None,
    region_router: RegionRouter | None = None,
) -> GatewayAgent:
    """Create and wire a GatewayAgent, also setting the module-level singleton."""
    global _agent
    agent = GatewayAgent(
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        session_manager=session_manager,
        projects=projects,
        provider_fn_factory=provider_fn_factory,
        user_configs=user_configs,
        request_validator=request_validator,
        quota_enforcer=quota_enforcer,
        policy_resolver=policy_resolver,
        pii_redactor=pii_redactor,
        injection_detector=injection_detector,
        audit_trail=audit_trail,
        event_dispatcher=event_dispatcher,
        region_router=region_router,
    )
    _agent = agent
    return agent
