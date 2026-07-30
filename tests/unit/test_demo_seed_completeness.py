"""Guards that the shipped demo seed populates every dashboard page.

The admin dashboard is the first thing a new user sees, and three pages
(Audit Log, API Keys, Webhooks) were reachable but permanently empty because
nothing seeded them. These tests pin the seed's coverage so a future edit that
drops a section fails here rather than silently emptying a page.
"""

from __future__ import annotations

import yaml

from src.gateway.config_loader import (
    load_demo_seed_config,
    serialize_demo_seed_config,
)
from src.gateway.security.audit_trail import AuditEventType
from src.gateway.security.event_dispatcher import DestinationType

SEED_PATH = "config/demo_seed.yaml"


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
