"""A budget set through the admin API must still block spending after a restart.

``PUT /admin/users/{id}/budget`` and ``PUT /admin/projects/{id}`` both write to
DynamoDB, and on the next boot ``_load_persisted_state`` reads the rows back into
``projects`` and ``user_configs``. That is where it stopped. The limits that
``check_budget`` / ``check_user_budget`` consult live in
``CostTracker._budgets`` and ``._user_budgets``, and the ones the quota endpoint
resolves live in the policy resolver's nodes — none of which a dict update
touches. So the dashboard displayed the limit, ``GET /admin/users/{id}`` returned
it, and nothing enforced it:

    set on instance 1: {'budget_limit': 5.0, 'alert_threshold': 4.0}
    persisted to store: {'alice': {'budget_limit': 5.0, ...}}
    after restart, budget: {'budget_limit': None, 'alert_threshold': None}
    alice has spent $500 against a $5 cap -> over_budget=False

Worse than a limit that was never set, because the operator has evidence it is
there. ``_apply_seed_data`` always registered seeded entities; the persisted path
never did, so identical configuration behaved differently depending on how it
arrived.

These drive the real route through the real serializers and the real
``load_projects`` / ``load_user_configs``, replacing only the boto3 table, so a
regression in any layer fails here. "Restart" is a second set of components over
the same rows — the condition is a fresh process, not a fresh dict.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.bootstrap import _register_persisted_budgets
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import PolicyNode, Project
from src.gateway.persistence import DynamoPersistence


class _FakeTable:
    """One dict standing in for the table, with the scan the load path needs."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def put_item(self, Item):  # noqa: N803 — boto3's parameter name
        self._rows[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._rows.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def delete_item(self, Key):  # noqa: N803
        self._rows.pop((Key["PK"], Key["SK"]), None)

    def scan(self, FilterExpression=None, ExclusiveStartKey=None):  # noqa: N803
        """Filter on entity_type by reading the condition boto3 built.

        The load path passes ``Attr("entity_type").eq(...)``; matching on its
        rendered value keeps the production filter under test rather than
        returning every row and letting the caller look right by accident.
        """
        wanted = FilterExpression.get_expression()["values"][1] if FilterExpression else None
        items = [
            dict(r) for r in self._rows.values()
            if wanted is None or r.get("entity_type") == wanted
        ]
        return {"Items": items}


class _TablePersistence(DynamoPersistence):
    """The real persistence class with only the boto3 table replaced."""

    def __init__(self, rows: dict | None = None) -> None:
        super().__init__()
        self._enabled = True
        self.rows = rows if rows is not None else {}

    def _get_table(self):
        return _FakeTable(self.rows)


def _admin_client(persistence, projects=None, user_configs=None):
    """One booted instance: the admin routes over a fresh CostTracker."""
    tracker = CostTracker(pricing_config={}, persistence=persistence)
    api = AdminAPI(
        cost_tracker=tracker,
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        projects=projects if projects is not None else {},
        persistence=persistence,
        user_configs=user_configs if user_configs is not None else {},
    )
    client = TestClient(Starlette(routes=create_admin_routes(api)))
    return client, tracker


def _restart(persistence):
    """Boot a second instance over the same rows, as bootstrap does.

    Deliberately runs the same two steps in the same order as
    ``build_gateway_components``: load the rows, then register what came back.
    A test that only called ``_register_persisted_budgets`` would pass even if
    the load path dropped the field.
    """
    tracker = CostTracker(pricing_config={}, persistence=persistence)
    resolver = PolicyHierarchyResolver(persistence=None)
    loaded_projects = asyncio.run(persistence.load_projects())
    loaded_user_configs = asyncio.run(persistence.load_user_configs())
    projects: dict = {}
    user_configs: dict = {}
    projects.update(loaded_projects)
    user_configs.update(loaded_user_configs)
    _register_persisted_budgets(tracker, resolver, loaded_projects, loaded_user_configs)
    return tracker, resolver, projects, user_configs


@pytest.fixture
def persistence(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    return _TablePersistence()


class TestAUserBudgetSurvivesARestart:
    """The reported bug: ``PUT /admin/users/{id}/budget`` then a deploy."""

    def test_the_limit_is_enforced_after_a_restart(self, persistence):
        client, _ = _admin_client(persistence)
        resp = client.put(
            "/admin/users/alice/budget",
            json={"budget_limit": 5.0, "alert_threshold": 4.0},
        )
        assert resp.status_code == 200

        tracker, _, _, _ = _restart(persistence)
        tracker._user_spend["alice"] = 500.0
        status = asyncio.run(tracker.check_user_budget("alice"))
        assert status.budget_limit == pytest.approx(5.0), (
            "the persisted limit was loaded into a dict but never registered "
            "with the tracker that enforces it"
        )
        assert status.is_over_budget is True

    def test_the_alert_threshold_survives_too(self, persistence):
        """Alerts are the earlier signal; losing them loses the warning entirely."""
        client, _ = _admin_client(persistence)
        client.put(
            "/admin/users/alice/budget",
            json={"budget_limit": 100.0, "alert_threshold": 80.0},
        )
        tracker, _, _, _ = _restart(persistence)
        tracker._user_spend["alice"] = 85.0
        status = asyncio.run(tracker.check_user_budget("alice"))
        assert status.alert_threshold == pytest.approx(80.0)
        assert status.is_alert_triggered is True
        assert status.is_over_budget is False

    def test_clearing_a_limit_survives_rather_than_reverting(self, persistence):
        """Setting a limit back to None is a decision, not an absence.

        A config row exists because someone configured the user, so it is
        registered even when both fields are None — otherwise a cleared limit
        would silently fall back to whatever the seed file said.
        """
        client, _ = _admin_client(persistence)
        client.put("/admin/users/alice/budget", json={"budget_limit": 5.0})
        client.put("/admin/users/alice/budget", json={"budget_limit": None})

        tracker, _, _, _ = _restart(persistence)
        tracker.register_user("alice", budget_limit=999.0)  # stand-in for a seed
        _register_persisted_budgets(
            tracker, PolicyHierarchyResolver(persistence=None), {},
            asyncio.run(persistence.load_user_configs()),
        )
        assert tracker.get_user_budget("alice")["budget_limit"] is None

    def test_a_user_with_no_row_is_unaffected(self, persistence):
        tracker, _, _, _ = _restart(persistence)
        assert tracker.get_user_budget("nobody") == {
            "budget_limit": None, "alert_threshold": None,
        }


class TestAProjectBudgetSurvivesARestart:
    """Same gap, same cause — found by checking whether the sibling path shared it."""

    def test_the_limit_is_enforced_after_a_restart(self, persistence):
        client, _ = _admin_client(persistence)
        resp = client.post(
            "/admin/projects",
            json={"project_id": "acme", "name": "Acme", "budget_limit": 5.0},
        )
        assert resp.status_code == 201

        tracker, _, _, _ = _restart(persistence)
        tracker._project_spend["acme"] = 500.0
        status = asyncio.run(tracker.check_budget("acme"))
        assert status.budget_limit == pytest.approx(5.0)
        assert status.is_over_budget is True

    def test_the_quota_endpoint_can_resolve_the_limit(self, persistence):
        """``QuotaEnforcer.check_budget`` reads the resolver, not the project.

        This is the path that returns 429, so a project present in ``projects``
        with no matching node is a budget the gateway displays and never blocks
        on.
        """
        client, _ = _admin_client(persistence)
        client.post(
            "/admin/projects",
            json={"project_id": "acme", "name": "Acme", "budget_limit": 5.0},
        )
        _, resolver, _, _ = _restart(persistence)
        assert asyncio.run(resolver.resolve("acme")).budget_limit == pytest.approx(5.0)

    def test_a_project_with_no_budget_is_not_registered(self, persistence):
        """Registering everything would turn "unlimited" into a node with no limit."""
        client, _ = _admin_client(persistence)
        client.post("/admin/projects", json={"project_id": "free", "name": "Free"})
        tracker, resolver, _, _ = _restart(persistence)
        assert asyncio.run(tracker.check_budget("free")).budget_limit is None
        assert "free" not in resolver._nodes

    def test_a_tighter_parent_limit_is_not_overwritten(self, persistence):
        """A real hierarchy must win over the flat per-project fallback node.

        The fallback exists for projects with no tree. Where a tree exists, the
        project's own limit is already enforced through ``cost_tracker``, and
        replacing the tree node would discard the parent cap — raising the
        effective limit rather than restoring it.
        """
        client, _ = _admin_client(persistence)
        client.post(
            "/admin/projects",
            json={"project_id": "acme", "name": "Acme", "budget_limit": 500.0},
        )
        loaded = asyncio.run(persistence.load_projects())

        resolver = PolicyHierarchyResolver(persistence=None)
        resolver._nodes["org"] = PolicyNode(
            node_id="org", node_type="organization", parent_id=None,
            display_name="Org", limits={"budget_limit": 10.0},
        )
        resolver._nodes["acme"] = PolicyNode(
            node_id="acme", node_type="project", parent_id="org",
            display_name="Acme", limits={},
        )
        _register_persisted_budgets(
            CostTracker(pricing_config={}), resolver, loaded, {})

        assert resolver._nodes["acme"].parent_id == "org", (
            "the fallback node replaced a node that had a parent"
        )
        assert asyncio.run(resolver.resolve("acme")).budget_limit == pytest.approx(10.0)


class TestTheRegistrationIsSafeToRun:
    """It runs on every boot with whatever the scan returned, including nothing."""

    def test_empty_state_registers_nothing(self):
        tracker = CostTracker(pricing_config={})
        resolver = PolicyHierarchyResolver(persistence=None)
        _register_persisted_budgets(tracker, resolver, {}, {})
        assert tracker._budgets == {}
        assert tracker._user_budgets == {}
        assert resolver._nodes == {}

    def test_none_is_tolerated(self):
        """A failed scan returns falsy, and boot must not die on it."""
        tracker = CostTracker(pricing_config={})
        _register_persisted_budgets(
            tracker, PolicyHierarchyResolver(persistence=None), None, None)
        assert tracker._budgets == {}

    def test_a_persisted_limit_replaces_a_seeded_one(self):
        """Order matters: persisted state merges *on top of* the seed.

        ``projects.update(loaded_projects)`` already says the persisted row wins;
        the registration has to agree, or the displayed limit and the enforced
        one come from different places.
        """
        tracker = CostTracker(pricing_config={})
        tracker.register_project("acme", budget_limit=1000.0)
        _register_persisted_budgets(
            tracker, PolicyHierarchyResolver(persistence=None),
            {"acme": Project(project_id="acme", name="Acme", budget_limit=5.0)}, {},
        )
        assert asyncio.run(tracker.check_budget("acme")).budget_limit == pytest.approx(5.0)
