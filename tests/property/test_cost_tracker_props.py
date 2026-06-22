# Feature: litellm-service, Properties 15-18: CostTracker property tests
"""Property-based tests for the CostTracker component.

Properties covered:
  15 – Cost calculation is correct for all provider/model combinations
  16 – Usage records contain all required fields for any completed request
  17 – Usage aggregation correctly filters and sums
  18 – Budget threshold triggers alert, hard limit triggers rejection
"""

import asyncio
from datetime import datetime, timedelta

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.cost_tracker import CostTracker
from src.gateway.models import (
    BudgetStatus,
    TokenPricing,
    UsageFilters,
    UsageRecord,
    UsageReport,
)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

VALID_PROVIDERS = ["openai", "anthropic", "bedrock", "azure_openai", "vertex_ai", "cohere"]
VALID_MODELS = ["gpt-4", "gpt-3.5-turbo", "claude-3-sonnet", "llama-2", "command-r"]

provider_strategy = st.sampled_from(VALID_PROVIDERS)
model_strategy = st.sampled_from(VALID_MODELS)

# Positive floats for pricing (avoid zero to ensure non-trivial cost)
pricing_cost_strategy = st.floats(min_value=0.0001, max_value=1.0, allow_nan=False, allow_infinity=False)

# Token counts: non-negative integers, reasonable range
token_strategy = st.integers(min_value=0, max_value=100_000)

# Positive token counts (at least 1 token)
positive_token_strategy = st.integers(min_value=1, max_value=100_000)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)



# ===========================================================================
# Property 15: Cost calculation is correct for all provider/model combinations
# Feature: litellm-service, Property 15
# ===========================================================================


@given(
    provider=provider_strategy,
    model=model_strategy,
    prompt_tokens=token_strategy,
    completion_tokens=token_strategy,
    prompt_token_cost=pricing_cost_strategy,
    completion_token_cost=pricing_cost_strategy,
)
@settings(max_examples=100)
def test_cost_calculation_correctness(
    provider, model, prompt_tokens, completion_tokens, prompt_token_cost, completion_token_cost
):
    """Property 15: Cost calculation is correct for all provider/model combinations.

    For any provider, model, prompt_tokens, and completion_tokens, the cost SHALL equal
    (prompt_tokens / 1000 * prompt_token_cost) + (completion_tokens / 1000 * completion_token_cost).

    **Validates: Requirements 7.2**
    """
    pricing_config = {
        provider: {
            model: TokenPricing(
                prompt_token_cost=prompt_token_cost,
                completion_token_cost=completion_token_cost,
            )
        }
    }
    tracker = CostTracker(pricing_config)

    actual_cost = tracker.calculate_cost(provider, model, prompt_tokens, completion_tokens)
    expected_cost = (prompt_tokens / 1000 * prompt_token_cost) + (
        completion_tokens / 1000 * completion_token_cost
    )

    assert abs(actual_cost - expected_cost) < 1e-9, (
        f"Cost mismatch: expected {expected_cost}, got {actual_cost}"
    )



# ===========================================================================
# Property 16: Usage records contain all required fields for any completed request
# Feature: litellm-service, Property 16
# ===========================================================================

REQUIRED_USAGE_RECORD_FIELDS = [
    "request_id",
    "project_id",
    "user_id",
    "provider",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost",
    "timestamp",
]

# Strategy for generating valid UsageRecords
usage_record_strategy = st.builds(
    UsageRecord,
    request_id=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "P"))),
    project_id=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "P"))),
    user_id=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "P"))),
    provider=provider_strategy,
    model=model_strategy,
    prompt_tokens=token_strategy,
    completion_tokens=token_strategy,
    total_tokens=token_strategy,
    cost=st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False),
    timestamp=st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 1, 1),
    ),
)


@given(record=usage_record_strategy)
@settings(max_examples=100)
def test_usage_record_completeness(record):
    """Property 16: Usage records contain all required fields for any completed request.

    For any completed request, the UsageRecord SHALL contain request_id, project_id,
    user_id, provider, model, prompt_tokens, completion_tokens, total_tokens, cost,
    and timestamp.

    **Validates: Requirements 7.1, 7.4**
    """
    tracker = CostTracker({})
    _run(tracker.record_usage(record))

    assert len(tracker._records) == 1
    stored = tracker._records[0]

    # Verify all required fields are present and non-None
    for field_name in REQUIRED_USAGE_RECORD_FIELDS:
        value = getattr(stored, field_name)
        assert value is not None, f"Required field '{field_name}' is None"

    # Verify the stored record matches the input exactly
    assert stored.request_id == record.request_id
    assert stored.project_id == record.project_id
    assert stored.user_id == record.user_id
    assert stored.provider == record.provider
    assert stored.model == record.model
    assert stored.prompt_tokens == record.prompt_tokens
    assert stored.completion_tokens == record.completion_tokens
    assert stored.total_tokens == record.total_tokens
    assert stored.cost == record.cost
    assert stored.timestamp == record.timestamp



# ===========================================================================
# Property 17: Usage aggregation correctly filters and sums
# Feature: litellm-service, Property 17
# ===========================================================================

# Strategy for generating a list of UsageRecords with controlled field values
# so we can build meaningful filters
bounded_provider_strategy = st.sampled_from(["openai", "anthropic", "bedrock"])
bounded_model_strategy = st.sampled_from(["gpt-4", "claude-3-sonnet", "llama-2"])
bounded_project_strategy = st.sampled_from(["proj-1", "proj-2", "proj-3"])
bounded_user_strategy = st.sampled_from(["user-a", "user-b", "user-c"])

