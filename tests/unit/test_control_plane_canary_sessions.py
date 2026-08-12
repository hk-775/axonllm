from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any

import pytest

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.models import MembershipStatus, TenantRole


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = REPO_ROOT / "scripts" / "operations"
sys.path.insert(0, str(OPERATIONS))

import prepare_control_plane_canary_sessions as sessions


REGION = "us-east-1"
USER_POOL_ID = "us-east-1_CANARY"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
CONTROL_HOST = "control.axon.example.com"
HOSTED_UI_HOST = "axonllm-123456789012.auth.us-east-1.amazoncognito.com"
CERTIFICATION_CLIENT = "certification-client"
ALB_CLIENT = "alb-client"
PUBLIC_CLIENT = "public-client"
CLIENT_SECRET = "certification-secret-never-persist"
ALB_CLIENT_SECRET = "alb-secret-never-persist"
TOTP_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
TABLE_NAME = "axonllm-agentcore-state"
VIEWER_CSRF = "v" * 43
ADMIN_CSRF = "a" * 43


def _identity_outputs() -> dict[str, Any]:
    return {
        sessions.IDENTITY_STACK: {
            "UserPoolId": USER_POOL_ID,
            "OidcIssuer": ISSUER,
            "OidcDiscoveryUrl": (
                f"{ISSUER}/.well-known/openid-configuration"
            ),
            "OidcClientId": PUBLIC_CLIENT,
            "OidcAudience": PUBLIC_CLIENT,
            "CertificationClientId": CERTIFICATION_CLIENT,
            "AlbClientId": ALB_CLIENT,
            "ControlPlaneDomainName": CONTROL_HOST,
            "HostedUiDomain": f"https://{HOSTED_UI_HOST}",
            "HostedUiDomainName": HOSTED_UI_HOST,
            "TenantClaimName": "custom:tenant_id",
            "ProjectClaimName": "custom:project_id",
        }
    }


def _control_outputs() -> dict[str, Any]:
    return {
        sessions.CONTROL_PLANE_STACK: {
            "RecoveryCutoverMode": "normal",
            "PrimaryStateTableName": TABLE_NAME,
            "SelectedRuntimeStateTableName": TABLE_NAME,
            "LoadBalancerDnsName": (
                "axonllm-control-1234567890.us-east-1.elb.amazonaws.com"
            ),
        }
    }


def _write(path: Path, value: Any) -> Path:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


class _AwsError(RuntimeError):
    def __init__(self, code: str, detail: str = "upstream secret") -> None:
        super().__init__(detail)
        self.response = {"Error": {"Code": code}}


class _DeterministicRandom:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        self.counter += 1
        material = b""
        block = 0
        while len(material) < size:
            block += 1
            material += hashlib.sha256(
                f"{self.counter}:{block}".encode()
            ).digest()
        return material[:size]


