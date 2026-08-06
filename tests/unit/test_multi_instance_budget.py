"""A budget limit is a fleet limit, not a limit per instance.

`infra/stack.py` deploys `desired_count=2` and auto-scales to 10, so spend
counted per process made a $100 budget admit roughly $200 — and up to $1000 once
scaled out. Nothing looked wrong from either instance: each one enforced its
limit correctly against a number that was only ever its own share.

`QuotaEnforcer.check_budget` is the gate under test here, because it is the call
that returns `allowed=False` and refuses the request. `CostTracker.check_budget`
reports a `BudgetStatus` on the response and is covered too, but a wrong number
there is a display bug where the same number in the enforcer is an overspend.

The shared table is deliberately one object handed to two enforcers: "two
processes, one counter" is the whole condition, and a single instance cannot
exhibit the failure at all.
"""

from __future__ import annotations

import asyncio

from datetime import datetime, timezone

from src.gateway.cost_tracker import CostTracker
from src.gateway.models import ResolvedPolicy, TokenPricing, UsageRecord
from src.gateway.quota_enforcer import QuotaEnforcer


def _run(coro):
    return asyncio.run(coro)


class SharedCounters:
    """One table's spend counters, standing in for DynamoDB.

    `add_spend` mirrors the real contract that makes the fix work: the returned
    value is the post-update total *including this caller's cost*, which is what
    lets an instance learn the fleet figure from a write it was already making.
    """

    def __init__(self) -> None:
        self.totals: dict[tuple[str, str], float] = {}
        self.enabled = True
        self.adds = 0
        self.reads = 0

    async def add_spend(self, scope: str, ident: str, cost: float) -> float | None:
        self.adds += 1
        total = self.totals.get((scope, ident), 0.0) + cost
        self.totals[(scope, ident)] = total
        return total

    async def get_spend(self, scope: str, ident: str) -> float | None:
        self.reads += 1
        return self.totals.get((scope, ident), 0.0)

    async def reset_spend(self, scope: str, ident: str) -> bool:
        self.totals.pop((scope, ident), None)
        return True

    # CostTracker.record_usage writes the record itself before touching counters.
    async def save_usage_record(self, record) -> None:
        pass


def _policy(budget_limit: float | None) -> ResolvedPolicy:
    return ResolvedPolicy(budget_limit=budget_limit)


def _enforce(enforcer, project_id: str, cost: float, limit: float | None):
    """Run the checks the way a request does — through `enforce_all`.

    Calling `check_budget` directly skips the fleet refresh, so it answers from
    whatever this instance happens to already know. That is right for
    `/admin/quotas/simulate` and wrong for anything claiming to test the gate.
    """
    return _run(enforcer.enforce_all(
        project_id=project_id, model="claude-sonnet-4", provider="anthropic",
        max_tokens=None, estimated_cost=cost, policy=_policy(limit),
    ))


def _usage(project_id: str, cost: float, request_id: str = "r1", user_id: str = "") -> UsageRecord:
    return UsageRecord(
        request_id=request_id,
        project_id=project_id,
        user_id=user_id,
        provider="anthropic",
        model="claude-sonnet-4",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost=cost,
        timestamp=datetime.now(timezone.utc),
    )


def _tracker(table, budgets=None) -> CostTracker:
    return CostTracker(
        pricing_config={"anthropic": {"claude-sonnet-4": TokenPricing(
            prompt_token_cost=0.003, completion_token_cost=0.015)}},
        budgets=budgets,
        persistence=table,
    )