filterable_record_strategy = st.builds(
    UsageRecord,
    request_id=st.uuids().map(str),
    project_id=bounded_project_strategy,
    user_id=bounded_user_strategy,
    provider=bounded_provider_strategy,
    model=bounded_model_strategy,
    prompt_tokens=st.integers(min_value=1, max_value=5000),
    completion_tokens=st.integers(min_value=1, max_value=5000),
    total_tokens=st.integers(min_value=2, max_value=10000),
    cost=st.floats(min_value=0.001, max_value=100.0, allow_nan=False, allow_infinity=False),
    timestamp=st.datetimes(
        min_value=datetime(2024, 1, 1),
        max_value=datetime(2024, 12, 31),
    ),
)


@given(
    records=st.lists(filterable_record_strategy, min_size=1, max_size=20),
    filter_provider=st.one_of(st.none(), bounded_provider_strategy),
    filter_model=st.one_of(st.none(), bounded_model_strategy),
    filter_project=st.one_of(st.none(), bounded_project_strategy),
    filter_user=st.one_of(st.none(), bounded_user_strategy),
)
@settings(max_examples=100)
def test_usage_aggregation_filtering(
    records, filter_provider, filter_model, filter_project, filter_user
):
    """Property 17: Usage aggregation correctly filters and sums.

    For any set of UsageRecords and any combination of filters, the aggregated
    UsageReport SHALL reflect the sum of only the records matching all applied filters.

    **Validates: Requirements 7.3**
    """
    tracker = CostTracker({})
    for r in records:
        _run(tracker.record_usage(r))

    filters = UsageFilters(
        provider=filter_provider,
        model=filter_model,
        project_id=filter_project,
        user_id=filter_user,
    )

    report = _run(tracker.get_aggregated_usage(filters))

    # Manually compute expected results by applying the same filters
    expected = records
    if filter_provider is not None:
        expected = [r for r in expected if r.provider == filter_provider]
    if filter_model is not None:
        expected = [r for r in expected if r.model == filter_model]
    if filter_project is not None:
        expected = [r for r in expected if r.project_id == filter_project]
    if filter_user is not None:
        expected = [r for r in expected if r.user_id == filter_user]

    assert report.total_requests == len(expected), (
        f"Request count mismatch: expected {len(expected)}, got {report.total_requests}"
    )
    assert report.total_tokens == sum(r.total_tokens for r in expected), (
        "Token sum mismatch"
    )
    assert abs(report.total_cost - sum(r.cost for r in expected)) < 1e-6, (
        f"Cost sum mismatch: expected {sum(r.cost for r in expected)}, got {report.total_cost}"
    )



# ===========================================================================
# Property 18: Budget threshold triggers alert, hard limit triggers rejection
# Feature: litellm-service, Property 18
# ===========================================================================


@given(
    budget_limit=st.floats(min_value=10.0, max_value=10000.0, allow_nan=False, allow_infinity=False),
    alert_fraction=st.floats(min_value=0.1, max_value=0.99, allow_nan=False, allow_infinity=False),
    spend_fraction=st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
    num_records=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=100)
def test_budget_threshold_and_limit_behavior(
    budget_limit, alert_fraction, spend_fraction, num_records
):
    """Property 18: Budget threshold triggers alert, hard limit triggers rejection.

    For any project with alert_threshold and budget_limit:
    - when spend >= alert_threshold → is_alert_triggered
    - when spend >= budget_limit → is_over_budget

    **Validates: Requirements 7.6, 7.7**
    """
    alert_threshold = budget_limit * alert_fraction
    total_spend = budget_limit * spend_fraction

    # Distribute total spend across num_records records
    per_record_cost = total_spend / num_records

    tracker = CostTracker({})
    tracker.register_project("proj-test", budget_limit=budget_limit, alert_threshold=alert_threshold)

    now = datetime.utcnow()
    for i in range(num_records):
        record = UsageRecord(
            request_id=f"req-{i}",
            project_id="proj-test",
            user_id="user-1",
            provider="openai",
            model="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost=per_record_cost,
            timestamp=now + timedelta(seconds=i),
        )
        _run(tracker.record_usage(record))

    status = _run(tracker.check_budget("proj-test"))

    # Verify budget status fields
    assert status.project_id == "proj-test"
    assert status.budget_limit == budget_limit
    assert status.alert_threshold == alert_threshold
    assert abs(status.current_spend - total_spend) < 1e-6, (
        f"Spend mismatch: expected {total_spend}, got {status.current_spend}"
    )

    # The actual spend stored is the sum of per_record_cost * num_records,
    # which may differ from total_spend due to floating-point division/summation.
    # We test against the actual current_spend the tracker computed.
    actual_spend = status.current_spend

    # Core property: alert triggered iff actual spend >= alert_threshold
    if actual_spend >= alert_threshold:
        assert status.is_alert_triggered is True, (
            f"Alert should be triggered: spend={actual_spend} >= threshold={alert_threshold}"
        )
    else:
        assert status.is_alert_triggered is False, (
            f"Alert should NOT be triggered: spend={actual_spend} < threshold={alert_threshold}"
        )

    # Core property: over budget iff actual spend >= budget_limit
    if actual_spend >= budget_limit:
        assert status.is_over_budget is True, (
            f"Should be over budget: spend={actual_spend} >= limit={budget_limit}"
        )
    else:
        assert status.is_over_budget is False, (
            f"Should NOT be over budget: spend={actual_spend} < limit={budget_limit}"
        )
