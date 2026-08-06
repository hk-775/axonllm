"""Which admin reads survive a second instance, and which the docs promise do.

Found by running the documented AWS install and then polling one endpoint: on a
two-task Fargate deployment ``GET /admin/overview`` alternated ``total_cost``
between ``0.000132`` and ``0`` on identical authenticated requests. Both answers
were honest — the record was written to DynamoDB, but each task aggregates an
in-memory list that is hydrated once at startup, so only the task that served the
chat counted it. The README's shared-state table said "Usage/cost records ✅
shared", which is true of the write and not of the read.

Nothing in the existing suite could catch that: every other test drives a single
``AdminAPI`` against one ``CostTracker``, which is a one-instance fleet by
construction. So these tests build *two* trackers over one persistence layer —
the shape the deployment actually has — and assert which endpoints agree.

They pin current behaviour rather than the behaviour we want. If someone adds a
read-through and ``overview`` starts agreeing across instances, the test named
for the divergence fails and points at the README row to update. That is the
intent: the docs and the code should not be able to drift apart quietly again.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.gateway.cost_tracker import CostTracker
from src.gateway.models import UsageRecord

_README = Path(__file__).resolve().parents[2] / "README.md"


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


class TestTwoInstancesDisagreeOnUsageAggregates:
    """The live symptom, reproduced without AWS."""

    def test_a_record_billed_on_one_instance_is_invisible_to_the_other(self):
        """This is the flapping ``total_cost`` in one assertion.

        ``overview`` sums ``_records``, so instance B — which never served the
        request — sums an empty list. No error, no warning, just a different
        number depending on which task the load balancer picked.
        """
        task_a = CostTracker(pricing_config={})
        task_b = CostTracker(pricing_config={})
        task_a._records.append(_record())

        assert sum(r.cost for r in task_a._records) == pytest.approx(0.000132)
        assert sum(r.cost for r in task_b._records) == 0.0, (
            "if this now agrees, a read-through was added — update the "
            "shared-state table in the README"
        )

    def test_request_counts_diverge_the_same_way(self):
        """``total_requests`` and ``request_count`` have no shared counter at all.

        Worth separating from cost: spend has a fleet-wide DynamoDB counter that
        *could* back a read-through, so cost is fixable without new storage.
        A request count has no such counter, which is why the README says it
        under-reports rather than promising a fix.
        """
        task_a = CostTracker(pricing_config={})
        task_b = CostTracker(pricing_config={})
        task_a._records.append(_record())
        assert len(task_a._records) == 1
        assert len(task_b._records) == 0


class TestTheReadmeDescribesThisHonestly:
    """The defect was a docs claim as much as a code one.

    A reader who believed the old table would conclude their client was broken,
    which is worse than the divergence itself.
    """

    def test_the_write_and_the_read_are_listed_separately(self):
        table = _README.read_text(encoding="utf-8")
        assert "Usage/cost records — **the write**" in table
        assert "Usage/cost records — **the admin read**" in table

    def test_the_read_row_is_not_marked_shared(self):
        """Guards the specific wording that was wrong.

        The old row was ``| Usage/cost **records** | ✅ |``. If a ✅ reappears on
        the admin-read row, the table is making the claim that the live
        deployment disproved.
        """
        for line in _README.read_text(encoding="utf-8").splitlines():
            if "the admin read" in line:
                assert "❌" in line and "✅" not in line, f"read row claims shared: {line}"
                break
        else:
            pytest.fail("the admin-read row is gone — did the table get rewritten?")

    def test_the_endpoint_that_does_read_through_is_named(self):
        """An adopter needs the working alternative, not just the caveat.

        ``/admin/quotas/{project_id}`` reads the shared spend counter per call
        and was stable across 8 polls on the live two-task deployment, so it is
        the answer to "then what should I query?".
        """
        text = _README.read_text(encoding="utf-8")
        section = text[text.index("Usage aggregates are per-process") :][:1600]
        assert "/admin/quotas/" in section


class TestTheComposeHostPortIsOverridable:
    """A clash on 8000 is silent, so the override has to be discoverable.

    Docker binds ``::`` and a local ``serve_dashboard.py`` binds ``0.0.0.0``;
    both start, then ``localhost`` resolves to ``::1`` first and the container
    answers for the gateway started by hand. Observed while running the
    documented compose and path-1 instructions at the same time.
    """

    def test_the_host_port_is_parameterised(self):
        compose = (_README.parent / "docker-compose.yml").read_text(encoding="utf-8")
        assert re.search(r'"\$\{AXON_HOST_PORT:-8000\}:8000"', compose), (
            "host port is hardcoded; every other value in this file uses "
            "${VAR:-default}"
        )

    def test_the_container_port_stays_8000(self):
        """The right-hand side must not move: AXON_SERVER_PORT and the
        healthcheck URL are both fixed at 8000 inside the container."""
        compose = (_README.parent / "docker-compose.yml").read_text(encoding="utf-8")
        assert "urlopen('http://localhost:8000/health')" in compose

    def test_the_readme_shows_how(self):
        assert "AXON_HOST_PORT=8002 docker compose up" in _README.read_text(encoding="utf-8")