class TestTheLimitAppliesToTheFleet:
    """The reported defect: $100 cap, two tasks, ~$200 admitted."""

    def test_the_other_instance_sees_spend_it_did_not_serve(self):
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)

        _run(first.record_spend("p1", 60.0))

        decision = _enforce(second, "p1", 1.0, 100.0)
        assert second.get_spend("p1") == 60.0, (
            "an instance that did not serve the request reported $0 spend — this is "
            "the defect: each task enforced the budget against its own share only"
        )
        assert decision.allowed is True  # 61 < 100, so this one is still fine

    def test_a_budget_is_not_doubled_by_a_second_instance(self):
        """The headline number: spend split evenly across two tasks.

        $60 on each of two instances is $120 against a $100 limit. Pre-fix both
        instances saw $60 and both allowed the next request.
        """
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)

        _run(first.record_spend("p1", 60.0))
        _run(second.record_spend("p1", 60.0))

        assert _enforce(second, "p1", 1.0, 100.0).allowed is False
        assert _enforce(first, "p1", 1.0, 100.0).allowed is False, (
            "the instance that stopped billing still admitted requests — its "
            "counter was frozen at its own last write"
        )

    def test_ten_instances_do_not_multiply_the_limit_by_ten(self):
        """Auto-scaling reaches 10, which is where the defect cost the most."""
        table = SharedCounters()
        fleet = [QuotaEnforcer(persistence=table) for _ in range(10)]

        for enforcer in fleet:
            _run(enforcer.record_spend("p1", 11.0))

        assert table.totals[("quota", "p1")] == 110.0
        for i, enforcer in enumerate(fleet):
            assert _enforce(enforcer, "p1", 1.0, 100.0).allowed is False, (
                f"instance {i} still admitted requests at $110 against a $100 limit"
            )

    def test_the_blocked_request_reports_the_fleet_figure_not_the_local_one(self):
        """The 429 body tells an operator why. It has to say $120, not $60.

        A message quoting this instance's share reads as a gateway rejecting a
        request that is comfortably inside its budget.
        """
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)
        _run(first.record_spend("p1", 60.0))
        _run(second.record_spend("p1", 60.0))

        decision = _enforce(second, "p1", 1.0, 100.0)
        assert decision.current_value == 120.0
        assert "121" in decision.reason


class TestDegradingWhenTheCounterIsUnavailable:
    def test_without_persistence_it_still_enforces_locally(self):
        """Single-node with no table is a supported mode, not an error."""
        enforcer = QuotaEnforcer()
        _run(enforcer.record_spend("p1", 150.0))
        assert _enforce(enforcer, "p1", 1.0, 100.0).allowed is False

    def test_a_failed_write_falls_back_to_local_accumulation(self):
        """Degrade to per-instance — the old behaviour — never to unlimited.

        A counter that cannot be written must not leave spend at zero: that is
        the one outcome worse than the defect being fixed.
        """
        table = SharedCounters()

        async def _fails(scope, ident, cost):
            return None

        table.add_spend = _fails
        enforcer = QuotaEnforcer(persistence=table)

        _run(enforcer.record_spend("p1", 60.0))
        _run(enforcer.record_spend("p1", 60.0))

        assert enforcer.get_spend("p1") == 120.0, "dropped spend entirely when the shared write failed"
        assert _enforce(enforcer, "p1", 1.0, 100.0).allowed is False

    def test_a_failed_read_does_not_zero_a_known_total(self):
        """None from the counter means "unknown", not "nothing spent".

        Treating it as 0.0 would unblock every project during a DynamoDB
        incident, which is precisely when an operator is not watching spend.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)
        _run(enforcer.record_spend("p1", 150.0))

        async def _fails(scope, ident):
            return None

        table.get_spend = _fails

        assert _run(enforcer.current_spend("p1")) == 150.0
        assert _enforce(enforcer, "p1", 1.0, 100.0).allowed is False

    def test_the_local_counter_never_moves_backwards(self):
        """Out-of-order responses must not re-admit already-spent requests.

        Two concurrent `add_spend` calls can return in either order; adopting a
        lower total after a higher one would hand back budget the fleet has
        already committed.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)
        _run(enforcer.record_spend("p1", 100.0))

        async def _stale(scope, ident, cost):
            return 20.0  # a response from an earlier, smaller write

        table.add_spend = _stale
        _run(enforcer.record_spend("p1", 5.0))

        assert enforcer.get_spend("p1") == 100.0, "a late low total rolled the counter back"


