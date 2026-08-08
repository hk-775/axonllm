"""A project or user config written on one task must bind the others.

``self.projects`` and ``self._user_configs`` were hydrated once at startup and
then only mutated by whichever task served the write. Behind the shipped
``desired_count=2`` that makes enforcement a coin flip, and unlike a stale count
it is not cosmetic — both dicts *gate* requests:

    in the store: {'alice': {'allowed_models': ['claude-haiku']}}
    task A, alice asks for claude-opus: 403 model_not_allowed
    task B, alice asks for claude-opus: 200 routed

    in the store: ['acme']
    task A chat for acme: project resolved, limit=$100.0
    task B chat for acme: project unknown -> no budget gate

An unresolved project is not an error; it means no budget limit, no
allowed-models list, and no rate limit. So the failure direction is open.

The mechanism is the same shared version counter the Cedar fix uses. Two
properties matter more than freshness, and are tested hardest:

* a failed config scan must never be adopted, because the empty result would
  clear every budget limit and model restriction the fleet is enforcing;
* adopting the dicts is not the same as arming enforcement — limits live in
  ``cost_tracker._user_budgets`` / ``._budgets``, which no dict update touches,
  so a refresh that skipped registration would display a limit nothing checks.
  That is the #89 bug reintroduced on a 5-second timer.

The fourth class covers a data-loss bug found while writing these: because
``save_user_config`` is a whole-item ``put_item`` and ``set_user_budget`` mutated
a throwaway dict from ``.get(user_id, {})``, setting a budget and then an
allowed-models list erased the budget from the table.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.gateway.admin.routes import AdminAPI
from src.gateway.chat.client_agent import ClientAgent
from src.gateway.config_sync import ConfigSyncService
from src.gateway.cost_tracker import CostTracker
from src.gateway.models import TokenPricing, UsageRecord
from src.gateway.persistence import DynamoPersistence
from tests.unit.test_persistence_cas_foundations import (
    _CasDynamoClient,
    _CasTable,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeTable:
    """One dict as the table, with the atomic ADD the version counter needs.

    ``broken`` names entity types whose scan raises, and ``broken_writes`` whose
    ``put_item`` does. Both fail at the *boto3* boundary rather than by stubbing a
    persistence method, which matters: ``load_projects`` catches its own
    exceptions and returns ``{}`` after setting a flag, so a test that replaces
    ``load_projects`` with something that raises exercises a path the real outage
    never takes — and passes whether or not the fix is there.
    """

    def __init__(self, rows: dict, broken=(), broken_writes=()) -> None:
        self._rows = rows
        self._broken = set(broken)
        self._broken_writes = set(broken_writes)

    def put_item(  # noqa: N803 — boto3's parameter names
        self,
        Item,
        ConditionExpression=None,
        ExpressionAttributeValues=None,
    ):
        if Item.get("entity_type") in self._broken_writes:
            raise RuntimeError(f"write of {Item.get('entity_type')} failed")
        if ConditionExpression and (Item["PK"], Item["SK"]) in self._rows:
            raise RuntimeError("conditional write rejected")
        self._rows[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._rows.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def update_item(
        self, Key, UpdateExpression, ExpressionAttributeNames,  # noqa: N803
        ExpressionAttributeValues, ReturnValues,  # noqa: N803
    ):
        """``ADD`` on a missing item creates it at the added value, as DynamoDB does."""
        row = self._rows.setdefault((Key["PK"], Key["SK"]), dict(Key))
        attr = next(iter(ExpressionAttributeNames.values()))
        delta = next(iter(ExpressionAttributeValues.values()))
        row[attr] = row.get(attr, Decimal("0")) + delta
        return {"Attributes": {attr: row[attr]}}

    def scan(self, FilterExpression=None, ExclusiveStartKey=None):  # noqa: N803
        wanted = FilterExpression.get_expression()["values"][1]
        if wanted in self._broken:
            raise RuntimeError(f"scan of {wanted} timed out")
        return {"Items": [
            dict(r) for r in self._rows.values() if r.get("entity_type") == wanted
        ]}


class _SharedCasClient(_CasDynamoClient):
    """CAS interpreter that keeps both test instances on one row dict."""

    def __init__(self, rows: dict, broken_writes: set[str]) -> None:
        super().__init__()
        self.rows = rows
        self._broken_writes = broken_writes

    def transact_write_items(self, **request) -> None:
        for operation in request["TransactItems"]:
            if "Put" not in operation:
                continue
            item = self.decode(operation["Put"]["Item"])
            if item.get("entity_type") in self._broken_writes:
                raise RuntimeError(
                    f"write of {item.get('entity_type')} failed"
                )

        shared = self.rows
        super().transact_write_items(**request)
        committed = self.rows
        if committed is not shared:
            shared.clear()
            shared.update(committed)
            self.rows = shared


class _ConfigCasTable(_CasTable):
    def __init__(
        self,
        client: _SharedCasClient,
        broken: set[str],
    ) -> None:
        super().__init__(client)
        self._broken = broken

    def scan(self, *, FilterExpression, **kwargs):  # noqa: N803
        wanted = FilterExpression.get_expression()["values"][1]
        if wanted in self._broken:
            raise RuntimeError(f"scan of {wanted} timed out")
        with self._client._lock:
            return {
                "Items": [
                    dict(row)
                    for row in self._client.rows.values()
                    if row.get("entity_type") == wanted
                ]
            }

    def update_item(
        self,
        *,
        Key,  # noqa: N803
        UpdateExpression,  # noqa: N803
        ExpressionAttributeNames,  # noqa: N803
        ExpressionAttributeValues,  # noqa: N803
        ReturnValues,  # noqa: N803
    ):
        del ReturnValues
        key = (Key["PK"], Key["SK"])
        with self._client._lock:
            row = self._client.rows.setdefault(key, dict(Key))
            if "ADD #spend :cost" in UpdateExpression:
                row["entity_type"] = ExpressionAttributeValues[
                    ":entity_type"
                ]
                row["budget_scope"] = ExpressionAttributeValues[":scope"]
                row.setdefault("epoch", ExpressionAttributeValues[":zero"])
                row["spend"] = (
                    row.get("spend", 0)
                    + ExpressionAttributeValues[":cost"]
                )
                return {
                    "Attributes": {
                        "epoch": row["epoch"],
                        "spend": row["spend"],
                    }
                }

            assert UpdateExpression.startswith("ADD ")
            attribute = next(iter(ExpressionAttributeNames.values()))
            delta = next(iter(ExpressionAttributeValues.values()))
            row[attribute] = row.get(attribute, 0) + delta
            return {"Attributes": {attribute: row[attribute]}}


class _Store(DynamoPersistence):
    """The real persistence class with only the boto3 table replaced.

    Counts scans so the version gate can be asserted on rather than assumed.
    """

    def __init__(self, rows: dict) -> None:
        super().__init__()
        self._enabled = True
        self.rows = rows
        self.project_scans = 0
        self.user_config_scans = 0
        self.version_reads = 0
        # Entity types whose scan / write fails, so an outage can be injected at
        # the boto3 boundary instead of by stubbing out a persistence method.
        self.broken: set[str] = set()
        self.broken_writes: set[str] = set()
        self._client = _SharedCasClient(rows, self.broken_writes)
        self._table = _ConfigCasTable(self._client, self.broken)

    def _get_table(self):
        return self._table

    async def load_projects(self):
        self.project_scans += 1
        return await super().load_projects()

    async def load_user_configs(self):
        self.user_config_scans += 1
        return await super().load_user_configs()

    async def get_config_version(self):
        self.version_reads += 1
        return await super().get_config_version()


class _Agent:
    """Stands in for GatewayAgent, holding the two dicts the request path reads.

    ``resolve`` and ``may_use`` are the two checks the real agent makes
    (``agent.py`` steps 4 and 5), reduced to what the outcome depends on.
    """

    def __init__(self, cost_tracker, projects: dict, user_configs: dict) -> None:
        self.cost_tracker = cost_tracker
        self._projects = projects
        self._user_configs = user_configs

    def resolve(self, project_id: str):
        return self._projects.get(project_id)

    def may_use(self, user_id: str, model: str) -> str:
        allowed = self._user_configs.get(user_id, {}).get("allowed_models")
        if allowed and model not in allowed:
            return "403 model_not_allowed"
        return "200 routed"


class _Instance:
    """One Fargate task, wired the way ``build_starlette_app`` wires it.

    The dicts are passed to the sync, the admin API and the agent as the *same*
    objects, because that sharing is the entire mechanism — a rebind anywhere puts
    the converged view in something nobody reads.
    """

    def __init__(self, rows: dict) -> None:
        self.store = _Store(rows)
        self.tracker = CostTracker(
            pricing_config={"anthropic": {"m": TokenPricing(
                prompt_token_cost=0.003, completion_token_cost=0.015)}},
            persistence=self.store,
        )
        self.projects: dict = {}
        self.user_configs: dict = {}
        self.sync = ConfigSyncService(
            projects=self.projects,
            user_configs=self.user_configs,
            cost_tracker=self.tracker,
            persistence=self.store,
        )
        self.agent = _Agent(self.tracker, self.projects, self.user_configs)
        self.api = AdminAPI(
            cost_tracker=self.tracker,
            health_tracker=None,
            model_registry=None,
            projects=self.projects,
            user_configs=self.user_configs,
            persistence=self.store,
            config_sync=self.sync,
        )
        self.client = ClientAgent(self.agent, default_user_id="chat-user")

    # --- writes, through the real handlers ---

    def create_project(self, project_id: str, **body):
        return _run(self.api.create_project(
            _Req({"project_id": project_id, "name": project_id, **body})))

    def update_project(self, project_id: str, **body):
        return _run(self.api.update_project(_Req(body, {"id": project_id})))

    def set_budget(self, user_id: str, limit, threshold=None):
        return _run(self.api.set_user_budget(_Req(
            {"budget_limit": limit, "alert_threshold": threshold}, {"id": user_id})))

    def set_allowed_models(self, user_id: str, models):
        return _run(self.api.set_user_allowed_models(
            _Req({"allowed_models": models}, {"id": user_id})))

    # --- the request path ---

    def request(self) -> None:
        """What AuthMiddleware does before any handler reads the config."""
        self.expire_ttl()
        _run(self.sync.refresh_if_stale())

    def may_use(self, user_id: str, model: str) -> str:
        self.request()
        return self.agent.may_use(user_id, model)

    def resolve(self, project_id: str):
        self.request()
        return self.agent.resolve(project_id)

    def enforced_limit(self, project_id: str):
        """The limit budget enforcement will actually compare against."""
        self.request()
        return _run(self.tracker.check_budget(project_id)).budget_limit

    def enforced_user_limit(self, user_id: str):
        self.request()
        return _run(self.tracker.check_user_budget(user_id)).budget_limit

    def expire_ttl(self) -> None:
        """Drive the clock by moving the recorded check, not by sleeping."""
        self.sync._last_version_check = float("-inf")
        self.tracker._last_usage_sync = float("-inf")

    def listed_projects(self) -> list[str]:
        body = _run(self.api.list_projects(None))
        return [p["project_id"] for p in json.loads(body.body)]

    def listed_users(self) -> dict[str, float | None]:
        body = _run(self.api.list_users(None))
        return {u["user_id"]: u["budget_limit"] for u in json.loads(body.body)}

    def api_users(self) -> list[str]:
        return _run(self.client.get_available_users())

    def serve_request_for(self, user_id: str, cost: float = 0.001) -> None:
        _run(self.tracker.record_usage(UsageRecord(
            request_id=f"req-{user_id}-{cost}", project_id="acme", user_id=user_id,
            model="m", provider="anthropic", prompt_tokens=1, completion_tokens=1,
            total_tokens=2, cost=cost,
            timestamp=datetime(2026, 8, 7, tzinfo=UTC),
        )))


class _Req:
    def __init__(self, body: dict, path_params: dict | None = None) -> None:
        self._body = body
        self.query_params: dict = {}
        self.path_params: dict = path_params or {}

    async def json(self) -> dict:
        return self._body


@pytest.fixture
def fleet():
    """Two instances over one table — the whole condition."""
    rows: dict = {}
    return _Instance(rows), _Instance(rows)


class TestAConfigWrittenOnOneTaskBindsTheOthers:
    """The reported divergence, reproduced without AWS."""

    def test_a_per_user_model_restriction_reaches_the_other_task(self, fleet):
        a, b = fleet
        assert b.may_use("alice", "claude-opus") == "200 routed", (
            "nothing is restricted yet; the test would pass vacuously"
        )
        a.set_allowed_models("alice", ["claude-haiku"])

        assert a.agent.may_use("alice", "claude-opus") == "403 model_not_allowed"
        assert b.may_use("alice", "claude-opus") == "403 model_not_allowed", (
            "the restriction bound only the task that served the write"
        )
        # And it still permits what it should.
        assert b.may_use("alice", "claude-haiku") == "200 routed"

    def test_a_project_created_on_one_task_resolves_on_the_other(self, fleet):
        a, b = fleet
        assert b.resolve("acme") is None
        a.create_project("acme", budget_limit=100.0)

        resolved = b.resolve("acme")
        assert resolved is not None, (
            "an unresolved project means no budget gate, no allowed-models list, "
            "and no rate limit — the failure is open"
        )
        assert resolved.budget_limit == 100.0

    def test_a_project_edit_reaches_the_other_task(self, fleet):
        """Not just creation: the old scan-and-``setdefault`` missed updates.

        ``_all_projects`` merged with ``setdefault``, so once an instance had seen
        a project it kept its own stale copy forever. Lowering a budget was the
        case that mattered — the tighter limit never arrived.
        """
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        assert b.resolve("acme").budget_limit == 100.0

        a.update_project("acme", budget_limit=10.0)
        assert b.resolve("acme").budget_limit == 10.0, (
            "the other task kept the looser limit it already knew"
        )

    def test_a_user_budget_set_on_one_task_is_enforced_by_the_other(self, fleet):
        a, b = fleet
        a.set_budget("alice", 1.0, 0.8)
        assert b.enforced_user_limit("alice") == 1.0

    def test_api_users_lists_the_fleets_users(self, fleet):
        """``GET /api/users`` read this process's records, so the selector split."""
        a, b = fleet
        a.serve_request_for("alice")
        b.serve_request_for("bob")
        a.expire_ttl()
        b.expire_ttl()

        assert a.api_users() == ["alice", "bob", "chat-user"]
        assert b.api_users() == ["alice", "bob", "chat-user"]

    def test_the_admin_user_list_shows_the_fleets_budgets(self, fleet):
        a, b = fleet
        a.serve_request_for("alice")
        a.set_budget("alice", 50.0)
        b.expire_ttl()
        assert b.listed_users() == {"alice": 50.0}

    def test_the_admin_project_list_shows_the_fleets_projects(self, fleet):
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.expire_ttl()
        assert b.listed_projects() == ["acme"]


