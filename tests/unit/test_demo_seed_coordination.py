"""Fleet-wide demo seed coordination and hydration tests."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

import pytest

from src.gateway.bootstrap import (
    _apply_seed_data,
    _coordinate_demo_seed,
    _demo_seed_fingerprint,
)
from src.gateway.config_loader import DemoSeedData
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.persistence import DynamoPersistence
from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.quota_enforcer import QuotaEnforcer
from src.gateway.security.audit_trail import (
    AuditEventType,
    AuditTrail,
)
from src.gateway.security.event_dispatcher import EventDispatcher


class _ConditionalCheckFailed(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "ConditionalCheckFailedException"},
        }
        super().__init__("condition failed")


class _SeedTable:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _storage_key(key: dict[str, str]) -> tuple[str, str]:
        return key["PK"], key["SK"]

    def update_item(self, **request):
        storage_key = self._storage_key(request["Key"])
        current = self.rows.get(storage_key)
        values = request["ExpressionAttributeValues"]

        if ":complete" in values:
            if (
                current is None
                or current.get("status") != values[":in_progress"]
                or current.get("owner_token") != values[":owner_token"]
            ):
                raise _ConditionalCheckFailed()
            current = copy.deepcopy(current)
            current["status"] = values[":complete"]
            current["completed_at"] = values[":completed_at"]
            current.pop("lease_expires_at", None)
            self.rows[storage_key] = current
            return {}

        now = values[":now"]
        claimable = current is None or (
            current.get("status") == values[":in_progress"]
            and current.get("lease_expires_at", now) < now
        )
        if not claimable:
            raise _ConditionalCheckFailed()
        claimed = {
            **request["Key"],
            "status": values[":in_progress"],
            "owner_token": values[":owner_token"],
            "seed_started_at": values[":seed_started_at"],
            "lease_expires_at": values[":lease_expires_at"],
            "entity_type": values[":entity_type"],
        }
        self.rows[storage_key] = claimed
        return {"Attributes": copy.deepcopy(claimed)}

    def get_item(self, **request):
        current = self.rows.get(self._storage_key(request["Key"]))
        return {"Item": copy.deepcopy(current)} if current is not None else {}

    def delete_item(self, **request):
        storage_key = self._storage_key(request["Key"])
        current = self.rows.get(storage_key)
        values = request["ExpressionAttributeValues"]
        if (
            current is None
            or current.get("status") != values[":in_progress"]
            or current.get("owner_token") != values[":owner_token"]
        ):
            raise _ConditionalCheckFailed()
        del self.rows[storage_key]
        return {}


class _SeedPersistence(DynamoPersistence):
    def __init__(self) -> None:
        super().__init__(table_name="demo-seed-test")
        self._enabled = True
        self._table = _SeedTable()


def test_demo_seed_marker_allows_one_owner_and_then_stays_complete():
    persistence = _SeedPersistence()

    first = asyncio.run(persistence.try_claim_demo_seed("seed-v1"))
    assert first is not None
    owner_token = first["owner_token"]

    assert asyncio.run(persistence.try_claim_demo_seed("seed-v1")) is None

    asyncio.run(
        persistence.complete_demo_seed(
            "seed-v1",
            owner_token,
        )
    )
    state = asyncio.run(persistence.get_demo_seed_state("seed-v1"))
    assert state is not None
    assert state["status"] == "complete"
    assert "lease_expires_at" not in state
    assert asyncio.run(persistence.try_claim_demo_seed("seed-v1")) is None


def test_abandoned_demo_seed_can_be_claimed_again():
    persistence = _SeedPersistence()
    first = asyncio.run(persistence.try_claim_demo_seed("seed-v1"))
    assert first is not None

    asyncio.run(
        persistence.abandon_demo_seed(
            "seed-v1",
            first["owner_token"],
        )
    )
    second = asyncio.run(persistence.try_claim_demo_seed("seed-v1"))

    assert second is not None
    assert second["owner_token"] != first["owner_token"]


@pytest.mark.parametrize("lease_seconds", [True, 0, 1.5])
def test_demo_seed_lease_requires_a_positive_integer(lease_seconds):
    persistence = _SeedPersistence()

    with pytest.raises(ValueError):
        asyncio.run(
            persistence.try_claim_demo_seed(
                "seed-v1",
                lease_seconds=lease_seconds,
            )
        )


def test_demo_seed_fingerprint_is_stable_and_data_sensitive():
    first = DemoSeedData(projects=[{"project_id": "p1", "name": "One"}])
    same = DemoSeedData(projects=[{"project_id": "p1", "name": "One"}])
    changed = DemoSeedData(projects=[{"project_id": "p1", "name": "Two"}])

    assert _demo_seed_fingerprint(first) == _demo_seed_fingerprint(same)
    assert _demo_seed_fingerprint(first) != _demo_seed_fingerprint(changed)


def test_coordination_returns_the_leader_owner_token():
    class _Leader:
        async def try_claim_demo_seed(self, seed_id, *, lease_seconds):
            assert seed_id == "seed-v1"
            assert lease_seconds > 0
            return {"owner_token": "owner-1"}

        async def get_demo_seed_state(self, seed_id):
            raise AssertionError("leader should not read a completion marker")

    assert asyncio.run(
        _coordinate_demo_seed(
            _Leader(),
            "seed-v1",
            wait_seconds=1,
            poll_seconds=0.001,
        )
    ) == "owner-1"


def test_coordination_waits_until_the_leader_completes():
    class _Follower:
        def __init__(self) -> None:
            self.reads = 0

        async def try_claim_demo_seed(self, seed_id, *, lease_seconds):
            return None

        async def get_demo_seed_state(self, seed_id):
            self.reads += 1
            if self.reads == 1:
                return {"status": "in_progress"}
            return {"status": "complete"}

    follower = _Follower()
    assert asyncio.run(
        _coordinate_demo_seed(
            follower,
            "seed-v1",
            wait_seconds=1,
            poll_seconds=0.001,
        )
    ) is None
    assert follower.reads == 2


def test_follower_applies_local_seed_state_without_durable_writes():
    class _Store:
        enabled = True

        async def save_usage_record(self, record):
            raise AssertionError("follower persisted a usage record")

    class _Keys:
        async def issue_key(self, **kwargs):
            raise AssertionError("follower issued an API key")

    class _Audit:
        async def record(self, **kwargs):
            raise AssertionError("follower persisted an audit event")

    seed = DemoSeedData(
        projects=[{"project_id": "project-a", "name": "Project A"}],
        usage_seeds=[
            {
                "project_id": "project-a",
                "user_id": "user-a",
                "provider": "bedrock",
                "model": "claude-sonnet",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost": 0.01,
            }
        ],
        api_keys=[
            {
                "project_id": "project-a",
                "name": "demo-key",
            }
        ],
        audit_events=[
            {
                "event_type": "auth_success",
                "project_id": "project-a",
                "user_id": "user-a",
                "request_id": "request-a",
            }
        ],
    )
    tracker = CostTracker({}, persistence=_Store())

    projects, _, _ = _apply_seed_data(
        seed,
        tracker,
        ProviderHealthTracker(),
        ["claude-sonnet"],
        QuotaEnforcer(),
        PolicyHierarchyResolver(persistence=None),
        _Audit(),
        _Keys(),
        EventDispatcher(),
        persist_records=False,
    )

    assert set(projects) == {"project-a"}
    assert len(tracker._records) == 1
    assert tracker._records[0].request_id == (
        "req-0000-project-a-user-a"
    )


def test_audit_hydration_replaces_the_buffer_with_the_durable_chain():
    source = AuditTrail()
    first = asyncio.run(
        source.record(
            AuditEventType.AUTH_SUCCESS,
            "user-a",
            "project-a",
            "request-a",
        )
    )
    second = asyncio.run(
        source.record(
            AuditEventType.LLM_REQUEST,
            "user-b",
            "project-a",
            "request-b",
        )
    )
    rows = [
        source._serialize_record(first),
        source._serialize_record(second),
    ]

    class _AuditPersistence:
        enabled = True

        async def load_audit_records_strict(self, project_id=None):
            assert project_id is None
            return copy.deepcopy(rows)

        async def load_audit_records(self, project_id=None):
            raise AssertionError("hydration must use the strict audit loader")

    hydrated = AuditTrail(persistence=_AuditPersistence())
    count = asyncio.run(hydrated.hydrate_recent_from_persistence())

    assert count == 2
    assert [record.record_id for record in hydrated.buffered_records()] == [
        first.record_id,
        second.record_id,
    ]
    assert hydrated.verify_chain() is True
    assert hydrated._last_hash == second.record_hash


def test_hydration_rejects_a_broken_durable_chain():
    timestamp = datetime.now(timezone.utc)
    rows = [
        {
            "record_id": "aud_bad",
            "event_type": "auth_success",
            "timestamp": timestamp.isoformat(),
            "user_id": "user-a",
            "project_id": "project-a",
            "request_id": "request-a",
            "data": "{}",
            "prev_hash": "not-genesis",
            "record_hash": "0" * 64,
        }
    ]

    class _AuditPersistence:
        enabled = True

        async def load_audit_records_strict(self, project_id=None):
            return rows

    hydrated = AuditTrail(persistence=_AuditPersistence())
    with pytest.raises(RuntimeError):
        asyncio.run(hydrated.hydrate_recent_from_persistence())
