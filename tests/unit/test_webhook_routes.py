"""Tests for webhook/event dispatcher admin API routes."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.webhook_routes import WebhookAPI, create_webhook_routes
from src.gateway.security.event_dispatcher import EventDispatcher


@pytest.fixture
def dispatcher():
    return EventDispatcher()


@pytest.fixture
def client(dispatcher):
    webhook_api = WebhookAPI(dispatcher=dispatcher)
    app = Starlette(routes=create_webhook_routes(webhook_api))
    return TestClient(app)


class TestListDestinations:
    def test_empty_list(self, client):
        resp = client.get("/admin/webhooks")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_after_adding(self, client):
        client.post("/admin/webhooks", json={
            "name": "slack",
            "type": "webhook",
            "config": {"url": "https://hooks.slack.com/test"},
        })
        resp = client.get("/admin/webhooks")
        assert resp.json()["count"] == 1
        assert resp.json()["destinations"][0]["name"] == "slack"


class TestAddDestination:
    def test_creates_webhook(self, client):
        resp = client.post("/admin/webhooks", json={
            "name": "my-hook",
            "type": "webhook",
            "config": {"url": "https://example.com/hook"},
            "event_filter": ["injection_blocked"],
        })
        assert resp.status_code == 201
        assert resp.json()["name"] == "my-hook"
        assert resp.json()["event_filter"] == ["injection_blocked"]

    def test_creates_sns_destination(self, client):
        resp = client.post("/admin/webhooks", json={
            "name": "sns-alerts",
            "type": "sns",
            "config": {"topic_arn": "arn:aws:sns:us-east-1:123456789012:alerts"},
        })
        assert resp.status_code == 201
        assert resp.json()["type"] == "sns"

    def test_missing_name_returns_400(self, client):
        resp = client.post("/admin/webhooks", json={"type": "webhook"})
        assert resp.status_code == 400

    def test_invalid_type_returns_400(self, client):
        resp = client.post("/admin/webhooks", json={"name": "x", "type": "invalid"})
        assert resp.status_code == 400


class TestRemoveDestination:
    def test_removes_existing(self, client):
        client.post("/admin/webhooks", json={"name": "to-remove", "type": "webhook"})
        resp = client.delete("/admin/webhooks/to-remove")
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"

        # Verify it's gone
        resp = client.get("/admin/webhooks")
        assert resp.json()["count"] == 0

    def test_remove_nonexistent_returns_404(self, client):
        resp = client.delete("/admin/webhooks/nope")
        assert resp.status_code == 404


class TestStats:
    def test_returns_stats(self, client):
        resp = client.get("/admin/webhooks/stats")
        assert resp.status_code == 200
        assert "dispatched" in resp.json()
        assert "errors" in resp.json()
