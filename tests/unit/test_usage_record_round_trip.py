"""Regression tests for the lossy UsageRecord DynamoDB round trip.

The bug: `serialize_usage_record` wrote 14 of `UsageRecord`'s 18 fields.
`cache_creation_tokens`, `latency_ms`, `status` and `routing_strategy` were
never persisted, so every record restored from DynamoDB carried the dataclass
default for all four instead of what actually happened.

Same shape as #B8: the field reads as present, so nothing raises and nothing
looks broken — the value is just a constant that no request produced. The
sharpest case is `status`, whose default is `"success"`. Restoring a table of
records that included failures gave back a set claiming every request had
succeeded, which is worse than absent data because it is confidently wrong in
the safe-looking direction.

The invariant these tests pin: **a field absent from a stored item means
"unknown", never a plausible measurement.** A pre-migration row must not claim
`status="success"`, and it must not claim a latency it never had. This mirrors
the `""` vs `"general"` rule for `task_type` — the same reasoning applied to
four more fields.

`test_every_field_survives_a_round_trip` is the one that matters long-term: it
is derived from `__dataclass_fields__`, so the next field added to
`UsageRecord` fails it unless the serializer is updated too. That is the check
whose absence let four fields drift out of sync in the first place.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import datetime, timezone
from decimal import Decimal

from src.gateway.models import UsageRecord
from src.gateway.persistence import DynamoPersistence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**overrides) -> UsageRecord:
    """A record with every field set to something distinguishable from its default."""
    base = dict(
        request_id="req-1",
        project_id="proj-1",
        user_id="alice",
        provider="bedrock",
        model="claude-sonnet",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost=0.0125,
        timestamp=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        cached_tokens=10,
        cache_creation_tokens=20,
        image_tokens=30,
        reasoning_tokens=40,
        latency_ms=1234.5,
        status="error",
        routing_strategy="cost-optimized",
        task_type="math",
        provider_request_id="chatcmpl-upstream-1",
    )
    base.update(overrides)
    return UsageRecord(**base)


def _round_trip(record: UsageRecord) -> UsageRecord:
    """Serialize and deserialize through the same conversions DynamoDB applies.

    `save_usage_record` runs floats through `_convert_floats_to_decimal` on the
    way in, and `load_usage_records` runs `_convert_decimals_to_native` on the
    way out. Skipping those is how a Decimal/int narrowing bug hides: a plain
    dict round trip would pass while the real table path returns a different
    type.
    """
    item = DynamoPersistence.serialize_usage_record(record)
    item = DynamoPersistence._convert_floats_to_decimal(item)
    # What boto3 hands back: every number is a Decimal.
    item = {
        k: (Decimal(str(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
        for k, v in item.items()
    }
    item = DynamoPersistence._convert_decimals_to_native(item)
    return DynamoPersistence.deserialize_usage_record(item)


# ---------------------------------------------------------------------------
# The four dropped fields
# ---------------------------------------------------------------------------


class TestDroppedFieldsArePersisted:
    def test_cache_creation_tokens_survives(self):
        assert _round_trip(_make_record()).cache_creation_tokens == 20

    def test_latency_ms_survives(self):
        assert _round_trip(_make_record()).latency_ms == 1234.5

    def test_status_survives(self):
        assert _round_trip(_make_record(status="error")).status == "error"

    def test_routing_strategy_survives(self):
        assert _round_trip(_make_record()).routing_strategy == "cost-optimized"

    def test_an_error_does_not_come_back_as_a_success(self):
        """The defect's worst symptom, stated directly.

        `status` defaults to "success", so before the fix a failed request was
        restored as a successful one. Any error-rate computed over restored
        records read 0%.
        """
        restored = _round_trip(_make_record(status="error"))
        assert restored.status != "success"
        assert restored.status == "error"

    def test_all_four_are_written_to_the_item(self):
        item = DynamoPersistence.serialize_usage_record(_make_record())
        for name in ("cache_creation_tokens", "latency_ms", "status", "routing_strategy"):
            assert name in item, f"{name} is not persisted"


# ---------------------------------------------------------------------------
# The guard against the next dropped field
# ---------------------------------------------------------------------------


class TestSerializerCoversTheDataclass:
    def test_every_field_survives_a_round_trip(self):
        """Derived from the dataclass, so a new field fails until it round-trips.

        The original bug was not that someone wrote the wrong line — it was
        that nothing connected `UsageRecord`'s field list to the serializer, so
        four additions drifted out silently. Asserting a hand-written list of
        names would reproduce exactly that gap.
        """
        record = _make_record()
        restored = _round_trip(record)
        for f in fields(UsageRecord):
            assert getattr(restored, f.name) == getattr(record, f.name), (
                f"{f.name} did not survive the round trip — add it to "
                f"serialize_usage_record and deserialize_usage_record"
            )

    def test_no_field_is_left_at_its_default(self):
        """Guards the test above from passing vacuously.

        If a helper value coincided with a field's default, the round-trip
        assertion would hold whether or not the field was persisted.
        """
        record = _make_record()
        defaulted = [f for f in fields(UsageRecord) if f.default is not MISSING]
        # Sanity-check the introspection itself: if this list were empty the
        # loop below would pass without asserting anything.
        assert defaulted, "expected UsageRecord to have fields with defaults"
        for f in defaulted:
            assert getattr(record, f.name) != f.default, (
                f"_make_record leaves {f.name} at its default, so a "
                f"round-trip assertion on it proves nothing"
            )


# ---------------------------------------------------------------------------
# Migration: rows written before the fields existed
# ---------------------------------------------------------------------------


class TestPreMigrationRows:
    def _legacy_item(self) -> dict:
        """An item as the old serializer wrote it — the four fields absent."""
        item = DynamoPersistence.serialize_usage_record(_make_record())
        for name in ("cache_creation_tokens", "latency_ms", "status", "routing_strategy"):
            del item[name]
        return item

    def test_a_legacy_row_still_deserializes(self):
        record = DynamoPersistence.deserialize_usage_record(self._legacy_item())
        assert record.request_id == "req-1"
        assert record.cost == 0.0125

    def test_legacy_status_is_unknown_not_success(self):
        """The decision this fix turns on.

        An absent `status` means the row predates the field, not that the
        request succeeded. Defaulting to "success" would manufacture a 100%
        success rate across all historical data — the same class of error as
        #B8's "general", where a missing value was reported as a real one.
        """
        record = DynamoPersistence.deserialize_usage_record(self._legacy_item())
        assert record.status == ""
        assert record.status != "success"

    def test_legacy_numeric_fields_are_zero(self):
        record = DynamoPersistence.deserialize_usage_record(self._legacy_item())
        assert record.cache_creation_tokens == 0
        assert record.latency_ms == 0.0

    def test_legacy_routing_strategy_is_empty(self):
        record = DynamoPersistence.deserialize_usage_record(self._legacy_item())
        assert record.routing_strategy == ""


# ---------------------------------------------------------------------------
# Type preservation through the Decimal conversion
# ---------------------------------------------------------------------------


class TestNumericTypes:
    def test_whole_latency_stays_a_float(self):
        """`_convert_decimals_to_native` narrows a whole Decimal to int.

        Without the explicit `float()` in the deserializer, a latency that
        lands exactly on 1234.0 comes back as `int` and the declared type
        quietly stops holding for that row only — the kind of defect that
        surfaces as a formatting or division surprise far from here.
        """
        restored = _round_trip(_make_record(latency_ms=1234.0))
        assert restored.latency_ms == 1234.0
        assert isinstance(restored.latency_ms, float)

    def test_fractional_latency_keeps_its_precision(self):
        assert _round_trip(_make_record(latency_ms=87.25)).latency_ms == 87.25

    def test_token_counts_stay_ints(self):
        restored = _round_trip(_make_record())
        assert isinstance(restored.cache_creation_tokens, int)
        assert isinstance(restored.total_tokens, int)

    def test_cost_keeps_its_precision(self):
        """Decimal(str(float)) is used precisely to avoid binary-float drift."""
        assert _round_trip(_make_record(cost=0.0125)).cost == 0.0125
