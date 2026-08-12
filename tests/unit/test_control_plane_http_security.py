"""Focused HTTP hardening tests for the browser control plane."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.config import AppConfig
from src.gateway.bootstrap import build_starlette_app
from src.gateway.middleware.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    ControlPlaneHTTPMiddleware,
)

ALB_HEADERS = {
    "X-Amzn-Oidc-Data": "signed-alb-token",
    "X-Amzn-Oidc-Identity": "user-123",
}
BEARER_HEADERS = {"Authorization": "Bearer direct-jwt"}
REPO_ROOT = Path(__file__).parents[2]


async def _read_body(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def _read_json(request: Request) -> JSONResponse:
    return JSONResponse(await request.json())


async def _status(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def _pass_through(request: Request, call_next):
    return await call_next(request)


def _app(
    *,
    production: bool = True,
    max_bytes: int = 64,
    request_max_bytes: int = 1024 * 1024,
) -> Starlette:
    app = Starlette(
        routes=[
            Route(
                "/admin/projects",
                _read_body,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            ),
            Route("/admin/json", _read_json, methods=["POST"]),
            Route("/v1/chat/completions", _read_body, methods=["POST"]),
            Route("/health", _status, methods=["GET"]),
        ]
    )
    # The production app has authentication/RBAC BaseHTTPMiddleware inside this
    # pure-ASGI boundary. Keep that receive/replay interaction under test.
    app.add_middleware(BaseHTTPMiddleware, dispatch=_pass_through)
    app.add_middleware(
        ControlPlaneHTTPMiddleware,
        production=production,
        request_max_body_bytes=request_max_bytes,
        admin_max_body_bytes=max_bytes,
    )
    return app


def _secure_client(app: Starlette) -> TestClient:
    return TestClient(app, base_url="https://control.example.test")


def test_alb_browser_get_issues_host_only_csrf_cookie() -> None:
    with _secure_client(_app()) as client:
        response = client.get("/admin/projects", headers=ALB_HEADERS)

    assert response.status_code == 200
    token = response.cookies.get(CSRF_COOKIE_NAME)
    assert token is not None
    assert len(token) == 43
    set_cookie = response.headers["set-cookie"]
    assert f"{CSRF_COOKIE_NAME}={token}" in set_cookie
    assert "Path=/" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Domain=" not in set_cookie
    assert "HttpOnly" not in set_cookie
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "csrf_header",
    [None, "A" * 43],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_alb_browser_mutation_rejects_missing_or_mismatched_csrf(
    csrf_header: str | None,
    method: str,
) -> None:
    with _secure_client(_app()) as client:
        page = client.get("/admin/projects", headers=ALB_HEADERS)
        headers = dict(ALB_HEADERS)
        if csrf_header is not None:
            headers[CSRF_HEADER_NAME] = csrf_header

        response = client.request(
            method,
            "/admin/projects",
            content=b"{}",
            headers=headers,
        )

    assert page.status_code == 200
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_alb_browser_mutation_accepts_matching_double_submit_token() -> None:
    with _secure_client(_app()) as client:
        page = client.get("/admin/projects", headers=ALB_HEADERS)
        token = page.cookies.get(CSRF_COOKIE_NAME)

        response = client.post(
            "/admin/projects",
            content=b'{"name":"analytics"}',
            headers={**ALB_HEADERS, CSRF_HEADER_NAME: token},
        )

    assert response.status_code == 200
    assert response.json() == {"size": 20}


def test_duplicate_csrf_headers_fail_closed() -> None:
    with _secure_client(_app()) as client:
        page = client.get("/admin/projects", headers=ALB_HEADERS)
        token = page.cookies.get(CSRF_COOKIE_NAME)
        response = client.post(
            "/admin/projects",
            content=b"{}",
            headers=[
                *ALB_HEADERS.items(),
                (CSRF_HEADER_NAME, token),
                (CSRF_HEADER_NAME, token),
            ],
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


@pytest.mark.parametrize(
    "credentials",
    [
        BEARER_HEADERS,
        {"X-Api-Key": "axon_direct-api-key"},
    ],
)
def test_explicit_admin_api_credentials_do_not_require_csrf(
    credentials: dict[str, str],
) -> None:
    with _secure_client(_app()) as client:
        response = client.post(
            "/admin/projects",
            content=b"{}",
            headers=credentials,
        )

    assert response.status_code == 200
    assert response.json() == {"size": 2}


def test_oversized_declared_admin_body_is_rejected_before_handler() -> None:
    with _secure_client(_app(max_bytes=16)) as client:
        response = client.post(
            "/admin/projects",
            content=b"x" * 17,
            headers=BEARER_HEADERS,
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"


def test_chunked_admin_body_is_limited_without_content_length() -> None:
    def chunks():
        yield b"x" * 9
        yield b"y" * 8

    with _secure_client(_app(max_bytes=16)) as client:
        response = client.post(
            "/admin/projects",
            content=chunks(),
            headers=BEARER_HEADERS,
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"


def test_admin_body_is_replayed_unchanged_to_json_handler() -> None:
    with _secure_client(_app(max_bytes=64)) as client:
        response = client.post(
            "/admin/json",
            json={"name": "analytics"},
            headers=BEARER_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {"name": "analytics"}


def test_non_admin_bearer_api_keeps_its_existing_body_contract() -> None:
    payload = b"x" * 65
    with _secure_client(_app(max_bytes=64)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=payload,
            headers=BEARER_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {"size": len(payload)}


def test_non_admin_request_is_rejected_at_global_ceiling() -> None:
    payload = b"x" * 65
    with _secure_client(_app(max_bytes=16, request_max_bytes=64)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=payload,
            headers=BEARER_HEADERS,
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"


def test_production_headers_cover_admin_success_and_limit_errors() -> None:
    with _secure_client(_app(production=True, max_bytes=16)) as client:
        success = client.get("/admin/projects")
        rejected = client.post(
            "/admin/projects",
            content=b"x" * 17,
            headers=BEARER_HEADERS,
        )

    for response in (success, rejected):
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-xss-protection"] == "0"
        assert "camera=()" in response.headers["permissions-policy"]
        assert "default-src 'self'" in response.headers[
            "content-security-policy"
        ]
        assert "frame-ancestors 'self'" in response.headers[
            "content-security-policy"
        ]
        assert "script-src 'self'" in response.headers[
            "content-security-policy"
        ]
        assert "style-src 'unsafe-inline'" in response.headers[
            "content-security-policy"
        ]
        assert "'unsafe-inline'" not in response.headers[
            "content-security-policy"
        ].partition("script-src")[2].partition(";")[0]
        assert "'unsafe-eval'" not in response.headers[
            "content-security-policy"
        ]
        assert response.headers["cache-control"] == "no-store"


def test_non_admin_production_policy_does_not_constrain_api_resources() -> None:
    with _secure_client(_app(production=True)) as client:
        response = client.get("/health")

    policy = response.headers["content-security-policy"]
    assert policy == (
        "base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'self'; form-action 'self'"
    )
    assert "unsafe-eval" not in policy


def test_development_does_not_claim_transport_security() -> None:
    with _secure_client(_app(production=False)) as client:
        response = client.get("/health")

    assert "strict-transport-security" not in response.headers
    assert "content-security-policy" not in response.headers


def test_dashboard_request_wrapper_echoes_the_csrf_cookie() -> None:
    source = (
        REPO_ROOT / "src" / "gateway" / "admin" / "dashboard.jsx"
    ).read_text(encoding="utf-8")
    compiled = (
        REPO_ROOT
        / "src"
        / "gateway"
        / "admin"
        / "static"
        / "dashboard.js"
    ).read_text(encoding="utf-8")

    for content in (source, compiled):
        assert "const CSRF_COOKIE = '__Host-axon-csrf';" in content
        assert "headers['X-Axon-CSRF-Token'] = csrfToken;" in content
        assert "credentials: 'same-origin'" in content


def test_dashboard_loads_only_external_same_origin_scripts() -> None:
    dashboard = (
        REPO_ROOT
        / "src"
        / "gateway"
        / "admin"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")

    script_tags = re.findall(r"<script\b([^>]*)>", dashboard)
    assert len(script_tags) == 3
    assert all(re.search(r'\bsrc="/admin/static/', tag) for tag in script_tags)
    assert dashboard.count("</script>") == len(script_tags)
    assert 'type="text/babel"' not in dashboard
    assert "/admin/static/vendor/babel.min.js" not in dashboard


def test_dashboard_compiled_asset_and_digest_are_current() -> None:
    compiled_path = (
        REPO_ROOT
        / "src"
        / "gateway"
        / "admin"
        / "static"
        / "dashboard.js"
    )
    compiled = compiled_path.read_bytes()
    digest = hashlib.sha256(compiled).hexdigest()
    dashboard = (
        REPO_ROOT
        / "src"
        / "gateway"
        / "admin"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert f"/admin/static/dashboard.js?v={digest}" in dashboard
    result = subprocess.run(
        ["node", "scripts/build_admin_dashboard.cjs", "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bootstrap_installs_control_plane_security_outermost() -> None:
    app = build_starlette_app(
        AppConfig(
            models_config_path="config/models.yaml",
            providers_config_path="config/providers.yaml",
            pricing_config_path="config/pricing.yaml",
            demo_seed_config_path="config/demo_seed.yaml",
            catalog_config_path="config/catalog.yaml",
        )
    )

    middleware = app.user_middleware[0]
    assert middleware.cls is ControlPlaneHTTPMiddleware
    assert middleware.kwargs["production"] is False