class _Cognito:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.signed_out: list[str] = []
        self.passwords: list[str] = []
        self.totp_codes: list[str] = []
        self.fail_create_after_write_for: str | None = None
        self.fail_delete_for: str | None = None

    def describe_user_pool_client(self, **kwargs):
        assert kwargs["UserPoolId"] == USER_POOL_ID
        if kwargs["ClientId"] == CERTIFICATION_CLIENT:
            return {
                "UserPoolClient": {
                    "ClientId": CERTIFICATION_CLIENT,
                    "ClientSecret": CLIENT_SECRET,
                    "ExplicitAuthFlows": [
                        "ALLOW_ADMIN_USER_PASSWORD_AUTH"
                    ],
                }
            }
        assert kwargs["ClientId"] == ALB_CLIENT
        return {
            "UserPoolClient": {
                "ClientId": ALB_CLIENT,
                "ClientSecret": ALB_CLIENT_SECRET,
                "AllowedOAuthFlowsUserPoolClient": True,
                "AllowedOAuthFlows": ["code"],
                "CallbackURLs": [
                    f"https://{CONTROL_HOST}/oauth2/idpresponse"
                ],
                "SupportedIdentityProviders": ["COGNITO"],
            }
        }

    def describe_user_pool(self, **kwargs):
        assert kwargs == {"UserPoolId": USER_POOL_ID}
        return {
            "UserPool": {
                "Id": USER_POOL_ID,
                "MfaConfiguration": "ON",
                "SoftwareTokenMfaConfiguration": {"Enabled": True},
            }
        }

    def admin_create_user(self, **kwargs):
        username = kwargs["Username"]
        assert username not in self.users
        subject = f"subject-{len(self.users) + 1}"
        self.passwords.append(kwargs["TemporaryPassword"])
        self.created.append(deepcopy(kwargs))
        self.users[username] = {
            "subject": subject,
            "status": "FORCE_CHANGE_PASSWORD",
            "attributes": deepcopy(kwargs["UserAttributes"]),
        }
        if username == self.fail_create_after_write_for:
            raise _AwsError("InternalError")
        return {
            "User": {
                "Username": username,
                "Attributes": [
                    {"Name": "sub", "Value": subject},
                    *deepcopy(kwargs["UserAttributes"]),
                ],
            }
        }

    def admin_initiate_auth(self, **kwargs):
        assert kwargs["AuthFlow"] == "ADMIN_USER_PASSWORD_AUTH"
        username = kwargs["AuthParameters"]["USERNAME"]
        assert username in self.users
        assert kwargs["AuthParameters"]["SECRET_HASH"]
        self.passwords.append(kwargs["AuthParameters"]["PASSWORD"])
        return {
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "Session": f"password:{username}",
        }

    def admin_respond_to_auth_challenge(self, **kwargs):
        username = kwargs["ChallengeResponses"]["USERNAME"]
        assert username in self.users
        assert kwargs["ChallengeResponses"]["SECRET_HASH"]
        if kwargs["ChallengeName"] == "NEW_PASSWORD_REQUIRED":
            self.passwords.append(
                kwargs["ChallengeResponses"]["NEW_PASSWORD"]
            )
            return {
                "ChallengeName": "MFA_SETUP",
                "Session": f"mfa:{username}",
            }
        assert kwargs["ChallengeName"] == "MFA_SETUP"
        assert kwargs["Session"] == f"verified:{username}"
        self.users[username]["status"] = "CONFIRMED"
        return {
            "AuthenticationResult": {
                "IdToken": f"header.{username}.signature"
            }
        }

    def associate_software_token(self, **kwargs):
        username = kwargs["Session"].removeprefix("mfa:")
        assert username in self.users
        return {
            "SecretCode": TOTP_SEED,
            "Session": f"associated:{username}",
        }

    def verify_software_token(self, **kwargs):
        username = kwargs["Session"].removeprefix("associated:")
        assert username in self.users
        self.totp_codes.append(kwargs["UserCode"])
        return {
            "Status": "SUCCESS",
            "Session": f"verified:{username}",
        }

    def admin_get_user(self, **kwargs):
        username = kwargs["Username"]
        if username not in self.users:
            raise _AwsError("UserNotFoundException")
        user = self.users[username]
        return {
            "Username": username,
            "Enabled": True,
            "UserStatus": user["status"],
            "UserAttributes": [
                {"Name": "sub", "Value": user["subject"]},
                *deepcopy(user["attributes"]),
            ],
        }

    def admin_user_global_sign_out(self, **kwargs):
        username = kwargs["Username"]
        if username not in self.users:
            raise _AwsError("UserNotFoundException")
        self.signed_out.append(username)
        return {}

    def admin_delete_user(self, **kwargs):
        username = kwargs["Username"]
        if username == self.fail_delete_for:
            raise _AwsError("InternalError")
        if username not in self.users:
            raise _AwsError("UserNotFoundException")
        self.deleted.append(username)
        del self.users[username]
        return {}