class TestSpendIsNotCountedTwice:
    def test_the_two_trackers_do_not_share_one_key(self):
        """CostTracker and QuotaEnforcer both bill the same request.

        They are separate counters that happen to agree. Pointing them at one key
        would double every charge, which fails a budget at half its limit — a
        fix that overcorrects into a new bug.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)
        tracker = _tracker(table)

        _run(tracker.record_usage(_usage("p1", 40.0)))
        _run(enforcer.record_spend("p1", 40.0))

        assert enforcer.get_spend("p1") == 40.0
        assert _run(tracker.check_budget("p1")).current_spend == 40.0
        assert _enforce(enforcer, "p1", 1.0, 100.0).allowed is True, (
            "$40 of spend blocked a request against a $100 budget — the two "
            "trackers are billing into the same counter"
        )

    def test_the_reported_budget_status_is_fleet_wide(self):
        """`CostTracker.check_budget` fills the BudgetStatus on the response.

        Not the blocking gate — that is QuotaEnforcer — but a caller watching
        `budget_status` to back off sees half the real spend if this counter stays
        per-process, and the admin overview under-reports every project.
        """
        table = SharedCounters()
        first = _tracker(table, budgets={"p1": {"budget_limit": 100.0, "alert_threshold": 80.0}})
        second = _tracker(table, budgets={"p1": {"budget_limit": 100.0, "alert_threshold": 80.0}})

        _run(first.record_usage(_usage("p1", 60.0, request_id="a")))
        _run(second.record_usage(_usage("p1", 60.0, request_id="b")))

        status = _run(second.check_budget("p1"))
        assert status.current_spend == 120.0, (
            f"reported ${status.current_spend} of a fleet total of $120"
        )
        assert status.is_over_budget is True

    def test_user_budgets_are_fleet_wide_too(self):
        """Per-user budgets split across instances exactly as project ones do."""
        table = SharedCounters()
        first, second = _tracker(table), _tracker(table)
        for tracker in (first, second):
            tracker.register_user("u1", budget_limit=100.0)

        _run(first.record_usage(_usage("p1", 60.0, request_id="a", user_id="u1")))
        _run(second.record_usage(_usage("p1", 60.0, request_id="b", user_id="u1")))

        assert _run(second.check_user_budget("u1")).current_spend == 120.0
        assert _run(second.check_user_budget("u1")).is_over_budget is True

    def test_startup_does_not_add_history_on_top_of_the_counter(self):
        """`load_records` sums records the counter already includes.

        Every persisted record was folded into the shared total when it was first
        billed, so seeding must *replace* the local sum, not add to it. Adding
        would double a project's spend on every restart.
        """
        table = SharedCounters()
        _run(table.add_spend("project", "p1", 30.0))
        tracker = _tracker(table)

        tracker.load_records([_usage("p1", 30.0)])
        assert tracker._project_spend["p1"] == 30.0  # local sum of the same charge
        _run(tracker.adopt_fleet_spend(["p1"]))

        assert _run(tracker.check_budget("p1")).current_spend == 30.0, (
            "startup added persisted history to a counter that already contained it"
        )

    def test_demo_seed_spend_stays_out_of_the_shared_counter(self):
        """Every instance fabricates the same seed, and ADD is not idempotent.

        Sharing seeded spend would multiply demo figures by the instance count and
        again on every restart, so a demo gateway would eventually refuse its own
        seeded projects.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)
        tracker = _tracker(table)

        for _ in range(3):  # three restarts, or three instances
            _run(tracker.record_usage(_usage("p1", 25.0), share=False))
            _run(enforcer.record_spend("p1", 25.0, share=False))

        assert table.adds == 0, "seeded spend was written to the shared counter"
        assert _enforce(enforcer, "p1", 1.0, 100.0).allowed is True


