"""Guards that the shipped demo seed populates every dashboard page.

The admin dashboard is the first thing a new user sees, and three pages
(Audit Log, API Keys, Webhooks) were reachable but permanently empty because
nothing seeded them. These tests pin the seed's coverage so a future edit that
drops a section fails here rather than silently emptying a page.
"""

from __future__ import annotations

import asyncio

import yaml

from src.gateway.auth.policy_hierarchy import (
    NODE_TYPES_ORDERED,
    PolicyHierarchyResolver,
)
from src.gateway.bootstrap import _validate_seed_hierarchy
from src.gateway.config_loader import (
    load_demo_seed_config,
    serialize_demo_seed_config,
)
from src.gateway.models import PolicyNode
from src.gateway.security.audit_trail import AuditEventType
from src.gateway.security.event_dispatcher import DestinationType

SEED_PATH = "config/demo_seed.yaml"


class _NoPersistence:
    """Stands in for DynamoPersistence: the seeded tree lives in memory only."""

    enabled = True

    async def load_all_policy_nodes(self):
        return []

    async def save_policy_node(self, node):
        pass


def _resolver_for(seed):
    """A resolver holding the seed's tree, built the way bootstrap builds it."""
    resolver = PolicyHierarchyResolver(
        persistence=_NoPersistence(), cache_ttl_seconds=0
    )
    for node in seed.policy_nodes:
        resolver._nodes[node["node_id"]] = PolicyNode(
            node_id=node["node_id"],
            node_type=node["node_type"],
            parent_id=node.get("parent_id"),
            display_name=node.get("display_name", node["node_id"]),
            limits=node.get("limits") or {},
        )
    return resolver


def _resolve(seed, node_id, environment=None):
    """The effective policy for a node — what the gateway actually enforces."""
    return asyncio.run(_resolver_for(seed).resolve(node_id, environment))


class TestShippedSeedCoversEveryPage:
    def test_all_sections_are_populated(self):
        seed = load_demo_seed_config(SEED_PATH)
        empty = [
            name
            for name in (
                "projects",
                "user_budgets",
                "usage_seeds",
                "policies",
                "policy_nodes",
                "unhealthy_providers",
                "audit_events",
                "api_keys",
                "webhook_destinations",
            )
            if not getattr(seed, name)
        ]
        assert empty == [], f"demo seed sections are empty: {empty}"

    def test_api_keys_span_both_projects(self):
        seed = load_demo_seed_config(SEED_PATH)
        project_ids = {k["project_id"] for k in seed.api_keys}
        assert project_ids == {"proj-alpha", "proj-beta"}

    def test_api_keys_include_a_revoked_example(self):
        """The Keys page renders revoked state differently; keep one to show it."""
        seed = load_demo_seed_config(SEED_PATH)
        assert any(k.get("revoked") for k in seed.api_keys)

    def test_audit_events_cover_the_security_view(self):
        """/admin/audit/security filters to these types -- all must be present."""
        seed = load_demo_seed_config(SEED_PATH)
        present = {ev["event_type"] for ev in seed.audit_events}
        required = {
            "injection_detected",
            "injection_blocked",
            "policy_deny",
            "auth_failure",
            "pii_redaction",
        }
        assert required <= present, f"missing: {required - present}"

    def test_every_audit_event_type_is_valid(self):
        seed = load_demo_seed_config(SEED_PATH)
        for ev in seed.audit_events:
            AuditEventType(ev["event_type"])  # raises ValueError if invalid

    def test_every_webhook_type_is_valid(self):
        seed = load_demo_seed_config(SEED_PATH)
        for wd in seed.webhook_destinations:
            DestinationType(wd.get("type", "webhook"))

    def test_webhooks_cover_all_destination_types(self):
        seed = load_demo_seed_config(SEED_PATH)
        types = {wd.get("type", "webhook") for wd in seed.webhook_destinations}
        assert types == {t.value for t in DestinationType}

    def test_webhooks_include_a_disabled_example(self):
        seed = load_demo_seed_config(SEED_PATH)
        assert any(wd.get("enabled") is False for wd in seed.webhook_destinations)

    def test_webhook_urls_are_non_routable(self):
        """Demo config must never point at a real endpoint."""
        seed = load_demo_seed_config(SEED_PATH)
        for wd in seed.webhook_destinations:
            url = wd.get("config", {}).get("url")
            if url:
                assert ".invalid" in url, f"{wd['name']} has a routable URL: {url}"

    def test_seeded_users_have_budgets(self):
        """Users page shows budget/spend; a member with no budget renders blank."""
        seed = load_demo_seed_config(SEED_PATH)
        budgeted = {ub["user_id"] for ub in seed.user_budgets}
        members = {m for p in seed.projects for m in p.get("members", [])}
        assert members <= budgeted, f"members without budgets: {members - budgeted}"

    def test_usage_seeds_reference_known_projects(self):
        seed = load_demo_seed_config(SEED_PATH)
        known = {p["project_id"] for p in seed.projects}
        referenced = {u["project_id"] for u in seed.usage_seeds}
        assert referenced <= known

    def test_new_sections_survive_serialization_round_trip(self):
        seed = load_demo_seed_config(SEED_PATH)
        round_tripped = yaml.safe_load(
            yaml.safe_dump(serialize_demo_seed_config(seed))
        )
        assert round_tripped["api_keys"] == seed.api_keys
        assert round_tripped["audit_events"] == seed.audit_events
        assert round_tripped["webhook_destinations"] == seed.webhook_destinations
        assert round_tripped["policy_nodes"] == seed.policy_nodes