class _Table:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.delete_calls: list[dict[str, Any]] = []
        self.fail_put_role: str | None = None
        self.fail_delete = False

    @staticmethod
    def _key(value: dict[str, Any]) -> tuple[str, str]:
        return value["PK"], value["SK"]

    def put_item(self, **kwargs):
        item = deepcopy(kwargs["Item"])
        role = item["principal_id"].split(":", 2)[1]
        if role == self.fail_put_role:
            raise _AwsError("InjectedFailure")
        key = self._key(item)
        if key in self.rows:
            raise _AwsError("ConditionalCheckFailedException")
        self.rows[key] = item
        return {}

    def get_item(self, **kwargs):
        item = self.rows.get(self._key(kwargs["Key"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def delete_item(self, **kwargs):
        if self.fail_delete:
            raise _AwsError("InjectedFailure")
        self.delete_calls.append(deepcopy(kwargs))
        key = self._key(kwargs["Key"])
        item = self.rows.get(key)
        if item is None:
            return {}
        values = kwargs["ExpressionAttributeValues"]
        if (
            item.get(sessions.FIXTURE_ID_FIELD) != values[":fixture"]
            or item.get("entity_type") != values[":entity"]
            or item.get("principal_id") != values[":principal"]
        ):
            raise _AwsError("ConditionalCheckFailedException")
        del self.rows[key]
        return {}


class _Factory:
    def __init__(self) -> None:
        self.cognito = _Cognito()
        self.dynamodb = _Table()
        self.calls: list[tuple[str, str, str]] = []

    def client(self, service_name: str, *, region_name: str):
        self.calls.append(("client", service_name, region_name))
        assert service_name == "cognito-idp"
        assert region_name == REGION
        return self.cognito

    def table(self, table_name: str, *, region_name: str):
        self.calls.append(("table", table_name, region_name))
        assert table_name == TABLE_NAME
        assert region_name == REGION
        return self.dynamodb


def _cookie(
    name: str,
    value: str,
    *,
    domain: str = CONTROL_HOST,
    path: str = "/",
    secure: bool = True,
    http_only: bool = True,
) -> sessions.BrowserCookie:
    return sessions.BrowserCookie(
        name=name,
        value=value,
        domain=domain,
        path=path,
        secure=secure,
        http_only=http_only,
    )


def _valid_browser_result(
    *,
    role: str,
    cookie_base: str = "AWSELBAuthSessionCookie",
) -> sessions.BrowserResult:
    start = f"https://{CONTROL_HOST}{sessions.PROBE_PATH}"
    csrf = (
        VIEWER_CSRF
        if role == "viewer"
        else ADMIN_CSRF if role == "admin" else role[0] * 43
    )
    if role == "viewer":
        alb_cookies = (
            _cookie(f"{cookie_base}-1", "viewer-fragment-1"),
            _cookie(f"{cookie_base}-0", "viewer-fragment-0"),
        )
    else:
        alb_cookies = (_cookie(cookie_base, f"{role}-session"),)
    return sessions.BrowserResult(
        cookies=(
            *alb_cookies,
            _cookie(
                sessions.CSRF_COOKIE_NAME,
                csrf,
                http_only=False,
            ),
        ),
        navigation_urls=(
            start,
            (
                f"https://{HOSTED_UI_HOST}/oauth2/authorize"
                f"?client_id={ALB_CLIENT}&response_type=code"
            ),
            (
                f"https://{HOSTED_UI_HOST}/login"
                f"?client_id={ALB_CLIENT}"
            ),
            (
                f"https://{CONTROL_HOST}/oauth2/idpresponse"
                "?code=opaque"
            ),
            start,
        ),
        final_url=start,
        final_status=200,
    )


class _Browser:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_role: str | None = None
        self.results: dict[str, sessions.BrowserResult] = {
            "member": _valid_browser_result(role="member"),
            "viewer": _valid_browser_result(role="viewer"),
            "admin": _valid_browser_result(
                role="admin",
                cookie_base="AxonLLMControlPlaneSession",
            ),
            "cross": _valid_browser_result(role="cross"),
            "ungranted": _valid_browser_result(role="ungranted"),
        }

    def acquire(self, **kwargs):
        role = next(
            name
            for name in sessions._CASE_NAMES
            if f"-{name}-" in kwargs["username"]
        )
        captured = dict(kwargs)
        captured["totp"] = kwargs["totp_code"]()
        del captured["totp_code"]
        self.calls.append(captured)
        if role == self.fail_role:
            raise sessions.CanarySessionError(
                "injected browser failure without credential text"
            )
        return self.results[role]


def _paths(tmp_path: Path) -> dict[str, Any]:
    return {
        "region": REGION,
        "identity_outputs": _write(
            tmp_path / "identity.json",
            _identity_outputs(),
        ),
        "control_plane_outputs": _write(
            tmp_path / "control.json",
            _control_outputs(),
        ),
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "credentials_output": tmp_path / "credentials.json",
        "state_output": tmp_path / "state.json",
        "member_cookie_env": "AXON_CANARY_MEMBER_SESSION_COOKIE",
        "viewer_cookie_env": "AXON_CANARY_VIEWER_SESSION_COOKIE",
        "viewer_csrf_env": "AXON_CANARY_VIEWER_CSRF_TOKEN",
        "admin_cookie_env": (
            "AXON_CANARY_TENANT_ADMIN_SESSION_COOKIE"
        ),
        "admin_csrf_env": "AXON_CANARY_TENANT_ADMIN_CSRF_TOKEN",
        "cross_tenant_cookie_env": (
            "AXON_CANARY_CROSS_TENANT_SESSION_COOKIE"
        ),
        "ungranted_project_cookie_env": (
            "AXON_CANARY_UNGRANTED_PROJECT_SESSION_COOKIE"
        ),
    }


def _prepare(
    tmp_path: Path,
    *,
    factory: _Factory | None = None,
    browser: _Browser | None = None,
    **overrides: Any,
) -> tuple[dict[str, Any], _Factory, _Browser, dict[str, Any]]:
    values = _paths(tmp_path)
    values.update(overrides)
    factory = factory or _Factory()
    browser = browser or _Browser()
    result = sessions.prepare_sessions(
        **values,
        aws_factory=factory,
        browser_session=browser,
        random_bytes=_DeterministicRandom(),
        clock=lambda: 59,
    )
    return result, factory, browser, values


def test_prepare_and_cleanup_browser_backed_sessions(
    tmp_path: Path,
) -> None:
    result, factory, browser, paths = _prepare(tmp_path)

    assert result == {
        "cleanupDeadlineEpoch": 2759,
        "userCount": 5,
    }
    credentials = json.loads(
        paths["credentials_output"].read_text(encoding="utf-8")
    )
    assert credentials == {
        "AXON_CANARY_MEMBER_SESSION_COOKIE": (
            "AWSELBAuthSessionCookie=member-session"
        ),
        "AXON_CANARY_VIEWER_SESSION_COOKIE": (
            "AWSELBAuthSessionCookie-0=viewer-fragment-0; "
            "AWSELBAuthSessionCookie-1=viewer-fragment-1"
        ),
        "AXON_CANARY_VIEWER_CSRF_TOKEN": VIEWER_CSRF,
        "AXON_CANARY_TENANT_ADMIN_SESSION_COOKIE": (
            "AxonLLMControlPlaneSession=admin-session"
        ),
        "AXON_CANARY_TENANT_ADMIN_CSRF_TOKEN": ADMIN_CSRF,
        "AXON_CANARY_CROSS_TENANT_SESSION_COOKIE": (
            "AWSELBAuthSessionCookie=cross-session"
        ),
        "AXON_CANARY_UNGRANTED_PROJECT_SESSION_COOKIE": (
            "AWSELBAuthSessionCookie=ungranted-session"
        ),
    }
    assert (
        stat.S_IMODE(paths["credentials_output"].stat().st_mode)
        == 0o600
    )
    assert stat.S_IMODE(paths["state_output"].stat().st_mode) == 0o600

    state_text = paths["state_output"].read_text(encoding="utf-8")
    state = json.loads(state_text)
    assert state["schema"] == sessions.STATE_SCHEMA
    assert state["cleanupDeadlineEpoch"] - state["createdAtEpoch"] == 2700
    assert [user["role"] for user in state["users"]] == [
        "member",
        "viewer",
        "admin",
        "cross",
        "ungranted",
    ]
    assert len(state["principals"]) == 5
    for secret in (
        CLIENT_SECRET,
        ALB_CLIENT_SECRET,
        TOTP_SEED,
        *factory.cognito.passwords,
        *credentials.values(),
    ):
        assert secret not in state_text
    output_text = paths["credentials_output"].read_text(
        encoding="utf-8"
    )
    for secret in (
        CLIENT_SECRET,
        ALB_CLIENT_SECRET,
        TOTP_SEED,
        *factory.cognito.passwords,
    ):
        assert secret not in output_text

    assert len(factory.cognito.created) == 5
    usernames = [
        request["Username"] for request in factory.cognito.created
    ]
    assert usernames[0] != usernames[1]
    assert all(
        request["MessageAction"] == "SUPPRESS"
        and request["ForceAliasCreation"] is False
        for request in factory.cognito.created
    )
    attribute_maps = [
        {
            attribute["Name"]: attribute["Value"]
            for attribute in request["UserAttributes"]
        }
        for request in factory.cognito.created
    ]
    assert all(
        attributes["custom:project_id"] == "project-a"
        and attributes["email_verified"] == "true"
        for attributes in attribute_maps
    )
    assert [
        attributes["custom:tenant_id"] for attributes in attribute_maps
    ][:3] == ["tenant-a"] * 3
    assert attribute_maps[3]["custom:tenant_id"].startswith(
        "canary-cross-"
    )
    assert attribute_maps[4]["custom:tenant_id"] == "tenant-a"
    assert factory.cognito.totp_codes == ["287082"] * 5
    assert [call["totp"] for call in browser.calls] == ["287082"] * 5
    assert all(
        call["start_url"]
        == f"https://{CONTROL_HOST}{sessions.PROBE_PATH}"
        and call["control_host"] == CONTROL_HOST
        and call["hosted_ui_host"] == HOSTED_UI_HOST
        and call["timeout_seconds"] == 60
        for call in browser.calls
    )

    principal_rows = list(factory.dynamodb.rows.values())
    assert len(principal_rows) == 5
    principals = {
        item["principal_id"].split(":", 2)[1]: (
            DynamoPrincipalRepository.deserialize(item)
        )
        for item in principal_rows
    }
    assert principals["viewer"].roles == frozenset(
        {TenantRole.TENANT_MEMBER}
    )
    assert principals["member"].roles == frozenset(
        {TenantRole.TENANT_MEMBER}
    )
    assert principals["admin"].roles == frozenset(
        {TenantRole.TENANT_ADMIN}
    )
    assert principals["cross"].tenant_id.startswith("canary-cross-")
    assert principals["ungranted"].project_ids == frozenset()
    assert all(
        principal.membership_status is MembershipStatus.ACTIVE
        for principal in principals.values()
    )
    assert all(
        principals[name].tenant_id == "tenant-a"
        for name in ("member", "viewer", "admin", "ungranted")
    )
    assert all(
        principals[name].project_ids == frozenset({"project-a"})
        for name in ("member", "viewer", "admin", "cross")
    )
    assert all(
        item[sessions.FIXTURE_ID_FIELD] == state["fixtureId"]
        for item in principal_rows
    )

    cleanup = sessions.cleanup_sessions(
        paths["state_output"],
        credentials_output=paths["credentials_output"],
        aws_factory=factory,
    )
    assert cleanup == {"removed": True}
    assert factory.cognito.users == {}
    assert factory.dynamodb.rows == {}
    assert set(factory.cognito.deleted) == set(usernames)
    assert set(factory.cognito.signed_out) == set(usernames)
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()
    assert sessions.cleanup_sessions(
        paths["state_output"],
        credentials_output=paths["credentials_output"],
        aws_factory=factory,
    ) == {"removed": False}


def test_partial_browser_failure_rolls_back_all_owned_resources(
    tmp_path: Path,
) -> None:
    factory = _Factory()
    browser = _Browser()
    browser.fail_role = "admin"
    paths = _paths(tmp_path)

    with pytest.raises(sessions.CanarySessionError) as failure:
        sessions.prepare_sessions(
            **paths,
            aws_factory=factory,
            browser_session=browser,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    assert factory.cognito.users == {}
    assert factory.dynamodb.rows == {}
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()
    message = str(failure.value)
    for secret in (
        CLIENT_SECRET,
        ALB_CLIENT_SECRET,
        TOTP_SEED,
        *factory.cognito.passwords,
    ):
        assert secret not in message


def test_user_is_cleaned_when_create_call_fails_after_remote_write(
    tmp_path: Path,
) -> None:
    factory = _Factory()
    deterministic = _DeterministicRandom()
    fixture_id = deterministic(32).hex()
    deterministic.counter = 0
    factory.cognito.fail_create_after_write_for = (
        f"axon-canary-member-{fixture_id[:24]}@example.invalid"
    )
    paths = _paths(tmp_path)

    with pytest.raises(
        sessions.CanarySessionError,
        match="Cognito canary-user creation failed",
    ):
        sessions.prepare_sessions(
            **paths,
            aws_factory=factory,
            browser_session=_Browser(),
            random_bytes=deterministic,
            clock=lambda: 59,
        )

    assert factory.cognito.users == {}
    assert factory.cognito.deleted == [
        factory.cognito.fail_create_after_write_for
    ]
    assert not paths["state_output"].exists()


def test_create_collision_never_deletes_an_unowned_cognito_user(
    tmp_path: Path,
) -> None:
    factory = _Factory()
    deterministic = _DeterministicRandom()
    fixture_id = deterministic(32).hex()
    deterministic.counter = 0
    username = (
        f"axon-canary-member-{fixture_id[:24]}@example.invalid"
    )
    factory.cognito.users[username] = {
        "subject": "preexisting-subject",
        "status": "CONFIRMED",
        "attributes": [
            {"Name": "email", "Value": username},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:tenant_id", "Value": "unowned-tenant"},
            {"Name": "custom:project_id", "Value": "unowned-project"},
        ],
    }
    paths = _paths(tmp_path)

    with pytest.raises(
        sessions.CanarySessionError,
        match="preparation failed and cleanup was incomplete",
    ):
        sessions.prepare_sessions(
            **paths,
            aws_factory=factory,
            browser_session=_Browser(),
            random_bytes=deterministic,
            clock=lambda: 59,
        )

    assert username in factory.cognito.users
    assert username not in factory.cognito.deleted
    assert paths["state_output"].exists()
    assert not paths["credentials_output"].exists()


def test_cleanup_uses_cognito_canonical_username_for_email_login() -> None:
    canonical = "31da34d2-2b94-45cd-91db-2c01f8e3d061"

    class CanonicalCognito:
        def __init__(self) -> None:
            self.signed_out: list[str] = []
            self.deleted: list[str] = []

        def admin_get_user(self, **kwargs):
            assert kwargs["Username"] == "canary@example.invalid"
            return {
                "Username": canonical,
                "UserAttributes": [
                    {"Name": "sub", "Value": "subject"},
                    {
                        "Name": "email",
                        "Value": "canary@example.invalid",
                    },
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "custom:tenant_id", "Value": "tenant-a"},
                    {"Name": "custom:project_id", "Value": "project-a"},
                ],
            }

        def admin_user_global_sign_out(self, **kwargs):
            self.signed_out.append(kwargs["Username"])

        def admin_delete_user(self, **kwargs):
            self.deleted.append(kwargs["Username"])

    cognito = CanonicalCognito()
    sessions._cleanup_user(
        cognito,
        {
            "username": "canary@example.invalid",
            "cognitoUsername": canonical,
            "tenantId": "tenant-a",
            "projectId": "project-a",
        },
        user_pool_id=USER_POOL_ID,
    )

    assert cognito.signed_out == [canonical]
    assert cognito.deleted == [canonical]


def test_cleanup_failure_retains_state_and_still_attempts_every_user(
    tmp_path: Path,
) -> None:
    result, factory, _browser, paths = _prepare(tmp_path)
    assert result["userCount"] == 5
    state = json.loads(
        paths["state_output"].read_text(encoding="utf-8")
    )
    failing_user = state["users"][0]["username"]
    factory.cognito.fail_delete_for = failing_user
    factory.dynamodb.fail_delete = True

    with pytest.raises(
        sessions.CanarySessionError,
        match="cleanup was incomplete",
    ):
        sessions.cleanup_sessions(
            paths["state_output"],
            credentials_output=paths["credentials_output"],
            aws_factory=factory,
        )

    assert paths["state_output"].exists()
    assert not paths["credentials_output"].exists()
    assert failing_user in factory.cognito.users
    assert len(factory.cognito.signed_out) == 5
    assert len(factory.cognito.deleted) == 4

    factory.cognito.fail_delete_for = None
    factory.dynamodb.fail_delete = False
    assert sessions.cleanup_sessions(
        paths["state_output"],
        credentials_output=paths["credentials_output"],
        aws_factory=factory,
    ) == {"removed": True}
    assert factory.cognito.users == {}
    assert factory.dynamodb.rows == {}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value[sessions.IDENTITY_STACK].__setitem__(
                "HostedUiDomain",
                f"http://{HOSTED_UI_HOST}",
            ),
            "must be an HTTPS URL",
        ),
        (
            lambda value: value[sessions.IDENTITY_STACK].__setitem__(
                "ControlPlaneDomainName",
                "127.0.0.1",
            ),
            "must not be an IP address",
        ),
        (
            lambda value: value[sessions.IDENTITY_STACK].__setitem__(
                "OidcIssuer",
                "https://attacker.example.com/pool",
            ),
            "issuer outputs are inconsistent",
        ),
    ],
)
def test_identity_outputs_fail_closed(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    identity = _identity_outputs()
    mutate(identity)
    paths = _paths(tmp_path)
    paths["identity_outputs"] = _write(
        tmp_path / "hostile-identity.json",
        identity,
    )

    with pytest.raises(sessions.CanarySessionError, match=message):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "RecoveryCutoverMode",
            "recovery",
            "normal recovery mode",
        ),
        (
            "SelectedRuntimeStateTableName",
            "axonllm-recovery-state",
            "primary state table",
        ),
        (
            "LoadBalancerDnsName",
            "attacker.example.com",
            "load balancer output is inconsistent",
        ),
    ],
)
def test_control_plane_outputs_fail_closed(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    control = _control_outputs()
    control[sessions.CONTROL_PLANE_STACK][field] = value
    paths = _paths(tmp_path)
    paths["control_plane_outputs"] = _write(
        tmp_path / "hostile-control.json",
        control,
    )

    with pytest.raises(sessions.CanarySessionError, match=message):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda result: sessions.BrowserResult(
                cookies=result.cookies,
                navigation_urls=(
                    *result.navigation_urls[:-1],
                    "https://attacker.example.com/capture",
                ),
                final_url=result.final_url,
                final_status=result.final_status,
            ),
            "left the approved hosts",
        ),
        (
            lambda result: sessions.BrowserResult(
                cookies=result.cookies,
                navigation_urls=tuple(
                    value.replace(
                        f"client_id={ALB_CLIENT}",
                        "client_id=wrong-client",
                    )
                    for value in result.navigation_urls
                ),
                final_url=result.final_url,
                final_status=result.final_status,
            ),
            "deployed ALB client",
        ),
        (
            lambda result: sessions.BrowserResult(
                cookies=result.cookies,
                navigation_urls=result.navigation_urls,
                final_url=result.final_url,
                final_status=403,
            ),
            "did not return HTTP 200",
        ),
    ],
)
def test_browser_redirects_fail_closed(
    tmp_path: Path,
    change,
    message: str,
) -> None:
    browser = _Browser()
    browser.results["viewer"] = change(browser.results["viewer"])
    paths = _paths(tmp_path)

    with pytest.raises(sessions.CanarySessionError, match=message):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=browser,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()


