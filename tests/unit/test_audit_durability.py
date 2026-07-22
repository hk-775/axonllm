"""Tests for audit-trail durability (#16): chain head survives restart,
verify against the durable store detects tampering/removal, and export."""

from __future__ import annotations

import json

from src.gateway.security.audit_trail import AuditEventType, AuditTrail


class FakePersistence:
    """Minimal stand-in for DynamoPersistence's audit surface."""

    enabled = True

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def put_item(self, item: dict) -> None:
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
    assert a2._last_hash == r2.record_hash          # head reloaded (the bug)
    r3 = await a2.record(AuditEventType.LLM_REQUEST, "u", "proj", "req3", {})
    assert r3.prev_hash == r2.record_hash            # chain links across restart


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
    p.rows[0]["data"] = json.dumps({"amount": 999999})   # tamper a persisted row
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
    del p.rows[1]                                         # remove the middle row
    result = await a.verify_persisted_chain()
    assert result["valid"] is False


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
