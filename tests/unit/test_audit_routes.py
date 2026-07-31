"""Tests for audit trail admin API routes."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.admin.audit_routes import AuditAPI, create_audit_routes
from src.gateway.security.audit_trail import AuditEventType, AuditTrail


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def trail():
    return AuditTrail(persistence=None, buffer_size=1000)


@pytest.fixture
def client(trail):
    audit_api = AuditAPI(audit_trail=trail)
    app = Starlette(routes=create_audit_routes(audit_api))
    return TestClient(app)


class TestQueryRecords:
    def test_empty_returns_zero(self, client):
        resp = client.get("/admin/audit/records")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_returns_recorded_events(self, client, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u1", "p1", "r1"))
        _run(trail.record(AuditEventType.LLM_REQUEST, "u2", "p2", "r2"))

        resp = client.get("/admin/audit/records")
        assert resp.json()["count"] == 2

    def test_filters_by_project(self, client, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "proj-a", "r1"))
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "proj-b", "r2"))

        resp = client.get("/admin/audit/records?project_id=proj-a")
        assert resp.json()["count"] == 1
        assert resp.json()["records"][0]["project_id"] == "proj-a"

    def test_filters_by_event_type(self, client, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", "r1"))
        _run(trail.record(AuditEventType.INJECTION_BLOCKED, "u", "p", "r2"))

        resp = client.get("/admin/audit/records?event_type=injection_blocked")
        assert resp.json()["count"] == 1
        assert resp.json()["records"][0]["event_type"] == "injection_blocked"

    def test_invalid_event_type_returns_400(self, client):
        resp = client.get("/admin/audit/records?event_type=bogus")
        assert resp.status_code == 400
        assert "valid_types" in resp.json()

    def test_respects_limit(self, client, trail):
        for i in range(20):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))

        resp = client.get("/admin/audit/records?limit=5")
        assert resp.json()["count"] == 5


class TestVerifyIntegrity:
    def test_valid_chain(self, client, trail):
        for i in range(5):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))

        resp = client.get("/admin/audit/verify")
        assert resp.status_code == 200
        assert resp.json()["chain_valid"] is True
        assert resp.json()["status"] == "intact"

    def test_tampered_chain(self, client, trail):
        for i in range(5):
            _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", f"r{i}"))
        trail._buffer[2].data["injected"] = True

        resp = client.get("/admin/audit/verify")
        assert resp.json()["chain_valid"] is False
        assert resp.json()["status"] == "TAMPERED"


class TestStats:
    def test_stats_empty(self, client):
        resp = client.get("/admin/audit/stats")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_stats_with_data(self, client, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p1", "r1"))
        _run(trail.record(AuditEventType.INJECTION_BLOCKED, "u", "p1", "r2"))
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p2", "r3"))

        resp = client.get("/admin/audit/stats")
        data = resp.json()
        assert data["total"] == 3
        assert data["by_type"]["llm_request"] == 2
        assert data["by_type"]["injection_blocked"] == 1
        assert data["by_project"]["p1"] == 2


class TestSecurityEvents:
    def test_returns_only_security_events(self, client, trail):
        _run(trail.record(AuditEventType.LLM_REQUEST, "u", "p", "r1"))
        _run(trail.record(AuditEventType.INJECTION_BLOCKED, "u", "p", "r2"))
        _run(trail.record(AuditEventType.PII_REDACTION, "u", "p", "r3"))
        _run(trail.record(AuditEventType.LLM_RESPONSE, "u", "p", "r4"))

        resp = client.get("/admin/audit/security")
        assert resp.json()["count"] == 2
        types = [r["event_type"] for r in resp.json()["records"]]
        assert "injection_blocked" in types
        assert "pii_redaction" in types
        assert "llm_request" not in types


class TestPiiPreview:
    """POST /admin/pii/preview — the demo panel's backing endpoint.

    Nothing here touches Comprehend: the NER column is exercised by monkeypatching
    the detector factory, because a real call is billable and would make the suite
    depend on network and IAM.
    """

    def test_shows_the_before_and_after(self, client):
        resp = client.post("/admin/pii/preview", json={
            "text": "Email me at a@b.com or call 555-234-5678."})
        assert resp.status_code == 200
        d = resp.json()
        assert "[EMAIL_1]" in d["redacted"]
        assert "[PHONE_1]" in d["redacted"]
        assert "a@b.com" not in d["redacted"]
        assert d["redacted_count"] == 2
        assert sorted(d["types_found"]) == ["email", "phone"]

    def test_the_round_trip_is_reported_and_lossless(self, client):
        text = "Email me at a@b.com or call 555-234-5678."
        d = client.post("/admin/pii/preview", json={"text": text}).json()
        assert d["reinjected"] == text
        assert d["round_trip_exact"] is True

    def test_a_name_is_not_redacted_by_pattern_matching(self, client):
        # The documented limit, pinned as a test: PII_PATTERNS has no name
        # pattern because a name has no shape to match. If someone adds one, the
        # demo panel's whole premise changes and this should force that
        # conversation rather than silently drift.
        d = client.post("/admin/pii/preview", json={
            "text": "I am Alice Smith and my email is a@b.com."}).json()
        assert "Alice Smith" in d["redacted"]
        assert "[EMAIL_1]" in d["redacted"]
        assert "name" not in d["types_found"]

    def test_nothing_is_persisted(self, client, trail):
        before = len(trail._buffer)
        client.post("/admin/pii/preview", json={"text": "ssn 123-45-6789"})
        # The endpoint exists because the audit trail must NOT store the PII it
        # redacts; writing a record here would defeat that.
        assert len(trail._buffer) == before

    def test_types_can_be_narrowed(self, client):
        d = client.post("/admin/pii/preview", json={
            "text": "a@b.com and 123-45-6789", "types": ["email"]}).json()
        assert "[EMAIL_1]" in d["redacted"]
        assert "123-45-6789" in d["redacted"]
        assert d["types_checked"] == ["email"]

    def test_supported_types_are_advertised(self, client):
        d = client.post("/admin/pii/preview", json={"text": "hi"}).json()
        assert "email" in d["supported_types"]
        assert "name" not in d["supported_types"]
        assert "name" in d["supported_ner_types"]

    @pytest.mark.parametrize("payload,fragment", [
        ({}, "text is required"),
        ({"text": ""}, "text is required"),
        ({"text": "   "}, "text is required"),
        ({"text": 42}, "text is required"),
        ({"text": "x" * 4001}, "exceeds 4000"),
        ({"text": "a@b.com", "types": "email"}, "list of strings"),
        ({"text": "a@b.com", "types": [1, 2]}, "list of strings"),
        ({"text": "a@b.com", "types": ["nonsense"]}, "unknown PII types"),
    ])
    def test_validation(self, client, payload, fragment):
        resp = client.post("/admin/pii/preview", json=payload)
        assert resp.status_code == 400
        assert fragment in resp.json()["error"]["message"]

    def test_invalid_json_is_a_400_not_a_500(self, client):
        resp = client.post("/admin/pii/preview", content=b"{not json",
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.json()["error"]["message"]

    def test_exactly_at_the_limit_is_accepted(self, client):
        # Boundary: 4000 is allowed, 4001 is not.
        resp = client.post("/admin/pii/preview", json={"text": "x" * 4000})
        assert resp.status_code == 200

    def test_ner_is_absent_unless_requested(self, client):
        # It bills per call, so a plain preview must never trigger it.
        d = client.post("/admin/pii/preview", json={"text": "Alice Smith"}).json()
        assert "ner" not in d

    def test_ner_column_adds_the_name(self, client, monkeypatch):
        class FakeDetector:
            async def detect(self, text, active_types):
                start = text.find("Alice Smith")
                return [(start, start + 11, "name")] if start >= 0 else []

        monkeypatch.setattr("src.gateway.security.pii_ner.build_entity_detector",
                            lambda region="us-east-1": FakeDetector())
        d = client.post("/admin/pii/preview", json={
            "text": "I am Alice Smith at a@b.com.", "ner": True}).json()
        # Left column unchanged — NER supplements rather than replaces.
        assert "Alice Smith" in d["redacted"]
        assert d["ner"]["available"] is True
        assert "[NAME_1]" in d["ner"]["redacted"]
        assert d["ner"]["additional_count"] == 1
        assert "name" in d["ner"]["types_found"]

    def test_a_detector_outage_is_reported_not_hidden(self, client, monkeypatch):
        class Boom:
            async def detect(self, text, active_types):
                raise RuntimeError("Throttling: rate exceeded")

        monkeypatch.setattr("src.gateway.security.pii_ner.build_entity_detector",
                            lambda region="us-east-1": Boom())
        d = client.post("/admin/pii/preview", json={
            "text": "I am Alice Smith at a@b.com.", "ner": True}).json()
        # Redaction fails open, so this returned available=True with two
        # identical columns until the mapping started carrying the error —
        # which reads as "entity detection found nothing".
        assert d["ner"]["available"] is False
        assert "Throttling" in d["ner"]["reason"]
        # The regex column is unaffected by the outage.
        assert "[EMAIL_1]" in d["redacted"]

    def test_no_detector_available_is_reported(self, client, monkeypatch):
        monkeypatch.setattr("src.gateway.security.pii_ner.build_entity_detector",
                            lambda region="us-east-1": None)
        d = client.post("/admin/pii/preview", json={
            "text": "Alice Smith", "ner": True}).json()
        assert d["ner"]["available"] is False
        assert "boto3" in d["ner"]["reason"]

    def test_ner_types_can_be_narrowed(self, client, monkeypatch):
        class FakeDetector:
            def __init__(self):
                self.asked = None

            async def detect(self, text, active_types):
                self.asked = list(active_types)
                return []

        fake = FakeDetector()
        monkeypatch.setattr("src.gateway.security.pii_ner.build_entity_detector",
                            lambda region="us-east-1": fake)
        client.post("/admin/pii/preview", json={
            "text": "Alice Smith", "ner": True, "ner_types": ["name"]})
        assert fake.asked == ["name"]

    def test_get_is_not_allowed(self, client):
        assert client.get("/admin/pii/preview").status_code == 405