@pytest.mark.parametrize(
    ("cookies", "message"),
    [
        (
            (
                _cookie("AWSELBAuthSessionCookie-0", "fragment-zero"),
                _cookie("AWSELBAuthSessionCookie-2", "fragment-two"),
                _cookie(
                    sessions.CSRF_COOKIE_NAME,
                    VIEWER_CSRF,
                    http_only=False,
                ),
            ),
            "not contiguous",
        ),
        (
            (
                _cookie(
                    "AWSELBAuthSessionCookie",
                    "session",
                    secure=False,
                ),
                _cookie(
                    sessions.CSRF_COOKIE_NAME,
                    VIEWER_CSRF,
                    http_only=False,
                ),
            ),
            "cookie scope is invalid",
        ),
        (
            (
                _cookie("AWSELBAuthSessionCookie", "session"),
                _cookie(
                    sessions.CSRF_COOKIE_NAME,
                    VIEWER_CSRF,
                    domain=".control.axon.example.com",
                    http_only=False,
                ),
            ),
            "cookie scope is invalid",
        ),
        (
            (_cookie("AWSELBAuthSessionCookie", "session"),),
            "CSRF cookie is missing",
        ),
        (
            (
                _cookie("AWSELBAuthSessionCookie", "session"),
                _cookie(
                    "AxonLLMControlPlaneSession",
                    "other-session",
                ),
                _cookie(
                    sessions.CSRF_COOKIE_NAME,
                    VIEWER_CSRF,
                    http_only=False,
                ),
            ),
            "missing or ambiguous",
        ),
    ],
)
def test_cookie_material_fails_closed(
    tmp_path: Path,
    cookies: tuple[sessions.BrowserCookie, ...],
    message: str,
) -> None:
    browser = _Browser()
    valid = browser.results["viewer"]
    browser.results["viewer"] = sessions.BrowserResult(
        cookies=cookies,
        navigation_urls=valid.navigation_urls,
        final_url=valid.final_url,
        final_status=valid.final_status,
    )
    paths = _paths(tmp_path)

    with pytest.raises(sessions.CanarySessionError, match=message):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=browser,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )
    assert not paths["credentials_output"].exists()


