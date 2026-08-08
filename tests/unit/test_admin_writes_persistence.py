"""Admin write endpoints must survive a restart.

Four endpoints mutated a shared in-memory object, returned 200/201, and never
wrote to DynamoDB. Because `GET` reads the same object, each one looked correct
until the process restarted:

  * `POST`/`DELETE /admin/projects/{id}/members` — a removed member regained
    access at the next deploy.
  * `POST`/`DELETE /admin/webhooks` — a destination stopped, or resumed,
    receiving security events. The failure mode is an absence of alerts, which
    nothing observable distinguishes from "no events occurred".
  * `POST`/`PUT`/`DELETE /admin/regions/spokes` and `PUT /admin/regions/config` —
    a region an operator had drained came back into rotation.

The membership handlers were a missed call site next to two siblings that got it
right (`add_project_model`/`remove_project_model`), which is why `_persist_project`
is now extracted: the omission is the bug, so the fix is to have one place to
omit.

Every test here drives the real route against the real serializers, replacing only
the boto3 boundary — a regression in either half fails here rather than in
production. The restart classes assert the harder direction: that a *deletion*
survives. An add can persist while a delete silently reverts, and for all three
resources the delete is the security-relevant direction.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.region_routes import RegionAPI, create_region_routes
from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.admin.webhook_routes import WebhookAPI, create_webhook_routes
from src.gateway.bootstrap import (
    _apply_persisted_destinations,
    _apply_persisted_infrastructure,
    _apply_persisted_topology,
)
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import AuthMethod, Project, RequestContext
from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
)
from src.gateway.multi_region.region_router import RegionRouter
from src.gateway.persistence import DynamoPersistence, PersistenceConflictError
from src.gateway.security.event_dispatcher import (
    DestinationType,
    EventDestination,
    EventDispatcher,
)


class _FakeTable:
    """One dict standing in for the table, keyed the way DynamoDB is."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def put_item(  # noqa: N803 — boto3's parameter names
        self,
        Item,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        current = self._rows.get((Item["PK"], Item["SK"]))
        if ConditionExpression == "attribute_not_exists(#revision)":
            if current is not None and "revision" in current:
                raise _ConditionalFailure
        elif ConditionExpression == "#revision = :expected":
            expected = ExpressionAttributeValues[":expected"]
            if current is None or current.get("revision") != expected:
                raise _ConditionalFailure
        self._rows[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key, **kwargs):  # noqa: N803
        item = self._rows.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def delete_item(self, Key):  # noqa: N803
        self._rows.pop((Key["PK"], Key["SK"]), None)


class _TablePersistence(DynamoPersistence):
    """The real persistence class with only the boto3 table replaced.

    Subclassed rather than mocked so the serializers — the part that decides what
    actually reaches the table and what comes back — are the code under test.
    The row dict is passed in so a test can share it across two "boots".
    """

    def __init__(self, rows: dict | None = None) -> None:
        super().__init__()
        self._enabled = True
        self.rows = rows if rows is not None else {}

    def _get_table(self):
        return _FakeTable(self.rows)


class _ConditionalFailure(Exception):
    response = {
        "Error": {
            "Code": "ConditionalCheckFailedException",
        }
    }


class _FailingPersistence(_TablePersistence):
    """Writes raise, as during a Dynamo outage."""

    def _get_table(self):
        class _Table:
            def put_item(self, **kwargs):
                raise RuntimeError("dynamo is down")

            def get_item(self, **kwargs):
                raise RuntimeError("dynamo is down")

            def delete_item(self, Key):  # noqa: N803
                raise RuntimeError("dynamo is down")

        return _Table()


class _RevisionProjectPersistence:
    """Revision-aware route store without emulating boto3 transactions."""

    enabled = True

    def __init__(
        self,
        projects: dict[str, Project] | None = None,
        *,
        fail_writes: bool = False,
    ) -> None:
        self.projects = deepcopy(projects or {})
        self.fail_writes = fail_writes
        self.writes = 0

    async def save_project(
        self,
        project: Project,
        *,
        expected_revision: int,
    ) -> int:
        self.writes += 1
        if self.fail_writes:
            raise RuntimeError("dynamo is down")
        current = self.projects.get(project.project_id)
        if current is None or current.revision != expected_revision:
            raise PersistenceConflictError("project changed concurrently")
        committed = replace(
            deepcopy(project),
            revision=expected_revision + 1,
        )
        self.projects[project.project_id] = committed
        return committed.revision

    async def get_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> Project | None:
        project = self.projects.get(project_id)
        if project is None or project.tenant_id != tenant_id:
            return None
        return deepcopy(project)

    async def load_projects(self) -> dict[str, Project]:
        return deepcopy(self.projects)


# --------------------------------------------------------------------------
# Project membership
# --------------------------------------------------------------------------


def _admin_client(persistence, projects):
    admin_api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        projects=projects,
        persistence=persistence,
    )
    return TestClient(Starlette(routes=create_admin_routes(admin_api)))


