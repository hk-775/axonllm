from __future__ import annotations

import hashlib
import json
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.auth.authorization import Action, ResourceRef, authorize
from src.gateway.models import MembershipStatus, TenantRole


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = REPO_ROOT / "scripts" / "operations"
sys.path.insert(0, str(OPERATIONS))

import certify_agentcore as certification
import prepare_agentcore_certification as fixtures


REGION = "us-east-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm-AbCdEf1234"
ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-certification"
IMAGE_DIGEST = "a" * 64
AGENTCORE_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:{IMAGE_DIGEST}"
CONTROL_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control-plane@sha256:{IMAGE_DIGEST}"
BEDROCK_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
CLIENT_SECRET = "client-secret-must-never-be-written"
TOTP_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
CANDIDATE_ENDPOINT_NAME = "candidate_" + "a" * 32


def _setup() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "target": "agentcore",
        "identity_mode": "managed-cognito",
        "aws_region": REGION,
        "tenant": {
            "tenant_id": "tenant-a",
            "project_id": "project-a",
            "project_name": "Production",
        },
        "admin": {
            "user_name": "admin@example.com",
            "email": "admin@example.com",
        },
        "runtime": {
            "verified_image_uri": AGENTCORE_IMAGE,
            "bedrock_invoke_resource_arns": [BEDROCK_ARN],
            "approved_https_prefix_list_id": "pl-123abc",
            "enabled_providers": ["bedrock"],
            "athena_query": {"role_arns": [ROLE_ARN]},
        },
        "managed_cognito": {
            "hosted_ui_domain_prefix": "axonllm-123456789012",
            "oauth_callback_urls": ["https://app.example.com/oauth/callback"],
        },
        "control_plane": {
            "domain_name": "axon.example.com",
            "verified_image_uri": CONTROL_IMAGE,
            "certificate_arn": ("arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"),
            "public_hosted_zone_id": "Z123ABC",
            "approved_ingress_prefix_list_id": "pl-123abc",
            "approved_https_prefix_list_id": "pl-456def",
        },
    }


def _certification() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "region": REGION,
        "runtimeArn": RUNTIME_ARN,
        "qualifier": CANDIDATE_ENDPOINT_NAME,
        "identities": {
            "activeCredentialEnv": "ACTIVE_TOKEN",
            "inactiveCredentialEnv": "INACTIVE_TOKEN",
            "ungrantedCredentialEnv": "UNGRANTED_TOKEN",
            "crossTenantCredentialEnv": "CROSS_TOKEN",
            "adminCredentialEnv": "ADMIN_TOKEN",
            "viewerCredentialEnv": "VIEWER_TOKEN",
        },
        "tenantConfig": {
            "tenantId": "tenant-a",
            "projectId": "project-a",
        },
        "providers": [{"provider": "bedrock", "model": "launch-certification"}],
        "query": {
            "catalog": "AwsDataCatalog",
            "database": "default",
            "datasourceId": "launch-data",
            "region": REGION,
            "roleArn": ROLE_ARN,
            "sql": "SELECT 1 AS ready",
            "maxRows": 10,
            "workgroup": "axon_read_only",
        },
    }


def _identity_outputs() -> dict[str, Any]:
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_CERTIFICATION"
    return {
        fixtures.IDENTITY_STACK: {
            "TenantClaimName": "custom:tenant_id",
            "ProjectClaimName": "custom:project_id",
            "UserPoolId": "us-east-1_CERTIFICATION",
            "OidcIssuer": issuer,
            "OidcDiscoveryUrl": (f"{issuer}/.well-known/openid-configuration"),
            "OidcClientId": "public-client",
            "OidcAudience": "public-client",
            "CertificationClientId": "certification-client",
        }
    }


def _runtime_outputs() -> dict[str, Any]:
    return {
        fixtures.RUNTIME_STACK: {
            "RecoveryCutoverMode": "normal",
            "StateTableName": "axonllm-agentcore-state",
            "SelectedRuntimeStateTableName": "axonllm-agentcore-state",
            "RuntimeArn": RUNTIME_ARN,
            "CandidateRuntimeEndpointName": CANDIDATE_ENDPOINT_NAME,
        }
    }


def _write(path: Path, value: Any) -> Path:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


class _AwsError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("unsafe upstream detail")
        self.response = {"Error": {"Code": code}}


class _DeterministicRandom:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        self.counter += 1
        seed = hashlib.sha256(str(self.counter).encode()).digest()
        return seed[:size]


