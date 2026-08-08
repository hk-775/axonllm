"""Tests for audit-trail durability (#16): chain head survives restart,
verify against the durable store detects tampering/removal, and export."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.gateway.security.audit_trail import (
    AuditEventType,
    AuditStoreUnavailable,
    AuditTrail,
)


class FakePersistence:
    """Minimal stand-in for DynamoPersistence's audit surface."""

    enabled = True

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.put_error: Exception | None = None

    async def put_item(self, item: dict) -> None:
        if self.put_error:
            error = self.put_error
            self.put_error = None
            raise error
        self.rows.append(item)

    async def load_audit_records(self, project_id: str | None = None) -> list[dict]:
        r = [x for x in self.rows if x.get("PK", "").startswith("AUDIT#")]
        if project_id:
            r = [x for x in r if x.get("project_id") == project_id]
        return sorted(r, key=lambda i: i.get("SK", ""))

    async def get_latest_audit_hash(self) -> str | None:
        r = await self.load_audit_records()
        return r[-1]["record_hash"] if r else None


async def test_chain_head_reloads_across_restart():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    await a.initialize()
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "req1", {"m": "x"})
    r2 = await a.record(AuditEventType.PII_REDACTION, "u", "proj", "req2", {"n": 1})

    # Simulate a process restart: fresh AuditTrail on the same durable store.
    a2 = AuditTrail(persistence=p)
    await a2.initialize()
    assert a2._last_hash == r2.record_hash  # head reloaded (the bug)
    r3 = await a2.record(AuditEventType.LLM_REQUEST, "u", "proj", "req3", {})
    assert r3.prev_hash == r2.record_hash  # chain links across restart


async def test_verify_persisted_clean():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    await a.initialize()
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {})
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r2", {})
    result = await a.verify_persisted_chain()
    assert result["valid"] is True and result["checked"] == 2


async def test_verify_persisted_detects_content_tampering():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    await a.initialize()
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {"amount": 1})
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r2", {})
    p.rows[0]["data"] = json.dumps({"amount": 999999})  # tamper a persisted row
    result = await a.verify_persisted_chain()
    assert result["valid"] is False
    assert "altered" in result["reason"] or "mismatch" in result["reason"]


async def test_verify_persisted_detects_removal():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    await a.initialize()
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {})
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r2", {})
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r3", {})
    del p.rows[1]  # remove the middle row
    result = await a.verify_persisted_chain()
    assert result["valid"] is False


async def test_failed_append_does_not_advance_chain_or_buffer():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    committed = await a.record(
        AuditEventType.LLM_REQUEST,
        "u",
        "proj",
        "r1",
        {},
    )
    original_buffer = list(a._buffer)

    p.put_error = RuntimeError("injected append failure")
    with pytest.raises(RuntimeError, match="injected append failure"):
        await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "failed", {})

    assert a._last_hash == committed.record_hash
    assert list(a._buffer) == original_buffer
    assert len(p.rows) == 1

    recovered = await a.record(
        AuditEventType.LLM_REQUEST,
        "u",
        "proj",
        "r2",
        {},
    )
    assert recovered.prev_hash == committed.record_hash
    assert a.verify_chain() is True


async def test_verify_persisted_detects_first_row_removal():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {})
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r2", {})
    del p.rows[0]

    result = await a.verify_persisted_chain()

    assert result["valid"] is False
    assert result["checked"] == 0
    assert "genesis" in result["reason"]


async def test_verify_persisted_detects_forged_first_row_predecessor():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    first = await a.record(
        AuditEventType.LLM_REQUEST,
        "u",
        "proj",
        "r1",
        {"k": "v"},
    )
    first.prev_hash = "forged-predecessor"
    first.record_hash = first.compute_hash()
    p.rows[0]["prev_hash"] = first.prev_hash
    p.rows[0]["record_hash"] = first.record_hash

    result = await a.verify_persisted_chain()

    assert result["valid"] is False
    assert result["broken_at"] == first.record_id
    assert result["checked"] == 0
    assert "genesis" in result["reason"]


async def test_verify_buffer_requires_expected_first_row_predecessor():
    a = AuditTrail(persistence=None)
    first = await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {})
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r2", {})
    a._buffer.popleft()

    assert a.verify_chain() is False
    assert a.verify_chain(expected_prev_hash=first.record_hash) is True


async def test_initialize_noop_without_persistence():
    a = AuditTrail(persistence=None)
    await a.initialize()
    assert a._last_hash == "genesis"


async def test_export_from_durable_store():
    p = FakePersistence()
    a = AuditTrail(persistence=p)
    await a.initialize()
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {})
    await a.record(AuditEventType.LLM_REQUEST, "u", "other", "r2", {})
    all_rows = await a.export_records()
    assert len(all_rows) == 2
    scoped = await a.export_records(project_id="proj")
    assert len(scoped) == 1 and scoped[0]["project_id"] == "proj"


async def test_export_from_buffer_when_no_persistence():
    a = AuditTrail(persistence=None)
    await a.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {"k": "v"})
    rows = await a.export_records()
    assert len(rows) == 1
    assert rows[0]["record_hash"] and rows[0]["prev_hash"] == "genesis"


