"""Admin reads describe the fleet, not whichever task the balancer picked.

Found by running the documented AWS install and polling one endpoint: on a
two-task Fargate deployment ``GET /admin/overview`` alternated ``total_cost``
between ``0.000132`` and ``0`` on identical authenticated requests. Both answers
were honest — the record was written to DynamoDB, but each task aggregated an
in-memory list hydrated once at startup, so only the task that served the chat
counted it.

#86 documented that divergence and pinned it. This file now pins the fix, so the
tests that asserted "these disagree" are gone rather than inverted: the condition
they described is no longer the intended behaviour, and leaving them as xfail
would imply the divergence is still a state we ship.

Two sources, deliberately not one — the split is the design and the tests are
arranged to catch a regression that collapses it:

* **money** reads the shared ``SPEND#`` counter per call, so it is exact,
  unaffected by the sync window, and survives ``MAX_RECORDS`` trimming.
* **counts and per-model/user aggregates** have no shared counter, so they come
  from a refreshed record list, rate-limited by ``USAGE_SYNC_TTL_SECONDS``.

The trackers here are two objects over one fake table, because "two processes,
one store" is the whole condition; a single instance cannot exhibit any of this.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.gateway.admin.routes import AdminAPI
from src.gateway.cost_tracker import CostTracker
from src.gateway.models import Project, TokenPricing, UsageRecord

_README = Path(__file__).resolve().parents[2] / "README.md"


def _run(coro):
    return asyncio.run(coro)


class SharedStore:
    """One table shared by every instance in a test, standing in for DynamoDB.

    ``load_usage_records`` returns records written by *any* instance, which is
    what the real scan does and what makes the fleet-wide read possible.
    ``scans`` counts them so the TTL can be asserted on rather than assumed.
    """

    def __init__(self) -> None:
        self.enabled = True
        self.records: list[UsageRecord] = []
        self.totals: dict[tuple[str, str], float] = {}
        self.scans = 0
        self.spend_reads = 0

    async def save_usage_record(self, record) -> None:
        self.records.append(record)

    async def load_usage_records(self) -> list[UsageRecord]:
        self.scans += 1
        return list(self.records)

    async def load_usage_records_or_none(self) -> list[UsageRecord] | None:
        """The variant the sync uses: None on failure, so an outage is not cached.

        Mirrors the real one's contract rather than aliasing it, so a test that
        makes the scan raise gets ``None`` here instead of an exception escaping
        into the handler.
        """
        try:
            return await self.load_usage_records()
        except Exception:
            return None

    async def add_spend(self, scope: str, ident: str, cost: float) -> float | None:
        total = self.totals.get((scope, ident), 0.0) + cost
        self.totals[(scope, ident)] = total
        return total

    async def get_spend(self, scope: str, ident: str) -> float | None:
        self.spend_reads += 1
        return self.totals.get((scope, ident))

    # AdminAPI._all_projects scans for projects created by another instance.
    async def load_projects(self) -> dict:
        return {}


def _record(**kw) -> UsageRecord:
    """A usage record with the fields these aggregates actually read."""
    defaults = dict(
        request_id="req_1",
        project_id="my-project",
        user_id="u1",
        model="claude-sonnet",
        provider="anthropic",
        prompt_tokens=8,
        completion_tokens=23,
        total_tokens=31,
        cost=0.000132,
        timestamp=datetime(2026, 8, 6, 15, 56, 39, tzinfo=UTC),
    )
    defaults.update(kw)
    return UsageRecord(**defaults)


def _tracker(store) -> CostTracker:
    return CostTracker(
        pricing_config={"anthropic": {"claude-sonnet": TokenPricing(
            prompt_token_cost=0.003, completion_token_cost=0.015)}},
        persistence=store,
    )


def _admin(tracker, store, projects=None) -> AdminAPI:
    """An AdminAPI over one tracker, with only what these endpoints touch."""
    return AdminAPI(
        cost_tracker=tracker,
        health_tracker=None,
        model_registry=None,
        projects=projects or {},
        persistence=store,
    )


class TestOneRecordIsVisibleFromEveryInstance:
    """The live symptom, reproduced without AWS — and now fixed."""

    def test_a_record_billed_on_one_instance_is_counted_by_the_other(self):
        """This is the flapping ``total_cost``, now stable.

        Task A serves the request; task B never sees it in-process. B's
        ``/admin/overview`` must still report it, because the record is in the
        store and the read goes there.
        """
        store = SharedStore()
        task_a, task_b = _tracker(store), _tracker(store)
        _run(task_a.record_usage(_record()))

        body = _run(_admin(task_b, store).overview(None))
        payload = _json(body)
        assert payload["total_cost"] == pytest.approx(0.000132)
        assert payload["total_requests"] == 1

    def test_both_instances_report_the_same_totals(self):
        """Neither task is privileged: the answer must not depend on the route.

        Asserting equality rather than a literal, since the defect was
        *disagreement* — two instances that are both wrong in the same way would
        at least not mislead an operator into blaming their client.
        """
        store = SharedStore()
        task_a, task_b = _tracker(store), _tracker(store)
        _run(task_a.record_usage(_record(request_id="r_a", cost=0.01)))
        _run(task_b.record_usage(_record(request_id="r_b", cost=0.02)))

        a = _json(_run(_admin(task_a, store).overview(None)))
        b = _json(_json_reset(store, task_b, _admin(task_b, store)))
        assert a["total_cost"] == pytest.approx(b["total_cost"])
        assert a["total_requests"] == b["total_requests"] == 2

    def test_request_counts_converge_too(self):
        """``total_requests`` has no shared counter, so it relies on the sync.

        Worth its own test: cost could be right via the counter while counts stay
        per-instance, which is the half-fix this is meant to rule out.
        """
        store = SharedStore()
        task_a, task_b = _tracker(store), _tracker(store)
        for i in range(3):
            _run(task_a.record_usage(_record(request_id=f"r{i}")))

        assert _json(_run(_admin(task_b, store).overview(None)))["total_requests"] == 3


class TestMoneyDoesNotDependOnTheSyncWindow:
    """Cost reads the shared counter, so it is exact rather than TTL-fresh."""

    def test_project_spend_comes_from_the_counter_not_the_records(self):
        """The records are deliberately withheld from the store here.

        Only the counter has the spend, so a ``current_spend`` that is right can
        only have come from the counter — this fails if someone "simplifies" the
        cost path into summing records.
        """
        store = SharedStore()
        tracker = _tracker(store)
        store.totals[("project", "my-project")] = 4.25

        api = _admin(tracker, store, {"my-project": Project(
            project_id="my-project", name="Mine", budget_limit=10.0)})
        row = _json(_run(api.list_projects(None)))[0]
        assert row["current_spend"] == pytest.approx(4.25)
        assert row["budget_utilization_pct"] == pytest.approx(42.5)

    def test_spend_survives_record_trimming(self):
        """The ceiling that makes summing records wrong even when it is fresh.

        ``record_usage`` trims to ``MAX_RECORDS // 2`` past the cap, so on a busy
        deployment the oldest history is simply gone. A summed total then
        under-reports permanently; the counter does not.
        """
        store = SharedStore()
        tracker = _tracker(store)
        tracker.MAX_RECORDS = 4
        for i in range(6):
            _run(tracker.record_usage(_record(request_id=f"r{i}", cost=1.0)))

        assert len(tracker._records) <= 4, "trim did not happen; test proves nothing"
        api = _admin(tracker, store, {"my-project": Project(
            project_id="my-project", name="Mine", budget_limit=100.0)})
        row = _json(_run(api.list_projects(None)))[0]
        assert row["current_spend"] == pytest.approx(6.0), (
            "spend was summed from the trimmed record list rather than read "
            "from the shared counter"
        )

    def test_an_unreadable_counter_falls_back_rather_than_reporting_zero(self):
        """A DynamoDB blip must not make a spending project look free.

        Reporting $0 for a project at its limit is the one failure mode worse
        than staleness, because it invites raising a budget that is already spent.
        """
        store = SharedStore()

        async def boom(scope, ident):
            raise RuntimeError("dynamo down")

        store.get_spend = boom
        tracker = _tracker(store)
        tracker._project_spend["my-project"] = 7.5

        api = _admin(tracker, store, {"my-project": Project(
            project_id="my-project", name="Mine", budget_limit=10.0)})
        row = _json(_run(api.list_projects(None)))[0]
        assert row["current_spend"] == pytest.approx(7.5)


class TestTheSyncIsRateLimited:
    """The scan is paged and grows with history; the dashboard polls every 3s."""

    def test_repeated_reads_within_the_window_scan_once(self):
        store = SharedStore()
        api = _admin(_tracker(store), store)
        for _ in range(5):
            _run(api.overview(None))
        assert store.scans == 1, f"expected one scan inside the TTL, got {store.scans}"

    def test_the_window_expiring_allows_another_scan(self):
        """Drives the clock by moving the recorded timestamp, not by sleeping."""
        store = SharedStore()
        api = _admin(_tracker(store), store)
        _run(api.overview(None))
        api._last_usage_sync -= api.USAGE_SYNC_TTL_SECONDS + 1
        _run(api.overview(None))
        assert store.scans == 2

    def test_a_failed_sync_is_retried_on_the_next_read(self):
        """The clock advances only on success.

        Otherwise one failure would serve one-instance numbers for a full TTL
        after the store recovered, which is the original bug on a timer.
        """
        store = SharedStore()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            raise RuntimeError("scan failed")

        store.load_usage_records = flaky
        api = _admin(_tracker(store), store)
        _run(api.overview(None))
        _run(api.overview(None))
        assert calls["n"] == 2, "a failed sync was cached as though it succeeded"

    def test_concurrent_reads_share_one_scan(self):
        """The TTL check straddles an await, so it cannot gate on its own.

        Eight dashboard panels loading together would each pass the check before
        any of them recorded a sync, turning one refresh into eight full table
        scans. The single-flight task is what makes the TTL mean what it says.
        """
        store = SharedStore()
        api = _admin(_tracker(store), store)

        async def eight_at_once():
            await asyncio.gather(*(api.overview(None) for _ in range(8)))

        _run(eight_at_once())
        assert store.scans == 1, f"expected one coalesced scan, got {store.scans}"

    def test_the_first_read_always_syncs(self):
        """Guards the ``-inf`` sentinel.

        ``time.monotonic()`` has an arbitrary origin and can be near zero early
        in a process, so a ``0`` sentinel would skip the very first sync — the
        one an operator is most likely to be looking at after a deploy.
        """
        store = SharedStore()
        api = _admin(_tracker(store), store)
        assert api._last_usage_sync == float("-inf")
        _run(api.overview(None))
        assert store.scans == 1


class TestTheSyncDoesNotCorruptBudgets:
    """The trap in this fix: refreshing must not re-count other instances' spend."""

    def test_syncing_leaves_the_spend_counters_alone(self):
        """``load_records`` bumps counters; the sync must not.

        ``_bump_spend_fleet_wide`` already *replaced* the local counter with the
        fleet total, so folding the fleet's records in again would double-count
        and start refusing requests under a budget the project has not reached.
        """
        store = SharedStore()
        task_a, task_b = _tracker(store), _tracker(store)
        _run(task_a.record_usage(_record(request_id="r_a", cost=1.0)))
        _run(task_b.record_usage(_record(request_id="r_b", cost=1.0)))

        # B billed $1 and adopted the $2 fleet total from the shared counter.
        assert task_b._project_spend["my-project"] == pytest.approx(2.0)

        _run(task_b.sync_records_from_store())
        assert task_b._project_spend["my-project"] == pytest.approx(2.0), (
            "the sync folded fleet records into the counter on top of the fleet "
            "total it already held — budgets will now block early"
        )

    def test_budget_checks_still_see_the_fleet_total(self):
        """The counter stays authoritative for enforcement after a sync."""
        store = SharedStore()
        task_a, task_b = _tracker(store), _tracker(store)
        task_b.register_project("my-project", budget_limit=5.0)
        _run(task_a.record_usage(_record(request_id="r_a", cost=3.0)))
        _run(task_b.record_usage(_record(request_id="r_b", cost=1.0)))
        _run(task_b.sync_records_from_store())

        status = _run(task_b.check_budget("my-project"))
        assert status.current_spend == pytest.approx(4.0)

    def test_locally_served_records_are_not_lost_by_a_sync(self):
        """A record whose store write failed must survive the refresh.

        The sync merges rather than replaces, so a dropped write degrades to
        "visible on one instance" instead of vanishing from the instance that
        served it.
        """
        store = SharedStore()

        async def drop(record):
            pass

        store.save_usage_record = drop
        tracker = _tracker(store)
        _run(tracker.record_usage(_record(request_id="only_here")))
        _run(tracker.sync_records_from_store())
        assert [r.request_id for r in tracker._records] == ["only_here"]

    def test_a_record_is_not_counted_twice_across_syncs(self):
        store = SharedStore()
        tracker = _tracker(store)
        _run(tracker.record_usage(_record()))
        for _ in range(3):
            _run(tracker.sync_records_from_store())
        assert len(tracker._records) == 1

    def test_reading_the_dashboard_does_not_reopen_a_closed_budget(self):
        """An admin read is a read. This one wasn't, and it unblocked spending.

        The first cut of ``fleet_spend`` cached its answer into
        ``_project_spend`` — the live enforcement counter — and the store
        returned ``0.0`` for a project with no counter row yet. Loading
        ``/admin/projects`` therefore zeroed the spend of every over-budget
        project and let requests through until the next bill landed.
        """
        store = SharedStore()
        tracker = _tracker(store)
        tracker.register_project("my-project", budget_limit=100.0)
        tracker._project_spend["my-project"] = 120.0
        assert _run(tracker.check_budget("my-project")).is_over_budget is True

        api = _admin(tracker, store, {"my-project": Project(
            project_id="my-project", name="Mine", budget_limit=100.0)})
        _run(api.list_projects(None))

        assert tracker._project_spend["my-project"] == pytest.approx(120.0)
        assert _run(tracker.check_budget("my-project")).is_over_budget is True, (
            "an admin read wrote to the enforcement counter and reopened the gate"
        )

    def test_an_absent_counter_is_not_reported_as_zero_spend(self):
        """"No row yet" and "spent nothing" need different answers.

        The store returns None for a missing counter so the caller can fall back
        to its own number; returning ``0.0`` made every not-yet-persisted project
        look free.
        """
        store = SharedStore()
        tracker = _tracker(store)
        tracker._project_spend["my-project"] = 3.0
        assert _run(store.get_spend("project", "my-project")) is None

        api = _admin(tracker, store, {"my-project": Project(
            project_id="my-project", name="Mine", budget_limit=10.0)})
        row = _json(_run(api.list_projects(None)))[0]
        assert row["current_spend"] == pytest.approx(3.0)

    def test_the_sync_trims_the_scanned_side_not_the_merged_list(self):
        """Which side gets trimmed decides whether local-only records survive.

        Trimming after the merge cuts from the head of a list whose head is the
        local records — exactly what the dedupe exists to keep — and makes the
        count oscillate every window with no traffic at all.
        """
        store = SharedStore()
        tracker = _tracker(store)
        tracker.MAX_RECORDS = 4

        async def drop(record):
            pass

        store.save_usage_record = drop
        _run(tracker.record_usage(_record(request_id="local_only")))
        store.save_usage_record = store.__class__.save_usage_record.__get__(store)

        base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        store.records = [
            _record(request_id=f"fleet{i}", timestamp=base + timedelta(minutes=i))
            for i in range(10)
        ]
        _run(tracker.sync_records_from_store())

        ids = [r.request_id for r in tracker._records]
        assert "local_only" in ids, "the merge trimmed away the record it was protecting"
        assert len(tracker._records) <= tracker.MAX_RECORDS
        assert "fleet9" in ids, "trimming kept the oldest scanned rows, not the newest"


