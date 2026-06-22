"""Tests for audit trail admin API routes."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.admin.audit_routes import AuditAPI, create_audit_routes
from src.gateway.security.audit_trail import AuditEventType, AuditTrail


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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
