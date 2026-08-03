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
from src.gateway.models import Project
from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
)
from src.gateway.multi_region.region_router import RegionRouter
from src.gateway.persistence import DynamoPersistence
from src.gateway.security.event_dispatcher import (
    DestinationType,
    EventDestination,
    EventDispatcher,
)


class _FakeTable:
    """One dict standing in for the table, keyed the way DynamoDB is."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def put_item(self, Item):  # noqa: N803 — boto3's parameter name
        self._rows[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):  # noqa: N803
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


class _FailingPersistence(_TablePersistence):
    """Writes raise, as during a Dynamo outage."""

    def _get_table(self):
        class _Table:
            def put_item(self, Item):  # noqa: N803
                raise RuntimeError("dynamo is down")

            def get_item(self, Key):  # noqa: N803
                raise RuntimeError("dynamo is down")

            def delete_item(self, Key):  # noqa: N803
                raise RuntimeError("dynamo is down")

        return _Table()


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
    persistence = _TablePersistence()
    projects = {
        "proj-alpha": Project(
            project_id="proj-alpha",
            name="Alpha",
            members=["keep@example.com", "doomed@example.com"],
        )
    }
    return _admin_client(persistence, projects), persistence, projects


def _stored_members(persistence, project_id="proj-alpha"):
    """Read membership back out of the table, through the real deserializer."""
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

        assert persistence.rows == {}
        assert projects["proj-alpha"].members.count("keep@example.com") == 1

    def test_a_no_op_remove_writes_nothing(self, members_wired):
        client, persistence, _ = members_wired

        client.delete("/admin/projects/proj-alpha/members/never@example.com")

        assert persistence.rows == {}

    def test_a_write_failure_does_not_fail_the_request(self, monkeypatch):
        """The in-memory change already succeeded and the caller cannot undo it,
        so a 500 here would be a lie in the other direction. `last_write_error` is
        what surfaces the drop to a health probe."""
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        persistence = _FailingPersistence()
        projects = {"proj-alpha": Project(project_id="proj-alpha", name="Alpha")}
        client = _admin_client(persistence, projects)

        resp = client.post(
            "/admin/projects/proj-alpha/members",
            json={"user_id": "new@example.com"},
        )

        assert resp.status_code == 200
        assert projects["proj-alpha"].members == ["new@example.com"]
        assert persistence.last_write_error is not None

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


# --------------------------------------------------------------------------
# Event destinations (webhooks)
# --------------------------------------------------------------------------


def _webhook_client(dispatcher, persistence):
    api = WebhookAPI(dispatcher=dispatcher, persistence=persistence)
    return TestClient(Starlette(routes=create_webhook_routes(api)))


@pytest.fixture
def webhooks_wired(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    dispatcher = EventDispatcher()
    persistence = _TablePersistence()
    return _webhook_client(dispatcher, persistence), dispatcher, persistence


def _post_dest(client, name, dtype="webhook", **extra):
    body = {"name": name, "type": dtype,
            "config": {"url": f"https://{name}.invalid/hook"}}
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
        assert stored[0]["config"] == {"url": "https://alerts.invalid/hook"}

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

    def test_a_write_failure_does_not_fail_the_request(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        dispatcher = EventDispatcher()
        persistence = _FailingPersistence()
        client = _webhook_client(dispatcher, persistence)

        assert _post_dest(client, "alerts").status_code == 201
        assert [d.name for d in dispatcher.destinations] == ["alerts"]
        assert persistence.last_write_error is not None

    def test_no_persistence_configured_still_works(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        dispatcher = EventDispatcher()
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
            "config": {"url": "https://moved.invalid/hook"},
        })

        assert resp.status_code == 200, "an update is not a creation"
        assert resp.json()["status"] == "updated"
        assert [d.name for d in dispatcher.destinations] == ["alerts"]
        stored = _stored_dests(persistence)
        assert len(stored) == 1
        assert stored[0]["config"] == {"url": "https://moved.invalid/hook"}

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
                   config={"topic_arn": "arn:aws:sns:us-east-1:000000000000:x"})

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

        assert resp.status_code == 201
        assert [s.region for s in hub.spokes] == ["us-east-1", "eu-west-1"]
        assert persistence.last_write_error is not None

    def test_no_persistence_configured_still_works(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        hub = _hub(SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY))
        client = _region_client(hub, None)

        assert client.post("/admin/regions/spokes",
                           json={"region": "eu-west-1"}).status_code == 201
        assert [s.region for s in hub.spokes] == ["us-east-1", "eu-west-1"]


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

    def test_an_unknown_role_is_skipped_not_fatal(self):
        hub = _hub()

        _apply_persisted_topology(hub, {
            "hub_region": "us-east-1",
            "health_check_interval_seconds": 30,
            "failover_threshold_consecutive": 3,
            "failover_cooldown_seconds": 60,
            "data_residency_strict": False,
            "spokes": [
                {"region": "weird", "role": "sideways", "weight": 10},
                {"region": "us-east-1", "role": "primary", "weight": 100},
            ],
        })

        assert [s.region for s in hub.spokes] == ["us-east-1"]

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