class TestTracesStayChronological:
    """Ordering, because the fleet-wide list no longer arrives sorted."""

    def test_the_newest_request_is_first_even_when_the_scan_is_unordered(self):
        """A scan returns rows in arbitrary order.

        Within one process the record list was chronological, so ``records[-limit:]``
        meant "recent" for free. That assumption dies with the merge — the tail
        becomes whatever the scan happened to return last, presented as recent.
        """
        store = SharedStore()
        base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        store.records = [
            _record(request_id="middle", timestamp=base + timedelta(minutes=5)),
            _record(request_id="newest", timestamp=base + timedelta(minutes=9)),
            _record(request_id="oldest", timestamp=base),
        ]
        api = _admin(_tracker(store), store)
        traces = _json(_run(api.traces(_FakeRequest())))["traces"]
        assert [t["request_id"] for t in traces] == ["newest", "middle", "oldest"]

    def test_the_limit_keeps_the_newest_not_an_arbitrary_slice(self):
        store = SharedStore()
        base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        store.records = [
            _record(request_id=f"r{i}", timestamp=base + timedelta(minutes=i))
            for i in (3, 0, 4, 1, 2)
        ]
        api = _admin(_tracker(store), store)
        traces = _json(_run(api.traces(_FakeRequest({"limit": "2"}))))["traces"]
        assert [t["request_id"] for t in traces] == ["r4", "r3"]

    def test_a_nonsense_limit_does_not_become_a_default_page(self):
        """``int("abc")`` raised inside the handler; the fallback must be the default.

        And the ceiling matters now that the list is fleet-wide: ``?limit=999999``
        would serialize the whole scanned history into one response.
        """
        store = SharedStore()
        base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        store.records = [
            _record(request_id=f"r{i}", timestamp=base + timedelta(minutes=i))
            for i in range(3)
        ]
        api = _admin(_tracker(store), store)
        assert len(_json(_run(api.traces(_FakeRequest({"limit": "abc"}))))["traces"]) == 3
        assert len(_json(_run(api.traces(_FakeRequest({"limit": "0"}))))["traces"]) == 1

    def test_an_undated_record_does_not_sort_as_newest(self):
        """A missing timestamp must not float to the top of a live view, and must
        not raise by comparing None to a datetime."""
        store = SharedStore()
        store.records = [
            _record(request_id="dated", timestamp=datetime(2026, 8, 6, tzinfo=UTC)),
            _record(request_id="undated", timestamp=None),
        ]
        api = _admin(_tracker(store), store)
        traces = _json(_run(api.traces(_FakeRequest())))["traces"]
        assert traces[0]["request_id"] == "dated"


