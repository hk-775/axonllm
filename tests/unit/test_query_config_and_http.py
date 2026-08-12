"""Configuration and HTTP contracts for the read-only query plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from src.gateway.config import AppConfig
from src.gateway.config_loader import load_app_config
from src.gateway.bootstrap import (
    build_gateway_components,
    build_starlette_app,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    RequestContext,
    TenantRole,
)
from src.gateway.query.routes import (
    QueryAPI,
    _request_object,
    create_query_routes,
)
from src.gateway.query.service import QueryServiceError


@dataclass
class _QueryService:
    result: dict[str, Any] | None = None
    error: QueryServiceError | None = None
    call: dict[str, Any] | None = None

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.call = kwargs
        if self.error is not None:
            raise self.error
        return self.result or {"request_id": "qry_test", "rows": []}


class _IdentityMiddleware:
    def __init__(self, app: Any, *, include_identity: bool = True) -> None:
        self.app = app
        self.include_identity = include_identity

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http" and self.include_identity:
            principal = Principal(
                principal_id="principal-a",
                tenant_id="tenant-a",
                subject="subject-a",
                issuer="https://issuer.example",
                roles=frozenset({TenantRole.TENANT_MEMBER}),
                auth_method=AuthMethod.OIDC_JWT,
                membership_status=MembershipStatus.ACTIVE,
                project_ids=frozenset({"project-a"}),
            )
            scope.setdefault("state", {})
            scope["state"]["principal"] = principal
            scope["state"]["context"] = RequestContext(
                user_id=principal.principal_id,
                project_id="project-a",
                roles=[TenantRole.TENANT_MEMBER.value],
                scopes=[],
                auth_method=AuthMethod.OIDC_JWT,
                tenant_id="tenant-a",
            )
        await self.app(scope, receive, send)


def _client(
    service: _QueryService,
    *,
    include_identity: bool = True,
) -> TestClient:
    app = Starlette(routes=create_query_routes(QueryAPI(service)))
    app.add_middleware(
        _IdentityMiddleware,
        include_identity=include_identity,
    )
    return TestClient(app)


def _streaming_request(
    chunks: list[bytes],
    *,
    stream_error: Exception | None = None,
) -> tuple[Request, list[int]]:
    events = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    receive_calls: list[int] = []

    async def receive() -> dict[str, object]:
        receive_calls.append(1)
        if stream_error is not None:
            raise stream_error
        return events.pop(0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/query",
        "raw_path": b"/v1/query",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("testserver", 80),
    }
    return Request(scope, receive), receive_calls


async def test_http_query_stream_reader_stops_at_64_kib() -> None:
    request, calls = _streaming_request(
        [
            b"x" * (32 * 1024),
            b"x" * (32 * 1024 + 1),
            b"unread",
        ]
    )

    with pytest.raises(ValueError, match="exceeds 64 KiB"):
        await _request_object(request)

    assert len(calls) == 2


async def test_http_query_stream_failures_are_sanitized() -> None:
    secret = "sensitive-asgi-stream-failure"
    request, _ = _streaming_request(
        [],
        stream_error=RuntimeError(secret),
    )

    with pytest.raises(
        ValueError,
        match="query request body could not be read",
    ) as raised:
        await _request_object(request)

    assert secret not in str(raised.value)


def test_http_query_uses_only_canonical_tenant_and_project() -> None:
    service = _QueryService()

    response = _client(service).post(
        "/v1/query",
        json={
            "project_id": "project-a",
            "datasource_id": "warehouse",
            "sql": "SELECT 1",
            "max_rows": 10,
            "request_id": "request-a",
        },
    )

    assert response.status_code == 200
    assert service.call is not None
    assert service.call["tenant_id"] == "tenant-a"
    assert service.call["project_id"] == "project-a"
    assert service.call["datasource_id"] == "warehouse"
    assert service.call["sql"] == "SELECT 1"


def test_http_query_rejects_project_context_override() -> None:
    service = _QueryService()

    response = _client(service).post(
        "/v1/query",
        json={
            "project_id": "project-b",
            "datasource_id": "warehouse",
            "sql": "SELECT 1",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == (
        "project_context_mismatch"
    )
    assert service.call is None


def test_http_query_rejects_unknown_fields_before_execution() -> None:
    service = _QueryService()

    response = _client(service).post(
        "/v1/query",
        json={
            "datasource_id": "warehouse",
            "sql": "SELECT 1",
            "role_arn": "arn:aws:iam::123456789012:role/attacker",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query_request"
    assert service.call is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"datasource_id":"one","datasource_id":"two","sql":"SELECT 1"}',
        b'{"datasource_id":"warehouse","sql":"SELECT 1","max_rows":NaN}',
    ],
)
def test_http_query_rejects_ambiguous_json(body: bytes) -> None:
    service = _QueryService()

    response = _client(service).post(
        "/v1/query",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query_request"
    assert service.call is None


def test_http_query_requires_canonical_identity() -> None:
    response = _client(
        _QueryService(),
        include_identity=False,
    ).post(
        "/v1/query",
        json={"datasource_id": "warehouse", "sql": "SELECT 1"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == (
        "canonical_identity_required"
    )


def test_http_query_returns_only_sanitized_service_error() -> None:
    service = _QueryService(
        error=QueryServiceError(
            503,
            "athena_role_unavailable",
            "The project query role could not be assumed.",
        )
    )

    response = _client(service).post(
        "/v1/query",
        json={"datasource_id": "warehouse", "sql": "SELECT 1"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "type": "query_error",
            "code": "athena_role_unavailable",
            "message": "The project query role could not be assumed.",
        }
    }


def _clean_query_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("AXON_") or key == (
            "LLM_ROUTER_DYNAMODB_ENABLED"
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")


def test_query_plane_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_query_environment(monkeypatch)

    config = load_app_config()

    assert config.athena_query_enabled is False
    assert config.athena_query_bindings == ""
    assert config.control_plane_only is False


def test_control_plane_only_omits_all_data_plane_routes() -> None:
    app = build_starlette_app(
        AppConfig(
            deployment_profile="development",
            auth_mode="LOG_ONLY",
            control_plane_only=True,
        )
    )
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/admin/datasources" not in paths
    assert "/api/chat" not in paths
    assert "/api/chat/stream" not in paths
    assert "/api/models" not in paths
    assert "/v1/chat/completions" not in paths
    assert "/v1/models" not in paths
    assert "/v1/query" not in paths
    assert "/admin/projects" in paths
    assert "/health" in paths
    assert "/ready" in paths


@pytest.mark.parametrize(
    ("control_plane_only", "expected"),
    [
        (False, ["start", "stop"]),
        (True, []),
    ],
)
def test_query_reconciler_lifecycle_is_data_plane_only(
    monkeypatch: pytest.MonkeyPatch,
    control_plane_only: bool,
    expected: list[str],
) -> None:
    calls: list[str] = []

    class Worker:
        async def start(self) -> None:
            calls.append("start")

        async def stop(self) -> None:
            calls.append("stop")

    components = build_gateway_components(
        AppConfig(
            deployment_profile="development",
            auth_mode="LOG_ONLY",
        )
    )
    components.datasource_repository = _QueryService()
    components.query_service = _QueryService()
    components.query_reconciliation_worker = Worker()
    monkeypatch.setattr(
        "src.gateway.bootstrap.build_gateway_components",
        lambda *_args, **_kwargs: components,
    )
    app = build_starlette_app(
        AppConfig(
            deployment_profile="development",
            auth_mode="ENFORCE",
            canonical_identity_required=True,
            durable_persistence_enabled=True,
            athena_query_enabled=True,
            control_plane_only=control_plane_only,
        )
    )

    with TestClient(app):
        if control_plane_only:
            assert calls == []
        else:
            assert calls == ["start"]

    assert calls == expected


def test_query_settings_are_loaded_strictly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_query_environment(monkeypatch)
    monkeypatch.setenv("AXON_AUTH_MODE", "ENFORCE")
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "true")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "true")
    monkeypatch.setenv("AXON_ATHENA_QUERY_ENABLED", "true")
    monkeypatch.setenv("AXON_CONTROL_PLANE_ONLY", "true")
    monkeypatch.setenv("AXON_ATHENA_QUERY_MAX_ROWS", "250")
    monkeypatch.setenv(
        "AXON_ATHENA_QUERY_MAX_RESULT_BYTES",
        "524288",
    )
    monkeypatch.setenv(
        "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED",
        "104857600",
    )
    monkeypatch.setenv(
        "AXON_ATHENA_QUERY_TIMEOUT_SECONDS",
        "12.5",
    )

    config = load_app_config()

    assert config.athena_query_enabled is True
    assert config.control_plane_only is True
    assert config.athena_query_max_rows == 250
    assert config.athena_query_max_result_bytes == 524288
    assert config.athena_query_max_bytes_scanned == 104857600
    assert config.athena_query_timeout_seconds == 12.5


@pytest.mark.parametrize("raw", ["1", "yes", "enabled", ""])
def test_query_enablement_rejects_ambiguous_boolean(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    _clean_query_environment(monkeypatch)
    monkeypatch.setenv("AXON_ATHENA_QUERY_ENABLED", raw)

    with pytest.raises(ValueError, match="true.*false"):
        load_app_config()


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_mode": "LOG_ONLY"},
        {"canonical_identity_required": False},
        {"durable_persistence_enabled": False},
    ],
)
def test_query_enablement_rejects_weak_runtime(
    changes: dict[str, object],
) -> None:
    settings: dict[str, object] = {
        "deployment_profile": "development",
        "auth_mode": "ENFORCE",
        "canonical_identity_required": True,
        "durable_persistence_enabled": True,
        "athena_query_enabled": True,
    }
    settings.update(changes)

    with pytest.raises(RuntimeError, match="canonical identity"):
        AppConfig(**settings)


def test_query_bindings_enforce_agentcore_character_boundary() -> None:
    multibyte_character = "\u00e9"

    AppConfig(
        athena_query_bindings=multibyte_character * 2_048,
    )
    with pytest.raises(ValueError, match="2,048-character"):
        AppConfig(
            athena_query_bindings=multibyte_character * 2_049,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("athena_query_timeout_seconds", 0),
        ("athena_query_timeout_seconds", True),
        ("athena_query_timeout_seconds", "30"),
        ("athena_query_timeout_seconds", float("nan")),
        ("athena_query_max_rows", 0),
        ("athena_query_max_rows", 10_001),
        ("athena_query_max_result_bytes", 1023),
        ("athena_query_max_bytes_scanned", 0),
        ("athena_query_poll_interval_seconds", 0.01),
        ("athena_query_poll_interval_seconds", False),
        ("athena_query_poll_interval_seconds", "0.25"),
    ],
)
def test_query_limits_reject_unsafe_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        AppConfig(**{field: value})