@pytest.fixture
def members_wired(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    projects = {
        "proj-alpha": Project(
            project_id="proj-alpha",
            name="Alpha",
            members=["keep@example.com", "doomed@example.com"],
        )
    }
    persistence = _RevisionProjectPersistence(projects)
    return _admin_client(persistence, projects), persistence, projects


def _stored_members(persistence, project_id="proj-alpha"):
    """Read membership back out of the table, through the real deserializer."""
    if isinstance(persistence, _RevisionProjectPersistence):
        return persistence.projects[project_id].members
    row = next(
        (r for r in persistence.rows.values()
         if r.get("entity_type") == "project" and r.get("project_id") == project_id),
        None,
    )
    assert row is not None, "no project row was written at all"
    return DynamoPersistence.deserialize_project(row).members


class TestMembershipSurvivesARestart:
    def test_adding_a_member_reaches_the_table(self, members_wired):
        client, persistence, _ = members_wired

        resp = client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "new@example.com"},
        )
        assert resp.status_code == 200

        assert "new@example.com" in _stored_members(persistence), (
            "the handler mutated project.members and returned 200 without writing"
        )

    def test_removing_a_member_reaches_the_table(self, members_wired):
        """The direction that matters: an unpersisted removal silently restored a
        member an operator had deliberately taken off the project."""
        client, persistence, _ = members_wired

        resp = client.delete("/admin/projects/proj-alpha/members/doomed@example.com")
        assert resp.status_code == 200

        stored = _stored_members(persistence)
        assert "doomed@example.com" not in stored
        assert "keep@example.com" in stored, "the rewrite dropped an unrelated member"

    def test_a_no_op_add_writes_nothing(self, members_wired):
        """Re-adding an existing member changes nothing, so it should not spend a
        write — and must not append a duplicate."""
        client, persistence, projects = members_wired

        client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "keep@example.com"},
        )

        assert persistence.writes == 0
        assert projects["proj-alpha"].members.count("keep@example.com") == 1

    def test_a_no_op_remove_writes_nothing(self, members_wired):
        client, persistence, _ = members_wired

        client.delete("/admin/projects/proj-alpha/members/never@example.com")

        assert persistence.writes == 0

    def test_a_write_failure_fails_closed_without_changing_live_state(
        self,
        monkeypatch,
    ):
        """A failed commit cannot leak into the shared enforcement object."""
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        projects = {"proj-alpha": Project(project_id="proj-alpha", name="Alpha")}
        persistence = _RevisionProjectPersistence(
            projects,
            fail_writes=True,
        )
        client = _admin_client(persistence, projects)

        resp = client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "new@example.com"},
        )

        assert resp.status_code == 503
        assert projects["proj-alpha"].members == []
        assert persistence.projects["proj-alpha"].members == []

    def test_no_persistence_configured_still_works(self, monkeypatch):
        """The default single-node deploy has no table; the routes must not
        require one."""
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        projects = {"proj-alpha": Project(project_id="proj-alpha", name="Alpha")}
        client = _admin_client(None, projects)

        resp = client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "new@example.com"},
        )

        assert resp.status_code == 200
        assert projects["proj-alpha"].members == ["new@example.com"]


class TestProjectOptimisticConcurrency:
    def test_conflict_reloads_authoritative_project_and_allows_retry(self):
        stale = Project(
            project_id="proj-alpha",
            name="Stale",
            members=["keep@example.com"],
        )
        projects = {"proj-alpha": stale}
        persistence = _RevisionProjectPersistence(projects)
        persistence.projects["proj-alpha"] = replace(
            stale,
            name="Committed elsewhere",
            revision=1,
        )
        client = _admin_client(persistence, projects)

        conflict = client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "new@example.com"},
        )

        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "project_write_conflict"
        assert conflict.json()["error"]["revision"] == 1
        assert conflict.headers["etag"] == '"1"'
        assert projects["proj-alpha"].name == "Committed elsewhere"
        assert projects["proj-alpha"].revision == 1
        assert stale.members == ["keep@example.com"]

        retried = client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "new@example.com"},
            headers={"If-Match": '"1"'},
        )
        assert retried.status_code == 200
        assert retried.json()["revision"] == 2
        assert retried.headers["etag"] == '"2"'
        assert projects["proj-alpha"].members == [
            "keep@example.com",
            "new@example.com",
        ]

    def test_project_get_and_update_expose_revision_etags(self):
        projects = {
            "proj-alpha": Project(project_id="proj-alpha", name="Alpha")
        }
        client = _admin_client(None, projects)

        loaded = client.get("/admin/projects/proj-alpha")
        updated = client.put(
            "/admin/projects/proj-alpha",
            json={"name": "Updated"},
            headers={"If-Match": loaded.headers["etag"]},
        )
        stale = client.put(
            "/admin/projects/proj-alpha",
            json={"name": "Must not publish"},
            headers={"If-Match": loaded.headers["etag"]},
        )

        assert loaded.json()["revision"] == 0
        assert loaded.headers["etag"] == '"0"'
        assert updated.status_code == 200
        assert updated.json()["revision"] == 1
        assert updated.headers["etag"] == '"1"'
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "project_write_conflict"
        assert projects["proj-alpha"].name == "Updated"

    def test_update_outage_does_not_publish_or_register_candidate_budget(self):
        project = Project(
            project_id="proj-alpha",
            name="Alpha",
            budget_limit=10.0,
        )
        projects = {"proj-alpha": project}
        persistence = _RevisionProjectPersistence(
            projects,
            fail_writes=True,
        )
        tracker = CostTracker(pricing_config={})
        tracker.register_project("proj-alpha", budget_limit=10.0)
        api = AdminAPI(
            cost_tracker=tracker,
            health_tracker=ProviderHealthTracker(),
            model_registry=ModelRegistry(),
            projects=projects,
            persistence=persistence,
        )
        client = TestClient(Starlette(routes=create_admin_routes(api)))

        response = client.put(
            "/admin/projects/proj-alpha",
            json={"name": "Rejected", "budget_limit": 999.0},
        )

        assert response.status_code == 503
        assert projects["proj-alpha"] is project
        assert project.name == "Alpha"
        assert project.budget_limit == 10.0
        status = asyncio.run(tracker.check_budget("proj-alpha"))
        assert status.budget_limit == 10.0


