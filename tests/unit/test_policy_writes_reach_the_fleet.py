"""A policy written on one task must govern requests served by the others.

Cedar statements are compiled once, at startup and on ``POST /admin/policies``.
The write recompiled only the instance that served it, so behind the shipped
``desired_count=2`` an operator's ``forbid`` was enforced by one task and ignored
by the other — per request, decided by the load balancer:

    task A denies DELETE: DENY
    task B denies DELETE: ALLOW
    task B's policy list: []
    in the store: ['no-delete']

This is the same shape as the admin-read divergence fixed in #87, but it fails
open on an authorization control rather than misreporting a number, and it is not
self-correcting: the policy is in the table, so a restart fixes it and nothing
before a restart does.

The mechanism is a shared version counter rather than a scan per request. Writes
``ADD 1`` to it; readers do one small ``GetItem`` per instance per window and only
re-scan the policy table when the number actually moved. Two properties matter
more than freshness and are tested hardest:

* a failed scan must never be adopted as an empty policy set — that converts one
  timed-out read into a fleet-wide authorization bypass;
* a failed *write* must not bump the version, or every other instance reloads and
  reports success for a set that does not contain the policy.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.gateway.admin.routes import AdminAPI
from src.gateway.auth.cedar_policy import CedarPolicyService
from src.gateway.cost_tracker import CostTracker
from src.gateway.models import AuthMethod, RequestContext
from src.gateway.persistence import DynamoPersistence

FORBID_WRITES = 'forbid(principal, action == Action::"write", resource);'
PERMIT_READS = 'permit(principal, action == Action::"read", resource);'


def _run(coro):
    return asyncio.run(coro)


class _FakeTable:
    """One dict as the table, with the atomic ADD the version counter needs."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def put_item(self, Item):  # noqa: N803 — boto3's parameter name
        self._rows[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._rows.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def delete_item(self, Key):  # noqa: N803
        self._rows.pop((Key["PK"], Key["SK"]), None)

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
        return {"Items": [
            dict(r) for r in self._rows.values() if r.get("entity_type") == wanted
        ]}


class _Store(DynamoPersistence):
    """The real persistence class with only the boto3 table replaced."""

    def __init__(self, rows: dict) -> None:
        super().__init__()
        self._enabled = True
        self.rows = rows

    def _get_table(self):
        return _FakeTable(self.rows)


class _Instance:
    """One task: its own policy list, evaluator, and admin API over a shared table.

    The list is passed to both, because that sharing is what makes a reload
    visible to ``GET /admin/policies`` — and it was silently broken by
    ``policies or []`` whenever the list was empty.
    """

    def __init__(self, rows: dict) -> None:
        self.store = _Store(rows)
        self.policies: list[dict] = []
        self.service = CedarPolicyService(self.policies, persistence=self.store)
        self.api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}),
            health_tracker=None,
            model_registry=None,
            projects={},
            policies=self.policies,
            persistence=self.store,
            policy_service=self.service,
        )

    def write(self, name: str, text: str, mode: str = "ENFORCE"):
        return _run(self.api.create_policy(
            _Req({"name": name, "policy_text": text, "mode": mode})))

    def decide(self, method: str = "DELETE", roles=("viewer",)) -> str:
        ctx = RequestContext(
            user_id="bob", project_id="p", roles=list(roles), scopes=[],
            auth_method=AuthMethod.API_KEY,
        )
        return _run(self.service.evaluate(ctx, method, "/admin/projects/x"))

    def poll_then_decide(self, method: str = "DELETE") -> str:
        """What the middleware does: adopt any remote change, then evaluate."""
        self.expire_ttl()
        _run(self.service.refresh_if_stale())
        return self.decide(method)

    def expire_ttl(self) -> None:
        """Drive the clock by moving the recorded check, not by sleeping."""
        self.service._last_version_check = float("-inf")

    def listed(self) -> list[str]:
        import json
        self.expire_ttl()
        body = _run(self.api.list_policies(None))
        return [p["name"] for p in json.loads(body.body)]


class _Req:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.query_params: dict = {}
        self.path_params: dict = {}

    async def json(self) -> dict:
        return self._body