class _Cognito:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.passwords: list[str] = []
        self.totp_codes: list[str] = []
        self.deleted: list[str] = []

    def describe_user_pool_client(self, **kwargs):
        assert kwargs == {
            "UserPoolId": "us-east-1_CERTIFICATION",
            "ClientId": "certification-client",
        }
        return {
            "UserPoolClient": {
                "ClientId": "certification-client",
                "ClientSecret": CLIENT_SECRET,
            }
        }

    def admin_create_user(self, **kwargs):
        username = kwargs["Username"]
        assert username not in self.users
        self.created.append(deepcopy(kwargs))
        self.passwords.append(kwargs["TemporaryPassword"])
        subject = f"subject-{len(self.created)}"
        self.users[username] = {
            "subject": subject,
            "attributes": deepcopy(kwargs["UserAttributes"]),
        }
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
        self.passwords.append(kwargs["AuthParameters"]["PASSWORD"])
        assert kwargs["AuthParameters"]["SECRET_HASH"]
        return {
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "Session": f"new:{username}",
        }

    def admin_respond_to_auth_challenge(self, **kwargs):
        username = kwargs["ChallengeResponses"]["USERNAME"]
        assert username in self.users
        assert kwargs["ChallengeResponses"]["SECRET_HASH"]
        if kwargs["ChallengeName"] == "NEW_PASSWORD_REQUIRED":
            password = kwargs["ChallengeResponses"]["NEW_PASSWORD"]
            self.passwords.append(password)
            return {
                "ChallengeName": "MFA_SETUP",
                "Session": f"mfa:{username}",
            }
        assert kwargs["ChallengeName"] == "MFA_SETUP"
        assert kwargs["Session"] == f"verified:{username}"
        token_suffix = hashlib.sha256(username.encode()).hexdigest()[:12]
        return {"AuthenticationResult": {"IdToken": f"header.{token_suffix}.signature"}}

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

    def admin_delete_user(self, **kwargs):
        username = kwargs["Username"]
        if username not in self.users:
            raise _AwsError("UserNotFoundException")
        self.deleted.append(username)
        del self.users[username]

    def admin_get_user(self, **kwargs):
        username = kwargs["Username"]
        if username not in self.users:
            raise _AwsError("UserNotFoundException")
        return {
            "Username": username,
            "UserAttributes": [
                {
                    "Name": "sub",
                    "Value": self.users[username]["subject"],
                }
            ],
        }


class _Table:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_entity_type: str | None = None
        self.collision_entity_type: str | None = None
        self.delete_calls: list[dict[str, Any]] = []

    @staticmethod
    def _key(value: dict[str, Any]) -> tuple[str, str]:
        return value["PK"], value["SK"]

    def put_item(self, **kwargs):
        item = deepcopy(kwargs["Item"])
        if item.get("entity_type") == self.fail_entity_type:
            raise _AwsError("InjectedFailure")
        key = self._key(item)
        if item.get("entity_type") == self.collision_entity_type:
            preexisting = deepcopy(item)
            preexisting.pop(fixtures._FIXTURE_ID_FIELD)
            self.rows[key] = preexisting
            raise _AwsError("ConditionalCheckFailedException")
        if key in self.rows:
            raise _AwsError("ConditionalCheckFailedException")
        self.rows[key] = item
        return {}

    def get_item(self, **kwargs):
        item = self.rows.get(self._key(kwargs["Key"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def delete_item(self, **kwargs):
        self.delete_calls.append(deepcopy(kwargs))
        key = self._key(kwargs["Key"])
        item = self.rows.get(key)
        if item is None:
            return {}
        values = kwargs["ExpressionAttributeValues"]
        if (
            item.get(fixtures._FIXTURE_ID_FIELD) != values[":fixture_id"]
            or item.get("entity_type") != values[":entity_type"]
            or (":principal_id" in values and item.get("principal_id") != values[":principal_id"])
            or (":document" in values and item.get("document") != values[":document"])
            or (":revision" in values and item.get("revision") != values[":revision"])
        ):
            raise _AwsError("ConditionalCheckFailedException")
        self.rows.pop(key)
        return {}


class _Factory:
    def __init__(self) -> None:
        self.cognito = _Cognito()
        self.dynamodb = _Table()
        self.calls: list[tuple[str, str, str | None]] = []

    def client(self, service_name: str, *, region_name: str):
        self.calls.append(("client", service_name, region_name))
        assert service_name == "cognito-idp"
        assert region_name == REGION
        return self.cognito

    def table(self, table_name: str, *, region_name: str):
        self.calls.append(("table", table_name, region_name))
        assert table_name == "axonllm-agentcore-state"
        assert region_name == REGION
        return self.dynamodb


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "setup_config": _write(tmp_path / "setup.json", _setup()),
        "certification_config": _write(
            tmp_path / "certification.json",
            _certification(),
        ),
        "identity_outputs": _write(
            tmp_path / "identity.json",
            _identity_outputs(),
        ),
        "runtime_outputs": _write(
            tmp_path / "runtime.json",
            _runtime_outputs(),
        ),
        "credentials_output": tmp_path / "credentials.json",
        "state_output": tmp_path / "cleanup.json",
    }


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "94287082"),
        (1_111_111_109, "07081804"),
        (1_111_111_111, "14050471"),
    ],
)
def test_rfc6238_matches_sha1_reference_vectors(
    timestamp: int,
    expected: str,
) -> None:
    assert (
        fixtures._rfc6238(
            TOTP_SEED,
            timestamp=timestamp,
            digits=8,
        )
        == expected
    )


