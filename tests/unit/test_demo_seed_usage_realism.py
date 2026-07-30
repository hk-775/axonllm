"""Guards that seeded usage records look like a populated trace log.

Three defects, all of the same shape: the seed produced records that were
*present* but carried a value no real request could have produced, so the
dashboard rendered confidently wrong numbers instead of looking broken.

1. ``request_id`` was ``f"req-{project_id}-{user_id}-{provider}"``. That tuple
   is not unique — the shipped seed has many calls sharing all three — and
   ``CostTracker.load_records`` de-dupes by ``request_id``. So a seed of dozens
   of rows collapsed to one row per distinct tuple on rehydration, and the Live
   Traces view read as a handful of retries of the same request.
2. Every record was stamped ``datetime.now()`` at import, putting the entire
   history on one clock minute. Any per-hour or per-day chart drew a single
   spike.
3. ``latency_ms`` was never set, so it defaulted to 0.0 and the dashboard's
   average-latency tile reported a gateway that answered instantly.

The invariant: a seeded record must be distinguishable from every other seeded
record, and must not claim a measurement no request produced. This is the same
rule ``test_usage_record_round_trip`` pins for the persistence layer — an absent
value must not surface as a plausible one.
"""

from __future__ import annotations

from src.gateway.config_loader import load_demo_seed_config

SEED_PATH = "config/demo_seed.yaml"


def _usage_seeds() -> list[dict]:
    return load_demo_seed_config(SEED_PATH).usage_seeds


class TestSeededRecordsAreDistinguishable:
    """The de-dup key must differ per seeded row."""

    def test_project_user_provider_is_not_unique_in_the_shipped_seed(self):
        """Pins *why* the old id scheme was wrong, not just that it changed.

        If the seed ever became one-row-per-tuple this test would pass
        vacuously and the guard below would prove nothing, so assert the
        collision the old key suffered from is really present in the data.
        """
        seeds = _usage_seeds()
        keys = [(s["project_id"], s["user_id"], s["provider"]) for s in seeds]
        assert len(set(keys)) < len(keys), (
            "shipped seed no longer has rows sharing project+user+provider; "
            "the uniqueness guard below has become vacuous"
        )

    def test_indexed_ids_are_unique(self):
        """What the gateway actually builds: index-prefixed, so never colliding."""
        seeds = _usage_seeds()
        ids = [f"req-{i:04d}-{s['project_id']}-{s['user_id']}" for i, s in enumerate(seeds)]
        assert len(set(ids)) == len(ids)

    def test_no_seeded_row_survives_dedup_loss(self):
        """The concrete symptom: rehydration must return every seeded row.

        ``CostTracker.load_records`` skips a record whose ``request_id`` it has
        already seen, so duplicate ids silently drop spend as well as traces.
        """
        from datetime import datetime, timedelta, timezone

        from src.gateway.cost_tracker import CostTracker
        from src.gateway.models import UsageRecord

        now = datetime.now(timezone.utc)
        seeds = _usage_seeds()
        records = [
            UsageRecord(
                request_id=f"req-{i:04d}-{s['project_id']}-{s['user_id']}",
                project_id=s["project_id"],
                user_id=s["user_id"],
                provider=s["provider"],
                model=s["model"],
                prompt_tokens=s.get("prompt_tokens", 0),
                completion_tokens=s.get("completion_tokens", 0),
                total_tokens=s.get("prompt_tokens", 0) + s.get("completion_tokens", 0),
                cost=s.get("cost", 0.0),
                timestamp=now - timedelta(minutes=float(s.get("minutes_ago", 0))),
                latency_ms=float(s.get("latency_ms", 0.0)),
            )
            for i, s in enumerate(seeds)
        ]

        tracker = CostTracker(pricing_config={})
        tracker.load_records(records)
        assert len(tracker._records) == len(seeds)


class TestSeededRecordsCarryRealMeasurements:
    """A seeded value must be something a request could have produced."""

    def test_every_row_declares_a_latency(self):
        missing = [i for i, s in enumerate(_usage_seeds()) if not s.get("latency_ms")]
        assert missing == [], (
            f"rows {missing[:10]} have no latency_ms, so they report 0ms — "
            f"a gateway that answered instantly"
        )

    def test_latencies_are_plausible(self):
        """Guards against a placeholder like 1ms or 60s slipping in."""
        for i, s in enumerate(_usage_seeds()):
            lat = float(s["latency_ms"])
            assert 10.0 <= lat <= 120_000.0, f"row {i} has implausible latency_ms={lat}"

    def test_every_row_declares_an_age(self):
        missing = [i for i, s in enumerate(_usage_seeds()) if "minutes_ago" not in s]
        assert missing == [], (
            f"rows {missing[:10]} have no minutes_ago, so they stamp at import "
            f"time and pile onto one clock minute"
        )

    def test_ages_span_more_than_one_hour(self):
        """The point of ``minutes_ago``: a history, not a spike.

        Without a spread, every time-bucketed chart on the dashboard draws a
        single bar regardless of how many rows the seed has.
        """
        ages = [float(s["minutes_ago"]) for s in _usage_seeds()]
        assert max(ages) - min(ages) > 60.0, (
            f"seeded ages span only {max(ages) - min(ages):.0f} minutes"
        )