@pytest.fixture
def fleet():
    """Two instances over one table — the whole condition."""
    rows: dict = {}
    return _Instance(rows), _Instance(rows)


class TestAPolicyWrittenOnOneTaskGovernsTheOthers:
    """The reported divergence, reproduced without AWS."""

    def test_the_other_task_adopts_a_forbid(self, fleet):
        a, b = fleet
        assert b.decide() == "ALLOW", "nothing is governed yet; test proves nothing"
        a.write("no-writes", FORBID_WRITES)

        assert a.decide() == "DENY"
        assert b.poll_then_decide() == "DENY", (
            "the policy was compiled only on the task that served the write"
        )

    def test_the_other_task_sees_it_in_the_listing(self, fleet):
        """An operator checking their work must not be told it is missing.

        Worth its own test: the evaluator could converge while
        ``GET /admin/policies`` still answers from the un-refreshed local list,
        which reads as "the write failed" and invites writing it again.
        """
        a, b = fleet
        a.write("no-writes", FORBID_WRITES)
        assert b.listed() == ["no-writes"]

    def test_a_removed_governing_policy_stops_denying(self, fleet):
        """Convergence has to work in the permissive direction too.

        Otherwise an operator who rolls back a mistaken ``forbid`` finds half the
        fleet still refusing requests, with nothing in the policy list to explain
        it.
        """
        a, b = fleet
        a.write("no-writes", FORBID_WRITES)
        assert b.poll_then_decide() == "DENY"

        # Rolled back by overwriting the statement with a harmless one, which is
        # what update-by-name does.
        a.write("no-writes", PERMIT_READS)
        assert b.poll_then_decide() == "ALLOW"

    def test_the_writer_does_not_re_scan_its_own_change(self, fleet):
        """The write already knows the answer; re-reading it is pure cost."""
        a, _ = fleet
        a.write("no-writes", FORBID_WRITES)
        scans = {"n": 0}
        original = a.store.load_all_cedar_policies_or_none

        async def counted():
            scans["n"] += 1
            return await original()

        a.store.load_all_cedar_policies_or_none = counted
        a.expire_ttl()
        _run(a.service.refresh_if_stale())
        assert scans["n"] == 0, "the writing instance re-scanned to learn its own write"