class TestAdoptingTheConfigArmsEnforcement:
    """The #89 trap, which a refresh could reintroduce on a 5-second timer.

    Limits live in ``cost_tracker._budgets`` / ``._user_budgets``, which no dict
    update touches. A refresh that only updated the dicts would show the operator
    their new limit on every page while nothing compared spend against it.
    """

    def test_an_adopted_project_budget_is_actually_checked(self, fleet):
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        assert b.enforced_limit("acme") == 100.0, (
            "the dict was adopted but the tracker was never told"
        )

    def test_an_adopted_user_budget_is_actually_checked(self, fleet):
        a, b = fleet
        a.set_budget("alice", 25.0, 20.0)
        assert b.enforced_user_limit("alice") == 25.0

    def test_a_cleared_limit_is_adopted_as_cleared(self, fleet):
        """None is a deliberate operator state, not "nothing was saved"."""
        a, b = fleet
        a.set_budget("alice", 25.0)
        assert b.enforced_user_limit("alice") == 25.0
        a.set_budget("alice", None)
        assert b.enforced_user_limit("alice") is None

    def test_the_refresh_does_not_touch_the_spend_counters(self, fleet):
        """Spend has one owner, and a read path must not be it.

        ``_bump_spend_fleet_wide`` already keeps the counters fleet-wide. Writing
        to them from a config refresh is how ``GET /admin/projects`` reopened a
        closed budget gate in #87.
        """
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.serve_request_for("bob", cost=150.0)
        before = b.tracker._project_spend.get("acme")

        b.request()
        assert b.tracker._project_spend.get("acme") == before
        # And the gate stays shut.
        assert _run(b.tracker.check_budget("acme")).is_over_budget is True


