# Feature: ensemble-routing, Properties 1-9: Ensemble routing property tests
"""Property-based tests for ensemble routing (scatter-gather-synthesize).

Properties covered (see .kiro/specs/ensemble-routing/design.md "Correctness
Properties"):

  1 – Latency bounded by slowest panel member plus judge (not the sum)
  2 – Cost equals sum of survivor panel calls plus judge
  3 – Quorum invariant: synthesis only when quorum met
  4 – Survivors-only synthesis
  5 – Access control covers all panel and judge models
  6 – EnsembleDecision completeness
  7 – Failure tolerance preserves outcome when quorum met
  8 – Cost ceiling enforced before dispatch
  9 – Invocation detection round-trip

The orchestration boundary ``Router.execute_with_fallback`` is mocked so that
each panel member and the judge can be made to succeed, fail, or sleep a
controlled duration deterministically. The cost tracker is mocked to verify
per-call usage recording and total-cost accounting. No real network calls are
made.
"""

import asyncio
import types

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from unittest.mock import AsyncMock, MagicMock

from src.gateway.agent import GatewayAgent
from src.gateway.ensemble_config import EnsembleConfig
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EnsemblePreset,
    TokenUsage,
)
from src.gateway.router import (
    AllProvidersExhaustedError,
    EnsembleAccessError,
    EnsembleCostCeilingError,
    EnsembleNoSurvivorsError,
    EnsembleQuorumError,
    EnsembleSynthesisError,
    Router,
)


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/unit/test_ensemble_router.py conventions)
# ---------------------------------------------------------------------------


def _make_request(model: str = "ensemble") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "What is the capital of France?"}],
        model=model,
    )


