"""Tests for PII redaction policy cascade through the hierarchy."""

import asyncio

import pytest

from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.models import PolicyNode


class FakePersistence:
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


class TestPIIRedactionCascade:
    def test_org_enables_pii_for_all_children(self, resolver, persistence):
        org = PolicyNode("org:acme", "org", None, "Acme",
                         limits={"pii_redaction_enabled": True, "pii_redact_types": ["email", "ssn"]})
        proj = PolicyNode("proj:ml", "project", "org:acme", "ML",
                          limits={"rate_limit_rpm": 500})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(proj))

        policy = _run(resolver.resolve("proj:ml"))
        assert policy.pii_redaction_enabled is True
        assert set(policy.pii_redact_types) == {"email", "ssn"}

    def test_child_adds_pii_types(self, resolver, persistence):
        org = PolicyNode("org:acme", "org", None, "Acme",
                         limits={"pii_redaction_enabled": True, "pii_redact_types": ["email"]})
        bu = PolicyNode("bu:health", "business_unit", "org:acme", "Healthcare",
                        limits={"pii_redact_types": ["ssn", "medical_record"]})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(bu))

        policy = _run(resolver.resolve("bu:health"))
        assert policy.pii_redaction_enabled is True
        # Union: child adds types on top of parent
        assert set(policy.pii_redact_types) == {"email", "ssn", "medical_record"}

    def test_child_cannot_disable_parent_pii(self, resolver, persistence):
        org = PolicyNode("org:acme", "org", None, "Acme",
                         limits={"pii_redaction_enabled": True, "pii_redact_types": ["email"]})
        proj = PolicyNode("proj:eng", "project", "org:acme", "Engineering",
                          limits={"pii_redaction_enabled": False})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(proj))

        policy = _run(resolver.resolve("proj:eng"))
        # Parent enabled it — child cannot turn it off
        assert policy.pii_redaction_enabled is True

    def test_no_pii_when_org_not_enabled(self, resolver, persistence):
        org = PolicyNode("org:startup", "org", None, "Startup",
                         limits={"rate_limit_rpm": 1000})
        proj = PolicyNode("proj:app", "project", "org:startup", "App",
                          limits={})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(proj))

        policy = _run(resolver.resolve("proj:app"))
        assert policy.pii_redaction_enabled is False
        assert policy.pii_redact_types is None

    def test_reinject_defaults_true(self, resolver, persistence):
        org = PolicyNode("org:a", "org", None, "A",
                         limits={"pii_redaction_enabled": True, "pii_redact_types": ["email"]})
        _run(persistence.save_policy_node(org))
        policy = _run(resolver.resolve("org:a"))
        assert policy.pii_reinject is True

    def test_parent_permanent_redaction_wins(self, resolver, persistence):
        # Parent turns OFF reinject (permanent redaction) — child cannot re-enable.
        org = PolicyNode("org:strict", "org", None, "Strict",
                         limits={"pii_redaction_enabled": True, "pii_redact_types": ["email"],
                                 "pii_reinject": False})
        proj = PolicyNode("proj:x", "project", "org:strict", "X",
                          limits={"pii_reinject": True})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(proj))
        policy = _run(resolver.resolve("proj:x"))
        assert policy.pii_reinject is False

    def test_full_hierarchy_accumulates_types(self, resolver, persistence):
        nodes = [
            PolicyNode("org:acme", "org", None, "Acme",
                       limits={"pii_redaction_enabled": True, "pii_redact_types": ["email"]}),
            PolicyNode("bu:finance", "business_unit", "org:acme", "Finance",
                       limits={"pii_redact_types": ["ssn", "credit_card"]}),
            PolicyNode("proj:payments", "project", "bu:finance", "Payments",
                       limits={"pii_redact_types": ["phone"]}),
            PolicyNode("proj:payments:prod", "environment", "proj:payments", "Production",
                       limits={"pii_redact_types": ["aws_account_id"]}),
        ]
        for n in nodes:
            _run(persistence.save_policy_node(n))

        policy = _run(resolver.resolve("proj:payments:prod"))
        assert policy.pii_redaction_enabled is True
        assert set(policy.pii_redact_types) == {
            "email", "ssn", "credit_card", "phone", "aws_account_id"
        }