class TestARestartedInstanceKnowsTheFleetTotal:
    def test_its_very_first_request_is_already_enforced(self):
        """A fresh task must not admit a request against an exhausted budget.

        This is the deploy-time face of the defect and the reason `enforce_all`
        refreshes rather than trusting `_spend_tracker`: seeding at startup alone
        would still leave every project created *after* boot unknown, so the
        first request for it on each task would be waved through.
        """
        table = SharedCounters()
        _run(table.add_spend("quota", "p1", 150.0))

        fresh = QuotaEnforcer(persistence=table)
        assert _enforce(fresh, "p1", 1.0, 100.0).allowed is False, (
            "a restarted instance admitted a request against a budget the fleet "
            "had already exhausted"
        )

    def test_startup_seeding_reports_the_right_spend_before_any_request(self):
        """`GET /admin/quotas` right after a deploy must not read $0.

        `enforce_all` covers the blocking path, but an operator checking spend
        before any traffic arrives goes through neither it nor `record_spend`.
        """
        table = SharedCounters()
        _run(table.add_spend("quota", "p1", 150.0))

        fresh = QuotaEnforcer(persistence=table)
        _run(fresh.adopt_fleet_spend(["p1"]))
        assert fresh.get_spend("p1") == 150.0

    def test_seeding_is_skipped_without_persistence(self):
        table = SharedCounters()
        table.enabled = False
        enforcer = QuotaEnforcer(persistence=table)

        _run(enforcer.adopt_fleet_spend(["p1"]))
        assert table.reads == 0, "read the shared counter despite persistence being disabled"

    def test_an_admin_read_reflects_another_instance(self):
        """`GET /admin/quotas/{id}` must not depend on which task answers.

        `get_spend` is only right once this instance has served the project;
        `current_spend` reads through so the operator gets one answer.
        """
        table = SharedCounters()
        busy, idle = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)
        _run(busy.record_spend("p1", 42.0))

        assert idle.get_spend("p1") == 0.0  # never served this project
        assert _run(idle.current_spend("p1")) == 42.0, (
            "an admin read reported $0 for a project another task is billing"
        )

    def test_a_busy_project_keeps_refreshing(self):
        """The refresh must fire for projects under traffic, not just idle ones.

        `record_spend` learns the fleet total from its own ADD, so stamping the
        read clock there looks free. It is not: every request would push the stamp
        forward and the interval would never elapse for a project that is being
        billed — so the refresh would only ever run for projects nobody is using,
        which is precisely backwards. A busy project is the one whose fleet total
        moves underneath it. This caught that during development.
        """
        table = SharedCounters()
        busy, other = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)

        # `busy` bills steadily; `other` bills the fleet past the limit meanwhile.
        for _ in range(5):
            _run(busy.record_spend("p1", 1.0))
            busy._spend_read_at.clear()  # stand in for the interval elapsing
            assert _enforce(busy, "p1", 1.0, 100.0).allowed is True
        _run(other.record_spend("p1", 200.0))

        busy._spend_read_at.clear()
        assert _enforce(busy, "p1", 1.0, 100.0).allowed is False, (
            "a continuously-billing instance never re-read the shared counter, so "
            "it kept admitting requests while the fleet was already over budget"
        )

    def test_the_refresh_is_rate_limited(self):
        """Bounded staleness, not a read per request.

        A consistent read on every proxied call is the cost the caches elsewhere
        in this codebase exist to avoid.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)

        for _ in range(20):
            _enforce(enforcer, "p1", 1.0, 100.0)

        assert table.reads == 1, f"read the shared counter {table.reads} times for 20 requests"

    def test_no_budget_means_no_read_at_all(self):
        """Gateways that set no hierarchy budget must not pay for this."""
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)

        for _ in range(5):
            _enforce(enforcer, "p1", 1.0, None)

        assert table.reads == 0, "read the spend counter for a policy with no budget"

    def test_an_admin_read_is_not_on_the_request_path(self):
        """The request path must stay at one write, no read.

        A read added to `record_spend` would double the per-request DynamoDB cost
        for a number `add_spend` already returned.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)

        _run(enforcer.record_spend("p1", 1.0))
        assert table.reads == 0, "read the counter on the request path"
        assert table.adds == 1


class TestBudgetAlertsFireOncePerFleet:
    def test_a_threshold_crossed_by_another_instance_is_not_re_announced(self):
        """Alerts are keyed to a crossing, and the fleet crosses 80% once.

        Adopting the fleet total without moving `prev` with it would make each
        instance's first request look like a jump from its own $0, firing every
        threshold below the current total at once — on every instance.
        """
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)
        alerts = []
        second.on_budget_alert(lambda pid, thr, spend, limit: alerts.append(thr))

        _run(first.record_spend("p1", 85.0, budget_limit=100.0))  # fleet crosses 80%
        _run(second.record_spend("p1", 1.0, budget_limit=100.0))  # 85 -> 86, no crossing

        assert alerts == [], f"re-announced thresholds the fleet had already crossed: {alerts}"

    def test_the_instance_that_crosses_it_still_alerts(self):
        """The guard above must not silence alerting altogether."""
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)
        alerts = []
        second.on_budget_alert(lambda pid, thr, spend, limit: alerts.append((thr, spend)))

        _run(first.record_spend("p1", 70.0, budget_limit=100.0))
        _run(second.record_spend("p1", 15.0, budget_limit=100.0))  # 70 -> 85 crosses 80%

        assert (0.8, 85.0) in alerts, (
            "the crossing was missed, and the reported spend must be the fleet "
            f"total rather than this instance's $15: {alerts}"
        )