def test_output_names_are_exact_and_unique(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths["admin_csrf_env"] = "AXON_CANARY_VIEWER_CSRF_TOKEN"

    with pytest.raises(
        sessions.CanarySessionError,
        match="must be unique",
    ):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    paths["admin_csrf_env"] = "lowercase"
    with pytest.raises(
        sessions.CanarySessionError,
        match="environment names are invalid",
    ):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )


def test_existing_or_linked_output_is_never_overwritten(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    existing = paths["credentials_output"]
    existing.write_text("do-not-replace", encoding="utf-8")
    with pytest.raises(
        sessions.CanarySessionError,
        match="already exists",
    ):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )
    assert existing.read_text(encoding="utf-8") == "do-not-replace"

    existing.unlink()
    target = tmp_path / "target"
    target.write_text("do-not-replace", encoding="utf-8")
    existing.symlink_to(target)
    with pytest.raises(
        sessions.CanarySessionError,
        match="already exists",
    ):
        sessions.prepare_sessions(
            **paths,
            aws_factory=_Factory(),
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )
    assert target.read_text(encoding="utf-8") == "do-not-replace"


def test_tampered_cleanup_state_cannot_delete_resources(
    tmp_path: Path,
) -> None:
    _result, factory, _browser, paths = _prepare(tmp_path)
    state = json.loads(
        paths["state_output"].read_text(encoding="utf-8")
    )
    state["users"][0]["username"] = "victim@example.com"
    _write(paths["state_output"], state)
    paths["state_output"].chmod(0o600)

    with pytest.raises(
        sessions.CanarySessionError,
        match="cleanup state user is malformed",
    ):
        sessions.cleanup_sessions(
            paths["state_output"],
            credentials_output=paths["credentials_output"],
            aws_factory=factory,
        )
    assert len(factory.cognito.users) == 5
    assert len(factory.dynamodb.rows) == 5