async def test_initialize_sync_is_loop_safe_when_embedded():
    """Embedded case (Ostiari builds the agent inside a running loop) +
    persistence on: initialize_sync must NOT raise (no asyncio.run in a live
    loop); it defers the reload to the running loop."""
    p = FakePersistence()
    seed = AuditTrail(persistence=p)
    await seed.record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {})
    head = p.rows[-1]["record_hash"]

    a = AuditTrail(persistence=p)
    a.initialize_sync()  # called from within this running loop
    import asyncio

    await asyncio.sleep(0.05)  # let the deferred task run
    assert a._last_hash == head


def test_initialize_sync_standalone_no_loop():
    """Standalone case (no running loop): initialize_sync runs to completion."""
    import asyncio

    p = FakePersistence()
    asyncio.run(AuditTrail(persistence=p).record(AuditEventType.LLM_REQUEST, "u", "proj", "r1", {}))
    head = p.rows[-1]["record_hash"]

    a = AuditTrail(persistence=p)
    a.initialize_sync()  # no running loop → runs now
    assert a._last_hash == head


def test_initialize_sync_no_persistence():
    a = AuditTrail(persistence=None)
    a.initialize_sync()  # no crash, stays genesis
    assert a._last_hash == "genesis"


class AtomicTenantPersistence:
    """Shared compare-and-swap audit store for multi-replica tests."""

    enabled = True

    def __init__(self) -> None:
        self.heads: dict[str, str] = {}
        self.rows: dict[str, list[dict]] = {}
        self.lock = asyncio.Lock()
        self.fail_append = False
        self.fail_load = False

    async def append_tenant_audit_record(
        self,
        tenant_id: str,
        record: dict,
        expected_prev_hash: str,
    ) -> bool:
        if self.fail_append:
            raise RuntimeError("append unavailable")
        await asyncio.sleep(0)
        async with self.lock:
            current = self.heads.get(tenant_id, "genesis")
            if current != expected_prev_hash:
                return False
            self.rows.setdefault(tenant_id, []).append(dict(record))
            self.heads[tenant_id] = record["record_hash"]
            return True

    async def get_latest_tenant_audit_hash(
        self,
        tenant_id: str,
    ) -> str | None:
        return self.heads.get(tenant_id)

    async def load_tenant_audit_records(
        self,
        tenant_id: str,
        project_id: str | None = None,
    ) -> list[dict]:
        if self.fail_load:
            raise RuntimeError("load unavailable")
        rows = list(self.rows.get(tenant_id, []))
        if project_id is not None:
            rows = [row for row in rows if row.get("project_id") == project_id]
        return rows


async def test_atomic_tenant_append_serializes_concurrent_replicas():
    persistence = AtomicTenantPersistence()
    replicas = [
        AuditTrail(persistence=persistence),
        AuditTrail(persistence=persistence),
    ]

    await asyncio.gather(
        *[
            replicas[index % 2].record(
                AuditEventType.LLM_REQUEST,
                "same-user",
                "same-project",
                f"request-{index}",
                tenant_id="tenant-a",
            )
            for index in range(30)
        ]
    )

    rows = persistence.rows["tenant-a"]
    assert len(rows) == 30
    previous = "genesis"
    for row in rows:
        assert row["prev_hash"] == previous
        previous = row["record_hash"]
    assert persistence.heads["tenant-a"] == previous

    verifier = AuditTrail(persistence=persistence)
    result = await verifier.verify_persisted_chain(tenant_id="tenant-a")
    assert result["available"] is True
    assert result["valid"] is True
    assert result["checked"] == 30


async def test_tenant_append_failure_does_not_advance_memory():
    persistence = AtomicTenantPersistence()
    persistence.fail_append = True
    trail = AuditTrail(persistence=persistence)

    with pytest.raises(AuditStoreUnavailable):
        await trail.record(
            AuditEventType.AUTH_FAILURE,
            "same-user",
            "same-project",
            "request-failed",
            tenant_id="tenant-a",
        )

    assert trail.buffered_records("tenant-a") == []
    assert trail._last_hashes["tenant-a"] == "genesis"
    assert persistence.rows == {}


async def test_durable_load_outage_is_unavailable_not_empty_valid_chain():
    persistence = AtomicTenantPersistence()
    persistence.fail_load = True
    trail = AuditTrail(persistence=persistence)

    result = await trail.verify_persisted_chain(tenant_id="tenant-a")

    assert result["available"] is False
    assert result["valid"] is False
    assert result["checked"] == 0
    assert "unavailable" in result["reason"]


async def test_tenant_export_rejects_cross_tenant_rows():
    persistence = AtomicTenantPersistence()
    persistence.rows["tenant-a"] = [
        {
            "tenant_id": "tenant-b",
            "record_id": "aud_cross_tenant",
        }
    ]
    trail = AuditTrail(persistence=persistence)

    with pytest.raises(AuditStoreUnavailable):
        await trail.export_records(tenant_id="tenant-a")