def test_prepare_and_cleanup_managed_cognito_certification_fixtures(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    factory = _Factory()

    summary = fixtures.prepare_fixtures(
        **paths,
        aws_factory=factory,
        random_bytes=_DeterministicRandom(),
        clock=lambda: 59,
    )

    assert summary == {
        "datasourceId": "launch-data",
        "principalCount": 6,
        "userCount": 6,
    }
    credentials = json.loads(paths["credentials_output"].read_text(encoding="utf-8"))
    assert set(credentials) == {
        "ACTIVE_TOKEN",
        "INACTIVE_TOKEN",
        "UNGRANTED_TOKEN",
        "CROSS_TOKEN",
        "ADMIN_TOKEN",
        "VIEWER_TOKEN",
    }
    assert all(token.startswith("header.") and token.endswith(".signature") for token in credentials.values())
    assert stat.S_IMODE(paths["credentials_output"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["state_output"].stat().st_mode) == 0o600

    state_text = paths["state_output"].read_text(encoding="utf-8")
    state = json.loads(state_text)
    fixture_id = state["fixtureId"]
    assert state["schema"] == fixtures.STATE_SCHEMA
    assert len(fixture_id) == 64
    assert set(fixture_id) <= set("0123456789abcdef")
    credentials_text = paths["credentials_output"].read_text(encoding="utf-8")
    for secret in (
        CLIENT_SECRET,
        TOTP_SEED,
        *factory.cognito.passwords,
        *credentials.values(),
    ):
        assert secret not in state_text
    assert CLIENT_SECRET not in credentials_text
    assert TOTP_SEED not in credentials_text
    for password in factory.cognito.passwords:
        assert password not in credentials_text
        assert len(password) >= 40
        assert any(character.islower() for character in password)
        assert any(character.isupper() for character in password)
        assert any(character.isdigit() for character in password)
        assert any(not character.isalnum() for character in password)

    assert len(factory.cognito.created) == 6
    assert all(
        request["MessageAction"] == "SUPPRESS" and request["ForceAliasCreation"] is False
        for request in factory.cognito.created
    )
    attribute_maps = {
        request["Username"]: {attribute["Name"]: attribute["Value"] for attribute in request["UserAttributes"]}
        for request in factory.cognito.created
    }
    assert all(
        attributes["custom:project_id"] == "project-a" and attributes["email_verified"] == "true"
        for attributes in attribute_maps.values()
    )
    cross_username = next(username for username in attribute_maps if username.startswith("axon-cert-cross-"))
    assert attribute_maps[cross_username]["custom:tenant_id"] != "tenant-a"
    assert factory.cognito.totp_codes == ["287082"] * 6

    rows = list(factory.dynamodb.rows.values())
    principal_rows = [row for row in rows if row["entity_type"] == "tenant_principal"]
    datasource_rows = [row for row in rows if row["entity_type"] == "athena_datasource"]
    assert len(principal_rows) == 6
    assert len(datasource_rows) == 1
    assert all(row[fixtures._FIXTURE_ID_FIELD] == fixture_id for row in rows)
    principals = {
        row["principal_id"].split(":", 2)[1]: DynamoPrincipalRepository.deserialize(row) for row in principal_rows
    }
    assert principals["active"].membership_status is MembershipStatus.ACTIVE
    assert principals["active"].project_ids == frozenset({"project-a"})
    assert principals["inactive"].membership_status is MembershipStatus.SUSPENDED
    assert principals["ungranted"].project_ids == frozenset()
    assert principals["ungranted"].roles == frozenset({TenantRole.PLATFORM_ADMIN})
    assert principals["cross"].tenant_id != "tenant-a"
    assert principals["cross"].project_ids == frozenset({"project-a"})
    assert principals["admin"].roles == frozenset(
        {TenantRole.TENANT_ADMIN}
    )
    assert principals["viewer"].roles == frozenset(
        {TenantRole.TENANT_MEMBER}
    )
    project_resource = ResourceRef(
        resource_type="project",
        resource_id="project-a",
        tenant_id="tenant-a",
        project_id="project-a",
    )
    assert (
        authorize(
            principals["ungranted"],
            Action.MODEL_LIST,
            project_resource,
        ).status_code
        == 404
    )
    assert (
        authorize(
            principals["cross"],
            Action.MODEL_LIST,
            project_resource,
        ).status_code
        == 404
    )
    assert authorize(
        principals["admin"],
        Action.TENANT_CONFIG_WRITE,
        project_resource,
    ).allowed
    assert authorize(
        principals["viewer"],
        Action.TENANT_CONFIG_READ,
        project_resource,
    ).allowed
    assert not authorize(
        principals["viewer"],
        Action.TENANT_CONFIG_WRITE,
        project_resource,
    ).allowed

    datasource = json.loads(datasource_rows[0]["document"])
    datetime_string = datasource["created_at"]
    assert datasource == {
        "catalog": "AwsDataCatalog",
        "created_at": datetime_string,
        "database": "default",
        "enabled": True,
        "name": "launch-data",
        "region": REGION,
        "role_arn": ROLE_ARN,
        "updated_at": datetime_string,
        "workgroup": "axon_read_only",
    }

    result = fixtures.cleanup_fixtures(
        paths["state_output"],
        credentials_output=paths["credentials_output"],
        aws_factory=factory,
    )

    assert result == {"removed": True}
    assert factory.dynamodb.rows == {}
    assert len(factory.dynamodb.delete_calls) == 7
    assert all(
        call["ExpressionAttributeNames"]["#fixture_id"] == fixtures._FIXTURE_ID_FIELD
        and call["ExpressionAttributeValues"][":fixture_id"] == fixture_id
        for call in factory.dynamodb.delete_calls
    )
    assert factory.cognito.users == {}
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()
    assert fixtures.cleanup_fixtures(
        paths["state_output"],
        credentials_output=paths["credentials_output"],
        aws_factory=factory,
    ) == {"removed": False}


def test_prepare_rolls_back_every_partial_resource_without_secret_leakage(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    factory = _Factory()
    factory.dynamodb.fail_entity_type = "athena_datasource"

    with pytest.raises(fixtures.FixtureError) as failure:
        fixtures.prepare_fixtures(
            **paths,
            aws_factory=factory,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    message = str(failure.value)
    assert CLIENT_SECRET not in message
    assert TOTP_SEED not in message
    assert "header." not in message
    assert factory.dynamodb.rows == {}
    assert factory.cognito.users == {}
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()


@pytest.mark.parametrize(
    "entity_type",
    ("tenant_principal", "athena_datasource"),
)
def test_prepare_collision_never_deletes_preexisting_record(
    tmp_path: Path,
    entity_type: str,
) -> None:
    paths = _paths(tmp_path)
    factory = _Factory()
    factory.dynamodb.collision_entity_type = entity_type

    with pytest.raises(
        fixtures.FixtureError,
        match="without overwriting state",
    ):
        fixtures.prepare_fixtures(
            **paths,
            aws_factory=factory,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    assert len(factory.dynamodb.rows) == 1
    surviving = next(iter(factory.dynamodb.rows.values()))
    assert surviving["entity_type"] == entity_type
    assert fixtures._FIXTURE_ID_FIELD not in surviving
    assert all(
        call["Key"]
        != {
            "PK": surviving["PK"],
            "SK": surviving["SK"],
        }
        for call in factory.dynamodb.delete_calls
    )
    assert factory.cognito.users == {}
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()


def test_cleanup_refuses_replaced_authority_but_continues_other_cleanup(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    factory = _Factory()
    fixtures.prepare_fixtures(
        **paths,
        aws_factory=factory,
        random_bytes=_DeterministicRandom(),
        clock=lambda: 59,
    )
    active_key = next(
        key
        for key, row in factory.dynamodb.rows.items()
        if row.get("principal_id", "").startswith("certification:active:")
    )
    factory.dynamodb.rows[active_key]["principal_id"] = "operator-owned-principal"

    with pytest.raises(
        fixtures.FixtureError,
        match="cleanup was incomplete",
    ):
        fixtures.cleanup_fixtures(
            paths["state_output"],
            aws_factory=factory,
        )

    assert active_key in factory.dynamodb.rows
    assert list(factory.dynamodb.rows) == [active_key]
    assert factory.cognito.users == {}
    assert not paths["credentials_output"].exists()
    assert paths["state_output"].exists()


def test_cleanup_refuses_replaced_cognito_user_but_continues_other_cleanup(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    factory = _Factory()
    fixtures.prepare_fixtures(
        **paths,
        aws_factory=factory,
        random_bytes=_DeterministicRandom(),
        clock=lambda: 59,
    )
    state = json.loads(
        paths["state_output"].read_text(encoding="utf-8")
    )
    replaced = state["users"][0]["username"]
    factory.cognito.users[replaced]["subject"] = "replacement-subject"

    with pytest.raises(
        fixtures.FixtureError,
        match="cleanup was incomplete",
    ):
        fixtures.cleanup_fixtures(
            paths["state_output"],
            aws_factory=factory,
        )

    assert list(factory.cognito.users) == [replaced]
    assert factory.dynamodb.rows == {}
    assert not paths["credentials_output"].exists()
    assert paths["state_output"].exists()


def test_prepare_rejects_unreviewed_datasource_role_before_aws_calls(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    certification = _certification()
    certification["query"]["roleArn"] = "arn:aws:iam::123456789012:role/not-reviewed"
    _write(paths["certification_config"], certification)
    factory = _Factory()

    with pytest.raises(
        fixtures.FixtureError,
        match="not in the reviewed setup",
    ):
        fixtures.prepare_fixtures(
            **paths,
            aws_factory=factory,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    assert factory.calls == []
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()


def test_prepare_requires_exact_managed_tenant_config_binding(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    certification = _certification()
    certification["tenantConfig"]["projectId"] = "project-other"
    _write(paths["certification_config"], certification)
    factory = _Factory()

    with pytest.raises(
        fixtures.FixtureError,
        match="binding does not match setup",
    ):
        fixtures.prepare_fixtures(
            **paths,
            aws_factory=factory,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    assert factory.calls == []
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()


def test_prepare_requires_canaries_for_every_enabled_provider(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    setup = _setup()
    setup["runtime"]["enabled_providers"] = [
        "bedrock",
        "openai",
    ]
    _write(paths["setup_config"], setup)
    factory = _Factory()

    with pytest.raises(
        fixtures.FixtureError,
        match="exactly match",
    ):
        fixtures.prepare_fixtures(
            **paths,
            aws_factory=factory,
            random_bytes=_DeterministicRandom(),
            clock=lambda: 59,
        )

    assert factory.calls == []
    assert not paths["credentials_output"].exists()
    assert not paths["state_output"].exists()


def test_prepare_accepts_optional_ai21_when_setup_and_certification_match(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    providers = sorted(certification.PRODUCTION_ALLOWED_PROVIDERS)
    setup = _setup()
    setup["runtime"]["enabled_providers"] = providers
    _write(paths["setup_config"], setup)
    certification_raw = _certification()
    certification_raw["profile"] = (
        certification.PRODUCTION_LAUNCH_PROFILE
    )
    certification_raw["providers"] = [
        {
            "provider": provider,
            "model": f"{provider}-certification",
            "features": sorted(
                certification.PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[
                    provider
                ]
            ),
        }
        for provider in providers
    ]
    _write(paths["certification_config"], certification_raw)

    fixtures._validate_configuration(
        fixtures.load_agentcore_setup(paths["setup_config"]),
        fixtures.load_config(paths["certification_config"]),
    )


def test_cli_exposes_prepare_and_cleanup_subcommands() -> None:
    cleanup = fixtures.build_parser().parse_args(["cleanup", "--state", "cleanup.json"])
    assert cleanup.command == "cleanup"
    assert cleanup.state == "cleanup.json"

    prepare = fixtures.build_parser().parse_args(
        [
            "prepare",
            "--setup-config",
            "setup.json",
            "--certification-config",
            "certification.json",
            "--identity-outputs",
            "identity.json",
            "--runtime-outputs",
            "runtime.json",
            "--credentials-output",
            "credentials.json",
            "--state-output",
            "cleanup.json",
        ]
    )
    assert prepare.command == "prepare"