class TestTheRefreshCannotOpenAHole:
    """The two ways this fix could be worse than the bug."""

    def test_a_failed_scan_does_not_drop_the_enforced_set(self, fleet):
        """One timed-out read must not become a fleet-wide bypass.

        ``load_all_cedar_policies`` returns ``[]`` on failure so a Dynamo outage
        cannot block startup. Adopting that during a live reload would silently
        un-enforce every policy, which is why the reload path uses the
        ``_or_none`` variant.
        """
        a, b = fleet
        a.write("no-writes", FORBID_WRITES)
        assert b.poll_then_decide() == "DENY"

        async def boom():
            raise RuntimeError("scan timed out")

        b.store.load_all_cedar_policies = boom
        a.write("second", PERMIT_READS)  # moves the version, so b will try to reload
        assert b.poll_then_decide() == "DENY", (
            "a failed policy scan was adopted as an empty policy set"
        )

    def test_a_failed_scan_is_retried_rather_than_cached(self, fleet):
        """The known version only advances on a set actually adopted."""
        a, b = fleet
        a.write("no-writes", FORBID_WRITES)
        b.poll_then_decide()

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            raise RuntimeError("scan timed out")

        b.store.load_all_cedar_policies = flaky
        a.write("second", PERMIT_READS)
        b.poll_then_decide()
        b.poll_then_decide()
        assert calls["n"] == 2, "a failed reload was cached as though it succeeded"

    def test_an_unreadable_version_keeps_the_current_set(self, fleet):
        """Cannot-read is not no-policies."""
        a, b = fleet
        a.write("no-writes", FORBID_WRITES)
        assert b.poll_then_decide() == "DENY"

        async def boom():
            raise RuntimeError("dynamo down")

        b.store.get_policy_version = boom
        assert b.poll_then_decide() == "DENY"

    def test_a_failed_write_does_not_bump_the_version(self):
        """Otherwise the fleet reloads and reports success for a set without it.

        Every other instance would re-scan, find the old set, log an adoption at
        the new version, and be *further* from correct than before — while the
        operator's 201 said the policy exists.
        """
        rows: dict = {}
        store = _Store(rows)
        before = _run(store.get_policy_version())

        async def boom(_policy):
            raise RuntimeError("dynamo down")

        class _FailingTable(_FakeTable):
            def put_item(self, Item):  # noqa: N803
                raise RuntimeError("dynamo down")

        store._get_table = lambda: _FailingTable(rows)
        _run(store.save_cedar_policy(
            {"name": "no-writes", "policy_text": FORBID_WRITES, "mode": "ENFORCE"}))
        assert _run(store.get_policy_version()) == before

    def test_adopting_the_stored_set_does_not_drop_seeded_policies(self):
        """Only API-written policies are in DynamoDB; seeded ones are not.

        So a refresh that replaces the local set with the stored one silently
        un-enforces every policy from demo_seed.yaml — including a seeded forbid —
        the first time anyone POSTs an unrelated policy anywhere in the fleet:

            booted with: ['seed-guard']
            after adopting the fleet set: ['other']
            does the seeded forbid still apply? ALLOW

        Bootstrap already merges seed and stored by name; the reload has to agree.
        """
        rows: dict = {}
        store = _Store(rows)
        seeded = [{"name": "seed-guard", "policy_text": FORBID_WRITES,
                   "mode": "ENFORCE"}]
        service = CedarPolicyService(seeded, persistence=store)

        # Another task writes something unrelated through the API.
        _run(store.save_cedar_policy(
            {"name": "other", "policy_text": PERMIT_READS, "mode": "ENFORCE"}))

        service._last_version_check = float("-inf")
        assert _run(service.refresh_if_stale()) is True
        assert sorted(p["name"] for p in service._policies) == ["other", "seed-guard"]

        ctx = RequestContext(
            user_id="bob", project_id="p", roles=["viewer"], scopes=[],
            auth_method=AuthMethod.API_KEY,
        )
        assert _run(service.evaluate(ctx, "DELETE", "/x")) == "DENY", (
            "the seeded forbid was dropped by adopting the stored set"
        )

    def test_a_stored_policy_replaces_the_seeded_one_of_the_same_name(self):
        """Update-by-name is the identity everywhere else; it holds here too."""
        rows: dict = {}
        store = _Store(rows)
        service = CedarPolicyService(
            [{"name": "guard", "policy_text": FORBID_WRITES, "mode": "ENFORCE"}],
            persistence=store,
        )
        _run(store.save_cedar_policy(
            {"name": "guard", "policy_text": PERMIT_READS, "mode": "ENFORCE"}))

        service._last_version_check = float("-inf")
        _run(service.refresh_if_stale())
        assert len(service._policies) == 1
        ctx = RequestContext(
            user_id="bob", project_id="p", roles=["viewer"], scopes=[],
            auth_method=AuthMethod.API_KEY,
        )
        assert _run(service.evaluate(ctx, "DELETE", "/x")) == "ALLOW"

    def test_a_gateway_with_no_stored_policies_stops_polling(self, fleet):
        """An absent version counter is 0 — a known state, not an unreadable one.

        Reporting None for "nothing written yet" would mean the clock never
        advances, so a deployment that only uses seed-file policies would read
        this counter on every request forever.
        """
        _, b = fleet
        b.expire_ttl()
        _run(b.service.refresh_if_stale())

        reads = {"n": 0}
        original = b.store.get_policy_version

        async def counted():
            reads["n"] += 1
            return await original()

        b.store.get_policy_version = counted
        _run(b.service.refresh_if_stale())
        assert reads["n"] == 0, "an empty store never records a successful check"

    def test_concurrent_requests_share_one_version_read(self, fleet):
        """The TTL check straddles an await, so it cannot gate on its own."""
        a, b = fleet
        a.write("no-writes", FORBID_WRITES)
        reads = {"n": 0}
        original = b.store.get_policy_version

        async def counted():
            reads["n"] += 1
            return await original()

        b.store.get_policy_version = counted
        b.expire_ttl()

        async def eight_at_once():
            await asyncio.gather(*(b.service.refresh_if_stale() for _ in range(8)))

        asyncio.run(eight_at_once())
        assert reads["n"] == 1, f"expected one coalesced read, got {reads['n']}"

    def test_the_window_bounds_the_reads_not_the_correctness(self, fleet):
        """Inside the window an instance serves what it has; that is the trade.

        ``poll_then_decide`` expires the TTL, so this drives the clock the other
        way — a fresh check means the next request must not read the counter
        again, and the stale answer it serves is the documented 5-second window.
        """
        a, b = fleet
        b.poll_then_decide()  # b records a version check at "now"
        a.write("no-writes", FORBID_WRITES)

        reads = {"n": 0}
        original = b.store.get_policy_version

        async def counted():
            reads["n"] += 1
            return await original()

        b.store.get_policy_version = counted
        assert _run(b.service.refresh_if_stale()) is False
        assert reads["n"] == 0, "the TTL did not suppress the read"
        assert b.decide() == "ALLOW"
        # And it converges as soon as the window passes.
        assert b.poll_then_decide() == "DENY"