class TestTheRefreshCannotOpenAHole:
    """Three ways this fix could be worse than the bug it fixes."""

    def test_a_failed_project_scan_is_not_adopted(self, fleet):
        """``load_projects`` returns ``{}`` on failure, and ``{}`` is a valid answer.

        That is correct at startup, where the alternative is refusing to boot. On a
        live refresh it converts one timed-out scan into a fleet-wide loss of every
        budget gate, which is why the sync uses the ``_or_none`` variant.
        """
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.request()
        assert b.resolve("acme") is not None

        a.create_project("second", budget_limit=50.0)  # move the version
        b.store.broken.add("project")
        b.expire_ttl()
        assert _run(b.sync.refresh_if_stale()) is False
        assert b.agent.resolve("acme") is not None, (
            "one timed-out scan cleared the fleet's projects, so every request "
            "lost its budget gate"
        )
        assert _run(b.tracker.check_budget("acme")).budget_limit == 100.0

    def test_a_failed_user_config_scan_is_not_adopted(self, fleet):
        a, b = fleet
        a.set_allowed_models("alice", ["claude-haiku"])
        assert b.may_use("alice", "claude-opus") == "403 model_not_allowed"

        a.set_allowed_models("carol", ["claude-haiku"])  # move the version
        b.store.broken.add("user_config")
        b.expire_ttl()
        assert _run(b.sync.refresh_if_stale()) is False
        assert b.agent.may_use("alice", "claude-opus") == "403 model_not_allowed", (
            "a failed scan un-enforced every model restriction in the fleet"
        )

    def test_one_failed_scan_of_the_pair_discards_both(self, fleet):
        """Half a config is not a config.

        The two scans are one logical read. Adopting the project half while the
        user half failed would leave the instance enforcing a state no operator
        ever wrote.
        """
        a, b = fleet
        a.set_allowed_models("alice", ["claude-haiku"])
        b.request()
        a.create_project("acme", budget_limit=100.0)

        b.store.broken.add("user_config")
        b.expire_ttl()
        assert _run(b.sync.refresh_if_stale()) is False
        assert b.agent.resolve("acme") is None, "adopted half a config"
        assert b.agent.may_use("alice", "claude-opus") == "403 model_not_allowed"

    def test_a_failed_scan_is_retried_rather_than_cached(self, fleet):
        """The version is not recorded on a failed adopt, so the next request retries."""
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)

        b.store.broken.add("project")
        b.expire_ttl()
        _run(b.sync.refresh_if_stale())
        assert b.agent.resolve("acme") is None

        b.store.broken.discard("project")
        b.expire_ttl()
        _run(b.sync.refresh_if_stale())
        assert b.agent.resolve("acme") is not None, (
            "the failure was cached, so the outage cost a full window after recovery"
        )

    def test_a_failed_write_does_not_bump_the_version(self, fleet):
        """Otherwise the fleet reloads and reports success for a change that never landed."""
        a, b = fleet
        before = _run(b.store.get_config_version())

        a.store.broken_writes.add("project")
        a.create_project("ghost", budget_limit=5.0)

        assert _run(b.store.get_config_version()) == before, (
            "the version moved for a project that is not in the table"
        )
        assert "ghost" not in _run(b.store.load_projects())

    def test_a_failed_user_config_write_does_not_bump_the_version(self, fleet):
        a, b = fleet
        before = _run(b.store.get_config_version())

        a.store.broken_writes.add("user_config")
        a.set_budget("alice", 10.0)

        assert _run(b.store.get_config_version()) == before

    def test_the_seeded_config_survives_a_refresh(self, fleet):
        """Seed-file projects and users are not in DynamoDB.

        Replacing rather than merging would silently drop every seeded one — the
        same bug the Cedar refresh had, where adopting the stored set
        un-enforced a seeded ``forbid``.
        """
        rows: dict = {}
        seeded = _Instance(rows)
        seeded.projects["seed-proj"] = _project("seed-proj", 5.0)
        seeded.user_configs["seed-user"] = {"allowed_models": ["claude-haiku"]}

        other = _Instance(rows)
        other.create_project("acme", budget_limit=100.0)

        seeded.request()
        assert sorted(seeded.projects) == ["acme", "seed-proj"], (
            "adopting the stored set dropped the seeded project"
        )
        assert seeded.agent.may_use("seed-user", "claude-opus") == "403 model_not_allowed"

    def test_a_stored_entry_wins_over_the_local_one(self, fleet):
        """The other half of the merge: stored is newer, so it must not lose.

        A ``setdefault``-style merge is what made project *edits* invisible.
        """
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.request()
        a.update_project("acme", budget_limit=1.0)
        assert b.resolve("acme").budget_limit == 1.0


