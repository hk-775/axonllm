"""Tests for webhook/event dispatcher admin API routes."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.webhook_routes import WebhookAPI, create_webhook_routes
from src.gateway.persistence import PersistenceConflictError
from src.gateway.security.event_dispatcher import (
    EventDispatcher,
    SecurityEvent,
)

_PUBLIC_IPV4 = "93.184.216.34"
_WEBHOOK_URL = "https://webhook.example.com/events"


async def _public_resolver(hostname, port):
    return (_PUBLIC_IPV4,)


def _dispatcher():
    return EventDispatcher(
        resolver=_public_resolver,
        aws_region="us-east-1",
        aws_account_id="123456789012",
    )


@pytest.fixture
def dispatcher():
    return _dispatcher()


@pytest.fixture
def client(dispatcher):
    webhook_api = WebhookAPI(dispatcher=dispatcher)
    app = Starlette(routes=create_webhook_routes(webhook_api))
    return TestClient(app)


def _tenant_client(dispatcher, persistence, tenant_id, role):
    class ContextMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.context = SimpleNamespace(
                tenant_id=tenant_id,
                roles=[role],
            )
            return await call_next(request)

    webhook_api = WebhookAPI(
        dispatcher=dispatcher,
        persistence=persistence,
    )
    app = Starlette(routes=create_webhook_routes(webhook_api))
    app.add_middleware(ContextMiddleware)
    return TestClient(app)


class _TenantPersistence:
    enabled = True

    def __init__(self):
        self.rows = {}
        self.revisions = {}
        self.fail_save = False
        self.fail_load = False

    async def load_tenant_event_destinations_snapshot(self, tenant_id):
        if self.fail_load:
            raise RuntimeError("dynamo credentials should never leak")
        rows = self.rows.get(tenant_id)
        if rows is None:
            return None
        return deepcopy(rows), self.revisions.get(tenant_id, 0)

    async def save_tenant_event_destinations(
        self,
        tenant_id,
        destinations,
        expected_revision,
    ):
        if self.fail_save:
            raise RuntimeError("dynamo credentials should never leak")
        current_revision = self.revisions.get(tenant_id, 0)
        if current_revision != expected_revision:
            raise PersistenceConflictError("concurrent destination write")
        self.rows[tenant_id] = deepcopy(destinations)
        self.revisions[tenant_id] = current_revision + 1
        return current_revision + 1


class _InterleavingPersistence(_TenantPersistence):
    def __init__(self):
        super().__init__()
        self.injected = False

    async def save_tenant_event_destinations(
        self,
        tenant_id,
        destinations,
        expected_revision,
    ):
        if not self.injected:
            self.injected = True
            self.rows[tenant_id] = [
                {
                    "tenant_id": tenant_id,
                    "name": "concurrent-alert",
                    "destination_type": "webhook",
                    "config": {
                        "url": "https://concurrent.example.com/events"
                    },
                    "event_filter": None,
                    "enabled": True,
                }
            ]
            self.revisions[tenant_id] = expected_revision + 1
            raise PersistenceConflictError(
                "injected concurrent destination write"
            )
        return await super().save_tenant_event_destinations(
            tenant_id,
            destinations,
            expected_revision,
        )


class _LegacyPersistence:
    enabled = True

    def __init__(self):
        self.rows = None
        self.revision = 0
        self.fail_load = False
        self.fail_save = False

    async def load_event_destinations_snapshot(self):
        if self.fail_load:
            raise RuntimeError("legacy destination read failed")
        if self.rows is None:
            return None
        return deepcopy(self.rows), self.revision

    async def save_event_destinations(
        self,
        destinations,
        expected_revision,
    ):
        if self.fail_save:
            raise RuntimeError("legacy destination write failed")
        if self.revision != expected_revision:
            raise PersistenceConflictError("concurrent destination write")
        self.rows = deepcopy(destinations)
        self.revision += 1
        return self.revision


class _InterleavingLegacyPersistence(_LegacyPersistence):
    def __init__(self):
        super().__init__()
        self.injected = False

    async def save_event_destinations(
        self,
        destinations,
        expected_revision,
    ):
        if not self.injected:
            self.injected = True
            self.rows = [
                {
                    "name": "concurrent-alert",
                    "destination_type": "webhook",
                    "config": {
                        "url": "https://concurrent.example.com/events"
                    },
                    "event_filter": None,
                    "enabled": True,
                }
            ]
            self.revision = expected_revision + 1
            raise PersistenceConflictError(
                "injected concurrent destination write"
            )
        return await super().save_event_destinations(
            destinations,
            expected_revision,
        )


class TestListDestinations:
    def test_empty_list(self, client):
        resp = client.get("/admin/webhooks")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_after_adding(self, client):
        client.post(
            "/admin/webhooks",
            json={
                "name": "slack",
                "type": "webhook",
                "config": {"url": "https://hooks.slack.com/test"},
            },
        )
        resp = client.get("/admin/webhooks")
        assert resp.json()["count"] == 1
        assert resp.json()["destinations"][0]["name"] == "slack"


class TestAddDestination:
    def test_creates_webhook(self, client):
        resp = client.post(
            "/admin/webhooks",
            json={
                "name": "my-hook",
                "type": "webhook",
                "config": {"url": "https://example.com/hook"},
                "event_filter": ["injection_blocked"],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "my-hook"
        assert resp.json()["event_filter"] == ["injection_blocked"]

    def test_creates_sns_destination(self, client):
        resp = client.post(
            "/admin/webhooks",
            json={
                "name": "sns-alerts",
                "type": "sns",
                "config": {"topic_arn": "arn:aws:sns:us-east-1:123456789012:alerts"},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["type"] == "sns"

    def test_missing_name_returns_400(self, client):
        resp = client.post("/admin/webhooks", json={"type": "webhook"})
        assert resp.status_code == 400

    def test_invalid_type_returns_400(self, client):
        resp = client.post("/admin/webhooks", json={"name": "x", "type": "invalid"})
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "url",
        [
            "http://webhook.example.com/events",
            "https://127.0.0.1/events",
            "https://[::1]/events",
            "https://metadata.internal/events",
        ],
    )
    def test_rejects_unsafe_webhook_before_install(
        self,
        client,
        dispatcher,
        url,
    ):
        response = client.post(
            "/admin/webhooks",
            json={
                "name": "unsafe",
                "type": "webhook",
                "config": {"url": url},
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_destination"
        assert dispatcher.destinations == []

    def test_rejects_unresolvable_webhook_before_persistence(self):
        async def failing_resolver(hostname, port):
            raise OSError("DNS unavailable")

        dispatcher = EventDispatcher(resolver=failing_resolver)
        persistence = _TenantPersistence()
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )

        response = client.post(
            "/admin/webhooks",
            json={
                "name": "unresolved",
                "type": "webhook",
                "config": {"url": _WEBHOOK_URL},
            },
        )

        assert response.status_code == 400
        assert persistence.rows == {}
        assert dispatcher.destinations_for_tenant("tenant-a") == []

    def test_rejects_cross_account_sns_topic(self, client):
        response = client.post(
            "/admin/webhooks",
            json={
                "name": "sns-alerts",
                "type": "sns",
                "config": {
                    "topic_arn": (
                        "arn:aws:sns:us-east-1:210987654321:alerts"
                    )
                },
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_destination"

    def test_creates_same_context_cloudwatch_destination(self, client):
        response = client.post(
            "/admin/webhooks",
            json={
                "name": "audit",
                "type": "cloudwatch",
                "config": {
                    "log_group_arn": (
                        "arn:aws:logs:us-east-1:123456789012:"
                        "log-group:/axonllm/security"
                    ),
                    "log_stream": "events",
                },
            },
        )

        assert response.status_code == 201
        assert response.json()["type"] == "cloudwatch"


class TestRemoveDestination:
    def test_removes_existing(self, client):
        client.post(
            "/admin/webhooks",
            json={
                "name": "to-remove",
                "type": "webhook",
                "config": {"url": _WEBHOOK_URL},
            },
        )
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


class TestTenantScopedDestinations:
    def test_same_name_in_two_tenants_remains_isolated(self):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        tenant_a = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )
        tenant_b = _tenant_client(
            dispatcher,
            persistence,
            "tenant-b",
            "tenant_admin",
        )

        assert (
            tenant_a.post(
                "/admin/webhooks",
                json={
                    "name": "alerts",
                    "type": "webhook",
                    "config": {
                        "url": "https://tenant-a.example.com/events",
                        "region": "us-east-1",
                    },
                },
            ).status_code
            == 201
        )
        assert (
            tenant_b.post(
                "/admin/webhooks",
                json={
                    "name": "alerts",
                    "type": "webhook",
                    "config": {
                        "url": "https://tenant-b.example.com/events",
                        "region": "eu-west-1",
                    },
                },
            ).status_code
            == 201
        )

        response_a = tenant_a.get("/admin/webhooks").json()
        response_b = tenant_b.get("/admin/webhooks").json()
        assert response_a["tenant_id"] == "tenant-a"
        assert response_b["tenant_id"] == "tenant-b"
        assert response_a["destinations"][0]["config"]["region"] == "us-east-1"
        assert response_b["destinations"][0]["config"]["region"] == "eu-west-1"
        assert persistence.rows["tenant-a"][0]["tenant_id"] == "tenant-a"
        assert persistence.rows["tenant-b"][0]["tenant_id"] == "tenant-b"

    def test_authenticated_tenant_cannot_override_scope(self):
        client = _tenant_client(
            _dispatcher(),
            _TenantPersistence(),
            "tenant-a",
            "tenant_admin",
        )

        response = client.get("/admin/webhooks?tenant_id=tenant-b")

        assert response.status_code == 403
        assert response.json()["error"]["type"] == "invalid_tenant_scope"

    def test_authenticated_tenant_cannot_override_body_scope(self):
        client = _tenant_client(
            _dispatcher(),
            _TenantPersistence(),
            "tenant-a",
            "tenant_admin",
        )

        response = client.post(
            "/admin/webhooks",
            json={
                "tenant_id": "tenant-b",
                "name": "alerts",
                "type": "webhook",
            },
        )

        assert response.status_code == 403
        assert response.json()["error"]["type"] == "invalid_tenant_scope"

    def test_canonical_role_without_tenant_fails_closed(self):
        client = _tenant_client(
            _dispatcher(),
            _TenantPersistence(),
            None,
            "tenant_admin",
        )

        response = client.get("/admin/webhooks?tenant_id=tenant-a")

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_tenant_scope"

    def test_direct_stub_without_state_keeps_legacy_scope(self):
        class RequestStub:
            query_params = {}

        response = asyncio.run(
            WebhookAPI(_dispatcher()).list_destinations(RequestStub())
        )

        assert response.status_code == 200

    def test_remove_same_name_changes_only_authenticated_tenant(self):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        for tenant_id in ("tenant-a", "tenant-b"):
            persistence.rows[tenant_id] = [
                {
                    "tenant_id": tenant_id,
                    "name": "alerts",
                    "destination_type": "webhook",
                    "config": {"marker": tenant_id},
                    "event_filter": None,
                    "enabled": True,
                }
            ]
        tenant_a = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )
        tenant_b = _tenant_client(
            dispatcher,
            persistence,
            "tenant-b",
            "tenant_admin",
        )

        assert tenant_a.delete("/admin/webhooks/alerts").status_code == 200

        assert tenant_a.get("/admin/webhooks").json()["count"] == 0
        tenant_b_destinations = tenant_b.get("/admin/webhooks").json()[
            "destinations"
        ]
        assert len(tenant_b_destinations) == 1
        assert tenant_b_destinations[0]["config"]["marker"] == "tenant-b"

    def test_tenant_cannot_test_another_tenants_destination(
        self,
        monkeypatch,
    ):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        persistence.rows["tenant-b"] = [
            {
                "tenant_id": "tenant-b",
                "name": "alerts",
                "destination_type": "webhook",
                "config": {"url": "https://tenant-b.example.test"},
                "event_filter": None,
                "enabled": True,
            }
        ]
        sent = False

        async def record_send(event, destination):
            nonlocal sent
            sent = True

        monkeypatch.setattr(
            dispatcher,
            "_send_to_destination",
            record_send,
        )
        tenant_a = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )

        response = tenant_a.post("/admin/webhooks/alerts/test")

        assert response.status_code == 404
        assert sent is False

    def test_canonical_tenant_without_durable_store_fails_closed(self):
        client = _tenant_client(
            _dispatcher(),
            None,
            "tenant-a",
            "tenant_admin",
        )

        response = client.post(
            "/admin/webhooks",
            json={
                "name": "alerts",
                "type": "webhook",
                "config": {"url": _WEBHOOK_URL},
            },
        )

        assert response.status_code == 503
        assert response.json()["error"]["type"] == "webhook_store_unavailable"


class TestDurableFirstMutation:
    def test_failed_update_does_not_advance_memory(self):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        persistence.rows["tenant-a"] = [
            {
                "tenant_id": "tenant-a",
                "name": "alerts",
                "destination_type": "webhook",
                "config": {"region": "us-east-1"},
                "event_filter": None,
                "enabled": True,
            }
        ]
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )
        assert client.get("/admin/webhooks").status_code == 200
        persistence.fail_save = True

        response = client.post(
            "/admin/webhooks",
            json={
                "name": "alerts",
                "type": "webhook",
                "config": {
                    "url": _WEBHOOK_URL,
                    "region": "eu-west-1",
                },
            },
        )

        assert response.status_code == 503
        live = dispatcher.destinations_for_tenant("tenant-a")
        assert len(live) == 1
        assert live[0].config["region"] == "us-east-1"
        assert persistence.rows["tenant-a"][0]["config"]["region"] == "us-east-1"
        assert "credentials" not in response.text

    def test_load_outage_does_not_replace_existing_memory(self):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        persistence.fail_load = True
        from src.gateway.security.event_dispatcher import (
            DestinationType,
            EventDestination,
        )

        dispatcher.add_destination(
            EventDestination(
                tenant_id="tenant-a",
                name="live-alerts",
                destination_type=DestinationType.WEBHOOK,
                config={"region": "us-east-1"},
            )
        )
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )

        response = client.get("/admin/webhooks")

        assert response.status_code == 503
        assert [item.name for item in dispatcher.destinations_for_tenant("tenant-a")] == ["live-alerts"]
        assert "credentials" not in response.text


class TestDestinationConvergence:
    def test_concurrent_add_is_rebased_without_losing_first_writer(self):
        dispatcher = _dispatcher()
        persistence = _InterleavingPersistence()
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )

        response = client.post(
            "/admin/webhooks",
            json={
                "name": "requested-alert",
                "type": "webhook",
                "config": {"url": _WEBHOOK_URL},
            },
        )

        assert response.status_code == 201
        assert persistence.revisions["tenant-a"] == 2
        assert {
            destination["name"]
            for destination in persistence.rows["tenant-a"]
        } == {"concurrent-alert", "requested-alert"}

    def test_dispatch_refreshes_a_destination_changed_by_another_task(
        self,
        monkeypatch,
    ):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        persistence.rows["tenant-a"] = [
            {
                "tenant_id": "tenant-a",
                "name": "first",
                "destination_type": "webhook",
                "config": {"url": _WEBHOOK_URL},
                "event_filter": None,
                "enabled": True,
            }
        ]
        persistence.revisions["tenant-a"] = 1
        api = WebhookAPI(dispatcher, persistence)
        api.DESTINATION_SYNC_TTL_SECONDS = 0
        sent: list[str] = []

        async def record_send(event, destination):
            sent.append(destination.name)

        monkeypatch.setattr(
            dispatcher,
            "_send_to_destination",
            record_send,
        )
        event = SecurityEvent(
            event_id="event-1",
            event_type="auth_failure",
            timestamp="2026-08-07T00:00:00+00:00",
            tenant_id="tenant-a",
        )

        asyncio.run(dispatcher.dispatch(event))
        persistence.rows["tenant-a"] = [
            {
                "tenant_id": "tenant-a",
                "name": "second",
                "destination_type": "webhook",
                "config": {"url": _WEBHOOK_URL},
                "event_filter": None,
                "enabled": True,
            }
        ]
        persistence.revisions["tenant-a"] = 2
        asyncio.run(dispatcher.dispatch(event))

        assert sent == ["first", "second"]

    def test_dispatch_fails_closed_when_refresh_store_is_unavailable(
        self,
        monkeypatch,
    ):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        persistence.rows["tenant-a"] = [
            {
                "tenant_id": "tenant-a",
                "name": "alerts",
                "destination_type": "webhook",
                "config": {"url": _WEBHOOK_URL},
                "event_filter": None,
                "enabled": True,
            }
        ]
        persistence.revisions["tenant-a"] = 1
        api = WebhookAPI(dispatcher, persistence)
        api.DESTINATION_SYNC_TTL_SECONDS = 0
        sent: list[str] = []

        async def record_send(event, destination):
            sent.append(destination.name)

        monkeypatch.setattr(
            dispatcher,
            "_send_to_destination",
            record_send,
        )
        event = SecurityEvent(
            event_id="event-1",
            event_type="auth_failure",
            timestamp="2026-08-07T00:00:00+00:00",
            tenant_id="tenant-a",
        )
        asyncio.run(dispatcher.dispatch(event))
        persistence.fail_load = True

        asyncio.run(dispatcher.dispatch(event))

        assert sent == ["alerts"]
        assert dispatcher.stats_for_tenant("tenant-a")["errors"] == 1

    def test_legacy_concurrent_add_rebases_without_losing_first_writer(
        self,
    ):
        dispatcher = _dispatcher()
        persistence = _InterleavingLegacyPersistence()
        client = TestClient(
            Starlette(
                routes=create_webhook_routes(
                    WebhookAPI(dispatcher, persistence)
                )
            )
        )

        response = client.post(
            "/admin/webhooks",
            json={
                "name": "requested-alert",
                "type": "webhook",
                "config": {"url": _WEBHOOK_URL},
            },
        )

        assert response.status_code == 201
        assert persistence.revision == 2
        assert {
            destination["name"]
            for destination in persistence.rows
        } == {"concurrent-alert", "requested-alert"}

    def test_legacy_dispatch_refreshes_changes_from_another_task(
        self,
        monkeypatch,
    ):
        dispatcher = _dispatcher()
        persistence = _LegacyPersistence()
        persistence.rows = [
            {
                "name": "first",
                "destination_type": "webhook",
                "config": {"url": _WEBHOOK_URL},
                "event_filter": None,
                "enabled": True,
            }
        ]
        persistence.revision = 1
        api = WebhookAPI(dispatcher, persistence)
        api.DESTINATION_SYNC_TTL_SECONDS = 0
        sent: list[str] = []

        async def record_send(event, destination):
            sent.append(destination.name)

        monkeypatch.setattr(
            dispatcher,
            "_send_to_destination",
            record_send,
        )
        event = SecurityEvent(
            event_id="event-legacy",
            event_type="auth_failure",
            timestamp="2026-08-07T00:00:00+00:00",
        )

        asyncio.run(dispatcher.dispatch(event))
        persistence.rows[0]["name"] = "second"
        persistence.revision = 2
        asyncio.run(dispatcher.dispatch(event))

        assert sent == ["first", "second"]

    def test_legacy_dispatch_stops_when_refresh_store_is_unavailable(
        self,
        monkeypatch,
    ):
        dispatcher = _dispatcher()
        persistence = _LegacyPersistence()
        persistence.rows = [
            {
                "name": "alerts",
                "destination_type": "webhook",
                "config": {"url": _WEBHOOK_URL},
                "event_filter": None,
                "enabled": True,
            }
        ]
        persistence.revision = 1
        api = WebhookAPI(dispatcher, persistence)
        api.DESTINATION_SYNC_TTL_SECONDS = 0
        sent: list[str] = []

        async def record_send(event, destination):
            sent.append(destination.name)

        monkeypatch.setattr(
            dispatcher,
            "_send_to_destination",
            record_send,
        )
        event = SecurityEvent(
            event_id="event-legacy",
            event_type="auth_failure",
            timestamp="2026-08-07T00:00:00+00:00",
        )
        asyncio.run(dispatcher.dispatch(event))
        persistence.fail_load = True

        asyncio.run(dispatcher.dispatch(event))

        assert sent == ["alerts"]
        assert dispatcher.stats["errors"] == 1


class TestDestinationRedaction:
    @pytest.fixture
    def sensitive_destination(self):
        dispatcher = _dispatcher()
        persistence = _TenantPersistence()
        persistence.rows["tenant-a"] = [
            {
                "tenant_id": "tenant-a",
                "name": "private-alerts",
                "destination_type": "webhook",
                "config": {
                    "url": "https://user:secret@example.test/hook?token=value",
                    "headers": {"Authorization": "Bearer top-secret"},
                    "topic_arn": "arn:aws:sns:us-east-1:123:private",
                    "log_group": "/private/security",
                    "log_stream": "credential-bearing-stream",
                    "region": "us-east-1",
                    "timeout": 7,
                },
                "event_filter": ["auth_failure", "injection_blocked"],
                "enabled": True,
            }
        ]
        return dispatcher, persistence

    def test_member_serializer_redacts_targets_and_filters(
        self,
        sensitive_destination,
    ):
        dispatcher, persistence = sensitive_destination
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_auditor",
        )

        response = client.get("/admin/webhooks")
        destination = response.json()["destinations"][0]

        assert destination["config"] == {"configured": True}
        assert destination["event_filter_configured"] is True
        assert "event_filter" not in destination
        for secret in (
            "example.test",
            "top-secret",
            "arn:aws:sns",
            "/private/security",
            "credential-bearing-stream",
        ):
            assert secret not in response.text

    def test_admin_serializer_exposes_only_non_secret_operations(
        self,
        sensitive_destination,
    ):
        dispatcher, persistence = sensitive_destination
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )

        response = client.get("/admin/webhooks")
        destination = response.json()["destinations"][0]
        config = destination["config"]

        assert config["url_configured"] is True
        assert config["headers_configured"] is True
        assert config["topic_arn_configured"] is True
        assert config["log_group_configured"] is True
        assert config["log_stream_configured"] is True
        assert config["region"] == "us-east-1"
        assert config["timeout"] == 7
        assert destination["event_filter"] == [
            "auth_failure",
            "injection_blocked",
        ]
        for secret in (
            "example.test",
            "top-secret",
            "arn:aws:sns",
            "/private/security",
            "credential-bearing-stream",
        ):
            assert secret not in response.text

    def test_destination_test_never_returns_raw_error(
        self,
        sensitive_destination,
        monkeypatch,
    ):
        dispatcher, persistence = sensitive_destination
        client = _tenant_client(
            dispatcher,
            persistence,
            "tenant-a",
            "tenant_admin",
        )
        assert client.get("/admin/webhooks").status_code == 200

        async def fail_send(event, destination):
            raise RuntimeError("https://secret.example.test token=top-secret")

        monkeypatch.setattr(
            dispatcher,
            "_send_to_destination",
            fail_send,
        )
        response = client.post("/admin/webhooks/private-alerts/test")

        assert response.status_code == 502
        assert response.json()["error"] == "Destination delivery failed"
        assert "secret.example.test" not in response.text
        assert "top-secret" not in response.text
