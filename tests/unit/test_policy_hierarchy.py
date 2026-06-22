"""Tests for hierarchical policy resolution."""

import asyncio

import pytest

from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.models import PolicyNode, ResolvedPolicy


class FakePersistence:
    """In-memory persistence for testing."""

    def __init__(self):
        self._nodes: dict[str, PolicyNode] = {}
        self._enabled = True

    @property
    def enabled(self):
        return self._enabled

    async def save_policy_node(self, node: PolicyNode) -> None:
        self._nodes[node.node_id] = node

    async def get_policy_node(self, node_id: str) -> PolicyNode | None:
        return self._nodes.get(node_id)

    async def load_all_policy_nodes(self) -> list[PolicyNode]:
        return list(self._nodes.values())


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def persistence():
    return FakePersistence()


@pytest.fixture
def resolver(persistence):
    return PolicyHierarchyResolver(persistence=persistence, cache_ttl_seconds=0)


class TestSingleNode:
    def test_org_node_resolves_own_limits(self, resolver, persistence):
        node = PolicyNode(
            node_id="org:acme",
            node_type="org",
            parent_id=None,
            display_name="Acme Corp",
            limits={"rate_limit_rpm": 1000, "budget_limit": 50000.0},
        )
        _run(persistence.save_policy_node(node))
        policy = _run(resolver.resolve("org:acme"))

        assert policy.rate_limit_rpm == 1000
        assert policy.budget_limit == 50000.0

    def test_unknown_node_returns_empty_policy(self, resolver):
        policy = _run(resolver.resolve("nonexistent"))
        assert policy.rate_limit_rpm is None
        assert policy.budget_limit is None


class TestTwoLevelHierarchy:
    def test_child_inherits_parent_limits(self, resolver, persistence):
        org = PolicyNode(
            node_id="org:acme",
            node_type="org",
            parent_id=None,
            display_name="Acme",
            limits={"rate_limit_rpm": 1000, "budget_limit": 50000.0},
        )
        project = PolicyNode(
            node_id="proj:ml-team",
            node_type="project",
            parent_id="org:acme",
            display_name="ML Team",
            limits={"rate_limit_rpm": 500},
        )
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(project))

        policy = _run(resolver.resolve("proj:ml-team"))

        assert policy.rate_limit_rpm == 500  # min(1000, 500)
        assert policy.budget_limit == 50000.0  # inherited from parent

    def test_child_cannot_exceed_parent(self, resolver, persistence):
        org = PolicyNode(
            node_id="org:acme",
            node_type="org",
            parent_id=None,
            display_name="Acme",
            limits={"rate_limit_rpm": 100},
        )
        project = PolicyNode(
            node_id="proj:ml",
            node_type="project",
            parent_id="org:acme",
            display_name="ML",
            limits={"rate_limit_rpm": 200},  # exceeds parent
        )
        _run(persistence.save_policy_node(org))

        violations = _run(resolver.validate_node_limits(project))
        assert len(violations) == 1
        assert "rate_limit_rpm" in violations[0]


class TestFourLevelHierarchy:
    def test_full_hierarchy_most_restrictive_wins(self, resolver, persistence):
        nodes = [
            PolicyNode("org:acme", "org", None, "Acme",
                       limits={"rate_limit_rpm": 10000, "budget_limit": 100000.0,
                               "allowed_models": ["claude-opus", "claude-sonnet", "gpt-4o"]}),
            PolicyNode("bu:engineering", "business_unit", "org:acme", "Engineering",
                       limits={"rate_limit_rpm": 5000, "budget_limit": 50000.0,
                               "allowed_models": ["claude-opus", "claude-sonnet", "gpt-4o"]}),
            PolicyNode("proj:ml-platform", "project", "bu:engineering", "ML Platform",
                       limits={"rate_limit_rpm": 2000,
                               "allowed_models": ["claude-opus", "claude-sonnet"]}),
            PolicyNode("proj:ml-platform:prod", "environment", "proj:ml-platform", "Production",
                       limits={"rate_limit_rpm": 1000, "max_tokens_per_request": 4096}),
        ]
        for n in nodes:
            _run(persistence.save_policy_node(n))

        policy = _run(resolver.resolve("proj:ml-platform:prod"))

        assert policy.rate_limit_rpm == 1000
        assert policy.budget_limit == 50000.0
        assert set(policy.allowed_models) == {"claude-opus", "claude-sonnet"}
        assert policy.max_tokens_per_request == 4096


class TestModelIntersection:
    def test_allowed_models_intersected(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"allowed_models": ["a", "b", "c"]})
        proj = PolicyNode("proj:y", "project", "org:x", "Y",
                          limits={"allowed_models": ["b", "c", "d"]})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(proj))

        policy = _run(resolver.resolve("proj:y"))
        assert set(policy.allowed_models) == {"b", "c"}

    def test_child_models_subset_of_parent(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"allowed_models": ["a", "b"]})
        proj = PolicyNode("proj:y", "project", "org:x", "Y",
                          limits={"allowed_models": ["a", "b", "c"]})
        _run(persistence.save_policy_node(org))

        violations = _run(resolver.validate_node_limits(proj))
        assert len(violations) == 1
        assert "allowed_models" in violations[0]


class TestCacheInvalidation:
    def test_cache_invalidated_on_update(self, resolver, persistence):
        resolver._cache_ttl = 9999  # long TTL
        node = PolicyNode("org:x", "org", None, "X",
                          limits={"rate_limit_rpm": 500})
        _run(persistence.save_policy_node(node))

        # First resolve caches
        policy1 = _run(resolver.resolve("org:x"))
        assert policy1.rate_limit_rpm == 500

        # Update node
        node.limits["rate_limit_rpm"] = 200
        _run(resolver.set_node(node))

        # Should pick up new value (cache invalidated)
        policy2 = _run(resolver.resolve("org:x"))
        assert policy2.rate_limit_rpm == 200


class TestSetNodeValidation:
    def test_set_node_rejects_exceeding_parent(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"budget_limit": 1000.0})
        _run(persistence.save_policy_node(org))
        _run(resolver.load_nodes())

        child = PolicyNode("proj:y", "project", "org:x", "Y",
                           limits={"budget_limit": 2000.0})  # exceeds parent

        with pytest.raises(ValueError, match="exceeds parent"):
            _run(resolver.set_node(child))

    def test_set_node_accepts_valid_child(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"budget_limit": 1000.0})
        _run(persistence.save_policy_node(org))
        _run(resolver.load_nodes())

        child = PolicyNode("proj:y", "project", "org:x", "Y",
                           limits={"budget_limit": 500.0})
        _run(resolver.set_node(child))  # should not raise

        assert "proj:y" in resolver._nodes
