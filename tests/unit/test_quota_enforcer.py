"""Tests for quota enforcer — policy hierarchy limits enforcement."""

import asyncio

import pytest

from src.gateway.models import ResolvedPolicy
from src.gateway.quota_enforcer import QuotaEnforcer


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def enforcer():
    return QuotaEnforcer()


class TestRateLimit:
    def test_allows_within_limit(self, enforcer):
        policy = ResolvedPolicy(rate_limit_rpm=10)
        for _ in range(10):
            result = _run(enforcer.check_rate_limit("proj-1", policy))
            assert result.allowed is True

    def test_blocks_over_limit(self, enforcer):
        policy = ResolvedPolicy(rate_limit_rpm=5)
        for _ in range(5):
            _run(enforcer.check_rate_limit("proj-1", policy))
        result = _run(enforcer.check_rate_limit("proj-1", policy))
        assert result.allowed is False
        assert result.limit_type == "rate_limit_rpm"
        assert result.limit_value == 5

    def test_no_limit_always_allows(self, enforcer):
        policy = ResolvedPolicy(rate_limit_rpm=None)
        for _ in range(1000):
            result = _run(enforcer.check_rate_limit("proj-1", policy))
            assert result.allowed is True

    def test_per_project_isolation(self, enforcer):
        policy = ResolvedPolicy(rate_limit_rpm=2)
        _run(enforcer.check_rate_limit("proj-a", policy))
        _run(enforcer.check_rate_limit("proj-a", policy))
        # proj-a is at limit
        assert _run(enforcer.check_rate_limit("proj-a", policy)).allowed is False
        # proj-b is fresh
        assert _run(enforcer.check_rate_limit("proj-b", policy)).allowed is True


class TestBudget:
    def test_allows_within_budget(self, enforcer):
        policy = ResolvedPolicy(budget_limit=100.0)
        _run(enforcer.record_spend("proj-1", 50.0))
        result = enforcer.check_budget("proj-1", 10.0, policy)
        assert result.allowed is True

    def test_blocks_over_budget(self, enforcer):
        policy = ResolvedPolicy(budget_limit=100.0)
        _run(enforcer.record_spend("proj-1", 95.0))
        result = enforcer.check_budget("proj-1", 10.0, policy)
        assert result.allowed is False
        assert result.limit_type == "budget_limit"
        assert result.current_value == 95.0

    def test_no_budget_always_allows(self, enforcer):
        policy = ResolvedPolicy(budget_limit=None)
        _run(enforcer.record_spend("proj-1", 999999.0))
        result = enforcer.check_budget("proj-1", 1000.0, policy)
        assert result.allowed is True

    def test_exact_at_limit_blocked(self, enforcer):
        policy = ResolvedPolicy(budget_limit=50.0)
        _run(enforcer.record_spend("proj-1", 50.0))
        result = enforcer.check_budget("proj-1", 0.01, policy)
        assert result.allowed is False


class TestMaxTokens:
    def test_allows_within_limit(self, enforcer):
        policy = ResolvedPolicy(max_tokens_per_request=4096)
        result = enforcer.check_max_tokens(2000, policy)
        assert result.allowed is True

    def test_blocks_over_limit(self, enforcer):
        policy = ResolvedPolicy(max_tokens_per_request=4096)
        result = enforcer.check_max_tokens(8000, policy)
        assert result.allowed is False
        assert result.limit_type == "max_tokens_per_request"

    def test_no_limit_allows_any(self, enforcer):
        policy = ResolvedPolicy(max_tokens_per_request=None)
        result = enforcer.check_max_tokens(100000, policy)
        assert result.allowed is True

    def test_none_requested_always_allowed(self, enforcer):
        policy = ResolvedPolicy(max_tokens_per_request=4096)
        result = enforcer.check_max_tokens(None, policy)
        assert result.allowed is True

    def test_cap_max_tokens(self, enforcer):
        policy = ResolvedPolicy(max_tokens_per_request=4096)
        assert enforcer.cap_max_tokens(8000, policy) == 4096
        assert enforcer.cap_max_tokens(2000, policy) == 2000
        assert enforcer.cap_max_tokens(None, policy) == 4096

    def test_cap_no_policy_limit(self, enforcer):
        policy = ResolvedPolicy(max_tokens_per_request=None)
        assert enforcer.cap_max_tokens(8000, policy) == 8000
        assert enforcer.cap_max_tokens(None, policy) is None