class TestTheWindowBoundsTheReadsNotTheCorrectness:
    """Cost. The refresh must not become two table scans per request."""

    def test_repeated_requests_inside_the_window_do_not_rescan(self, fleet):
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.expire_ttl()
        _run(b.sync.refresh_if_stale())
        scans = (b.store.project_scans, b.store.user_config_scans)

        for _ in range(20):
            _run(b.sync.refresh_if_stale())
        assert (b.store.project_scans, b.store.user_config_scans) == scans

    def test_an_unchanged_version_costs_one_getitem_and_no_scan(self, fleet):
        """The steady state: nothing is being written, so nothing should be scanned."""
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.expire_ttl()
        _run(b.sync.refresh_if_stale())
        scans = (b.store.project_scans, b.store.user_config_scans)
        reads = b.store.version_reads

        b.expire_ttl()
        _run(b.sync.refresh_if_stale())
        assert b.store.version_reads == reads + 1, "the counter should be re-read"
        assert (b.store.project_scans, b.store.user_config_scans) == scans, (
            "an unchanged version must not trigger a scan"
        )

    def test_concurrent_requests_share_one_refresh(self, fleet):
        """The TTL check straddles an await, so it cannot gate on its own."""
        a, b = fleet
        a.create_project("acme", budget_limit=100.0)
        b.expire_ttl()

        async def eight_at_once():
            await asyncio.gather(*(b.sync.refresh_if_stale() for _ in range(8)))

        _run(eight_at_once())
        assert b.store.project_scans == 1, (
            f"expected one coalesced scan, got {b.store.project_scans}"
        )

    def test_an_unreadable_version_does_not_advance_the_clock(self, fleet):
        """An outage must not buy a full window of divergence."""
        a, b = fleet

        async def boom():
            return None

        b.store.get_config_version = boom
        b.expire_ttl()
        _run(b.sync.refresh_if_stale())
        assert b.sync._last_version_check == float("-inf"), (
            "a failed read recorded a successful check"
        )

    def test_an_absent_counter_reads_as_zero_not_unreadable(self, fleet):
        """A seed-only deployment must not poll on every request forever.

        ``get_config_version`` returning None for an absent row would mean the
        clock never advances, because only a successful read advances it.
        """
        a, _ = fleet
        assert _run(a.store.get_config_version()) == 0

        a.expire_ttl()
        _run(a.sync.refresh_if_stale())
        assert a.sync._last_version_check != float("-inf")

    def test_the_writer_reloads_before_acknowledging_its_own_write(self, fleet):
        a, _ = fleet
        a.create_project("acme", budget_limit=100.0)
        scans = a.store.project_scans

        a.expire_ttl()
        _run(a.sync.refresh_if_stale())
        assert a.store.project_scans == scans + 1, (
            "the writer acknowledged a counter it had not loaded"
        )


