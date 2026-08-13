"""Focused CloudFront application-browser authentication tests."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.auth.browser_session import (
    BrowserAuthAPI,
    BrowserAuthError,
    BrowserSessionConfig,
    BrowserSessionService,
    DynamoBrowserSessionStore,
    FLOW_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    browser_session_cookie_values,
    create_browser_auth_routes,
)
from src.gateway.auth.saml_service import (
    CLOUDFRONT_ENDPOINT_MODE,
    MANAGED_COGNITO_MODE,
    SamlConfig,
    SamlService,
)
from src.gateway.config import AppConfig, MAX_BROWSER_SESSION_SECONDS
from src.gateway.config_loader import load_app_config
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.middleware.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    ControlPlaneHTTPMiddleware,
)
from src.gateway.models import AuthMethod, RequestContext

NOW = 1_800_000_000
PUBLIC_URL = "https://d111111abcdef8.cloudfront.net"
HOSTED_UI_URL = (
    "https://axonllm-login.auth.us-east-1.amazoncognito.com"
)
CLIENT_ID = "public-pkce-client"
COGNITO_ISSUER = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Example"
)


class _MemoryStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(item: dict) -> tuple[str, str]:
        return item["PK"], item["SK"]

    async def create_flow(self, item: dict) -> bool:
        return await self._create(item)

    async def consume_flow(self, key: dict, *, now: int):
        stored = self.items.get(self._key(key))
        if stored is None or stored["expires_at"] <= now:
            return None
        return copy.deepcopy(self.items.pop(self._key(key)))

    async def create_session(self, item: dict) -> bool:
        return await self._create(item)

    async def _create(self, item: dict) -> bool:
        key = self._key(item)
        if key in self.items:
            return False
        self.items[key] = copy.deepcopy(item)
        return True

    async def get_session(self, key: dict):
        item = self.items.get(self._key(key))
        return copy.deepcopy(item) if item is not None else None

    async def replace_session(
        self,
        item: dict,
        *,
        expected_revision: int,
        now: int,
    ) -> bool:
        key = self._key(item)
        current = self.items.get(key)
        if (
            current is None
            or current["revision"] != expected_revision
            or current["absolute_expires_at"] <= now
        ):
            return False
        self.items[key] = copy.deepcopy(item)
        return True

    async def delete_session(
        self,
        key: dict,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        stored = self.items.get(self._key(key))
        if stored is None:
            return True
        if (
            expected_revision is not None
            and stored["revision"] != expected_revision
        ):
            return False
        self.items.pop(self._key(key), None)
        return True


class _OIDC:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def validate_id_token(
        self,
        token: str,
        *,
        expected_nonce: str | None = None,
    ) -> RequestContext | None:
        self.calls.append((token, expected_nonce))
        if token.startswith("invalid"):
            return None
        return RequestContext(
            user_id="user-1",
            project_id="project-1",
            roles=["tenant_member"],
            scopes=["openid"],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id="tenant-1",
            issuer=COGNITO_ISSUER,
            subject="subject-1",
        )


def _config(
    *,
    session_max_seconds: int = MAX_BROWSER_SESSION_SECONDS,
) -> BrowserSessionConfig:
    return BrowserSessionConfig(
        hosted_ui_url=HOSTED_UI_URL,
        client_id=CLIENT_ID,
        callback_url=f"{PUBLIC_URL}/auth/callback",
        signed_out_url=f"{PUBLIC_URL}/auth/signed-out",
        session_max_seconds=session_max_seconds,
    )


def _service(
    *,
    store: _MemoryStore | None = None,
    oidc: _OIDC | None = None,
    now: list[int] | None = None,
) -> tuple[BrowserSessionService, _MemoryStore, _OIDC, list[int]]:
    store = store or _MemoryStore()
    oidc = oidc or _OIDC()
    now = now or [NOW]
    return (
        BrowserSessionService(
            config=_config(),
            store=store,
            oidc_service=oidc,
            clock=lambda: now[0],
        ),
        store,
        oidc,
        now,
    )


def _authorization_state(location: str) -> str:
    return parse_qs(urlsplit(location).query)["state"][0]


@pytest.mark.asyncio
async def test_login_uses_authorization_code_and_s256_pkce() -> None:
    service, store, _, _ = _service()

    location, flow_token = await service.begin_login(
        "/admin/dashboard?tab=models"
    )

    parsed = urlsplit(location)
    query = parse_qs(parsed.query)
    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        == f"{HOSTED_UI_URL}/oauth2/authorize"
    )
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    assert len(query["state"][0]) == 43
    assert len(query["nonce"][0]) == 43
    assert flow_token == query["state"][0]

    [flow] = store.items.values()
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(flow["code_verifier"].encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert query["code_challenge"] == [expected_challenge]
    assert flow["return_to"] == "/admin/dashboard?tab=models"
    assert flow["expires_at"] == NOW + 600
    assert query["state"][0] not in flow["PK"]


@pytest.mark.asyncio
async def test_callback_consumes_state_once_and_session_crosses_replicas(
    monkeypatch,
) -> None:
    service, store, oidc, now = _service()
    location, _flow_token = await service.begin_login()
    state = _authorization_state(location)
    nonce = next(iter(store.items.values()))["nonce"]

    async def tokens(_form):
        return {
            "id_token": "id-token-1",
            "refresh_token": "refresh-token-1",
            "expires_in": 900,
        }

    monkeypatch.setattr(service, "_request_tokens", tokens)
    session_token, return_to = await service.complete_login(
        code="authorization-code",
        state=state,
    )

    assert return_to == "/admin/dashboard"
    assert len(session_token) == 43
    assert oidc.calls == [("id-token-1", nonce)]
    session = next(
        item
        for item in store.items.values()
        if item["entity_type"] == "browser_session"
    )
    assert session["absolute_expires_at"] == (
        NOW + MAX_BROWSER_SESSION_SECONDS
    )
    assert session["expires_at"] == session["absolute_expires_at"]
    assert session_token not in session["PK"]

    replica = BrowserSessionService(
        config=_config(),
        store=store,
        oidc_service=oidc,
        clock=lambda: now[0],
    )
    context = await replica.authenticate(session_token)
    assert context is not None
    assert context.tenant_id == "tenant-1"

    with pytest.raises(BrowserAuthError, match="already used"):
        await service.complete_login(
            code="another-code",
            state=state,
        )


@pytest.mark.asyncio
async def test_refresh_is_revisioned_and_absolute_expiry_is_not_extended(
    monkeypatch,
) -> None:
    service, store, oidc, now = _service()
    location, _flow_token = await service.begin_login()
    state = _authorization_state(location)
    responses = [
        {
            "id_token": "id-token-1",
            "refresh_token": "refresh-token-1",
            "expires_in": 100,
        },
        {
            "id_token": "id-token-2",
            "expires_in": 100,
        },
    ]

    async def tokens(_form):
        return responses.pop(0)

    monkeypatch.setattr(service, "_request_tokens", tokens)
    session_token, _ = await service.complete_login(
        code="authorization-code",
        state=state,
    )
    initial = next(
        item
        for item in store.items.values()
        if item["entity_type"] == "browser_session"
    )
    absolute_expiry = initial["absolute_expires_at"]

    now[0] = initial["refresh_after"] + 1
    context = await service.authenticate(session_token)
    assert context is not None
    refreshed = next(iter(store.items.values()))
    assert refreshed["revision"] == 3
    assert refreshed["refresh_token"] == "refresh-token-1"
    assert refreshed["absolute_expires_at"] == absolute_expiry
    assert oidc.calls[-1] == ("id-token-2", None)

    now[0] = absolute_expiry + 1
    assert await service.authenticate(session_token) is None
    assert store.items == {}


@pytest.mark.asyncio
async def test_refresh_lease_serializes_multiple_replicas(
    monkeypatch,
) -> None:
    service, store, oidc, now = _service()
    location, _flow_token = await service.begin_login()
    state = _authorization_state(location)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    refresh_calls = 0

    async def tokens(form):
        nonlocal refresh_calls
        if form["grant_type"] == "authorization_code":
            return {
                "id_token": "id-token-1",
                "refresh_token": "refresh-token-1",
                "expires_in": 100,
            }
        refresh_calls += 1
        refresh_started.set()
        await release_refresh.wait()
        return {
            "id_token": "id-token-2",
            "expires_in": 100,
        }

    monkeypatch.setattr(service, "_request_tokens", tokens)
    session_token, _ = await service.complete_login(
        code="authorization-code",
        state=state,
    )
    session = next(iter(store.items.values()))
    now[0] = session["refresh_after"] + 1

    replica = BrowserSessionService(
        config=_config(),
        store=store,
        oidc_service=oidc,
        clock=lambda: now[0],
    )
    monkeypatch.setattr(replica, "_request_tokens", tokens)
    first = asyncio.create_task(service.authenticate(session_token))
    await refresh_started.wait()
    second_context = await replica.authenticate(session_token)
    release_refresh.set()
    first_context = await first

    assert first_context is not None
    assert second_context is not None
    assert refresh_calls == 1


def test_browser_routes_set_host_cookie_and_no_store(
    monkeypatch,
) -> None:
    service, _, _, _ = _service()
    logged_out: list[str | None] = []

    async def complete_login(*, code, state):
        assert code == "code"
        assert len(state) == 43
        return "A" * 43, "/admin/dashboard"

    async def logout(token):
        logged_out.append(token)

    monkeypatch.setattr(service, "complete_login", complete_login)
    monkeypatch.setattr(service, "logout", logout)
    app = Starlette(
        routes=create_browser_auth_routes(BrowserAuthAPI(service))
    )
    with TestClient(
        app,
        base_url=PUBLIC_URL,
        follow_redirects=False,
    ) as client:
        login = client.get("/auth/login")
        state = _authorization_state(login.headers["location"])
        callback = client.get(
            f"/auth/callback?code=code&state={state}"
        )
        config = client.get("/auth/config")
        logged_out_response = client.post(
            "/auth/logout",
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={'A' * 43}",
            },
        )
        get_logout = client.get("/auth/logout")
        signed_out = client.get("/auth/signed-out")

    assert callback.status_code == 302
    assert callback.headers["location"] == "/admin/dashboard"
    login_cookie = login.headers["set-cookie"]
    assert f"{FLOW_COOKIE_NAME}={state}" in login_cookie
    assert "Path=/" in login_cookie
    assert "Secure" in login_cookie
    assert "HttpOnly" in login_cookie
    assert "SameSite=lax" in login_cookie
    assert "Domain=" not in login_cookie
    callback_cookies = callback.headers.get_list("set-cookie")
    session_cookie = next(
        value
        for value in callback_cookies
        if f"{SESSION_COOKIE_NAME}={'A' * 43}" in value
    )
    assert "Path=/" in session_cookie
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Domain=" not in session_cookie
    assert any(
        f"{FLOW_COOKIE_NAME}=" in value and "Max-Age=0" in value
        for value in callback_cookies
    )
    assert logged_out == ["A" * 43]
    logout_location = urlsplit(
        logged_out_response.json()["logout_url"]
    )
    assert (
        f"{logout_location.scheme}://{logout_location.netloc}"
        f"{logout_location.path}"
        == f"{HOSTED_UI_URL}/logout"
    )
    assert parse_qs(logout_location.query) == {
        "client_id": [CLIENT_ID],
        "logout_uri": [f"{PUBLIC_URL}/auth/signed-out"],
    }
    assert get_logout.status_code == 405
    logout_cookies = logged_out_response.headers.get_list("set-cookie")
    assert any(
        f"{SESSION_COOKIE_NAME}=" in value and "Max-Age=0" in value
        for value in logout_cookies
    )
    assert any(
        f"{CSRF_COOKIE_NAME}=" in value and "Max-Age=0" in value
        for value in logout_cookies
    )
    for response in (
        callback,
        login,
        config,
        logged_out_response,
        signed_out,
    ):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"


def test_callback_rejects_state_not_bound_to_the_browser(
    monkeypatch,
) -> None:
    service, _, _, _ = _service()
    completed: list[tuple[str, str]] = []

    async def complete_login(*, code, state):
        completed.append((code, state))
        return "A" * 43, "/admin/dashboard"

    monkeypatch.setattr(service, "complete_login", complete_login)
    app = Starlette(
        routes=create_browser_auth_routes(BrowserAuthAPI(service))
    )
    with TestClient(
        app,
        base_url=PUBLIC_URL,
        follow_redirects=False,
    ) as client:
        response = client.get(
            f"/auth/callback?code=stolen&state={'S' * 43}"
        )

    assert response.status_code == 400
    assert completed == []
    assert any(
        f"{FLOW_COOKIE_NAME}=" in value and "Max-Age=0" in value
        for value in response.headers.get_list("set-cookie")
    )


class _MiddlewareBrowserSessions:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticate(self, token: str):
        self.calls.append(token)
        if token != "A" * 43:
            return None
        return RequestContext(
            user_id="user-1",
            project_id="project-1",
            roles=["member"],
            scopes=[],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id="tenant-1",
        )

    def login_url(self, return_to: str | None = None) -> str:
        return "/auth/login?return_to=" + (
            return_to or "/admin/dashboard"
        )


def _auth_app(service: _MiddlewareBrowserSessions) -> Starlette:
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse(
            {"user_id": request.state.context.user_id}
        )

    app = Starlette(
        routes=[
            Route("/admin/dashboard", handler),
            Route("/admin/projects", handler),
            Route("/api/data", handler),
        ]
    )
    app.add_middleware(
        AuthMiddleware,
        browser_session_service=service,
        mode="ENFORCE",
    )
    return app


def test_browser_session_precedes_headers_and_conflicts_fail_closed() -> None:
    service = _MiddlewareBrowserSessions()
    with TestClient(
        _auth_app(service),
        base_url=PUBLIC_URL,
    ) as client:
        authenticated = client.get(
            "/admin/projects",
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={'A' * 43}",
                "Accept": "application/json",
            },
        )
        conflict = client.get(
            "/admin/projects",
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={'A' * 43}",
                "Authorization": "Bearer direct-token",
                "Accept": "application/json",
            },
        )

    assert authenticated.status_code == 200
    assert authenticated.json()["user_id"] == "user-1"
    assert conflict.status_code == 401
    assert service.calls == ["A" * 43]


def test_only_browser_document_navigation_redirects_to_login() -> None:
    service = _MiddlewareBrowserSessions()
    with TestClient(
        _auth_app(service),
        base_url=PUBLIC_URL,
        follow_redirects=False,
    ) as client:
        navigation = client.get(
            "/admin/dashboard",
            headers={
                "Accept": "text/html",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        api = client.get(
            "/admin/projects",
            headers={"Accept": "application/json"},
        )
        xhr = client.get(
            "/admin/projects",
            headers={
                "Accept": "text/html",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        expired = client.get(
            "/admin/dashboard",
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={'B' * 43}",
                "Accept": "text/html",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )

    assert navigation.status_code == 302
    assert navigation.headers["location"].startswith("/auth/login")
    assert api.status_code == 401
    assert api.json()["error"]["login_url"].startswith("/auth/login")
    assert xhr.status_code == 401
    assert expired.status_code == 302
    assert f"{SESSION_COOKIE_NAME}=" in expired.headers["set-cookie"]
    assert "Max-Age=0" in expired.headers["set-cookie"]


def test_app_browser_session_receives_and_must_echo_csrf_cookie() -> None:
    async def handler(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route(
                "/admin/projects",
                handler,
                methods=["GET", "POST"],
            )
        ]
    )
    app.add_middleware(
        ControlPlaneHTTPMiddleware,
        production=True,
    )
    session_cookie = f"{SESSION_COOKIE_NAME}={'A' * 43}"
    with TestClient(app, base_url=PUBLIC_URL) as client:
        page = client.get(
            "/admin/projects",
            headers={"Cookie": session_cookie},
        )
        token = page.cookies.get(CSRF_COOKIE_NAME)
        rejected = client.post(
            "/admin/projects",
            content=b"{}",
            headers={"Cookie": session_cookie},
        )
        mixed_rejected = client.post(
            "/admin/projects",
            content=b"{}",
            headers={
                "Cookie": session_cookie,
                "Authorization": "Bearer conflicting-token",
            },
        )
        accepted = client.post(
            "/admin/projects",
            content=b"{}",
            headers={
                "Cookie": (
                    f"{session_cookie}; {CSRF_COOKIE_NAME}={token}"
                ),
                CSRF_HEADER_NAME: token,
            },
        )

    assert token is not None
    assert rejected.status_code == 403
    assert mixed_rejected.status_code == 403
    assert accepted.status_code == 200


def test_cloudfront_config_loader_requires_public_cognito_contract(
    monkeypatch,
) -> None:
    values = {
        "AXON_DEPLOYMENT_PROFILE": "production",
        "AXON_AUTH_MODE": "ENFORCE",
        "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
        "LLM_ROUTER_DYNAMODB_ENABLED": "true",
        "AXON_CONTROL_PLANE_ONLY": "true",
        "AXON_CONTROL_PLANE_ENDPOINT_MODE": "cloudfront",
        "AXON_CONTROL_PLANE_URL": PUBLIC_URL,
        "AXON_OIDC_ISSUER": COGNITO_ISSUER,
        "AXON_OIDC_AUDIENCE": CLIENT_ID,
        "AXON_BROWSER_AUTH_MODE": "oidc-session",
        "AXON_BROWSER_AUTH_CLIENT_ID": CLIENT_ID,
        "AXON_BROWSER_AUTH_AUTHORIZATION_ENDPOINT": (
            f"{HOSTED_UI_URL}/oauth2/authorize"
        ),
        "AXON_BROWSER_AUTH_TOKEN_ENDPOINT": (
            f"{HOSTED_UI_URL}/oauth2/token"
        ),
        "AXON_BROWSER_AUTH_LOGOUT_ENDPOINT": (
            f"{HOSTED_UI_URL}/logout"
        ),
        "AXON_BROWSER_AUTH_REDIRECT_URI": (
            f"{PUBLIC_URL}/auth/callback"
        ),
        "AXON_BROWSER_AUTH_SIGNED_OUT_URI": (
            f"{PUBLIC_URL}/auth/signed-out"
        ),
        "AXON_BROWSER_AUTH_SESSION_TTL_SECONDS": "28800",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config = load_app_config()

    assert config.browser_auth_enabled is True
    assert config.browser_auth_callback_url == (
        f"{PUBLIC_URL}/auth/callback"
    )
    assert config.browser_session_max_seconds == 8 * 60 * 60


def test_browser_session_lifetime_cannot_exceed_eight_hours() -> None:
    with pytest.raises(ValueError, match="browser_session_max_seconds"):
        AppConfig(
            browser_session_max_seconds=(
                MAX_BROWSER_SESSION_SECONDS + 1
            )
        )


def test_saml_handoff_uses_application_login_in_cloudfront_mode() -> None:
    config = SamlConfig(
        federation_mode=MANAGED_COGNITO_MODE,
        login_path="/admin/dashboard",
        deployment_profile="production",
        auth_mode="ENFORCE",
        canonical_identity_required=True,
        control_plane_only=True,
        aws_region="us-east-1",
        oidc_issuer=COGNITO_ISSUER,
        oidc_audience=CLIENT_ID,
        endpoint_mode=CLOUDFRONT_ENDPOINT_MODE,
        browser_auth_client_id=CLIENT_ID,
    )
    service = SamlService(config)

    assert service.enabled is True
    target = service.login_target("/chat?project=alpha")
    assert target.startswith("/auth/login?")
    assert parse_qs(urlsplit(target).query)["return_to"] == [
        "/chat?project=alpha"
    ]


class _ConditionalFailure(Exception):
    response = {
        "Error": {"Code": "ConditionalCheckFailedException"}
    }


class _DynamoTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.last_get: dict | None = None

    def put_item(self, **kwargs):
        item = copy.deepcopy(kwargs["Item"])
        key = (item["PK"], item["SK"])
        if key in self.items:
            raise _ConditionalFailure
        self.items[key] = item
        return {}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        stored = self.items.get((key["PK"], key["SK"]))
        now = kwargs.get("ExpressionAttributeValues", {}).get(":now")
        if stored is None or (
            now is not None and stored["expires_at"] <= now
        ):
            raise _ConditionalFailure
        self.items.pop((key["PK"], key["SK"]))
        return {"Attributes": copy.deepcopy(stored)}

    def get_item(self, **kwargs):
        self.last_get = copy.deepcopy(kwargs)
        key = kwargs["Key"]
        stored = self.items.get((key["PK"], key["SK"]))
        return (
            {"Item": copy.deepcopy(stored)}
            if stored is not None
            else {}
        )


class _Persistence:
    enabled = True

    def __init__(self) -> None:
        self.table = _DynamoTable()

    def _get_table(self):
        return self.table


@pytest.mark.asyncio
async def test_dynamo_flow_consumption_is_atomic_and_reads_are_strong() -> None:
    persistence = _Persistence()
    store = DynamoBrowserSessionStore(persistence)
    flow = {
        "PK": "BROWSER_AUTH_FLOW#digest",
        "SK": "FLOW",
        "expires_at": NOW + 60,
    }
    assert await store.create_flow(flow) is True
    assert (
        await store.consume_flow(
            {"PK": flow["PK"], "SK": flow["SK"]},
            now=NOW,
        )
        == flow
    )
    assert (
        await store.consume_flow(
            {"PK": flow["PK"], "SK": flow["SK"]},
            now=NOW,
        )
        is None
    )

    session = {
        "PK": "BROWSER_SESSION#digest",
        "SK": "SESSION",
        "expires_at": NOW + 60,
    }
    assert await store.create_session(session) is True
    assert await store.get_session(
        {"PK": session["PK"], "SK": session["SK"]}
    ) == session
    assert persistence.table.last_get["ConsistentRead"] is True


def test_cookie_parser_preserves_duplicate_session_credentials() -> None:
    assert browser_session_cookie_values(
        [
            f"a=1; {SESSION_COOKIE_NAME}=first",
            f"{SESSION_COOKIE_NAME}=second",
        ]
    ) == ["first", "second"]