def test_cleanup_rejects_non_private_state(tmp_path: Path) -> None:
    _result, factory, _browser, paths = _prepare(tmp_path)
    paths["state_output"].chmod(0o644)

    with pytest.raises(
        sessions.CanarySessionError,
        match="owner-only regular file",
    ):
        sessions.cleanup_sessions(
            paths["state_output"],
            credentials_output=paths["credentials_output"],
            aws_factory=factory,
        )
    assert len(factory.cognito.users) == 5


@pytest.mark.parametrize(
    ("lifetime", "timeout", "message"),
    [
        (59, 60, "lifetime is out of bounds"),
        (3601, 60, "lifetime is out of bounds"),
        (2700, 4, "browser timeout is out of bounds"),
        (2700, 181, "browser timeout is out of bounds"),
    ],
)
def test_runtime_bounds_are_enforced_before_aws_mutation(
    tmp_path: Path,
    lifetime: int,
    timeout: int,
    message: str,
) -> None:
    factory = _Factory()
    paths = _paths(tmp_path)
    paths["lifetime_seconds"] = lifetime
    paths["browser_timeout_seconds"] = timeout
    with pytest.raises(sessions.CanarySessionError, match=message):
        sessions.prepare_sessions(
            **paths,
            aws_factory=factory,
            browser_session=_Browser(),
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )
    assert factory.calls == []