class TestSettingOneFieldDoesNotEraseAnother:
    """``save_user_config`` is a whole-item ``put_item``, so a partial dict deletes.

    ``set_user_budget`` built its config from ``.get(user_id, {})``, which returns
    a *throwaway* dict on a miss — so the limits went to DynamoDB and never into
    ``_user_configs``. The next write for that user then serialized a config with
    no budget in it:

        after the budget write, store:  {'budget_limit': 100.0, ...}
        after the budget write, local:  {}
        after the models write, store:  {'allowed_models': ['claude-haiku']}

    Restart rehydrates from that row, so the limit was gone for good.
    """

    def test_setting_allowed_models_keeps_the_budget(self, fleet):
        a, _ = fleet
        a.set_budget("alice", 100.0, 80.0)
        a.set_allowed_models("alice", ["claude-haiku"])

        stored = _run(a.store.load_user_configs())["alice"]
        assert stored["budget_limit"] == 100.0, "the budget was erased from the table"
        assert stored["alert_threshold"] == 80.0
        assert stored["allowed_models"] == ["claude-haiku"]

    def test_setting_a_budget_keeps_the_allowed_models(self, fleet):
        a, _ = fleet
        a.set_allowed_models("alice", ["claude-haiku"])
        a.set_budget("alice", 100.0, 80.0)

        stored = _run(a.store.load_user_configs())["alice"]
        assert stored["allowed_models"] == ["claude-haiku"]
        assert stored["budget_limit"] == 100.0

    def test_the_budget_lands_in_the_local_dict_too(self, fleet):
        """The dict is what the request path reads, so a write that skips it is lost."""
        a, _ = fleet
        a.set_budget("alice", 100.0, 80.0)
        assert a.user_configs["alice"]["budget_limit"] == 100.0

    def test_both_fields_survive_a_round_trip_to_the_other_task(self, fleet):
        a, b = fleet
        a.set_budget("alice", 100.0, 80.0)
        a.set_allowed_models("alice", ["claude-haiku"])

        assert b.enforced_user_limit("alice") == 100.0
        assert b.agent.may_use("alice", "claude-opus") == "403 model_not_allowed"