class _RevisionUserConfigPersistence:
    enabled = True

    def __init__(
        self,
        configs: dict[str, dict] | None = None,
        *,
        fail_writes: bool = False,
    ) -> None:
        self.configs = deepcopy(configs or {})
        self.fail_writes = fail_writes
        self.expected_revisions: list[int] = []

    async def save_user_config(
        self,
        user_id: str,
        config: dict,
        *,
        expected_revision: int,
    ) -> int:
        self.expected_revisions.append(expected_revision)
        if self.fail_writes:
            raise RuntimeError("dynamo is down")
        current = self.configs.get(user_id, {"revision": 0})
        if current.get("revision", 0) != expected_revision:
            raise PersistenceConflictError("user config changed concurrently")
        revision = expected_revision + 1
        self.configs[user_id] = {
            **deepcopy(config),
            "revision": revision,
        }
        return revision

    async def load_user_configs_or_none(self) -> dict[str, dict]:
        return deepcopy(self.configs)


def _user_config_client(persistence, configs):
    tracker = CostTracker(pricing_config={})
    api = AdminAPI(
        cost_tracker=tracker,
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        user_configs=configs,
        persistence=persistence,
    )
    client = TestClient(Starlette(routes=create_admin_routes(api)))
    return client, api, tracker


class TestUserConfigOptimisticConcurrency:
    def test_success_publishes_revision_and_preserves_other_fields(self):
        configs = {
            "alice": {
                "allowed_models": ["model-a"],
                "budget_limit": None,
                "alert_threshold": None,
                "revision": 0,
            }
        }
        persistence = _RevisionUserConfigPersistence(configs)
        client, api, tracker = _user_config_client(persistence, configs)

        budget = client.put(
            "/admin/users/alice/budget",
            json={"budget_limit": 25.0, "alert_threshold": 20.0},
            headers={"If-Match": '"0"'},
        )
        models = client.put(
            "/admin/users/alice/allowed-models",
            json={"allowed_models": ["model-b"]},
            headers={"If-Match": budget.headers["etag"]},
        )

        assert budget.status_code == 200
        assert budget.json()["revision"] == 1
        assert models.status_code == 200
        assert models.json()["revision"] == 2
        assert persistence.expected_revisions == [0, 1]
        assert api._user_configs["alice"] == {
            "allowed_models": ["model-b"],
            "budget_limit": 25.0,
            "alert_threshold": 20.0,
            "revision": 2,
        }
        assert tracker.get_user_budget("alice")["budget_limit"] == 25.0

    def test_conflict_reloads_config_without_registering_rejected_budget(self):
        stale = {
            "allowed_models": ["model-a"],
            "budget_limit": 10.0,
            "alert_threshold": 8.0,
            "revision": 0,
        }
        configs = {"alice": deepcopy(stale)}
        persistence = _RevisionUserConfigPersistence(configs)
        persistence.configs["alice"] = {
            **stale,
            "budget_limit": 15.0,
            "alert_threshold": 12.0,
            "revision": 1,
        }
        client, api, tracker = _user_config_client(persistence, configs)
        tracker.register_user(
            "alice",
            budget_limit=stale["budget_limit"],
            alert_threshold=stale["alert_threshold"],
        )

        response = client.put(
            "/admin/users/alice/budget",
            json={"budget_limit": 999.0, "alert_threshold": 900.0},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == (
            "user_config_write_conflict"
        )
        assert response.json()["error"]["revision"] == 1
        assert response.headers["etag"] == '"1"'
        assert api._user_configs["alice"]["budget_limit"] == 15.0
        assert tracker.get_user_budget("alice") == {
            "budget_limit": 15.0,
            "alert_threshold": 12.0,
        }

    def test_outage_does_not_publish_or_register_budget(self):
        config = {
            "allowed_models": ["model-a"],
            "budget_limit": 10.0,
            "alert_threshold": 8.0,
            "revision": 3,
        }
        configs = {"alice": deepcopy(config)}
        persistence = _RevisionUserConfigPersistence(
            configs,
            fail_writes=True,
        )
        client, api, tracker = _user_config_client(persistence, configs)
        tracker.register_user(
            "alice",
            budget_limit=10.0,
            alert_threshold=8.0,
        )

        response = client.put(
            "/admin/users/alice/budget",
            json={"budget_limit": 999.0, "alert_threshold": 900.0},
        )

        assert response.status_code == 503
        assert api._user_configs["alice"] == config
        assert tracker.get_user_budget("alice") == {
            "budget_limit": 10.0,
            "alert_threshold": 8.0,
        }

    def test_allowed_models_outage_keeps_live_config(self):
        config = {
            "allowed_models": ["model-a"],
            "budget_limit": 10.0,
            "alert_threshold": 8.0,
            "revision": 3,
        }
        configs = {"alice": deepcopy(config)}
        persistence = _RevisionUserConfigPersistence(
            configs,
            fail_writes=True,
        )
        client, api, _tracker = _user_config_client(
            persistence,
            configs,
        )

        response = client.put(
            "/admin/users/alice/allowed-models",
            json={"allowed_models": ["rejected-model"]},
        )

        assert response.status_code == 503
        assert api._user_configs["alice"] == config


class _FailingTenantProjectPersistence:
    enabled = True

    async def get_project(self, project_id, tenant_id):
        assert project_id == "project-a"
        assert tenant_id == "tenant-a"
        return Project(
            project_id="project-a",
            name="Project A",
            tenant_id="tenant-a",
            allowed_models=["model-a"],
        )

    async def save_project(self, project, *, expected_revision):
        return False


class _TenantProjectRequest:
    def __init__(self, body, path_params):
        self._body = body
        self.path_params = path_params
        self.state = SimpleNamespace(
            context=RequestContext(
                user_id="admin-a",
                project_id="project-a",
                roles=["tenant_admin"],
                scopes=[],
                auth_method=AuthMethod.OIDC_JWT,
                tenant_id="tenant-a",
                principal_id="principal:admin-a",
            )
        )

    async def json(self):
        return self._body


class TestCanonicalProjectWritesFailClosed:
    @staticmethod
    def _api():
        return AdminAPI(
            cost_tracker=CostTracker(pricing_config={}),
            health_tracker=ProviderHealthTracker(),
            model_registry=ModelRegistry(),
            projects={},
            persistence=_FailingTenantProjectPersistence(),
        )

    def test_project_update_reports_durable_store_failure(self):
        response = asyncio.run(self._api().update_project(
            _TenantProjectRequest(
                {"name": "Changed"},
                {"id": "project-a"},
            )
        ))

        assert response.status_code == 503

    def test_project_model_update_reports_durable_store_failure(self):
        response = asyncio.run(self._api().add_project_model(
            _TenantProjectRequest(
                {"model": "model-b"},
                {"id": "project-a"},
            )
        ))

        assert response.status_code == 503


# --------------------------------------------------------------------------
# Event destinations (webhooks)
# --------------------------------------------------------------------------


async def _safe_webhook_resolver(hostname, port):
    return ("93.184.216.34",)


def _webhook_dispatcher():
    return EventDispatcher(
        resolver=_safe_webhook_resolver,
        aws_region="us-east-1",
        aws_account_id="123456789012",
    )


def _webhook_client(dispatcher, persistence):
    api = WebhookAPI(dispatcher=dispatcher, persistence=persistence)
    return TestClient(Starlette(routes=create_webhook_routes(api)))


@pytest.fixture
def webhooks_wired(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    dispatcher = _webhook_dispatcher()
    persistence = _TablePersistence()
    return _webhook_client(dispatcher, persistence), dispatcher, persistence


def _post_dest(client, name, dtype="webhook", **extra):
    body = {"name": name, "type": dtype,
            "config": {"url": f"https://{name}.webhook.example.com/hook"}}
    body.update(extra)
    return client.post("/admin/webhooks", json=body)


def _stored_dests(persistence):
    """Read the destination set back through the real deserializer."""
    import asyncio
    return asyncio.run(persistence.load_event_destinations())


class TestDestinationsSurviveARestart:
    def test_adding_a_destination_reaches_the_table(self, webhooks_wired):
        client, _, persistence = webhooks_wired

        assert _post_dest(client, "alerts").status_code == 201

        stored = _stored_dests(persistence)
        assert [d["name"] for d in stored] == ["alerts"]
        assert stored[0]["config"] == {
            "url": "https://alerts.webhook.example.com/hook"
        }

    def test_removing_a_destination_reaches_the_table(self, webhooks_wired):
        client, _, persistence = webhooks_wired
        _post_dest(client, "keep")
        _post_dest(client, "doomed")

        assert client.delete("/admin/webhooks/doomed").status_code == 200

        assert [d["name"] for d in _stored_dests(persistence)] == ["keep"]

    def test_removing_every_destination_stores_an_empty_set(self, webhooks_wired):
        """`[]` and "nothing saved" are different states. If a fully-drained set
        read back as None, startup would restore the seeded destinations and
        resume delivering to all of them."""
        client, _, persistence = webhooks_wired
        _post_dest(client, "only")

        client.delete("/admin/webhooks/only")

        assert _stored_dests(persistence) == []

    def test_a_missing_destination_is_a_404_and_writes_nothing(self, webhooks_wired):
        client, _, persistence = webhooks_wired
        _post_dest(client, "real")
        before = dict(persistence.rows)

        assert client.delete("/admin/webhooks/ghost").status_code == 404

        assert persistence.rows == before

    def test_an_unfiltered_destination_stays_unfiltered(self, webhooks_wired):
        """`event_filter=None` means *every* event type. DynamoDB drops None
        attributes, so without the json.dumps it would come back as a filter
        matching nothing — the destination would go silent."""
        client, _, persistence = webhooks_wired
        _post_dest(client, "catch-all")

        assert _stored_dests(persistence)[0]["event_filter"] is None

    def test_a_filtered_destination_keeps_its_filter(self, webhooks_wired):
        client, _, persistence = webhooks_wired
        _post_dest(client, "narrow", event_filter=["injection_blocked"])

        assert _stored_dests(persistence)[0]["event_filter"] == ["injection_blocked"]

    def test_a_disabled_destination_stays_disabled(self, webhooks_wired):
        client, _, persistence = webhooks_wired
        _post_dest(client, "paused", enabled=False)

        assert _stored_dests(persistence)[0]["enabled"] is False

    def test_store_failure_fails_closed_without_advancing_memory(
        self,
        monkeypatch,
    ):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        dispatcher = _webhook_dispatcher()
        persistence = _FailingPersistence()
        client = _webhook_client(dispatcher, persistence)

        response = _post_dest(client, "alerts")

        assert response.status_code == 503
        assert dispatcher.destinations == []
        assert response.json()["error"]["type"] == (
            "webhook_store_unavailable"
        )
        assert "dynamo" not in response.text.lower()

    def test_no_persistence_configured_still_works(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        dispatcher = _webhook_dispatcher()
        client = _webhook_client(dispatcher, None)

        assert _post_dest(client, "alerts").status_code == 201
        assert [d.name for d in dispatcher.destinations] == ["alerts"]


class TestReAddingADestinationReplacesIt:
    def test_a_second_post_of_the_same_name_updates_rather_than_duplicating(
        self, webhooks_wired
    ):
        """The dispatcher sends to *every* match, so an append would double-deliver
        each event, and remove-by-name would then delete only one of the pair."""
        client, dispatcher, persistence = webhooks_wired
        _post_dest(client, "alerts")

        resp = client.post("/admin/webhooks", json={
            "name": "alerts", "type": "webhook",
            "config": {"url": "https://moved.webhook.example.com/hook"},
        })

        assert resp.status_code == 200, "an update is not a creation"
        assert resp.json()["status"] == "updated"
        assert [d.name for d in dispatcher.destinations] == ["alerts"]
        stored = _stored_dests(persistence)
        assert len(stored) == 1
        assert stored[0]["config"] == {
            "url": "https://moved.webhook.example.com/hook"
        }

    def test_a_first_post_reports_created(self, webhooks_wired):
        client, _, _ = webhooks_wired
        resp = _post_dest(client, "alerts")
        assert resp.status_code == 201
        assert resp.json()["status"] == "created"


class TestApplyingPersistedDestinationsAtStartup:
    def test_a_stored_set_replaces_the_seeded_one(self):
        """Replace, not merge. A merge cannot express a deletion: a destination
        removed through the API is absent from the stored set, so merging would
        leave the seeded copy in place and it would resume receiving events."""
        dispatcher = EventDispatcher()
        dispatcher.add_destination(EventDestination(
            name="seeded", destination_type=DestinationType.WEBHOOK,
            config={"url": "https://seeded.invalid/hook"},
        ))

        _apply_persisted_destinations(dispatcher, [
            {"name": "stored", "destination_type": "webhook",
             "config": {"url": "https://stored.invalid/hook"}, "event_filter": None,
             "enabled": True},
        ])

        assert [d.name for d in dispatcher.destinations] == ["stored"]

    def test_an_empty_stored_set_clears_the_seeded_destinations(self):
        dispatcher = EventDispatcher()
        dispatcher.add_destination(EventDestination(
            name="seeded", destination_type=DestinationType.WEBHOOK, config={}))

        _apply_persisted_destinations(dispatcher, [])

        assert dispatcher.destinations == []

    def test_an_unknown_destination_type_is_skipped_not_fatal(self):
        """A row written by a newer build must not stop the gateway from booting."""
        dispatcher = EventDispatcher()

        _apply_persisted_destinations(dispatcher, [
            {"name": "future", "destination_type": "carrier-pigeon", "config": {},
             "event_filter": None, "enabled": True},
            {"name": "fine", "destination_type": "webhook", "config": {},
             "event_filter": None, "enabled": True},
        ])

        assert [d.name for d in dispatcher.destinations] == ["fine"]

    def test_a_duplicated_stored_name_installs_once(self):
        """Hand-edited rows shouldn't be able to double-deliver every event."""
        dispatcher = EventDispatcher()

        _apply_persisted_destinations(dispatcher, [
            {"name": "dup", "destination_type": "webhook", "config": {"n": 1},
             "event_filter": None, "enabled": True},
            {"name": "dup", "destination_type": "webhook", "config": {"n": 2},
             "event_filter": None, "enabled": True},
        ])

        assert [d.name for d in dispatcher.destinations] == ["dup"]
        assert dispatcher.destinations[0].config == {"n": 2}

    def test_a_round_trip_preserves_every_field(self, webhooks_wired):
        """What comes out of the table must dispatch identically to what went in."""
        client, _, persistence = webhooks_wired
        _post_dest(client, "narrow", event_filter=["injection_blocked"],
                   enabled=False)
        _post_dest(client, "sns", dtype="sns",
                   config={"topic_arn": "arn:aws:sns:us-east-1:123456789012:x"})

        fresh = EventDispatcher()
        _apply_persisted_destinations(fresh, _stored_dests(persistence))

        by_name = {d.name: d for d in fresh.destinations}
        assert set(by_name) == {"narrow", "sns"}
        assert by_name["narrow"].event_filter == ["injection_blocked"]
        assert by_name["narrow"].enabled is False
        assert by_name["sns"].destination_type is DestinationType.SNS
        assert by_name["sns"].config["topic_arn"].endswith(":x")


# --------------------------------------------------------------------------
# Multi-region topology
# --------------------------------------------------------------------------


def _hub(*spokes) -> HubConfig:
    return HubConfig(hub_region="us-east-1", spokes=list(spokes))


def _region_client(hub, persistence):
    api = RegionAPI(
        router=RegionRouter(hub_config=hub),
        monitor=SpokeHealthMonitor(hub_config=hub),
        persistence=persistence,
    )
    return TestClient(Starlette(routes=create_region_routes(api)))


@pytest.fixture
def regions_wired(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    hub = _hub(
        SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY, weight=100),
        SpokeConfig(region="eu-west-1", role=SpokeRole.ACTIVE, weight=50),
    )
    persistence = _TablePersistence()
    return _region_client(hub, persistence), hub, persistence


def _stored_topology(persistence):
    import asyncio
    return asyncio.run(persistence.load_region_topology())


class TestTopologySurvivesARestart:
    def test_adding_a_spoke_reaches_the_table(self, regions_wired):
        client, _, persistence = regions_wired

        resp = client.post("/admin/regions/spokes",
                           json={"region": "ap-south-1", "role": "active",
                                 "weight": 25})
        assert resp.status_code == 201

        stored = _stored_topology(persistence)
        assert [s["region"] for s in stored["spokes"]] == [
            "us-east-1", "eu-west-1", "ap-south-1"]

    def test_removing_a_spoke_reaches_the_table(self, regions_wired):
        """The direction that matters: an unpersisted removal put a region an
        operator had drained back into rotation at the next deploy."""
        client, _, persistence = regions_wired

        assert client.delete("/admin/regions/spokes/eu-west-1").status_code == 200

        assert [s["region"] for s in _stored_topology(persistence)["spokes"]] == [
            "us-east-1"]

    def test_updating_a_spoke_reaches_the_table(self, regions_wired):
        client, _, persistence = regions_wired

        resp = client.put("/admin/regions/spokes/eu-west-1",
                          json={"weight": 0, "role": "failover"})
        assert resp.status_code == 200

        stored = {s["region"]: s for s in _stored_topology(persistence)["spokes"]}
        assert stored["eu-west-1"]["weight"] == 0
        assert stored["eu-west-1"]["role"] == "failover"

    def test_updating_hub_config_reaches_the_table(self, regions_wired):
        client, _, persistence = regions_wired

        resp = client.put("/admin/regions/config",
                          json={"data_residency_strict": True,
                                "failover_cooldown_seconds": 999})
        assert resp.status_code == 200

        stored = _stored_topology(persistence)
        assert stored["data_residency_strict"] is True
        assert stored["failover_cooldown_seconds"] == 999

    def test_health_status_is_not_persisted(self, regions_wired):
        """A manual status override is health state, not configuration. Restoring
        a stale UNHEALTHY would hold a recovered region out of rotation; a stale
        HEALTHY would send traffic to a region that is still down."""
        client, hub, persistence = regions_wired

        resp = client.put("/admin/regions/eu-west-1/status",
                          json={"status": "unhealthy"})
        assert resp.status_code == 200
        assert hub.get_spoke("eu-west-1").status is SpokeStatus.UNHEALTHY

        assert persistence.rows == {}, "health state must not be written"

    def test_status_is_absent_from_the_serialized_row(self, regions_wired):
        """Not merely unread on the way back in — never written. A stored `status`
        is a trap for the next person to add a field to the restore path, and it
        also lets an unhealthy spoke's state leak into config via a later
        unrelated topology edit."""
        client, hub, persistence = regions_wired
        hub.get_spoke("eu-west-1").status = SpokeStatus.UNHEALTHY

        client.put("/admin/regions/config", json={"failover_cooldown_seconds": 5})

        row = persistence.rows[("REGION_TOPOLOGY", "CONFIG")]
        import json as _json
        for spoke in _json.loads(row["spokes"]):
            assert "status" not in spoke, spoke

    def test_a_rejected_spoke_writes_nothing(self, regions_wired):
        client, _, persistence = regions_wired

        assert client.post("/admin/regions/spokes",
                           json={"region": "x", "role": "nonsense"}).status_code == 400
        assert client.post("/admin/regions/spokes",
                           json={"region": "us-east-1"}).status_code == 409
        assert client.delete("/admin/regions/spokes/ghost").status_code == 404

        assert persistence.rows == {}

    def test_a_write_failure_does_not_fail_the_request(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        hub = _hub(SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY))
        persistence = _FailingPersistence()
        client = _region_client(hub, persistence)

        resp = client.post("/admin/regions/spokes", json={"region": "eu-west-1"})

        assert resp.status_code == 503
        assert [s.region for s in hub.spokes] == ["us-east-1"]
        assert persistence.last_write_error is not None

    def test_no_persistence_configured_still_works(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        hub = _hub(SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY))
        client = _region_client(hub, None)

        assert client.post("/admin/regions/spokes",
                           json={"region": "eu-west-1"}).status_code == 201
        assert [s.region for s in hub.spokes] == ["us-east-1", "eu-west-1"]

    def test_concurrent_full_set_writes_do_not_drop_a_spoke(self):
        rows: dict = {}
        persistence_a = _TablePersistence(rows)
        persistence_b = _TablePersistence(rows)
        hub_a = _hub(
            SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)
        )
        hub_b = _hub(
            SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)
        )
        client_a = _region_client(hub_a, persistence_a)
        client_b = _region_client(hub_b, persistence_b)

        assert client_a.post(
            "/admin/regions/spokes",
            json={"region": "eu-west-1"},
        ).status_code == 201

        conflict = client_b.post(
            "/admin/regions/spokes",
            json={"region": "ap-south-1"},
        )
        assert conflict.status_code == 409
        assert [spoke.region for spoke in hub_b.spokes] == [
            "us-east-1",
            "eu-west-1",
        ]

        assert client_b.post(
            "/admin/regions/spokes",
            json={"region": "ap-south-1"},
        ).status_code == 201
        assert [
            spoke["region"]
            for spoke in _stored_topology(persistence_b)["spokes"]
        ] == ["us-east-1", "eu-west-1", "ap-south-1"]


class TestApplyingPersistedTopologyAtStartup:
    def test_the_hub_config_object_is_mutated_not_replaced(self):
        """RegionRouter and SpokeHealthMonitor hold a reference to the same
        HubConfig, so rebinding would leave one of them routing on the old
        topology."""
        hub = _hub(SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY))
        router = RegionRouter(hub_config=hub)
        monitor = SpokeHealthMonitor(hub_config=hub)

        _apply_persisted_topology(hub, {
            "hub_region": "eu-west-1",
            "health_check_interval_seconds": 15,
            "failover_threshold_consecutive": 2,
            "failover_cooldown_seconds": 45,
            "data_residency_strict": True,
            "spokes": [{"region": "eu-west-1", "role": "primary", "weight": 100}],
        })

        assert [s.region for s in router.config.spokes] == ["eu-west-1"]
        assert [s.region for s in monitor.config.spokes] == ["eu-west-1"]
        assert router.config.hub_region == "eu-west-1"
        assert router.config.failover_cooldown_seconds == 45

    def test_a_stored_topology_replaces_the_config_file_spokes(self):
        """Replace, not merge — a merge would resurrect every spoke an operator
        had removed through the API, which is the bug this persistence fixes."""
        hub = _hub(
            SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY),
            SpokeConfig(region="removed-by-operator", role=SpokeRole.ACTIVE),
        )

        _apply_persisted_topology(hub, {
            "hub_region": "us-east-1",
            "health_check_interval_seconds": 30,
            "failover_threshold_consecutive": 3,
            "failover_cooldown_seconds": 60,
            "data_residency_strict": False,
            "spokes": [{"region": "us-east-1", "role": "primary", "weight": 100}],
        })

        assert [s.region for s in hub.spokes] == ["us-east-1"]

    def test_restored_spokes_start_at_the_default_status(self):
        """Not the status they had when written — the first health check decides."""
        hub = _hub()

        _apply_persisted_topology(hub, {
            "hub_region": "us-east-1",
            "health_check_interval_seconds": 30,
            "failover_threshold_consecutive": 3,
            "failover_cooldown_seconds": 60,
            "data_residency_strict": False,
            "spokes": [{"region": "us-east-1", "role": "primary", "weight": 100}],
        })

        assert hub.spokes[0].status is SpokeStatus.HEALTHY

    def test_an_unknown_role_rejects_the_snapshot_atomically(self):
        hub = _hub(
            SpokeConfig(region="original", role=SpokeRole.PRIMARY),
        )

        with pytest.raises(ValueError, match="unknown role"):
            _apply_persisted_topology(hub, {
                "hub_region": "eu-west-1",
                "health_check_interval_seconds": 15,
                "failover_threshold_consecutive": 2,
                "failover_cooldown_seconds": 45,
                "data_residency_strict": True,
                "spokes": [
                    {"region": "weird", "role": "sideways", "weight": 10},
                    {"region": "eu-west-1", "role": "primary", "weight": 100},
                ],
            })

        assert hub.hub_region == "us-east-1"
        assert [s.region for s in hub.spokes] == ["original"]
        assert hub.health_check_interval_seconds == 30
        assert hub.failover_threshold_consecutive == 3
        assert hub.failover_cooldown_seconds == 60
        assert hub.data_residency_strict is False

    def test_a_round_trip_preserves_every_routable_field(self, regions_wired):
        """What comes back out of the table must route identically to what went
        in — the restart half of the guarantee."""
        client, hub, persistence = regions_wired
        client.put("/admin/regions/spokes/eu-west-1", json={
            "weight": 30, "role": "failover", "failover_priority": 2,
            "data_residency_zones": ["eu"], "providers": ["bedrock"],
            "models": ["claude-sonnet"],
        })

        fresh = _hub()
        _apply_persisted_topology(fresh, _stored_topology(persistence))

        spoke = fresh.get_spoke("eu-west-1")
        assert spoke.weight == 30
        assert spoke.role is SpokeRole.FAILOVER
        assert spoke.failover_priority == 2
        assert spoke.data_residency_zones == ["eu"]
        assert spoke.providers == ["bedrock"]
        assert spoke.models == ["claude-sonnet"]

    def test_an_emptied_topology_is_distinguishable_from_nothing_saved(
        self, regions_wired
    ):
        """A topology saved with every spoke removed must read back as a real
        (empty) topology, not None — otherwise startup silently restores
        spokes.yaml and traffic goes to regions nobody configured."""
        client, _, persistence = regions_wired
        client.delete("/admin/regions/spokes/us-east-1")
        client.delete("/admin/regions/spokes/eu-west-1")

        stored = _stored_topology(persistence)
        assert stored is not None
        assert stored["spokes"] == []

    def test_nothing_saved_reads_back_as_none(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        assert _stored_topology(_TablePersistence()) is None


class TestEmptyIsNotTheSameAsUnset:
    """`[]` is falsy, so the difference between "an operator removed everything"
    and "nothing was ever saved" is one keystroke wide in `bootstrap`. Getting it
    wrong restores the seed over a deliberate removal — the resurrection bug this
    whole change exists to prevent — so the boundary is asserted directly.
    """

    def _seeded(self):
        dispatcher = EventDispatcher()
        dispatcher.add_destination(EventDestination(
            name="seeded", destination_type=DestinationType.WEBHOOK, config={}))
        hub = _hub(SpokeConfig(region="seeded-region", role=SpokeRole.PRIMARY))
        return dispatcher, hub

    def test_an_empty_stored_destination_set_clears_the_seed(self):
        dispatcher, hub = self._seeded()

        _apply_persisted_infrastructure(dispatcher, hub, [], None)

        assert dispatcher.destinations == [], (
            "an emptied set was treated as 'nothing saved' and the seeded "
            "destination resumed receiving security events"
        )

    def test_an_empty_stored_spoke_list_clears_the_seed(self):
        dispatcher, hub = self._seeded()

        _apply_persisted_infrastructure(dispatcher, hub, None, {
            "hub_region": "us-east-1",
            "health_check_interval_seconds": 30,
            "failover_threshold_consecutive": 3,
            "failover_cooldown_seconds": 60,
            "data_residency_strict": False,
            "spokes": [],
        })

        assert hub.spokes == []

    def test_none_leaves_the_seed_alone(self):
        """No stored state at all is the clean-install case: seeded destinations
        and spokes.yaml are all there is, and must be left intact."""
        dispatcher, hub = self._seeded()

        _apply_persisted_infrastructure(dispatcher, hub, None, None)

        assert [d.name for d in dispatcher.destinations] == ["seeded"]
        assert [s.region for s in hub.spokes] == ["seeded-region"]