class TestPersistenceOffIsUnchanged:
    """Single-instance and no-DynamoDB deployments must not pay for any of this."""

    def test_no_persistence_means_no_sync_and_local_numbers(self):
        tracker = CostTracker(pricing_config={})
        _run(tracker.record_usage(_record()))
        payload = _json(_run(_admin(tracker, None).overview(None)))
        assert payload["total_cost"] == pytest.approx(0.000132)

    def test_sync_reports_that_it_did_not_run(self):
        """The bool is the caller's way to tell "fleet-wide" from "this instance"."""
        assert _run(CostTracker(pricing_config={}).sync_records_from_store()) is False


class TestTheReadmeDescribesTheFix:
    """The defect was a docs claim as much as a code one."""

    def test_the_admin_read_row_is_no_longer_marked_unshared(self):
        for line in _README.read_text(encoding="utf-8").splitlines():
            if "the admin read" in line:
                assert "✅" in line, f"read row still claims per-instance: {line}"
                return
        pytest.fail("the admin-read row is gone — did the table get rewritten?")

    def test_the_staleness_window_is_documented(self):
        """An operator needs to know the counts can be seconds behind."""
        text = _README.read_text(encoding="utf-8")
        assert re.search(r"10\s*s(econd)?", text)


class TestTheComposeHostPortIsOverridable:
    """Kept from #86: a clash on 8000 is silent, so the override must be findable."""

    def test_the_host_port_is_parameterised(self):
        compose = (_README.parent / "docker-compose.yml").read_text(encoding="utf-8")
        assert re.search(r'"\$\{AXON_HOST_PORT:-8000\}:8000"', compose), (
            "host port is hardcoded; every other value in this file uses "
            "${VAR:-default}"
        )

    def test_the_container_port_stays_8000(self):
        compose = (_README.parent / "docker-compose.yml").read_text(encoding="utf-8")
        assert "urlopen('http://localhost:8000/health')" in compose

    def test_the_readme_shows_how(self):
        assert "AXON_HOST_PORT=8002 docker compose up" in _README.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _FakeRequest:
    """Just the query_params these handlers read."""

    def __init__(self, params: dict | None = None) -> None:
        self.query_params = params or {}


def _json(response):
    """Decode a JSONResponse body."""
    import json
    return json.loads(response.body)


def _json_reset(store, tracker, api):
    """Read ``overview`` from a second instance without its own TTL interfering."""
    return _run(api.overview(None))