class TestTheRouteItselfStillWorks:
    """``GET /api/users`` through the real handler, not just the agent method.

    ``ChatAPI.list_users`` had no test, which is how making
    ``get_available_users`` async could have shipped a route returning a
    coroutine. The handler catches every exception and answers 500, so the
    failure would have been a silently empty user selector rather than a crash.
    """

    def test_the_endpoint_returns_the_fleets_users(self, fleet):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from src.gateway.chat.routes import ChatAPI, create_chat_routes

        a, b = fleet
        a.serve_request_for("alice")
        b.serve_request_for("bob")
        a.expire_ttl()

        app = Starlette(routes=create_chat_routes(ChatAPI(a.client)))
        response = TestClient(app).get("/api/users")

        assert response.status_code == 200, response.text
        assert response.json() == ["alice", "bob", "chat-user"]


class TestPersistenceOffIsUnchanged:
    """Single-instance and no-DynamoDB deployments must behave exactly as before."""

    def test_the_sync_never_polls_without_persistence(self):
        tracker = CostTracker(pricing_config={})
        sync = ConfigSyncService(
            projects={}, user_configs={}, cost_tracker=tracker, persistence=None)
        assert _run(sync.refresh_if_stale()) is False

    def test_a_disabled_store_never_polls(self):
        rows: dict = {}
        store = _Store(rows)
        store._enabled = False
        tracker = CostTracker(pricing_config={}, persistence=store)
        sync = ConfigSyncService(
            projects={}, user_configs={}, cost_tracker=tracker, persistence=store)
        assert _run(sync.refresh_if_stale()) is False
        assert store.version_reads == 0

    def test_writes_still_work_with_no_sync_wired(self):
        """``config_sync`` is optional, so every existing caller constructs unchanged."""
        rows: dict = {}
        store = _Store(rows)
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}, persistence=store),
            health_tracker=None,
            model_registry=None,
            projects={},
            user_configs={},
            persistence=store,
        )
        _run(api.create_project(_Req({"project_id": "acme", "name": "Acme"})))
        assert "acme" in _run(store.load_projects())

    def test_the_project_list_still_falls_back_to_the_scan(self):
        """Without a sync, ``_all_projects`` keeps its old read-through behaviour."""
        rows: dict = {}
        writer = _Instance(rows)
        writer.create_project("acme", budget_limit=100.0)

        store = _Store(rows)
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}, persistence=store),
            health_tracker=None,
            model_registry=None,
            projects={},
            user_configs={},
            persistence=store,
        )
        body = _run(api.list_projects(None))
        assert [p["project_id"] for p in json.loads(body.body)] == ["acme"]

    def test_api_users_still_answers_without_persistence(self):
        tracker = CostTracker(pricing_config={})
        agent = _Agent(tracker, {}, {"alice": {}})
        client = ClientAgent(agent, default_user_id="chat-user")
        assert _run(client.get_available_users()) == ["alice", "chat-user"]