# ── Policy hierarchy ────────────────────────────────────────────────────────
# The Govern > Hierarchy page renders this tree, and the quota enforcer reads
# the resolved result on every request. Before it was seeded, the only nodes
# that existed were flat per-project ones synthesized from each project's
# budget_limit -- every one parentless, so the inheritance the page exists to
# show had nothing to show.


class TestSeededPolicyHierarchy:
    def test_the_tree_spans_all_four_levels(self):
        """One level demonstrates no inheritance; that was the original bug."""
        seed = load_demo_seed_config(SEED_PATH)
        types = {n["node_type"] for n in seed.policy_nodes}
        assert types == set(NODE_TYPES_ORDERED), f"missing levels: {set(NODE_TYPES_ORDERED) - types}"

    def test_exactly_one_root(self):
        seed = load_demo_seed_config(SEED_PATH)
        roots = [n for n in seed.policy_nodes if n.get("parent_id") is None]
        assert [r["node_id"] for r in roots] == ["org:acme"]

    def test_every_parent_exists(self):
        """get_ancestry stops at a missing parent and returns a partial path, so
        a typo'd parent_id resolves to fewer limits than the seed describes."""
        seed = load_demo_seed_config(SEED_PATH)
        ids = {n["node_id"] for n in seed.policy_nodes}
        dangling = {
            n["node_id"]: n["parent_id"]
            for n in seed.policy_nodes
            if n.get("parent_id") is not None and n["parent_id"] not in ids
        }
        assert dangling == {}

    def test_no_node_id_is_repeated(self):
        """The resolver keys by node_id, so a duplicate silently wins."""
        seed = load_demo_seed_config(SEED_PATH)
        ids = [n["node_id"] for n in seed.policy_nodes]
        assert len(ids) == len(set(ids))

    def test_each_node_nests_under_a_broader_level(self):
        """A project parented to an environment would type-check but describes a
        hierarchy inverted against the page's own Org > BU > Project > Env
        heading, and against the order the resolver documents."""
        seed = load_demo_seed_config(SEED_PATH)
        rank = {t: i for i, t in enumerate(NODE_TYPES_ORDERED)}
        by_id = {n["node_id"]: n for n in seed.policy_nodes}
        for node in seed.policy_nodes:
            parent_id = node.get("parent_id")
            if parent_id is None:
                continue
            parent = by_id[parent_id]
            assert rank[node["node_type"]] > rank[parent["node_type"]], (
                f"{node['node_id']} ({node['node_type']}) is parented to "
                f"{parent_id} ({parent['node_type']})"
            )

    def test_the_shipped_tree_satisfies_the_resolver(self):
        """No child may exceed its parent. The bootstrap path assigns these nodes
        directly (a tree does not arrive parent-first), so this rule is checked
        over the whole tree rather than per node on arrival -- and if the seed
        broke it the dashboard would display a limit that is not the one in
        force, because the merge silently clamps to the parent's value."""
        _validate_seed_hierarchy(_resolver_for(load_demo_seed_config(SEED_PATH)))

    def test_every_seeded_project_is_in_the_tree(self):
        """A project absent here still gets a flat fallback node from its
        budget_limit, which renders as an unparented root beside the real tree."""
        seed = load_demo_seed_config(SEED_PATH)
        in_tree = {
            n["node_id"] for n in seed.policy_nodes if n["node_type"] == "project"
        }
        assert {p["project_id"] for p in seed.projects} <= in_tree

    def test_the_default_chat_project_keeps_room_to_demo(self):
        """bootstrap picks the first seeded project as the chat playground's
        default, so the resolved ceiling for proj-alpha is what a live demo runs
        against. Tightening the tree until the demo 429s is the failure this
        catches -- the numbers are per-minute and per-request, not per-session."""
        seed = load_demo_seed_config(SEED_PATH)
        default_project = seed.projects[0]["project_id"]
        policy = _resolve(seed, default_project)
        assert policy.rate_limit_rpm is None or policy.rate_limit_rpm >= 60
        assert policy.max_tokens_per_request is None or policy.max_tokens_per_request >= 4096
        assert policy.budget_limit is None or policy.budget_limit >= 100.0

    def test_the_default_chat_project_keeps_its_full_model_list(self):
        """allowed_models resolves as an intersection down the chain. A list set
        anywhere above proj-alpha would quietly shrink what the Models page
        offers and what the playground can call."""
        seed = load_demo_seed_config(SEED_PATH)
        project = seed.projects[0]
        policy = _resolve(seed, project["project_id"])
        if policy.allowed_models is not None:
            missing = set(project["allowed_models"]) - set(policy.allowed_models)
            assert missing == set(), f"hierarchy removes {missing}"

    def test_a_project_budget_agrees_with_its_project_record(self):
        """Projects and Quotas read different sources for the same number: the
        Project record, and the resolved policy. Disagreement is not a rendering
        detail -- the resolved one is the value actually enforced."""
        seed = load_demo_seed_config(SEED_PATH)
        for project in seed.projects:
            if project.get("budget_limit") is None:
                continue
            policy = _resolve(seed, project["project_id"])
            assert policy.budget_limit == project["budget_limit"], (
                f"{project['project_id']}: project says {project['budget_limit']}, "
                f"hierarchy enforces {policy.budget_limit}"
            )

    def test_an_environment_id_is_addressable_by_its_env_suffix(self):
        """Environment nodes are reached as "<project>:<env>" -- get_ancestry
        builds that id from the ?env= query parameter, so the separator is load-
        bearing and an id that does not follow it is unreachable."""
        seed = load_demo_seed_config(SEED_PATH)
        for node in seed.policy_nodes:
            if node["node_type"] != "environment":
                continue
            assert node["node_id"] == f"{node['parent_id']}:{node['node_id'].rsplit(':', 1)[1]}"
            resolved = _resolve(
                seed, node["parent_id"], node["node_id"].rsplit(":", 1)[1]
            )
            # Reached the env node, not just its parent.
            assert resolved.rate_limit_rpm == node["limits"]["rate_limit_rpm"]

    def test_redaction_stays_on_once_a_parent_enables_it(self):
        """The cascade's core promise: no descendant can switch redaction off."""
        seed = load_demo_seed_config(SEED_PATH)
        enablers = [
            n["node_id"]
            for n in seed.policy_nodes
            if n.get("limits", {}).get("pii_redaction_enabled")
        ]
        assert enablers, "no node enables PII redaction; the cascade is undemonstrated"
        by_id = {n["node_id"]: n for n in seed.policy_nodes}

        def descends_from(node_id, ancestor):
            seen = set()
            current = by_id[node_id].get("parent_id")
            while current and current not in seen:
                if current == ancestor:
                    return True
                seen.add(current)
                current = by_id[current].get("parent_id")
            return False

        for node in seed.policy_nodes:
            if any(descends_from(node["node_id"], a) for a in enablers):
                assert _resolve(seed, node["node_id"]).pii_redaction_enabled is True