def _make_response(
    model: str, content: str = "answer", provider: str = "bedrock"
) -> ChatCompletionResponse:
    """Build a response whose ``model`` field doubles as the cost-map key."""
    return ChatCompletionResponse(
        id=f"resp-{model}",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _make_preset(
    name: str = "test",
    panel: list[str] | None = None,
    judge: str = "judge",
    quorum: int = 1,
    fallback_policy: str = "error",
    cost_ceiling: float | None = None,
    ranking_criteria: str = "length",
) -> EnsemblePreset:
    return EnsemblePreset(
        name=name,
        panel=panel if panel is not None else ["m0", "m1", "m2"],
        judge=judge,
        quorum=quorum,
        fallback_policy=fallback_policy,
        cost_ceiling=cost_ceiling,
        ranking_criteria=ranking_criteria,
    )


def _make_cost_tracker(costs: dict[str, float] | None = None):
    """Mock CostTracker: calculate_cost keyed by response.model, async record_usage."""
    costs = costs or {}
    tracker = MagicMock()
    tracker.calculate_cost = MagicMock(
        side_effect=lambda provider, model, *args, **kwargs: costs.get(model, 0.0)
    )
    tracker.record_usage = AsyncMock()
    return tracker


def _make_factory():
    """provider_fn_factory whose create() returns a dummy provider_fn.

    Since ``execute_with_fallback`` is mocked, the provider_fn itself is never
    invoked; the factory only needs a callable ``create`` method.
    """
    factory = MagicMock()
    factory.create = MagicMock(return_value=AsyncMock())
    return factory


def _make_router(cost_tracker=None) -> Router:
    """Router with a real (empty) registry/health tracker; callers patch
    ``execute_with_fallback`` to control per-model outcomes."""
    return Router(
        model_registry=ModelRegistry(),
        health_tracker=ProviderHealthTracker(),
        max_retries=0,
        base_delay=0.0,
        cost_tracker=cost_tracker,
    )


def _exec_side_effect(
    responses: dict[str, ChatCompletionResponse],
    failures: set[str] | None = None,
    latencies: dict[str, float] | None = None,
):
    """Build an ``execute_with_fallback`` side effect keyed by request.model.

    - models in ``latencies`` sleep the mapped duration (controlled latency)
    - models in ``failures`` raise ``AllProvidersExhaustedError``
    - otherwise the mapped response is returned
    """
    failures = failures or set()
    latencies = latencies or {}

    async def _side_effect(request, provider_fn, allowed_models=None, **kwargs):
        model = request.model
        delay = latencies.get(model)
        if delay:
            await asyncio.sleep(delay)
        if model in failures:
            raise AllProvidersExhaustedError(
                [{"provider": "bedrock", "status_code": 503, "message": "down"}]
            )
        return responses[model]

    return _side_effect


def _run(coro):
    """Run a coroutine to completion on a fresh event loop (Hypothesis-safe)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Distinct, non-overlapping model-name alphabet so substring checks are safe.
_panel_name_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=8
)


@st.composite
def _distinct_models(draw, min_panel=2, max_panel=5):
    """Draw a distinct panel + judge set of model identifiers."""
    n = draw(st.integers(min_value=min_panel, max_value=max_panel))
    names = draw(
        st.lists(_panel_name_strategy, min_size=n + 1, max_size=n + 1, unique=True)
    )
    panel = names[:n]
    judge = names[n]
    return panel, judge


# ===========================================================================
# Property 1: Latency Bounded by Slowest Panel Member Plus Judge
# Feature: ensemble-routing, Property 1
# Validates: Requirements 1.1, 1.4
# ===========================================================================


@given(panel_size=st.integers(min_value=2, max_value=5))
@settings(max_examples=100, deadline=None)
def test_latency_bounded_by_slowest_member_plus_judge(panel_size):
    """Property 1: panel calls overlap, followed by the judge.

    Every panel coroutine yields once after recording its start. When it
    resumes, all other panel coroutines must already have started. This proves
    concurrent dispatch without relying on wall-clock thresholds that become
    flaky under CI runner contention. The judge must not start until every
    panel coroutine has completed.

    **Validates: Requirements 1.1, 1.4**
    """
    panel = [f"m{i}" for i in range(panel_size)]
    panel_models = set(panel)
    preset = _make_preset(panel=panel, judge="judge", quorum=1)

    responses = {m: _make_response(m, f"resp {m}") for m in panel}
    responses["judge"] = _make_response("judge", "synthesized")
    started: set[str] = set()
    completed: set[str] = set()

    async def _concurrency_probe(
        request, provider_fn, allowed_models=None, **kwargs
    ):
        model = request.model
        if model == "judge":
            assert started == panel_models
            assert completed == panel_models
            return responses[model]

        started.add(model)
        await asyncio.sleep(0)
        assert started == panel_models, (
            f"{model} resumed before all panel calls started: {started}"
        )
        completed.add(model)
        return responses[model]

    router = _make_router(cost_tracker=_make_cost_tracker())
    router.execute_with_fallback = AsyncMock(side_effect=_concurrency_probe)

    response, decision = _run(
        router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
        )
    )

    assert decision.judge_invoked is True
    assert response.model == "judge"


# ===========================================================================
# Property 2: Cost Equals Sum of Survivor Panel Calls Plus Judge
# Feature: ensemble-routing, Property 2
# Validates: Requirements 8.1, 8.2, 8.3, 8.4, 9.5
# ===========================================================================


@given(
    data=st.data(),
    panel_size=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=100, deadline=None)
def test_cost_equals_survivor_sum_plus_judge(data, panel_size):
    """Property 2: ``total_cost`` == sum(survivor costs) + judge cost.

    One usage entry is recorded per survivor and exactly one for the judge;
    timed-out/failed members record no usage. quorum=1 guarantees synthesis
    whenever at least one member survives.

    **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 9.5**
    """
    panel = [f"m{i}" for i in range(panel_size)]
    judge = "judge"
    preset = _make_preset(panel=panel, judge=judge, quorum=1)

    # Each member succeeds or fails; require at least one survivor.
    success_flags = data.draw(
        st.lists(
            st.booleans(), min_size=panel_size, max_size=panel_size
        ).filter(any)
    )
    survivors = [panel[i] for i, ok in enumerate(success_flags) if ok]
    failures = {panel[i] for i, ok in enumerate(success_flags) if not ok}

    costs = {
        m: data.draw(st.floats(min_value=0.0, max_value=1.0))
        for m in panel + [judge]
    }
    responses = {m: _make_response(m, f"resp {m}") for m in survivors}
    responses[judge] = _make_response(judge, "synthesized")

    tracker = _make_cost_tracker(costs)
    router = _make_router(cost_tracker=tracker)
    router.execute_with_fallback = AsyncMock(
        side_effect=_exec_side_effect(responses, failures=failures)
    )

    response, decision = _run(
        router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
            project_id="proj-1",
            user_id="user-1",
        )
    )

    expected = sum(costs[m] for m in survivors) + costs[judge]
    assert decision.judge_invoked is True
    assert decision.total_cost == expected
    assert decision.total_cost >= 0.0
    # One usage entry per survivor + exactly one for the judge.
    assert tracker.record_usage.await_count == len(survivors) + 1


# ===========================================================================
# Property 3: Quorum Invariant — Synthesis Only When Quorum Met
# Feature: ensemble-routing, Property 3
# Validates: Requirements 3.1, 3.7, 4.3, 4.4, 4.5, 4.6
# ===========================================================================


@given(
    data=st.data(),
    panel_size=st.integers(min_value=1, max_value=5),
    fallback_policy=st.sampled_from(["best-single", "error"]),
)
@settings(max_examples=100, deadline=None)
def test_quorum_invariant_synthesis_iff_quorum_met(data, panel_size, fallback_policy):
    """Property 3: judge invoked iff survivors >= quorum (and > 0).

    - 0 survivors → always ``EnsembleNoSurvivorsError`` regardless of policy.
    - survivors >= quorum → judge invoked, synthesized answer returned.
    - 0 < survivors < quorum → fallback policy applied, judge NOT invoked:
        * best-single → ranked survivor returned, ``fallback_used`` True.
        * error → ``EnsembleQuorumError`` carrying the decision.

    **Validates: Requirements 3.1, 3.7, 4.3, 4.4, 4.5, 4.6**
    """
    panel = [f"m{i}" for i in range(panel_size)]
    judge = "judge"
    quorum = data.draw(st.integers(min_value=1, max_value=panel_size))
    preset = _make_preset(
        panel=panel, judge=judge, quorum=quorum, fallback_policy=fallback_policy
    )

    success_flags = data.draw(
        st.lists(st.booleans(), min_size=panel_size, max_size=panel_size)
    )
    survivors = [panel[i] for i, ok in enumerate(success_flags) if ok]
    failures = {panel[i] for i, ok in enumerate(success_flags) if not ok}
    survivor_count = len(survivors)

    responses = {m: _make_response(m, f"resp {m}") for m in survivors}
    responses[judge] = _make_response(judge, "synthesized")

    router = _make_router(cost_tracker=_make_cost_tracker())
    router.execute_with_fallback = AsyncMock(
        side_effect=_exec_side_effect(responses, failures=failures)
    )

    async def _go():
        return await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
        )

    if survivor_count == 0:
        try:
            _run(_go())
            assert False, "expected EnsembleNoSurvivorsError with 0 survivors"
        except EnsembleNoSurvivorsError as exc:
            assert exc.decision.judge_invoked is False
    elif survivor_count >= quorum:
        response, decision = _run(_go())
        assert decision.judge_invoked is True
        assert response.model == judge
        assert decision.quorum_met is True
    else:  # 0 < survivor_count < quorum → fallback policy
        if fallback_policy == "best-single":
            response, decision = _run(_go())
            assert decision.judge_invoked is False
            assert decision.fallback_used is True
            assert decision.quorum_met is False
            assert response.model in survivors
        else:
            try:
                _run(_go())
                assert False, "expected EnsembleQuorumError under error policy"
            except EnsembleQuorumError as exc:
                assert exc.decision.judge_invoked is False
                assert exc.decision.quorum_met is False


# ===========================================================================
# Property 4: Survivors-Only Synthesis
# Feature: ensemble-routing, Property 4
# Validates: Requirements 3.3, 5.1, 5.3
# ===========================================================================


@given(
    data=st.data(),
    panel_size=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=100, deadline=None)
def test_survivors_only_synthesis(data, panel_size):
    """Property 4: judge input contains only survivor content, never failures.

    With at least one failure and at least one survivor (quorum=1), the message
    list handed to the judge must include every survivor's content/identity and
    must exclude every failed member's content and identity.

    **Validates: Requirements 3.3, 5.1, 5.3**
    """
    panel = [f"member{i}" for i in range(panel_size)]
    judge = "judgemodel"
    preset = _make_preset(panel=panel, judge=judge, quorum=1)

    # Require at least one survivor AND at least one failure.
    success_flags = data.draw(
        st.lists(st.booleans(), min_size=panel_size, max_size=panel_size).filter(
            lambda flags: any(flags) and not all(flags)
        )
    )
    survivors = [panel[i] for i, ok in enumerate(success_flags) if ok]
    failed = [panel[i] for i, ok in enumerate(success_flags) if not ok]
    failures = set(failed)

    # Unique, non-overlapping content markers per member.
    contents = {m: f"CONTENT_{m}_xyz" for m in panel}
    responses = {m: _make_response(m, contents[m]) for m in survivors}
    responses[judge] = _make_response(judge, "synthesized")

    router = _make_router(cost_tracker=_make_cost_tracker())
    router.execute_with_fallback = AsyncMock(
        side_effect=_exec_side_effect(responses, failures=failures)
    )

    response, decision = _run(
        router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="explain the topic",
            preset=preset,
            allowed_models=None,
        )
    )

    assert decision.judge_invoked is True
    judge_call = router.execute_with_fallback.await_args_list[-1]
    judge_req = judge_call.args[0]
    assert judge_req.model == judge
    judge_text = str(judge_req.messages)

    for m in survivors:
        assert contents[m] in judge_text, f"survivor {m} content missing from judge input"
        assert m in judge_text, f"survivor {m} identity missing from judge input"
    for m in failed:
        assert contents[m] not in judge_text, f"failed {m} content leaked into judge input"
        assert m not in judge_text, f"failed {m} identity leaked into judge input"


# ===========================================================================
# Property 5: Access Control Covers All Panel and Judge Models
# Feature: ensemble-routing, Property 5
# Validates: Requirements 11.2, 11.3
# ===========================================================================


@given(
    data=st.data(),
    spec=_distinct_models(min_panel=2, max_panel=4),
)
@settings(max_examples=100, deadline=None)
def test_access_control_covers_panel_and_judge(data, spec):
    """Property 5: dispatch proceeds iff every panel member + judge is allowed.

    If any panel member or the judge is absent from a non-None allowed set, the
    request is rejected with ``EnsembleAccessError`` before any dispatch (no
    ``execute_with_fallback`` call). Otherwise dispatch proceeds.

    **Validates: Requirements 11.2, 11.3**
    """
    panel, judge = spec
    required = [*panel, judge]
    preset = _make_preset(panel=panel, judge=judge, quorum=1)

    # Draw an allowed set as an arbitrary subset of the required models, plus
    # possibly some extra unrelated models.
    keep_flags = data.draw(
        st.lists(st.booleans(), min_size=len(required), max_size=len(required))
    )
    allowed = {m for m, keep in zip(required, keep_flags) if keep}
    allowed |= set(data.draw(st.lists(_panel_name_strategy, max_size=3)))
    # Ensure extras never accidentally complete a missing required model.
    all_required_allowed = all(m in allowed for m in required)

    responses = {m: _make_response(m, f"resp {m}") for m in panel}
    responses[judge] = _make_response(judge, "synthesized")

    tracker = _make_cost_tracker()
    router = _make_router(cost_tracker=tracker)
    router.execute_with_fallback = AsyncMock(
        side_effect=_exec_side_effect(responses)
    )

    async def _go():
        return await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=allowed,
        )

    if all_required_allowed:
        response, decision = _run(_go())
        assert decision.judge_invoked is True
        assert response.model == judge
    else:
        try:
            _run(_go())
            assert False, "expected EnsembleAccessError for a disallowed model"
        except EnsembleAccessError as exc:
            assert exc.model in required
            assert exc.model not in allowed
        # No dispatch and no usage recorded.
        router.execute_with_fallback.assert_not_awaited()
        tracker.record_usage.assert_not_awaited()


# ===========================================================================
# Property 6: EnsembleDecision Completeness
# Feature: ensemble-routing, Property 6
# Validates: Requirements 9.1, 9.2, 9.3, 9.4, 5.2, 8.6
# ===========================================================================


@given(
    data=st.data(),
    panel_size=st.integers(min_value=1, max_value=5),
    fallback_policy=st.sampled_from(["best-single", "error"]),
)
@settings(max_examples=100, deadline=None)
def test_decision_completeness(data, panel_size, fallback_policy):
    """Property 6: succeeded ∪ failed == panel, intersection empty, counts sane.

    For any success/failure pattern the EnsembleDecision partitions the panel
    cleanly, names the judge, reports a consistent quorum status, sets
    ``cost_multiplier == N + 1``, and gives every failed member a non-null
    reason. The decision is obtained whether the route returns or raises (the
    quorum/no-survivor errors carry the partial decision).

    **Validates: Requirements 9.1, 9.2, 9.3, 9.4, 5.2, 8.6**
    """
    panel = [f"m{i}" for i in range(panel_size)]
    judge = "judge"
    quorum = data.draw(st.integers(min_value=1, max_value=panel_size))
    preset = _make_preset(
        panel=panel, judge=judge, quorum=quorum, fallback_policy=fallback_policy
    )

    success_flags = data.draw(
        st.lists(st.booleans(), min_size=panel_size, max_size=panel_size)
    )
    survivors = [panel[i] for i, ok in enumerate(success_flags) if ok]
    failures = {panel[i] for i, ok in enumerate(success_flags) if not ok}

    responses = {m: _make_response(m, f"resp {m}") for m in survivors}
    responses[judge] = _make_response(judge, "synthesized")  # judge always ok

    router = _make_router(cost_tracker=_make_cost_tracker())
    router.execute_with_fallback = AsyncMock(
        side_effect=_exec_side_effect(responses, failures=failures)
    )

    async def _go():
        return await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
        )

    try:
        _, decision = _run(_go())
    except (EnsembleNoSurvivorsError, EnsembleQuorumError) as exc:
        decision = exc.decision

    # Partition: union == panel, intersection empty.
    succeeded_set = set(decision.succeeded)
    failed_set = {f["model"] for f in decision.failed}
    assert succeeded_set | failed_set == set(panel)
    assert succeeded_set & failed_set == set()
    assert set(decision.panel_members) == set(panel)
    assert len(decision.panel_members) == panel_size

    # Judge + counts + quorum consistency.
    assert decision.judge_model == judge
    assert decision.succeeded_count == len(survivors)
    assert decision.quorum_threshold == quorum
    assert decision.quorum_met == (len(survivors) >= quorum)
    assert decision.cost_multiplier == panel_size + 1

    # Every failed member has a non-null reason.
    for f in decision.failed:
        assert f["reason"] is not None


# ===========================================================================
# Property 7: Failure Tolerance Preserves Outcome When Quorum Met
# Feature: ensemble-routing, Property 7
# Validates: Requirements 5.1, 5.3
# ===========================================================================


@given(
    data=st.data(),
    panel_size=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=100, deadline=None)
def test_failure_tolerance_preserves_outcome_when_quorum_met(data, panel_size):
    """Property 7: with survivors >= quorum, a synthesized answer is produced
    regardless of which/how many members failed.

    A quorum-many subset is forced to survive; an arbitrary subset of the rest
    fails. The judge must still synthesize over the survivors.

    **Validates: Requirements 5.1, 5.3**
    """
    panel = [f"m{i}" for i in range(panel_size)]
    judge = "judge"
    quorum = data.draw(st.integers(min_value=1, max_value=panel_size))
    preset = _make_preset(panel=panel, judge=judge, quorum=quorum)

    # Force exactly `quorum` guaranteed survivors; the remaining members may
    # independently succeed or fail (arbitrary failure pattern).
    forced_survivor_idx = set(
        data.draw(
            st.lists(
                st.integers(min_value=0, max_value=panel_size - 1),
                min_size=quorum,
                max_size=quorum,
                unique=True,
            )
        )
    )
    extra_flags = data.draw(
        st.lists(st.booleans(), min_size=panel_size, max_size=panel_size)
    )
    survivors = [
        panel[i]
        for i in range(panel_size)
        if i in forced_survivor_idx or extra_flags[i]
    ]
    failures = {panel[i] for i in range(panel_size) if panel[i] not in survivors}

    responses = {m: _make_response(m, f"resp {m}") for m in survivors}
    responses[judge] = _make_response(judge, "synthesized")

    router = _make_router(cost_tracker=_make_cost_tracker())
    router.execute_with_fallback = AsyncMock(
        side_effect=_exec_side_effect(responses, failures=failures)
    )

    response, decision = _run(
        router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
        )
    )

    assert len(survivors) >= quorum
    assert decision.judge_invoked is True
    assert decision.quorum_met is True
    assert response.model == judge
    assert set(decision.succeeded) == set(survivors)


# ===========================================================================
# Property 8: Cost Ceiling Enforced Before Dispatch
# Feature: ensemble-routing, Property 8
# Validates: Requirements 8.5, 8.7
# ===========================================================================


@given(
    panel_size=st.integers(min_value=1, max_value=8),
    per_call=st.floats(min_value=0.01, max_value=1.0),
    headroom=st.floats(min_value=0.001, max_value=0.5),
)
@settings(max_examples=100, deadline=None)
def test_cost_ceiling_enforced_before_dispatch(panel_size, per_call, headroom):
    """Property 8: estimated (N+1)*per_call over the ceiling rejects pre-dispatch.

    When the estimate exceeds the configured ceiling, ``EnsembleCostCeilingError``
    is raised reporting both the estimate and the ceiling, with no panel dispatch
    and no usage recorded.

    **Validates: Requirements 8.5, 8.7**
    """
    panel = [f"m{i}" for i in range(panel_size)]
    judge = "judge"
    estimated = (panel_size + 1) * per_call
    # Choose a ceiling strictly below the estimate so enforcement triggers.
    ceiling = estimated - headroom
    assume(ceiling > 0)

    preset = _make_preset(
        panel=panel, judge=judge, quorum=1, cost_ceiling=ceiling
    )

    tracker = _make_cost_tracker()
    router = _make_router(cost_tracker=tracker)
    router.execute_with_fallback = AsyncMock()  # must never be awaited

    try:
        _run(
            router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=None,
                per_call_cost_estimate=per_call,
            )
        )
        assert False, "expected EnsembleCostCeilingError"
    except EnsembleCostCeilingError as exc:
        assert exc.estimated == estimated
        assert exc.ceiling == ceiling

    router.execute_with_fallback.assert_not_awaited()
    tracker.record_usage.assert_not_awaited()


# ===========================================================================
# Property 9: Invocation Detection Round-Trip
# Feature: ensemble-routing, Property 9
# Validates: Requirements 7.1, 7.2, 7.3, 7.4
# ===========================================================================


_ENSEMBLE_YAML = """
ensemble:
  default_preset: budget
  presets:
    budget:
      panel:
        - m0
        - m1
      judge: judge
      quorum: 1
      fallback_policy: error
    quality:
      panel:
        - m0
        - m1
      judge: judge2
      quorum: 1
      fallback_policy: error
"""


def _detection_agent() -> GatewayAgent:
    """Minimal stand-in exposing ``_is_ensemble_request`` over a configured router.

    ``_is_ensemble_request`` only reads ``self.router._ensemble_config``, so a
    lightweight namespace suffices and avoids constructing the full agent graph.
    """
    config = EnsembleConfig.from_yaml(_ENSEMBLE_YAML)
    router_stub = types.SimpleNamespace(_ensemble_config=config)
    return types.SimpleNamespace(router=router_stub)


def _detect(agent, model, context):
    return GatewayAgent._is_ensemble_request(agent, _make_request(model=model), context)


# Model strings that are neither "ensemble" nor "ensemble:<name>" nor "ensemble:".
_non_ensemble_model = st.text(min_size=0, max_size=20).filter(
    lambda s: s != "ensemble" and not s.startswith("ensemble:")
)
_preset_name = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=20
)


@given(
    model_choice=st.one_of(
        st.just("ensemble"),
        st.builds(lambda n: f"ensemble:{n}", _preset_name),
        st.just("ensemble:"),
        _non_ensemble_model,
    ),
    flag=st.booleans(),
)
@settings(max_examples=200, deadline=None)
def test_invocation_detection_round_trip(model_choice, flag):
    """Property 9: detection resolves model/flag to the correct triple.

    - ``model == "ensemble"`` → (True, None, None) [default preset]
    - ``model == "ensemble:<name>"`` → (True, name, None) [named preset]
    - ``model == "ensemble:"`` → (True, None, "missing preset name")
    - otherwise: flag True → (True, None, None); flag False → (False, None, None)
    The model value always takes precedence over the context flag.

    **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
    """
    agent = _detection_agent()
    context = {"ensemble": True} if flag else {}

    is_ensemble, preset_name, error = _detect(agent, model_choice, context)

    # Independent oracle mirroring the detection contract.
    if model_choice == "ensemble":
        expected = (True, None, None)
    elif model_choice.startswith("ensemble:"):
        name = model_choice[len("ensemble:"):]
        if name == "":
            expected = (True, None, "missing preset name")
        else:
            expected = (True, name, None)
    elif flag:
        expected = (True, None, None)
    else:
        expected = (False, None, None)

    assert (is_ensemble, preset_name, error) == expected


def test_invocation_detection_model_precedence_over_flag():
    """Req 7.7: a named model value wins over a context ``ensemble`` flag."""
    agent = _detection_agent()
    is_ensemble, preset_name, error = _detect(
        agent, "ensemble:quality", {"ensemble": True}
    )
    assert (is_ensemble, preset_name, error) == (True, "quality", None)


def test_invocation_detection_disabled_when_unconfigured():
    """Req 12.4: detection returns the non-ensemble path when config is absent."""
    router_stub = types.SimpleNamespace(_ensemble_config=None)
    agent = types.SimpleNamespace(router=router_stub)
    assert _detect(agent, "ensemble", {"ensemble": True}) == (False, None, None)
