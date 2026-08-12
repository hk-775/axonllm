#!/usr/bin/env python3
"""Create short-lived managed-Cognito sessions for control-plane canaries."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Protocol
from urllib.parse import SplitResult, parse_qs, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.gateway.auth.dynamo_principal_repository import (  # noqa: E402
    DynamoPrincipalRepository,
)
from src.gateway.models import (  # noqa: E402
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)


IDENTITY_STACK = "AxonLLMIdentityStack"
CONTROL_PLANE_STACK = "AxonLLMControlPlaneStack"
STATE_SCHEMA = "axonllm.control-plane-canary-sessions/v1"
CSRF_COOKIE_NAME = "__Host-axon-csrf"
ALB_SESSION_COOKIE_BASES = (
    "AWSELBAuthSessionCookie",
    "AxonLLMControlPlaneSession",
)
FIXTURE_ID_FIELD = "control_plane_canary_fixture_id"
PROBE_PATH = "/admin/projects"

_MAX_JSON_BYTES = 256 * 1024
_MAX_OUTPUT_BYTES = 128 * 1024
_MAX_NAVIGATIONS = 32
_MAX_COOKIES = 16
_MAX_COOKIE_HEADER_BYTES = 64 * 1024
_MIN_LIFETIME_SECONDS = 60
_MAX_LIFETIME_SECONDS = 3600
_MIN_BROWSER_TIMEOUT_SECONDS = 5
_MAX_BROWSER_TIMEOUT_SECONDS = 180
_REGION_PATTERN = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+")
_USER_POOL_PATTERN = re.compile(
    r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+_[A-Za-z0-9]+"
)
_SAFE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_ENV_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_FIXTURE_PATTERN = re.compile(r"[0-9a-f]{64}")
_CASE_NAMES = (
    "member",
    "viewer",
    "admin",
    "cross",
    "ungranted",
)
_USERNAME_PATTERN = re.compile(
    r"axon-canary-(member|viewer|admin|cross|ungranted)-"
    r"([0-9a-f]{24})@example\.invalid"
)
_IDENTITY_KEY_PATTERN = re.compile(r"IDENTITY#[0-9a-f]{64}")
_CSRF_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_COOKIE_VALUE_PATTERN = re.compile(
    r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]{1,16384}"
)


class CanarySessionError(RuntimeError):
    """A credential-safe canary-session lifecycle failure."""


class AwsFactory(Protocol):
    def client(self, service_name: str, *, region_name: str) -> Any: ...

    def table(self, table_name: str, *, region_name: str) -> Any: ...


class _BotoFactory:
    def client(self, service_name: str, *, region_name: str) -> Any:
        import boto3

        return boto3.client(service_name, region_name=region_name)

    def table(self, table_name: str, *, region_name: str) -> Any:
        import boto3

        return boto3.resource(
            "dynamodb",
            region_name=region_name,
        ).Table(table_name)


@dataclass(frozen=True)
class IdentityOutputs:
    user_pool_id: str
    issuer: str
    certification_client_id: str
    alb_client_id: str
    control_host: str
    hosted_ui_url: str
    hosted_ui_host: str
    tenant_claim: str
    project_claim: str


@dataclass(frozen=True)
class ControlPlaneOutputs:
    table_name: str
    load_balancer_host: str


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str
    secure: bool
    http_only: bool


@dataclass(frozen=True)
class BrowserResult:
    cookies: tuple[BrowserCookie, ...]
    navigation_urls: tuple[str, ...]
    final_url: str
    final_status: int


class BrowserSession(Protocol):
    def acquire(
        self,
        *,
        start_url: str,
        username: str,
        password: str,
        totp_code: Callable[[], str],
        control_host: str,
        hosted_ui_host: str,
        timeout_seconds: int,
    ) -> BrowserResult: ...


@dataclass(frozen=True)
class OutputNames:
    member_cookie: tuple[str, ...]
    viewer_cookie: tuple[str, ...]
    viewer_csrf: tuple[str, ...]
    admin_cookie: tuple[str, ...]
    admin_csrf: tuple[str, ...]
    cross_tenant_cookie: tuple[str, ...]
    ungranted_project_cookie: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        member_cookie: str | Sequence[str],
        viewer_cookie: str | Sequence[str],
        viewer_csrf: str | Sequence[str],
        admin_cookie: str | Sequence[str],
        admin_csrf: str | Sequence[str],
        cross_tenant_cookie: str | Sequence[str],
        ungranted_project_cookie: str | Sequence[str],
    ) -> OutputNames:
        groups = {
            "member cookie": _env_names(member_cookie, "member cookie"),
            "viewer cookie": _env_names(viewer_cookie, "viewer cookie"),
            "viewer CSRF": _env_names(viewer_csrf, "viewer CSRF"),
            "admin cookie": _env_names(admin_cookie, "admin cookie"),
            "admin CSRF": _env_names(admin_csrf, "admin CSRF"),
            "cross-tenant cookie": _env_names(
                cross_tenant_cookie,
                "cross-tenant cookie",
            ),
            "ungranted-project cookie": _env_names(
                ungranted_project_cookie,
                "ungranted-project cookie",
            ),
        }
        flattened = [name for names in groups.values() for name in names]
        if len(flattened) != len(set(flattened)):
            raise CanarySessionError(
                "credential output environment names must be unique"
            )
        return cls(
            member_cookie=groups["member cookie"],
            viewer_cookie=groups["viewer cookie"],
            viewer_csrf=groups["viewer CSRF"],
            admin_cookie=groups["admin cookie"],
            admin_csrf=groups["admin CSRF"],
            cross_tenant_cookie=groups["cross-tenant cookie"],
            ungranted_project_cookie=groups[
                "ungranted-project cookie"
            ],
        )

    @property
    def all(self) -> frozenset[str]:
        return frozenset(
            (
                *self.member_cookie,
                *self.viewer_cookie,
                *self.viewer_csrf,
                *self.admin_cookie,
                *self.admin_csrf,
                *self.cross_tenant_cookie,
                *self.ungranted_project_cookie,
            )
        )


@dataclass(frozen=True)
class _CanaryIdentity:
    name: str
    role: TenantRole
    username: str
    claim_tenant_id: str
    principal_tenant_id: str
    project_ids: frozenset[str]


class PlaywrightBrowserSession:
    """Acquire cookies in an isolated Chromium context."""

    _USERNAME_SELECTOR = (
        "input[name='username'], input#signInFormUsername"
    )
    _PASSWORD_SELECTOR = (
        "input[name='password'], input#signInFormPassword"
    )
    _LOGIN_SUBMIT_SELECTOR = (
        "input[name='signInSubmitButton'], "
        "button[name='signInSubmitButton'], "
        "button[type='submit'], input[type='submit']"
    )
    _TOTP_SELECTOR = (
        "input[name='totpCode'], input[name='totpcode'], "
        "input[name='mfaCode'], input#mfaCode, "
        "input[name='SOFTWARE_TOKEN_MFA_CODE'], "
        "input[autocomplete='one-time-code']"
    )
    _MFA_SUBMIT_SELECTOR = (
        "input[name='mfaSubmitButton'], "
        "button[name='mfaSubmitButton'], "
        "button[type='submit'], input[type='submit']"
    )

    def acquire(
        self,
        *,
        start_url: str,
        username: str,
        password: str,
        totp_code: Callable[[], str],
        control_host: str,
        hosted_ui_host: str,
        timeout_seconds: int,
    ) -> BrowserResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise CanarySessionError(
                "Playwright and its Chromium runtime are required"
            ) from None

        timeout_ms = timeout_seconds * 1000
        navigations: list[str] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        accept_downloads=False,
                        ignore_https_errors=False,
                    )
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)

                    def record_navigation(frame: Any) -> None:
                        if (
                            frame == page.main_frame
                            and frame.url != "about:blank"
                        ):
                            navigations.append(frame.url)

                    page.on("framenavigated", record_navigation)
                    # A 302 does not always commit the requested URL as a
                    # frame navigation, so preserve the exact trusted origin.
                    navigations.append(start_url)
                    page.goto(
                        start_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    _validate_navigation_prefix(
                        navigations,
                        control_host=control_host,
                        hosted_ui_host=hosted_ui_host,
                    )
                    if _url_host(page.url, "Cognito login URL") != (
                        hosted_ui_host
                    ):
                        raise CanarySessionError(
                            "ALB did not reach the expected Cognito login host"
                        )

                    page.locator(self._USERNAME_SELECTOR).first.fill(
                        username
                    )
                    page.locator(self._PASSWORD_SELECTOR).first.fill(
                        password
                    )
                    page.locator(self._LOGIN_SUBMIT_SELECTOR).first.click()
                    page.locator(self._TOTP_SELECTOR).first.wait_for(
                        state="visible",
                        timeout=timeout_ms,
                    )
                    _validate_navigation_prefix(
                        navigations,
                        control_host=control_host,
                        hosted_ui_host=hosted_ui_host,
                    )
                    if _url_host(page.url, "Cognito MFA URL") != (
                        hosted_ui_host
                    ):
                        raise CanarySessionError(
                            "Cognito MFA left the expected identity host"
                        )
                    page.locator(self._TOTP_SELECTOR).first.fill(totp_code())
                    page.locator(self._MFA_SUBMIT_SELECTOR).first.click()
                    page.wait_for_url(
                        lambda url: _url_host(
                            str(url),
                            "post-authentication URL",
                        )
                        == control_host,
                        timeout=timeout_ms,
                        wait_until="domcontentloaded",
                    )
                    response = page.goto(
                        start_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    if response is None:
                        raise CanarySessionError(
                            "control-plane probe returned no HTTP response"
                        )
                    cookies = tuple(
                        BrowserCookie(
                            name=item.get("name"),
                            value=item.get("value"),
                            domain=item.get("domain"),
                            path=item.get("path"),
                            secure=item.get("secure"),
                            http_only=item.get("httpOnly"),
                        )
                        for item in context.cookies([start_url])
                    )
                    return BrowserResult(
                        cookies=cookies,
                        navigation_urls=tuple(navigations),
                        final_url=page.url,
                        final_status=response.status,
                    )
                finally:
                    browser.close()
        except CanarySessionError:
            raise
        except Exception:
            raise CanarySessionError(
                "browser session acquisition failed"
            ) from None


def _safe_string(
    value: Any,
    location: str,
    *,
    maximum: int = 2048,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise CanarySessionError(f"{location} is missing or invalid")
    return value


def _env_names(
    value: str | Sequence[str],
    location: str,
) -> tuple[str, ...]:
    raw = (value,) if isinstance(value, str) else tuple(value)
    if (
        not 1 <= len(raw) <= 8
        or len(raw) != len(set(raw))
        or any(
            not isinstance(name, str)
            or _ENV_PATTERN.fullmatch(name) is None
            for name in raw
        )
    ):
        raise CanarySessionError(
            f"{location} environment names are invalid"
        )
    return raw


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CanarySessionError("JSON input contains duplicate fields")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise CanarySessionError("JSON input contains a non-finite number")


def _read_json(path: Path) -> Any:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise CanarySessionError(
                f"input must be a regular file: {path}"
            )
        if before.st_size > _MAX_JSON_BYTES:
            raise CanarySessionError(f"input is too large: {path}")
        raw = path.read_bytes()
        after = path.stat()
    except CanarySessionError:
        raise
    except OSError as exc:
        raise CanarySessionError(f"cannot read input: {path}") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(raw) != after.st_size
    ):
        raise CanarySessionError(f"input changed while being read: {path}")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except CanarySessionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanarySessionError(
            f"input is not strict UTF-8 JSON: {path}"
        ) from exc


def _stack_outputs(path: Path, stack_name: str) -> dict[str, str]:
    payload = _read_json(path)
    outputs = payload.get(stack_name) if type(payload) is dict else None
    if type(outputs) is not dict:
        raise CanarySessionError(
            f"CDK outputs do not contain {stack_name}"
        )
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in outputs.items()
    ):
        raise CanarySessionError(
            f"{stack_name} outputs must be string values"
        )
    return outputs


def _required_output(
    outputs: dict[str, str],
    name: str,
    stack_name: str,
) -> str:
    return _safe_string(
        outputs.get(name),
        f"{stack_name}.{name}",
        maximum=4096,
    )


def _dns_name(value: Any, location: str) -> str:
    name = _safe_string(value, location, maximum=253)
    if name != name.lower() or name.endswith("."):
        raise CanarySessionError(f"{location} is not a canonical DNS name")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise CanarySessionError(f"{location} must not be an IP address")
    labels = name.split(".")
    if (
        len(labels) < 2
        or any(
            not 1 <= len(label) <= 63
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
                label,
            )
            is None
            for label in labels
        )
    ):
        raise CanarySessionError(f"{location} is not a valid DNS name")
    return name


def _https_url(value: Any, location: str) -> SplitResult:
    raw = _safe_string(value, location, maximum=4096)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise CanarySessionError(f"{location} is malformed") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise CanarySessionError(
            f"{location} must be an HTTPS URL without credentials or fragments"
        )
    _dns_name(parsed.hostname, f"{location} host")
    return parsed


def _url_host(value: str, location: str) -> str:
    return _dns_name(_https_url(value, location).hostname, f"{location} host")


def _identity_outputs(
    outputs: dict[str, str],
    *,
    region: str,
) -> IdentityOutputs:
    if _REGION_PATTERN.fullmatch(region) is None:
        raise CanarySessionError("AWS region is invalid")
    user_pool_id = _required_output(
        outputs,
        "UserPoolId",
        IDENTITY_STACK,
    )
    if (
        _USER_POOL_PATTERN.fullmatch(user_pool_id) is None
        or not user_pool_id.startswith(f"{region}_")
    ):
        raise CanarySessionError("managed Cognito user pool ID is invalid")
    issuer = _required_output(outputs, "OidcIssuer", IDENTITY_STACK)
    issuer_url = _https_url(issuer, "managed Cognito issuer")
    aws_suffix = (
        "amazonaws.com.cn"
        if issuer_url.hostname
        and issuer_url.hostname.endswith(".amazonaws.com.cn")
        else "amazonaws.com"
    )
    if (
        issuer_url.hostname
        != f"cognito-idp.{region}.{aws_suffix}"
        or issuer_url.path != f"/{user_pool_id}"
        or issuer_url.query
        or _required_output(
            outputs,
            "OidcDiscoveryUrl",
            IDENTITY_STACK,
        )
        != f"{issuer}/.well-known/openid-configuration"
    ):
        raise CanarySessionError(
            "managed Cognito issuer outputs are inconsistent"
        )

    control_host = _dns_name(
        _required_output(
            outputs,
            "ControlPlaneDomainName",
            IDENTITY_STACK,
        ),
        "control-plane domain",
    )
    hosted_ui_url = _required_output(
        outputs,
        "HostedUiDomain",
        IDENTITY_STACK,
    )
    hosted = _https_url(hosted_ui_url, "Cognito hosted UI")
    hosted_ui_host = _dns_name(
        _required_output(
            outputs,
            "HostedUiDomainName",
            IDENTITY_STACK,
        ),
        "Cognito hosted UI domain",
    )
    cognito_suffix = (
        "amazoncognito.com.cn"
        if aws_suffix.endswith(".cn")
        else "amazoncognito.com"
    )
    hosted_pattern = re.compile(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        + rf"\.auth\.{re.escape(region)}\.{re.escape(cognito_suffix)}"
    )
    if (
        hosted.hostname != hosted_ui_host
        or hosted.path not in {"", "/"}
        or hosted.query
        or hosted_pattern.fullmatch(hosted_ui_host) is None
        or control_host == hosted_ui_host
    ):
        raise CanarySessionError(
            "managed Cognito hosted UI outputs are inconsistent"
        )

    tenant_claim = _required_output(
        outputs,
        "TenantClaimName",
        IDENTITY_STACK,
    )
    project_claim = _required_output(
        outputs,
        "ProjectClaimName",
        IDENTITY_STACK,
    )
    if (
        tenant_claim != "custom:tenant_id"
        or project_claim != "custom:project_id"
    ):
        raise CanarySessionError(
            "managed Cognito claim outputs are unexpected"
        )
    certification_client_id = _required_output(
        outputs,
        "CertificationClientId",
        IDENTITY_STACK,
    )
    alb_client_id = _required_output(
        outputs,
        "AlbClientId",
        IDENTITY_STACK,
    )
    public_client_id = _required_output(
        outputs,
        "OidcClientId",
        IDENTITY_STACK,
    )
    if (
        _required_output(
            outputs,
            "OidcAudience",
            IDENTITY_STACK,
        )
        != public_client_id
        or len(
            {certification_client_id, alb_client_id, public_client_id}
        )
        != 3
    ):
        raise CanarySessionError(
            "managed Cognito client outputs are inconsistent"
        )
    return IdentityOutputs(
        user_pool_id=user_pool_id,
        issuer=issuer,
        certification_client_id=certification_client_id,
        alb_client_id=alb_client_id,
        control_host=control_host,
        hosted_ui_url=hosted_ui_url.rstrip("/"),
        hosted_ui_host=hosted_ui_host,
        tenant_claim=tenant_claim,
        project_claim=project_claim,
    )


def _control_plane_outputs(
    outputs: dict[str, str],
    *,
    region: str,
) -> ControlPlaneOutputs:
    if (
        _required_output(
            outputs,
            "RecoveryCutoverMode",
            CONTROL_PLANE_STACK,
        )
        != "normal"
    ):
        raise CanarySessionError(
            "control plane must be in normal recovery mode"
        )
    primary = _required_output(
        outputs,
        "PrimaryStateTableName",
        CONTROL_PLANE_STACK,
    )
    selected = _required_output(
        outputs,
        "SelectedRuntimeStateTableName",
        CONTROL_PLANE_STACK,
    )
    if (
        primary != selected
        or _SAFE_NAME_PATTERN.fullmatch(selected) is None
    ):
        raise CanarySessionError(
            "control plane is not using its primary state table"
        )
    load_balancer_host = _dns_name(
        _required_output(
            outputs,
            "LoadBalancerDnsName",
            CONTROL_PLANE_STACK,
        ),
        "control-plane load balancer DNS name",
    )
    if not (
        load_balancer_host.endswith(
            f".{region}.elb.amazonaws.com"
        )
        or load_balancer_host.endswith(
            f".{region}.elb.amazonaws.com.cn"
        )
    ):
        raise CanarySessionError(
            "control-plane load balancer output is inconsistent"
        )
    return ControlPlaneOutputs(
        table_name=selected,
        load_balancer_host=load_balancer_host,
    )


def _absolute_path(path: str | Path) -> Path:
    return Path(
        os.path.abspath(
            os.path.expanduser(os.fspath(path)),
        )
    )


def _new_private_path(path: str | Path, location: str) -> Path:
    resolved = _absolute_path(path)
    if resolved.exists() or resolved.is_symlink():
        raise CanarySessionError(f"{location} already exists")
    try:
        resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise CanarySessionError(
            f"cannot create parent for {location}"
        ) from exc
    return resolved


def _write_private_json(
    path: Path,
    value: Any,
    *,
    maximum: int = _MAX_OUTPUT_BYTES,
) -> None:
    try:
        payload = (
            json.dumps(
                value,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanarySessionError(
            "cannot encode owner-only JSON output"
        ) from exc
    if len(payload) > maximum:
        raise CanarySessionError("owner-only JSON output is too large")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CanarySessionError(
            "cannot write owner-only JSON output"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _random_material(
    random_bytes: Callable[[int], bytes],
    size: int,
) -> bytes:
    value = random_bytes(size)
    if not isinstance(value, bytes) or len(value) != size:
        raise CanarySessionError(
            "secure random source returned invalid data"
        )
    return value


def _strong_password(
    random_bytes: Callable[[int], bytes],
) -> str:
    random_part = base64.urlsafe_b64encode(
        _random_material(random_bytes, 30)
    ).decode("ascii")
    return f"Aa1!{random_part}"


def _secret_hash(
    client_secret: str,
    username: str,
    client_id: str,
) -> str:
    digest = hmac.new(
        client_secret.encode("utf-8"),
        f"{username}{client_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _rfc6238(
    secret: str,
    *,
    timestamp: float | None = None,
) -> str:
    normalized = "".join(
        _safe_string(
            secret,
            "Cognito TOTP seed",
            maximum=512,
        ).split()
    ).upper()
    padded = normalized + ("=" * ((8 - len(normalized) % 8) % 8))
    try:
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError) as exc:
        raise CanarySessionError(
            "Cognito returned an invalid TOTP seed"
        ) from exc
    instant = time.time() if timestamp is None else timestamp
    if (
        isinstance(instant, bool)
        or not isinstance(instant, (int, float))
        or not math.isfinite(instant)
        or instant < 0
    ):
        raise CanarySessionError("TOTP timestamp is invalid")
    counter = int(instant // 30).to_bytes(8, "big")
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = int.from_bytes(digest[offset : offset + 4], "big")
    return f"{(binary & 0x7FFFFFFF) % 1_000_000:06d}"


def _required_session(response: Any, location: str) -> str:
    if type(response) is not dict:
        raise CanarySessionError(f"{location} returned an invalid response")
    return _safe_string(
        response.get("Session"),
        f"{location} session",
        maximum=8192,
    )


def _client_configuration(
    cognito: Any,
    identity: IdentityOutputs,
) -> str:
    try:
        certification_response = cognito.describe_user_pool_client(
            UserPoolId=identity.user_pool_id,
            ClientId=identity.certification_client_id,
        )
        alb_response = cognito.describe_user_pool_client(
            UserPoolId=identity.user_pool_id,
            ClientId=identity.alb_client_id,
        )
        pool_response = cognito.describe_user_pool(
            UserPoolId=identity.user_pool_id,
        )
    except Exception as exc:
        raise CanarySessionError(
            "cannot verify managed Cognito configuration"
        ) from exc

    certification = (
        certification_response.get("UserPoolClient")
        if type(certification_response) is dict
        else None
    )
    alb = (
        alb_response.get("UserPoolClient")
        if type(alb_response) is dict
        else None
    )
    pool = (
        pool_response.get("UserPool")
        if type(pool_response) is dict
        else None
    )
    if (
        type(certification) is not dict
        or certification.get("ClientId")
        != identity.certification_client_id
        or type(alb) is not dict
        or alb.get("ClientId") != identity.alb_client_id
        or type(pool) is not dict
        or pool.get("Id") != identity.user_pool_id
    ):
        raise CanarySessionError(
            "managed Cognito returned mismatched resources"
        )
    auth_flows = certification.get("ExplicitAuthFlows")
    if (
        not isinstance(auth_flows, list)
        or not {
            "ADMIN_USER_PASSWORD_AUTH",
            "ALLOW_ADMIN_USER_PASSWORD_AUTH",
        }.intersection(auth_flows)
    ):
        raise CanarySessionError(
            "certification client does not permit admin password setup"
        )
    client_secret = _safe_string(
        certification.get("ClientSecret"),
        "Cognito certification client secret",
        maximum=4096,
    )

    callback = f"https://{identity.control_host}/oauth2/idpresponse"
    if (
        alb.get("AllowedOAuthFlowsUserPoolClient") is not True
        or alb.get("AllowedOAuthFlows") != ["code"]
        or alb.get("CallbackURLs") != [callback]
        or alb.get("SupportedIdentityProviders") != ["COGNITO"]
        or not isinstance(alb.get("ClientSecret"), str)
        or not alb["ClientSecret"]
    ):
        raise CanarySessionError(
            "ALB Cognito client configuration is not production-safe"
        )
    software_mfa = pool.get("SoftwareTokenMfaConfiguration")
    if (
        pool.get("MfaConfiguration") != "ON"
        or type(software_mfa) is not dict
        or software_mfa.get("Enabled") is not True
    ):
        raise CanarySessionError(
            "managed Cognito must require software-token MFA"
        )
    return client_secret


def _create_user(
    cognito: Any,
    *,
    identity: IdentityOutputs,
    case: _CanaryIdentity,
    tenant_id: str,
    project_id: str,
    temporary_password: str,
) -> tuple[str, str]:
    try:
        response = cognito.admin_create_user(
            UserPoolId=identity.user_pool_id,
            Username=case.username,
            TemporaryPassword=temporary_password,
            MessageAction="SUPPRESS",
            ForceAliasCreation=False,
            UserAttributes=[
                {"Name": "email", "Value": case.username},
                {"Name": "email_verified", "Value": "true"},
                {"Name": identity.tenant_claim, "Value": tenant_id},
                {"Name": identity.project_claim, "Value": project_id},
            ],
        )
    except Exception as exc:
        raise CanarySessionError(
            "Cognito canary-user creation failed"
        ) from exc
    user = response.get("User") if type(response) is dict else None
    attributes = user.get("Attributes") if type(user) is dict else None
    canonical_username = (
        user.get("Username") if type(user) is dict else None
    )
    if (
        not isinstance(attributes, list)
        or not isinstance(canonical_username, str)
        or not canonical_username
        or canonical_username != canonical_username.strip()
        or len(canonical_username) > 256
        or any(ord(character) < 32 for character in canonical_username)
    ):
        raise CanarySessionError(
            "Cognito create-user response has no canonical identity"
        )
    subjects = [
        item.get("Value")
        for item in attributes
        if type(item) is dict and item.get("Name") == "sub"
    ]
    if len(subjects) != 1:
        raise CanarySessionError(
            "Cognito create-user response has no unique subject"
        )
    return (
        _safe_string(
            subjects[0],
            "Cognito subject",
            maximum=256,
        ),
        canonical_username,
    )


def _finish_user_setup(
    cognito: Any,
    *,
    identity: IdentityOutputs,
    client_secret: str,
    username: str,
    temporary_password: str,
    permanent_password: str,
    subject: str,
    tenant_id: str,
    project_id: str,
    clock: Callable[[], float],
) -> str:
    secret_hash = _secret_hash(
        client_secret,
        username,
        identity.certification_client_id,
    )
    try:
        response = cognito.admin_initiate_auth(
            UserPoolId=identity.user_pool_id,
            ClientId=identity.certification_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": temporary_password,
                "SECRET_HASH": secret_hash,
            },
        )
    except Exception as exc:
        raise CanarySessionError(
            "Cognito password authentication failed"
        ) from exc
    if response.get("ChallengeName") != "NEW_PASSWORD_REQUIRED":
        raise CanarySessionError(
            "new Cognito user did not require a permanent password"
        )
    try:
        response = cognito.admin_respond_to_auth_challenge(
            UserPoolId=identity.user_pool_id,
            ClientId=identity.certification_client_id,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=_required_session(response, "password challenge"),
            ChallengeResponses={
                "USERNAME": username,
                "NEW_PASSWORD": permanent_password,
                "SECRET_HASH": secret_hash,
            },
        )
    except CanarySessionError:
        raise
    except Exception as exc:
        raise CanarySessionError(
            "Cognito password challenge failed"
        ) from exc
    if response.get("ChallengeName") != "MFA_SETUP":
        raise CanarySessionError(
            "new Cognito user did not require TOTP enrollment"
        )

    try:
        association = cognito.associate_software_token(
            Session=_required_session(response, "MFA setup challenge"),
        )
        seed = _safe_string(
            (
                association.get("SecretCode")
                if type(association) is dict
                else None
            ),
            "Cognito TOTP seed",
            maximum=512,
        )
        verification = cognito.verify_software_token(
            Session=_required_session(
                association,
                "TOTP association",
            ),
            UserCode=_rfc6238(seed, timestamp=clock()),
            FriendlyDeviceName="AxonLLM control-plane launch canary",
        )
    except CanarySessionError:
        raise
    except Exception as exc:
        raise CanarySessionError(
            "Cognito TOTP enrollment failed"
        ) from exc
    if (
        type(verification) is not dict
        or verification.get("Status") != "SUCCESS"
    ):
        raise CanarySessionError("Cognito rejected TOTP enrollment")
    try:
        response = cognito.admin_respond_to_auth_challenge(
            UserPoolId=identity.user_pool_id,
            ClientId=identity.certification_client_id,
            ChallengeName="MFA_SETUP",
            Session=_required_session(
                verification,
                "TOTP verification",
            ),
            ChallengeResponses={
                "USERNAME": username,
                "SECRET_HASH": secret_hash,
            },
        )
    except CanarySessionError:
        raise
    except Exception as exc:
        raise CanarySessionError(
            "Cognito MFA challenge failed"
        ) from exc
    authentication = (
        response.get("AuthenticationResult")
        if type(response) is dict
        else None
    )
    if (
        type(authentication) is not dict
        or not isinstance(authentication.get("IdToken"), str)
        or not authentication["IdToken"]
    ):
        raise CanarySessionError(
            "Cognito did not complete canary-user enrollment"
        )
    try:
        user = cognito.admin_get_user(
            UserPoolId=identity.user_pool_id,
            Username=username,
        )
    except Exception as exc:
        raise CanarySessionError(
            "cannot verify enrolled Cognito canary user"
        ) from exc
    if (
        type(user) is not dict
        or user.get("Enabled") is not True
        or user.get("UserStatus") != "CONFIRMED"
    ):
        raise CanarySessionError(
            "enrolled Cognito canary user is not active"
        )
    attributes = user.get("UserAttributes")
    if not isinstance(attributes, list):
        raise CanarySessionError(
            "enrolled Cognito canary user has invalid attributes"
        )
    attribute_map: dict[str, str] = {}
    for attribute in attributes:
        if (
            type(attribute) is not dict
            or not isinstance(attribute.get("Name"), str)
            or not isinstance(attribute.get("Value"), str)
            or attribute["Name"] in attribute_map
        ):
            raise CanarySessionError(
                "enrolled Cognito canary user has invalid attributes"
            )
        attribute_map[attribute["Name"]] = attribute["Value"]
    if attribute_map != {
        "sub": subject,
        "email": username,
        "email_verified": "true",
        identity.tenant_claim: tenant_id,
        identity.project_claim: project_id,
    }:
        raise CanarySessionError(
            "enrolled Cognito canary user attributes changed"
        )
    return seed


def _principal(
    *,
    fixture_id: str,
    case: _CanaryIdentity,
    subject: str,
    issuer: str,
) -> dict[str, Any]:
    principal = Principal(
        principal_id=f"control-plane-canary:{case.name}:{subject}",
        tenant_id=case.principal_tenant_id,
        subject=subject,
        issuer=issuer,
        roles=frozenset({case.role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=case.project_ids,
        scopes=frozenset(),
        authorization_version=1,
        email=case.username,
    )
    item = DynamoPrincipalRepository.serialize(principal)
    item[FIXTURE_ID_FIELD] = fixture_id
    return item


def _put_principal(table: Any, item: dict[str, Any]) -> None:
    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )
        response = table.get_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            ConsistentRead=True,
        )
    except Exception as exc:
        raise CanarySessionError(
            "cannot create owned canary principal"
        ) from exc
    stored = response.get("Item") if type(response) is dict else None
    if stored != item:
        raise CanarySessionError(
            "canary principal verification failed"
        )


def _validate_navigation_prefix(
    navigation_urls: Sequence[str],
    *,
    control_host: str,
    hosted_ui_host: str,
) -> None:
    if not navigation_urls or len(navigation_urls) > _MAX_NAVIGATIONS:
        raise CanarySessionError("browser navigation sequence is invalid")
    allowed = {control_host, hosted_ui_host}
    for value in navigation_urls:
        parsed = _https_url(value, "browser navigation URL")
        if parsed.hostname not in allowed:
            raise CanarySessionError(
                "browser navigation left the approved hosts"
            )


def _validate_navigation_result(
    result: BrowserResult,
    *,
    start_url: str,
    identity: IdentityOutputs,
) -> None:
    _validate_navigation_prefix(
        result.navigation_urls,
        control_host=identity.control_host,
        hosted_ui_host=identity.hosted_ui_host,
    )
    hosts = tuple(
        _url_host(value, "browser navigation URL")
        for value in result.navigation_urls
    )
    if (
        hosts[0] != identity.control_host
        or identity.hosted_ui_host not in hosts
        or hosts[-1] != identity.control_host
    ):
        raise CanarySessionError(
            "browser did not complete the ALB/Cognito redirect"
        )
    client_ids: list[str] = []
    for value in result.navigation_urls:
        parsed = _https_url(value, "browser navigation URL")
        if parsed.hostname != identity.hosted_ui_host:
            continue
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
        )
        client_ids.extend(query.get("client_id", []))
    if (
        not client_ids
        or any(value != identity.alb_client_id for value in client_ids)
    ):
        raise CanarySessionError(
            "Cognito redirect did not use the deployed ALB client"
        )
    final = _https_url(result.final_url, "final control-plane URL")
    expected = _https_url(start_url, "control-plane start URL")
    if (
        final.hostname != identity.control_host
        or final.path != expected.path
        or final.query
        or not isinstance(result.final_status, int)
        or isinstance(result.final_status, bool)
        or result.final_status != 200
    ):
        raise CanarySessionError(
            "authenticated control-plane probe did not return HTTP 200"
        )


def _cookie_domain(
    cookie: BrowserCookie,
    control_host: str,
    location: str,
) -> None:
    if (
        not isinstance(cookie.domain, str)
        or cookie.domain != control_host
        or cookie.path != "/"
        or cookie.secure is not True
    ):
        raise CanarySessionError(
            f"{location} cookie scope is invalid"
        )


def _credential_values(
    result: BrowserResult,
    *,
    start_url: str,
    identity: IdentityOutputs,
) -> tuple[str, str]:
    _validate_navigation_result(
        result,
        start_url=start_url,
        identity=identity,
    )
    if (
        not isinstance(result.cookies, tuple)
        or not 1 <= len(result.cookies) <= _MAX_COOKIES
    ):
        raise CanarySessionError("browser cookie set is invalid")
    by_name: dict[str, BrowserCookie] = {}
    for cookie in result.cookies:
        if (
            not isinstance(cookie, BrowserCookie)
            or not isinstance(cookie.name, str)
            or cookie.name in by_name
            or not isinstance(cookie.value, str)
            or _COOKIE_VALUE_PATTERN.fullmatch(cookie.value) is None
        ):
            raise CanarySessionError("browser cookie set is malformed")
        by_name[cookie.name] = cookie

    csrf = by_name.get(CSRF_COOKIE_NAME)
    if csrf is None:
        raise CanarySessionError("Axon CSRF cookie is missing")
    _cookie_domain(csrf, identity.control_host, "CSRF")
    if (
        csrf.http_only is not False
        or _CSRF_TOKEN_PATTERN.fullmatch(csrf.value) is None
    ):
        raise CanarySessionError("Axon CSRF cookie is invalid")

    candidates: list[
        tuple[str, int | None, BrowserCookie]
    ] = []
    for base in ALB_SESSION_COOKIE_BASES:
        pattern = re.compile(rf"{re.escape(base)}(?:-([0-9]+))?")
        for name, cookie in by_name.items():
            match = pattern.fullmatch(name)
            if match is not None:
                candidates.append(
                    (
                        base,
                        (
                            int(match.group(1))
                            if match.group(1) is not None
                            else None
                        ),
                        cookie,
                    )
                )
    bases = {base for base, _, _ in candidates}
    if len(bases) != 1:
        raise CanarySessionError(
            "ALB session cookies are missing or ambiguous"
        )
    selected = [
        (index, cookie)
        for base, index, cookie in candidates
        if base in bases
    ]
    indices = [index for index, _ in selected]
    if None in indices:
        if len(indices) != 1:
            raise CanarySessionError(
                "ALB session cookie fragments are inconsistent"
            )
        ordered = [selected[0][1]]
    else:
        numeric = [index for index in indices if index is not None]
        if (
            len(numeric) > 8
            or len(numeric) != len(set(numeric))
            or sorted(numeric) != list(range(len(numeric)))
        ):
            raise CanarySessionError(
                "ALB session cookie fragments are not contiguous"
            )
        ordered = [
            cookie
            for _, cookie in sorted(
                selected,
                key=lambda value: int(value[0]),
            )
        ]
    for cookie in ordered:
        _cookie_domain(cookie, identity.control_host, "ALB session")
        if cookie.http_only is not True:
            raise CanarySessionError(
                "ALB session cookie must be HttpOnly"
            )
    header = "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in ordered
    )
    if len(header.encode("utf-8")) > _MAX_COOKIE_HEADER_BYTES:
        raise CanarySessionError("ALB session cookie header is too large")
    return header, csrf.value


def _cases(
    fixture_id: str,
    *,
    tenant_id: str,
    project_id: str,
) -> tuple[_CanaryIdentity, ...]:
    nonce = fixture_id[:24]
    cross_tenant_id = (
        "canary-cross-"
        + hashlib.sha256(
            f"{tenant_id}\0{fixture_id}".encode("utf-8")
        ).hexdigest()[:20]
    )
    return (
        _CanaryIdentity(
            name="member",
            role=TenantRole.TENANT_MEMBER,
            username=f"axon-canary-member-{nonce}@example.invalid",
            claim_tenant_id=tenant_id,
            principal_tenant_id=tenant_id,
            project_ids=frozenset({project_id}),
        ),
        _CanaryIdentity(
            name="viewer",
            role=TenantRole.TENANT_MEMBER,
            username=f"axon-canary-viewer-{nonce}@example.invalid",
            claim_tenant_id=tenant_id,
            principal_tenant_id=tenant_id,
            project_ids=frozenset({project_id}),
        ),
        _CanaryIdentity(
            name="admin",
            role=TenantRole.TENANT_ADMIN,
            username=f"axon-canary-admin-{nonce}@example.invalid",
            claim_tenant_id=tenant_id,
            principal_tenant_id=tenant_id,
            project_ids=frozenset({project_id}),
        ),
        _CanaryIdentity(
            name="cross",
            role=TenantRole.TENANT_MEMBER,
            username=f"axon-canary-cross-{nonce}@example.invalid",
            claim_tenant_id=cross_tenant_id,
            principal_tenant_id=cross_tenant_id,
            project_ids=frozenset({project_id}),
        ),
        _CanaryIdentity(
            name="ungranted",
            role=TenantRole.TENANT_MEMBER,
            username=f"axon-canary-ungranted-{nonce}@example.invalid",
            claim_tenant_id=tenant_id,
            principal_tenant_id=tenant_id,
            project_ids=frozenset(),
        ),
    )


def _state_for(
    *,
    fixture_id: str,
    region: str,
    identity: IdentityOutputs,
    control: ControlPlaneOutputs,
    credentials_path: Path,
    timestamp: float,
    lifetime_seconds: int,
) -> dict[str, Any]:
    created = int(timestamp)
    return {
        "schema": STATE_SCHEMA,
        "fixtureId": fixture_id,
        "region": region,
        "userPoolId": identity.user_pool_id,
        "tableName": control.table_name,
        "credentialsPath": str(credentials_path),
        "createdAtEpoch": created,
        "cleanupDeadlineEpoch": created + lifetime_seconds,
        "users": [],
        "principals": [],
    }


def _state_user(
    case: _CanaryIdentity,
    *,
    project_id: str,
) -> dict[str, Any]:
    return {
        "role": case.name,
        "username": case.username,
        "cognitoUsername": None,
        "tenantId": case.claim_tenant_id,
        "projectId": project_id,
    }


def _state_principal(
    case: _CanaryIdentity,
    item: dict[str, Any],
) -> dict[str, str]:
    return {
        "role": case.name,
        "PK": item["PK"],
        "SK": item["SK"],
        "principalId": item["principal_id"],
    }


def _validate_state(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "schema",
        "fixtureId",
        "region",
        "userPoolId",
        "tableName",
        "credentialsPath",
        "createdAtEpoch",
        "cleanupDeadlineEpoch",
        "users",
        "principals",
    }:
        raise CanarySessionError("cleanup state is malformed")
    if (
        value.get("schema") != STATE_SCHEMA
        or not isinstance(value.get("fixtureId"), str)
        or _FIXTURE_PATTERN.fullmatch(value["fixtureId"]) is None
        or not isinstance(value.get("region"), str)
        or _REGION_PATTERN.fullmatch(value["region"]) is None
        or not isinstance(value.get("userPoolId"), str)
        or _USER_POOL_PATTERN.fullmatch(value["userPoolId"]) is None
        or not isinstance(value.get("tableName"), str)
        or _SAFE_NAME_PATTERN.fullmatch(value["tableName"]) is None
        or not isinstance(value.get("credentialsPath"), str)
        or not Path(value["credentialsPath"]).is_absolute()
    ):
        raise CanarySessionError("cleanup state identifiers are malformed")
    created = value.get("createdAtEpoch")
    deadline = value.get("cleanupDeadlineEpoch")
    if (
        isinstance(created, bool)
        or not isinstance(created, int)
        or isinstance(deadline, bool)
        or not isinstance(deadline, int)
        or created < 0
        or not (
            created + _MIN_LIFETIME_SECONDS
            <= deadline
            <= created + _MAX_LIFETIME_SECONDS
        )
    ):
        raise CanarySessionError("cleanup state lifetime is malformed")

    users = value.get("users")
    if not isinstance(users, list) or len(users) > len(_CASE_NAMES):
        raise CanarySessionError("cleanup state users are malformed")
    seen_roles: set[str] = set()
    for user in users:
        if type(user) is not dict or set(user) != {
            "role",
            "username",
            "cognitoUsername",
            "tenantId",
            "projectId",
        }:
            raise CanarySessionError("cleanup state user is malformed")
        role = user.get("role")
        match = _USERNAME_PATTERN.fullmatch(user.get("username", ""))
        canonical_username = user.get("cognitoUsername")
        if (
            role not in _CASE_NAMES
            or role in seen_roles
            or match is None
            or match.group(1) != role
            or match.group(2) != value["fixtureId"][:24]
            or (
                canonical_username is not None
                and (
                    not isinstance(canonical_username, str)
                    or not canonical_username
                    or canonical_username != canonical_username.strip()
                    or len(canonical_username) > 256
                    or any(
                        ord(character) < 32
                        for character in canonical_username
                    )
                )
            )
            or not isinstance(user.get("tenantId"), str)
            or not user["tenantId"]
            or len(user["tenantId"]) > 128
            or any(ord(character) < 32 for character in user["tenantId"])
            or not isinstance(user.get("projectId"), str)
            or not user["projectId"]
            or len(user["projectId"]) > 128
            or any(
                ord(character) < 32
                for character in user["projectId"]
            )
        ):
            raise CanarySessionError("cleanup state user is malformed")
        seen_roles.add(role)

    principals = value.get("principals")
    if not isinstance(principals, list) or len(principals) > len(
        _CASE_NAMES
    ):
        raise CanarySessionError(
            "cleanup state principals are malformed"
        )
    seen_roles.clear()
    for principal in principals:
        if type(principal) is not dict or set(principal) != {
            "role",
            "PK",
            "SK",
            "principalId",
        }:
            raise CanarySessionError(
                "cleanup state principal is malformed"
            )
        role = principal.get("role")
        sort_key = principal.get("SK")
        if (
            role not in _CASE_NAMES
            or role in seen_roles
            or _IDENTITY_KEY_PATTERN.fullmatch(
                principal.get("PK", "")
            )
            is None
            or not isinstance(sort_key, str)
            or not sort_key.startswith("TENANT#")
            or not sort_key.removeprefix("TENANT#")
            or len(sort_key) > 135
            or any(ord(character) < 32 for character in sort_key)
            or not isinstance(principal.get("principalId"), str)
            or not principal["principalId"].startswith(
                f"control-plane-canary:{role}:"
            )
        ):
            raise CanarySessionError(
                "cleanup state principal is malformed"
            )
        seen_roles.add(role)
    return value


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CanarySessionError("cannot inspect cleanup state") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CanarySessionError(
            "cleanup state must be an owner-only regular file"
        )
    return _validate_state(_read_json(path))


def _unlink_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CanarySessionError("cannot inspect helper output") from exc
    if stat.S_ISDIR(metadata.st_mode):
        raise CanarySessionError(
            "refusing to remove a helper output directory"
        )
    try:
        path.unlink()
    except OSError as exc:
        raise CanarySessionError("cannot remove helper output") from exc


def _cleanup_principal(
    table: Any,
    value: dict[str, str],
    *,
    fixture_id: str,
) -> None:
    key = {"PK": value["PK"], "SK": value["SK"]}
    response = table.get_item(Key=key, ConsistentRead=True)
    item = response.get("Item") if type(response) is dict else None
    if item is None or item.get(FIXTURE_ID_FIELD) != fixture_id:
        return
    if (
        item.get("entity_type") != "tenant_principal"
        or item.get("principal_id") != value["principalId"]
    ):
        raise CanarySessionError(
            "refusing to delete a principal not owned by this helper"
        )
    table.delete_item(
        Key=key,
        ConditionExpression=(
            "#fixture = :fixture AND #entity = :entity "
            "AND principal_id = :principal"
        ),
        ExpressionAttributeNames={
            "#fixture": FIXTURE_ID_FIELD,
            "#entity": "entity_type",
        },
        ExpressionAttributeValues={
            ":fixture": fixture_id,
            ":entity": "tenant_principal",
            ":principal": value["principalId"],
        },
    )


def _cleanup_user(
    cognito: Any,
    value: dict[str, Any],
    *,
    user_pool_id: str,
) -> None:
    username = value["username"]
    try:
        response = cognito.admin_get_user(
            UserPoolId=user_pool_id,
            Username=username,
        )
    except Exception as exc:
        if _aws_error_code(exc) == "UserNotFoundException":
            return
        raise
    attributes = (
        response.get("UserAttributes")
        if type(response) is dict
        else None
    )
    canonical_username = (
        response.get("Username")
        if type(response) is dict
        else None
    )
    if (
        not isinstance(canonical_username, str)
        or not canonical_username
        or canonical_username != canonical_username.strip()
        or len(canonical_username) > 256
        or any(ord(character) < 32 for character in canonical_username)
        or (
            value["cognitoUsername"] is not None
            and canonical_username != value["cognitoUsername"]
        )
        or not isinstance(attributes, list)
    ):
        raise CanarySessionError(
            "refusing to delete an unverified Cognito canary user"
        )
    attribute_map: dict[str, str] = {}
    for attribute in attributes:
        if (
            type(attribute) is not dict
            or not isinstance(attribute.get("Name"), str)
            or not isinstance(attribute.get("Value"), str)
            or attribute["Name"] in attribute_map
        ):
            raise CanarySessionError(
                "refusing to delete an unverified Cognito canary user"
            )
        attribute_map[attribute["Name"]] = attribute["Value"]
    subject = attribute_map.pop("sub", None)
    if (
        not isinstance(subject, str)
        or not subject
        or attribute_map
        != {
            "email": username,
            "email_verified": "true",
            "custom:tenant_id": value["tenantId"],
            "custom:project_id": value["projectId"],
        }
    ):
        raise CanarySessionError(
            "refusing to delete an unverified Cognito canary user"
        )
    try:
        cognito.admin_user_global_sign_out(
            UserPoolId=user_pool_id,
            Username=canonical_username,
        )
    except Exception as exc:
        if _aws_error_code(exc) not in {
            "NotAuthorizedException",
            "UserNotFoundException",
        }:
            raise
    try:
        cognito.admin_delete_user(
            UserPoolId=user_pool_id,
            Username=canonical_username,
        )
    except Exception as exc:
        if _aws_error_code(exc) != "UserNotFoundException":
            raise


def _cleanup_state(
    state: dict[str, Any],
    *,
    state_path: Path,
    aws_factory: AwsFactory,
) -> None:
    failures: list[Exception] = []
    table: Any | None = None
    cognito: Any | None = None
    try:
        table = aws_factory.table(
            state["tableName"],
            region_name=state["region"],
        )
    except Exception as exc:
        failures.append(exc)
    if table is not None:
        for principal in reversed(state["principals"]):
            try:
                _cleanup_principal(
                    table,
                    principal,
                    fixture_id=state["fixtureId"],
                )
            except Exception as exc:
                failures.append(exc)

    try:
        cognito = aws_factory.client(
            "cognito-idp",
            region_name=state["region"],
        )
    except Exception as exc:
        failures.append(exc)
    if cognito is not None:
        for user in reversed(state["users"]):
            try:
                _cleanup_user(
                    cognito,
                    user,
                    user_pool_id=state["userPoolId"],
                )
            except Exception as exc:
                failures.append(exc)
    try:
        _unlink_file(Path(state["credentialsPath"]))
    except Exception as exc:
        failures.append(exc)
    if failures:
        raise CanarySessionError(
            "canary-session cleanup was incomplete; state was retained"
        )
    _unlink_file(state_path)


def prepare_sessions(
    *,
    region: str,
    identity_outputs: str | Path,
    control_plane_outputs: str | Path,
    tenant_id: str,
    project_id: str,
    credentials_output: str | Path,
    state_output: str | Path,
    member_cookie_env: str | Sequence[str],
    viewer_cookie_env: str | Sequence[str],
    viewer_csrf_env: str | Sequence[str],
    admin_cookie_env: str | Sequence[str],
    admin_csrf_env: str | Sequence[str],
    cross_tenant_cookie_env: str | Sequence[str],
    ungranted_project_cookie_env: str | Sequence[str],
    lifetime_seconds: int = 2700,
    browser_timeout_seconds: int = 60,
    aws_factory: AwsFactory | None = None,
    browser_session: BrowserSession | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Prepare viewer/admin principals and browser-derived session material."""
    region = _safe_string(region, "AWS region", maximum=64)
    if _REGION_PATTERN.fullmatch(region) is None:
        raise CanarySessionError("AWS region is invalid")
    tenant_id = _safe_string(tenant_id, "tenant ID", maximum=128)
    project_id = _safe_string(project_id, "project ID", maximum=128)
    if (
        isinstance(lifetime_seconds, bool)
        or not isinstance(lifetime_seconds, int)
        or not (
            _MIN_LIFETIME_SECONDS
            <= lifetime_seconds
            <= _MAX_LIFETIME_SECONDS
        )
    ):
        raise CanarySessionError("canary lifetime is out of bounds")
    if (
        isinstance(browser_timeout_seconds, bool)
        or not isinstance(browser_timeout_seconds, int)
        or not (
            _MIN_BROWSER_TIMEOUT_SECONDS
            <= browser_timeout_seconds
            <= _MAX_BROWSER_TIMEOUT_SECONDS
        )
    ):
        raise CanarySessionError("browser timeout is out of bounds")
    output_names = OutputNames.create(
        member_cookie=member_cookie_env,
        viewer_cookie=viewer_cookie_env,
        viewer_csrf=viewer_csrf_env,
        admin_cookie=admin_cookie_env,
        admin_csrf=admin_csrf_env,
        cross_tenant_cookie=cross_tenant_cookie_env,
        ungranted_project_cookie=ungranted_project_cookie_env,
    )
    identity = _identity_outputs(
        _stack_outputs(Path(identity_outputs), IDENTITY_STACK),
        region=region,
    )
    control = _control_plane_outputs(
        _stack_outputs(
            Path(control_plane_outputs),
            CONTROL_PLANE_STACK,
        ),
        region=region,
    )
    credential_path = _new_private_path(
        credentials_output,
        "credentials output",
    )
    state_path = _new_private_path(state_output, "cleanup state")
    if credential_path == state_path:
        raise CanarySessionError(
            "credentials and cleanup state paths must differ"
        )

    factory = aws_factory or _BotoFactory()
    browser = browser_session or PlaywrightBrowserSession()
    try:
        cognito = factory.client(
            "cognito-idp",
            region_name=region,
        )
        table = factory.table(
            control.table_name,
            region_name=region,
        )
    except Exception as exc:
        raise CanarySessionError(
            "cannot initialize canary-session AWS clients"
        ) from exc
    client_secret = _client_configuration(cognito, identity)
    fixture_id = _random_material(random_bytes, 32).hex()
    timestamp = clock()
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp < 0
    ):
        raise CanarySessionError("system clock returned an invalid time")
    state = _state_for(
        fixture_id=fixture_id,
        region=region,
        identity=identity,
        control=control,
        credentials_path=credential_path,
        timestamp=timestamp,
        lifetime_seconds=lifetime_seconds,
    )
    _write_private_json(state_path, state)

    credentials: dict[str, str] = {}
    start_url = f"https://{identity.control_host}{PROBE_PATH}"
    try:
        for case in _cases(
            fixture_id,
            tenant_id=tenant_id,
            project_id=project_id,
        ):
            state["users"].append(
                _state_user(case, project_id=project_id)
            )
            _write_private_json(state_path, state)
            temporary_password = _strong_password(random_bytes)
            permanent_password = _strong_password(random_bytes)
            if temporary_password == permanent_password:
                raise CanarySessionError(
                    "secure random source repeated password material"
                )
            subject, canonical_username = _create_user(
                cognito,
                identity=identity,
                case=case,
                tenant_id=case.claim_tenant_id,
                project_id=project_id,
                temporary_password=temporary_password,
            )
            state["users"][-1]["cognitoUsername"] = canonical_username
            _write_private_json(state_path, state)
            totp_seed = _finish_user_setup(
                cognito,
                identity=identity,
                client_secret=client_secret,
                username=case.username,
                temporary_password=temporary_password,
                permanent_password=permanent_password,
                subject=subject,
                tenant_id=case.claim_tenant_id,
                project_id=project_id,
                clock=clock,
            )
            principal_item = _principal(
                fixture_id=fixture_id,
                case=case,
                subject=subject,
                issuer=identity.issuer,
            )
            state["principals"].append(
                _state_principal(case, principal_item)
            )
            _write_private_json(state_path, state)
            _put_principal(table, principal_item)

            result = browser.acquire(
                start_url=start_url,
                username=case.username,
                password=permanent_password,
                totp_code=lambda seed=totp_seed: _rfc6238(
                    seed,
                    timestamp=clock(),
                ),
                control_host=identity.control_host,
                hosted_ui_host=identity.hosted_ui_host,
                timeout_seconds=browser_timeout_seconds,
            )
            cookie, csrf = _credential_values(
                result,
                start_url=start_url,
                identity=identity,
            )
            cookie_names = {
                "member": output_names.member_cookie,
                "viewer": output_names.viewer_cookie,
                "admin": output_names.admin_cookie,
                "cross": output_names.cross_tenant_cookie,
                "ungranted": output_names.ungranted_project_cookie,
            }[case.name]
            csrf_names = {
                "viewer": output_names.viewer_csrf,
                "admin": output_names.admin_csrf,
            }.get(case.name, ())
            credentials.update({name: cookie for name in cookie_names})
            credentials.update({name: csrf for name in csrf_names})
            temporary_password = ""
            permanent_password = ""
            totp_seed = ""

        if set(credentials) != output_names.all:
            raise CanarySessionError(
                "credential map does not match requested environment names"
            )
        _write_private_json(credential_path, credentials)
    except Exception as exc:
        credentials.clear()
        try:
            _cleanup_state(
                state,
                state_path=state_path,
                aws_factory=factory,
            )
        except Exception as cleanup_exc:
            raise CanarySessionError(
                "canary-session preparation failed and cleanup was incomplete"
            ) from cleanup_exc
        if isinstance(exc, CanarySessionError):
            raise
        raise CanarySessionError(
            "canary-session preparation failed"
        ) from exc
    finally:
        client_secret = ""

    return {
        "cleanupDeadlineEpoch": state["cleanupDeadlineEpoch"],
        "userCount": len(state["users"]),
    }