class TestResettingClearsTheFleetCounter:
    def test_a_reset_reaches_the_other_instances(self):
        """A billing-cycle reset on one task must unblock all of them.

        Popping a local dict leaves every other instance blocking against the old
        total, so the project stays refused and the operator has no way to see
        why.
        """
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)
        _run(first.record_spend("p1", 150.0))
        _run(second.current_spend("p1"))
        assert _enforce(second, "p1", 1.0, 100.0).allowed is False

        assert _run(first.reset_spend("p1")) is True

        assert _run(second.current_spend("p1")) == 0.0
        assert _enforce(second, "p1", 1.0, 100.0).allowed is True, (
            "a reset on one instance left the others blocking the project"
        )

    def test_a_reset_does_not_suppress_the_next_refresh(self):
        """Clearing local state must clear the read stamp with it.

        A reset that leaves the stamp in place means the very next request skips
        the refresh — for up to SPEND_REFRESH_SECONDS this instance answers from a
        zeroed local counter while the shared one may already be filling up again,
        so spend billed by another instance in that window is ignored.
        """
        table = SharedCounters()
        first, second = QuotaEnforcer(persistence=table), QuotaEnforcer(persistence=table)
        _run(first.record_spend("p1", 150.0))
        assert _enforce(first, "p1", 1.0, 100.0).allowed is False

        _run(first.reset_spend("p1"))
        _run(second.record_spend("p1", 150.0))  # another instance bills straight away

        assert _enforce(first, "p1", 1.0, 100.0).allowed is False, (
            "the reset left a read stamp behind, so the refresh was skipped and "
            "spend billed by another instance was invisible"
        )

    def test_a_failed_reset_is_reported_rather_than_claimed(self):
        """Returning success for a counter still at its old value misleads.

        The operator would believe the project was unblocked while every other
        instance keeps refusing it.
        """
        table = SharedCounters()

        async def _fails(scope, ident):
            return False

        table.reset_spend = _fails
        enforcer = QuotaEnforcer(persistence=table)
        _run(enforcer.record_spend("p1", 150.0))

        assert _run(enforcer.reset_spend("p1")) is False

    def test_a_failed_reset_still_clears_local_state(self):
        """A partial reset beats none, and the shared value is re-read anyway."""
        table = SharedCounters()

        async def _fails(scope, ident):
            return False

        table.reset_spend = _fails
        enforcer = QuotaEnforcer(persistence=table)
        _run(enforcer.record_spend("p1", 150.0))
        _run(enforcer.reset_spend("p1"))

        assert enforcer.get_spend("p1") == 0.0

    def test_reset_without_persistence_succeeds(self):
        """Single-node has no shared counter to fail at, so it must not report failure."""
        enforcer = QuotaEnforcer()
        _run(enforcer.record_spend("p1", 150.0))
        assert _run(enforcer.reset_spend("p1")) is True
        assert enforcer.get_spend("p1") == 0.0


class TestConcurrentBillingIsNotLost:
    def test_simultaneous_charges_all_land(self):
        """Read-modify-write would lose updates; ADD cannot.

        Ten concurrent charges against one project have to sum, not collapse to
        the last writer.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)

        async def _bill_all():
            await asyncio.gather(*(enforcer.record_spend("p1", 10.0) for _ in range(10)))

        asyncio.run(_bill_all())

        assert table.totals[("quota", "p1")] == 100.0
        assert enforcer.get_spend("p1") == 100.0

    def test_concurrent_charges_fire_each_threshold_once(self):
        """No gap and no overlap between concurrent callers' intervals.

        Each `add_spend` returns a total including exactly its own cost, so the
        `(total - cost, total]` intervals tile the range without overlapping —
        which is what makes it safe to do the shared write outside the lock. A
        threshold therefore falls in exactly one interval and fires once.
        """
        table = SharedCounters()
        enforcer = QuotaEnforcer(persistence=table)
        alerts = []
        enforcer.on_budget_alert(lambda pid, thr, spend, limit: alerts.append(thr))

        async def _bill_all():
            await asyncio.gather(*(
                enforcer.record_spend("p1", 10.0, budget_limit=100.0) for _ in range(10)
            ))

        asyncio.run(_bill_all())

        assert sorted(alerts) == [0.8, 0.9, 1.0], f"thresholds fired {alerts}, expected each once"