class TestSharedStateIsActuallyShared:
    """The falsy-container class of bug, pinned on the objects this fix depends on.

    ``x or {}`` substitutes a new object whenever the caller passes an empty one,
    which is every gateway that boots without seed data. Every assertion here is
    identity, not equality: converging into a copy is the failure.
    """

    def test_the_sync_mutates_the_dicts_it_was_given(self, fleet):
        a, _ = fleet
        assert a.sync._projects is a.projects
        assert a.sync._user_configs is a.user_configs

    def test_the_admin_api_holds_the_same_dicts(self, fleet):
        a, _ = fleet
        assert a.api.projects is a.projects
        assert a.api._user_configs is a.user_configs

    def test_the_agent_holds_the_same_dicts(self, fleet):
        a, _ = fleet
        assert a.agent._projects is a.projects
        assert a.agent._user_configs is a.user_configs

    def test_a_refresh_is_visible_through_every_holder(self, fleet):
        a, b = fleet
        b.create_project("acme", budget_limit=100.0)
        a.request()
        assert "acme" in a.projects
        assert "acme" in a.api.projects
        assert "acme" in a.agent._projects


def _project(project_id: str, budget_limit: float):
    from src.gateway.models import Project

    return Project(project_id=project_id, name=project_id, budget_limit=budget_limit)