def cleanup_sessions(
    state_path: str | Path,
    *,
    credentials_output: str | Path | None = None,
    aws_factory: AwsFactory | None = None,
) -> dict[str, bool]:
    """Idempotently remove only resources owned by a persisted helper state."""
    path = _absolute_path(state_path)
    state = _load_state(path)
    if state is None:
        return {"removed": False}
    if (
        credentials_output is not None
        and _absolute_path(credentials_output)
        != Path(state["credentialsPath"])
    ):
        raise CanarySessionError(
            "credentials output does not match cleanup state"
        )
    factory = aws_factory or _BotoFactory()
    _cleanup_state(
        state,
        state_path=path,
        aws_factory=factory,
    )
    return {"removed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare short-lived managed-Cognito ALB sessions for "
            "AxonLLM control-plane canaries"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--region", required=True)
    prepare.add_argument("--identity-outputs", required=True)
    prepare.add_argument("--control-plane-outputs", required=True)
    prepare.add_argument("--tenant-id", required=True)
    prepare.add_argument("--project-id", required=True)
    prepare.add_argument("--credentials-output", required=True)
    prepare.add_argument("--state-output", required=True)
    prepare.add_argument(
        "--member-cookie-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--viewer-cookie-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--viewer-csrf-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--admin-cookie-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--admin-csrf-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--cross-tenant-cookie-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--ungranted-project-cookie-env",
        action="append",
        required=True,
    )
    prepare.add_argument(
        "--lifetime-seconds",
        type=int,
        default=2700,
    )
    prepare.add_argument(
        "--browser-timeout-seconds",
        type=int,
        default=60,
    )

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument(
        "--state",
        "--state-output",
        dest="state",
        required=True,
    )
    cleanup.add_argument("--credentials-output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_sessions(
                region=args.region,
                identity_outputs=args.identity_outputs,
                control_plane_outputs=args.control_plane_outputs,
                tenant_id=args.tenant_id,
                project_id=args.project_id,
                credentials_output=args.credentials_output,
                state_output=args.state_output,
                member_cookie_env=args.member_cookie_env,
                viewer_cookie_env=args.viewer_cookie_env,
                viewer_csrf_env=args.viewer_csrf_env,
                admin_cookie_env=args.admin_cookie_env,
                admin_csrf_env=args.admin_csrf_env,
                cross_tenant_cookie_env=(
                    args.cross_tenant_cookie_env
                ),
                ungranted_project_cookie_env=(
                    args.ungranted_project_cookie_env
                ),
                lifetime_seconds=args.lifetime_seconds,
                browser_timeout_seconds=args.browser_timeout_seconds,
            )
        else:
            cleanup_sessions(
                args.state,
                credentials_output=args.credentials_output,
            )
    except CanarySessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "error: control-plane canary-session operation failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