def test_rfc6238_matches_reference_vector() -> None:
    assert sessions._rfc6238(TOTP_SEED, timestamp=59) == "287082"


def test_playwright_dependency_failure_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = __import__

    def blocked_import(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("password=do-not-leak")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked_import)
    browser = sessions.PlaywrightBrowserSession()
    with pytest.raises(
        sessions.CanarySessionError,
        match="Playwright and its Chromium runtime are required",
    ) as failure:
        browser.acquire(
            start_url=f"https://{CONTROL_HOST}{sessions.PROBE_PATH}",
            username="secret-user",
            password="secret-password",
            totp_code=lambda: "123456",
            control_host=CONTROL_HOST,
            hosted_ui_host=HOSTED_UI_HOST,
            timeout_seconds=60,
        )
    assert "secret-password" not in str(failure.value)


def test_parser_requires_separate_viewer_and_admin_outputs() -> None:
    parser = sessions.build_parser()
    parsed = parser.parse_args(
        [
            "prepare",
            "--region",
            REGION,
            "--identity-outputs",
            "identity.json",
            "--control-plane-outputs",
            "control.json",
            "--tenant-id",
            "tenant-a",
            "--project-id",
            "project-a",
            "--credentials-output",
            "credentials.json",
            "--state-output",
            "state.json",
            "--member-cookie-env",
            "MEMBER_COOKIE",
            "--viewer-cookie-env",
            "VIEWER_COOKIE",
            "--viewer-csrf-env",
            "VIEWER_CSRF",
            "--admin-cookie-env",
            "ADMIN_COOKIE",
            "--admin-csrf-env",
            "ADMIN_CSRF",
            "--cross-tenant-cookie-env",
            "CROSS_COOKIE",
            "--ungranted-project-cookie-env",
            "UNGRANTED_COOKIE",
        ]
    )
    assert parsed.member_cookie_env == ["MEMBER_COOKIE"]
    assert parsed.viewer_cookie_env == ["VIEWER_COOKIE"]
    assert parsed.admin_cookie_env == ["ADMIN_COOKIE"]
    assert parsed.cross_tenant_cookie_env == ["CROSS_COOKIE"]
    assert parsed.ungranted_project_cookie_env == ["UNGRANTED_COOKIE"]