class TestModelAllowed:
    def test_allowed_model_passes(self, enforcer):
        policy = ResolvedPolicy(allowed_models=["claude-opus", "claude-sonnet"])
        result = enforcer.check_model_allowed("claude-opus", policy)
        assert result.allowed is True

    def test_disallowed_model_blocked(self, enforcer):
        policy = ResolvedPolicy(allowed_models=["claude-opus", "claude-sonnet"])
        result = enforcer.check_model_allowed("gpt-4o", policy)
        assert result.allowed is False
        assert "gpt-4o" in result.reason

    def test_no_restriction_allows_any(self, enforcer):
        policy = ResolvedPolicy(allowed_models=None)
        result = enforcer.check_model_allowed("any-model", policy)
        assert result.allowed is True


class TestProviderAllowed:
    def test_allowed_provider_passes(self, enforcer):
        policy = ResolvedPolicy(allowed_providers=["anthropic", "bedrock"])
        result = enforcer.check_provider_allowed("anthropic", policy)
        assert result.allowed is True

    def test_disallowed_provider_blocked(self, enforcer):
        policy = ResolvedPolicy(allowed_providers=["anthropic", "bedrock"])
        result = enforcer.check_provider_allowed("openai", policy)
        assert result.allowed is False
        assert "openai" in result.reason

    def test_no_restriction_allows_any(self, enforcer):
        policy = ResolvedPolicy(allowed_providers=None)
        result = enforcer.check_provider_allowed("anything", policy)
        assert result.allowed is True


class TestEnforceAll:
    def test_all_pass(self, enforcer):
        policy = ResolvedPolicy(
            rate_limit_rpm=100,
            budget_limit=1000.0,
            max_tokens_per_request=8192,
            allowed_models=["claude-opus"],
            allowed_providers=["anthropic"],
        )
        result = _run(enforcer.enforce_all(
            project_id="proj-1",
            model="claude-opus",
            provider="anthropic",
            max_tokens=4096,
            estimated_cost=0.05,
            policy=policy,
        ))
        assert result.allowed is True

    def test_model_blocked(self, enforcer):
        policy = ResolvedPolicy(allowed_models=["claude-sonnet"])
        result = _run(enforcer.enforce_all(
            project_id="proj-1", model="gpt-4o", provider=None,
            max_tokens=None, estimated_cost=0.0, policy=policy,
        ))
        assert result.allowed is False
        assert result.limit_type == "allowed_models"

    def test_budget_blocked(self, enforcer):
        policy = ResolvedPolicy(budget_limit=10.0)
        _run(enforcer.record_spend("proj-1", 9.5))
        result = _run(enforcer.enforce_all(
            project_id="proj-1", model="x", provider=None,
            max_tokens=None, estimated_cost=1.0, policy=policy,
        ))
        assert result.allowed is False
        assert result.limit_type == "budget_limit"

    def test_provider_blocked(self, enforcer):
        policy = ResolvedPolicy(allowed_providers=["bedrock"])
        result = _run(enforcer.enforce_all(
            project_id="proj-1", model="x", provider="openai",
            max_tokens=None, estimated_cost=0.0, policy=policy,
        ))
        assert result.allowed is False
        assert result.limit_type == "allowed_providers"

    def test_no_policy_limits_allows_everything(self, enforcer):
        policy = ResolvedPolicy()
        result = _run(enforcer.enforce_all(
            project_id="proj-1", model="anything", provider="anything",
            max_tokens=99999, estimated_cost=99999.0, policy=policy,
        ))
        assert result.allowed is True


class TestSpendTracking:
    def test_record_and_get(self, enforcer):
        _run(enforcer.record_spend("proj-1", 10.0))
        _run(enforcer.record_spend("proj-1", 5.0))
        assert enforcer.get_spend("proj-1") == 15.0

    def test_reset(self, enforcer):
        _run(enforcer.record_spend("proj-1", 100.0))
        _run(enforcer.reset_spend("proj-1"))
        assert enforcer.get_spend("proj-1") == 0.0

    def test_isolated_projects(self, enforcer):
        _run(enforcer.record_spend("proj-a", 10.0))
        _run(enforcer.record_spend("proj-b", 20.0))
        assert enforcer.get_spend("proj-a") == 10.0
        assert enforcer.get_spend("proj-b") == 20.0
