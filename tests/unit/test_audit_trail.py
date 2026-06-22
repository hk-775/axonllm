"""Tests for immutable audit trail."""

import asyncio

import pytest

from src.gateway.security.audit_trail import AuditEventType, AuditRecord, AuditTrail


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def trail():
    return AuditTrail(persistence=None, buffer_size=100)


class TestBasicRecording:
    def test_records_event(self, trail):
        record = _run(trail.record(
            event_type=AuditEventType.LLM_REQUEST,
            user_id="user-1",
            project_id="proj-1",
            request_id="req-123",
            data={"model": "claude-sonnet"},
        ))
        assert record.record_id.startswith("aud_")
        assert record.event_type == AuditEventType.LLM_REQUEST
        assert record.user_id == "user-1"
        assert record.data["model"] == "claude-sonnet"

    def test_record_has_hash(self, trail):
        record = _run(trail.record(
            event_type=AuditEventType.AUTH_SUCCESS,
            user_id="u", project_id="p", request_id="r",
        ))
        assert record.record_hash != ""
        assert len(record.record_hash) == 64  # SHA-256

    def test_records_are_sequential(self, trail):
        r1 = _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", "r1"))
        r2 = _run(trail.record(AuditEventType.LLM_RESPONSE, "u", "p", "r1"))
        assert r2.prev_hash == r1.record_hash


class TestHashChain:
    def test_valid_chain(self, trail):
        for i in range(5):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))
        assert trail.verify_chain() is True

    def test_tampered_record_detected(self, trail):
        for i in range(5):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))

        # Tamper with a record
        trail._buffer[2].data["model"] = "tampered"
        assert trail.verify_chain() is False

    def test_empty_buffer_is_valid(self, trail):
        assert trail.verify_chain() is True


class TestLLMRequestRecording:
    def test_records_llm_request_metadata(self, trail):
        record = _run(trail.record_llm_request(
            user_id="user-1",
            project_id="proj-1",
            request_id="req-abc",
            model="claude-opus",
            provider="anthropic",
            message_count=3,
            pii_redacted_count=2,
            injection_score=0.1,
        ))
        assert record.event_type == AuditEventType.LLM_REQUEST
        assert record.data["model"] == "claude-opus"
        assert record.data["pii_redacted_count"] == 2
        assert record.data["injection_score"] == 0.1


class TestInjectionRecording:
    def test_records_blocked_injection(self, trail):
        record = _run(trail.record_injection_event(
            user_id="u",
            project_id="p",
            request_id="r",
            threat_level="high",
            patterns=["role_override"],
            blocked=True,
        ))
        assert record.event_type == AuditEventType.INJECTION_BLOCKED
        assert record.data["blocked"] is True
        assert "role_override" in record.data["patterns"]

    def test_records_detected_not_blocked(self, trail):
        record = _run(trail.record_injection_event(
            user_id="u", project_id="p", request_id="r",
            threat_level="medium", patterns=["extraction"], blocked=False,
        ))
        assert record.event_type == AuditEventType.INJECTION_DETECTED


class TestPIIRedactionRecording:
    def test_records_pii_event(self, trail):
        record = _run(trail.record_pii_redaction(
            user_id="u", project_id="p", request_id="r",
            redacted_types=["email", "ssn"], count=3,
        ))
        assert record.event_type == AuditEventType.PII_REDACTION
        assert record.data["count"] == 3
        assert "email" in record.data["redacted_types"]


class TestQueryRecent:
    def test_filters_by_project(self, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "proj-a", "r1"))
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "proj-b", "r2"))
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "proj-a", "r3"))

        results = trail.query_recent(project_id="proj-a")
        assert len(results) == 2
        assert all(r.project_id == "proj-a" for r in results)

    def test_filters_by_event_type(self, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", "r1"))
        _run(trail.record(AuditEventType.INJECTION_BLOCKED, "u", "p", "r2"))
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", "r3"))

        results = trail.query_recent(event_type=AuditEventType.INJECTION_BLOCKED)
        assert len(results) == 1

    def test_respects_limit(self, trail):
        for i in range(20):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))
        results = trail.query_recent(limit=5)
        assert len(results) == 5


class TestBufferSize:
    def test_buffer_truncates(self):
        trail = AuditTrail(persistence=None, buffer_size=5)
        for i in range(10):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))
        assert len(trail._buffer) == 5