class TestPersistenceOffIsUnchanged:
    """Single-instance and local-dev deployments must not poll anything."""

    def test_no_persistence_means_no_refresh(self):
        service = CedarPolicyService([])
        assert _run(service.refresh_if_stale()) is False

    def test_a_local_write_still_takes_effect_immediately(self):
        policies: list[dict] = []
        service = CedarPolicyService(policies, persistence=None)
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}), health_tracker=None,
            model_registry=None, projects={}, policies=policies,
            persistence=None, policy_service=service,
        )
        _run(api.create_policy(_Req(
            {"name": "no-writes", "policy_text": FORBID_WRITES, "mode": "ENFORCE"})))
        ctx = RequestContext(
            user_id="bob", project_id="p", roles=["viewer"], scopes=[],
            auth_method=AuthMethod.API_KEY,
        )
        assert _run(service.evaluate(ctx, "DELETE", "/x")) == "DENY"


class TestSharedStateIsActuallyShared:
    """``x or {}`` substitutes a new object when the caller passes an empty one.

    Found while fixing the above: the evaluator refreshed into the list it was
    given, and ``GET /admin/policies`` kept answering from a different one. The
    same expression was applied to the projects and user-config dicts, where the
    consequence is larger — those are shared with ``GatewayAgent``, so a project
    created through the API was invisible to the request path until a restart. It
    only bit gateways that boot with no seed data, i.e. every production one.
    """

    def test_the_policy_list_is_the_callers_list(self):
        policies: list[dict] = []
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}), health_tracker=None,
            model_registry=None, projects={}, policies=policies, persistence=None,
        )
        assert api.policies is policies

    def test_an_empty_projects_dict_is_still_shared(self):
        """The consequence: a project created via the API reaches the request path.

        ``GatewayAgent`` looks its project up in this same dict, so a copy here
        means `POST /admin/projects` returns 201 for a project that no chat
        request can resolve until the process restarts.
        """
        projects: dict = {}
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}), health_tracker=None,
            model_registry=None, projects=projects, persistence=None,
        )
        _run(api.create_project(_Req({"project_id": "acme", "name": "Acme"})))
        assert "acme" in projects, (
            "the admin API wrote to a copy; the request path would not see it"
        )

    def test_the_agent_shares_an_empty_projects_dict(self):
        """The other half of the same expression, on the reading side."""
        from src.gateway.agent import GatewayAgent

        projects: dict = {}
        user_configs: dict = {}
        agent = GatewayAgent(
            router=None, rate_limiter=None, guardrail_engine=None,
            cache_manager=None, cost_tracker=CostTracker(pricing_config={}),
            projects=projects, user_configs=user_configs,
        )
        assert agent._projects is projects
        assert agent._user_configs is user_configs

    def test_an_empty_user_configs_dict_is_still_shared(self):
        user_configs: dict = {}
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}), health_tracker=None,
            model_registry=None, projects={}, user_configs=user_configs,
            persistence=None,
        )
        assert api._user_configs is user_configs

    def test_a_populated_container_is_shared_too(self):
        """The old form worked here, which is why the bug survived: it is only
        wrong for the falsy case, and the seeded path is the one people test."""
        projects = {"seed": object()}
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}), health_tracker=None,
            model_registry=None, projects=projects, persistence=None,
        )
        assert api.projects is projects
