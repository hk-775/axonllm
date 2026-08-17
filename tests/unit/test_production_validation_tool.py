from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATIONS_DIR = REPO_ROOT / "scripts" / "operations"
sys.path.insert(0, str(OPERATIONS_DIR))

import run_production_validation as validation


ENDPOINTS = (
    "https://task-a.example.test",
    "https://task-b.example.test",
)
TARGET_GROUP_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/axon-prod/0123456789abcdef"
TOKENS = {
    "AXON_CANARY_MEMBER_TOKEN": "member-production-secret",
    "AXON_CANARY_VIEWER_TOKEN": "viewer-production-secret",
    "AXON_CANARY_CROSS_TENANT_TOKEN": "cross-production-secret",
    "AXON_CANARY_UNGRANTED_PROJECT_TOKEN": "ungranted-production-secret",
    "AXON_CANARY_TENANT_ADMIN_TOKEN": "admin-production-secret",
    "AXON_CANARY_VIEWER_CSRF_TOKEN": "v" * 43,
    "AXON_CANARY_TENANT_ADMIN_CSRF_TOKEN": "a" * 43,
}
STATUS_BY_TOKEN = {
    TOKENS["AXON_CANARY_MEMBER_TOKEN"]: 200,
    TOKENS["AXON_CANARY_VIEWER_TOKEN"]: 403,
    TOKENS["AXON_CANARY_CROSS_TENANT_TOKEN"]: 403,
    TOKENS["AXON_CANARY_UNGRANTED_PROJECT_TOKEN"]: 403,
    TOKENS["AXON_CANARY_TENANT_ADMIN_TOKEN"]: 200,
}


def _rollback_journal(
    tmp_path: Path,
    name: str = "production-validation-rollback.json",
) -> validation.rollback_journal.RollbackJournal:
    return validation.rollback_journal.RollbackJournal.create(
        tmp_path / name,
        clock=lambda: "2026-08-12T12:00:00+00:00",
    )


def _configuration(
    *,
    target: str = "agentcore-http",
    fargate_credential_type: str = "alb-session-cookie",
    request_count: int = 4,
    concurrency: int = 2,
    max_error_rate: float = 0,
    max_p95_latency_ms: float = 100,
) -> dict:
    configuration = {
        "schemaVersion": 1,
        "target": target,
        "timeoutSeconds": 5,
        "canaries": [
            {
                "name": "member-read",
                "category": "authenticated_read_allowed",
                "method": "GET",
                "path": "/v1/models",
                "credentialEnv": "AXON_CANARY_MEMBER_TOKEN",
                "credentialType": "bearer",
                "expectedStatuses": [200],
            },
            {
                "name": "viewer-mutation",
                "category": "viewer_mutation_denied",
                "method": "PUT",
                "path": "/admin/projects/launch-canary-project",
                "credentialEnv": "AXON_CANARY_VIEWER_TOKEN",
                "credentialType": "bearer",
                "expectedErrorCode": "admin_access_denied",
                "expectedStatuses": [403],
                "jsonBody": {"cache_enabled": True},
            },
            {
                "name": "cross-tenant",
                "category": "cross_tenant_denied",
                "method": "GET",
                "path": "/v1/models",
                "credentialEnv": "AXON_CANARY_CROSS_TENANT_TOKEN",
                "credentialType": "bearer",
                "expectedStatuses": [403],
            },
            {
                "name": "ungranted-project",
                "category": "ungranted_project_denied",
                "method": "GET",
                "path": "/v1/models",
                "credentialEnv": "AXON_CANARY_UNGRANTED_PROJECT_TOKEN",
                "credentialType": "bearer",
                "expectedStatuses": [403],
            },
        ],
        "load": {
            "request": {
                "method": "GET",
                "path": "/v1/models",
                "credentialEnv": "AXON_CANARY_MEMBER_TOKEN",
                "credentialType": "bearer",
                "expectedStatuses": [200],
            },
            "requestCount": request_count,
            "concurrency": concurrency,
            "maxErrorRate": max_error_rate,
            "maxP95LatencyMs": max_p95_latency_ms,
        },
    }
    if target == "fargate":
        viewer = next(canary for canary in configuration["canaries"] if canary["category"] == "viewer_mutation_denied")
        viewer["csrfTokenEnv"] = "AXON_CANARY_VIEWER_CSRF_TOKEN"
        configuration["canaries"].append(
            {
                "name": "tenant-admin-mutation-round-trip",
                "category": "tenant_admin_mutation_round_trip",
                "method": "PUT",
                "path": viewer["path"],
                "credentialEnv": "AXON_CANARY_TENANT_ADMIN_TOKEN",
                "credentialType": fargate_credential_type,
                "csrfTokenEnv": "AXON_CANARY_TENANT_ADMIN_CSRF_TOKEN",
                "expectedStatuses": [200],
                "jsonBody": dict(viewer["jsonBody"]),
            }
        )
        for request in [
            *configuration["canaries"],
            configuration["load"]["request"],
        ]:
            request["credentialType"] = fargate_credential_type
    return configuration


class ContractTransport:
    def __init__(
        self,
        *,
        status_by_token: dict[str, int] | None = None,
        viewer_error_code: str = "admin_access_denied",
        fail_changed_state_verification: bool = False,
        fail_cleanup_read: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.status_by_token = status_by_token or STATUS_BY_TOKEN
        self.viewer_error_code = viewer_error_code
        self.fail_changed_state_verification = fail_changed_state_verification
        self.fail_cleanup_read = fail_cleanup_read
        self.events = events
        self.requests: list[validation.HttpRequest] = []
        self.project_states = {endpoint: {"revision": 7, "cache_enabled": False} for endpoint in ENDPOINTS}
        self._admin_get_counts = {endpoint: 0 for endpoint in ENDPOINTS}
        self._lock = threading.Lock()

    def _token(self, request: validation.HttpRequest) -> str:
        authorization = request.headers.get("Authorization")
        if authorization is not None:
            assert authorization.startswith("Bearer ")
            return authorization.removeprefix("Bearer ")
        cookie = request.headers["Cookie"]
        candidates = []
        for part in cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            candidates.append(value if separator else name)
        return next(candidate for candidate in candidates if candidate in self.status_by_token)

    def __call__(
        self,
        request: validation.HttpRequest,
        timeout_seconds: float,
    ) -> validation.HttpObservation:
        assert timeout_seconds == 5
        with self._lock:
            self.requests.append(request)
            if self.events is not None:
                self.events.append("http")
            token = self._token(request)
            endpoint = next(value for value in ENDPOINTS if request.url.startswith(value))
            if request.method == "PUT" and "Authorization" not in request.headers:
                expected_csrf = (
                    TOKENS["AXON_CANARY_TENANT_ADMIN_CSRF_TOKEN"]
                    if token == TOKENS["AXON_CANARY_TENANT_ADMIN_TOKEN"]
                    else TOKENS["AXON_CANARY_VIEWER_CSRF_TOKEN"]
                )
                assert request.headers["X-Axon-CSRF-Token"] == expected_csrf
                assert f"__Host-axon-csrf={expected_csrf}" in request.headers["Cookie"]
            if token == TOKENS["AXON_CANARY_VIEWER_TOKEN"]:
                body = json.dumps({"error": {"code": self.viewer_error_code}}).encode()
                return validation.HttpObservation(403, 10, body=body)
            if token == TOKENS["AXON_CANARY_TENANT_ADMIN_TOKEN"] and request.url.endswith(
                "/admin/projects/launch-canary-project"
            ):
                state = self.project_states[endpoint]
                if request.method == "GET":
                    self._admin_get_counts[endpoint] += 1
                    if self.fail_changed_state_verification and self._admin_get_counts[endpoint] == 2:
                        return validation.HttpObservation(
                            200,
                            10,
                            body=b'{"unexpected":true}',
                        )
                    if self.fail_cleanup_read and self._admin_get_counts[endpoint] == 3:
                        return validation.HttpObservation(
                            None,
                            10,
                            error_type="transport_error",
                        )
                    return validation.HttpObservation(
                        200,
                        10,
                        body=json.dumps(state).encode(),
                    )
                expected_revision = f'"{state["revision"]}"'
                assert request.headers["If-Match"] == expected_revision
                update = json.loads(request.body or b"")
                state.update(update)
                state["revision"] += 1
                return validation.HttpObservation(
                    200,
                    10,
                    body=json.dumps(
                        {
                            "revision": state["revision"],
                            "status": "updated",
                        }
                    ).encode(),
                )
            return validation.HttpObservation(
                status_code=self.status_by_token[token],
                latency_ms=10,
            )


class SequenceTransport:
    def __init__(
        self,
        observations: list[validation.HttpObservation],
    ) -> None:
        self._observations = iter(observations)
        self.requests: list[validation.HttpRequest] = []

    def __call__(
        self,
        request: validation.HttpRequest,
        timeout_seconds: float,
    ) -> validation.HttpObservation:
        self.requests.append(request)
        return next(self._observations)


def _target_health_payload(
    *,
    target_ids: tuple[str, ...] = ("10.0.1.10", "10.0.2.10"),
) -> dict:
    return {
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": target_id, "Port": 8000},
                "TargetHealth": {"State": "healthy"},
            }
            for target_id in target_ids
        ],
    }


def _target_health_snapshot(
    *,
    target_ids: tuple[str, ...] = ("10.0.1.10", "10.0.2.10"),
) -> validation.TargetHealthSnapshot:
    payload = json.dumps(
        _target_health_payload(target_ids=target_ids),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return validation.parse_target_health_snapshot(
        json.loads(payload),
        target_group_arn=TARGET_GROUP_ARN,
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )


class RecordingTargetHealthCollector:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        post_target_ids: tuple[str, ...] = ("10.0.1.10", "10.0.2.10"),
    ) -> None:
        self.events = events
        self.post_target_ids = post_target_ids
        self.calls: list[str] = []

    def __call__(self, phase: str) -> validation.TargetHealthSnapshot:
        self.calls.append(phase)
        if self.events is not None:
            self.events.append(phase)
        return _target_health_snapshot(
            target_ids=(self.post_target_ids if phase == "post-load" else ("10.0.1.10", "10.0.2.10"))
        )


def test_local_contract_covers_model_access_and_tenant_writes() -> None:
    report = validation.evaluate_authorization_contract()

    assert report["status"] == "PASS"
    assert report["sourcePolicyContractExercised"] is True

    checks = report["checks"]
    model_checks = [
        check for check in checks if check["action"] == "model.list" and check["id"].endswith("_model_list_allowed")
    ]
    assert {check["role"] for check in model_checks} == {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
        "service",
    }
    assert all(check["allowed"] for check in model_checks)

    write_checks = {check["role"]: check for check in checks if check["id"].endswith("_tenant_config_write")}
    assert write_checks["tenant_admin"]["allowed"] is True
    assert write_checks["tenant_member"]["allowed"] is False
    assert write_checks["tenant_auditor"]["allowed"] is False


def test_validation_runs_every_canary_and_load_endpoint_without_leaking_tokens() -> None:
    config = validation.parse_config(_configuration())
    transport = ContractTransport()

    report = validation.run_validation(
        config,
        ENDPOINTS,
        environ=TOKENS,
        transport=transport,
        monotonic=iter([10.0, 12.0]).__next__,
    )

    assert report["overallStatus"] == "PASS"
    assert report["target"] == "agentcore-http"
    assert report["claims"] == {
        "agentcoreCutoverValidated": False,
        "backingInstanceIdentityValidated": False,
    }
    assert report["canaries"]["status"] == "PASS"
    assert report["canaries"]["requestCount"] == 8
    assert {
        result["category"] for result in report["canaries"]["results"]
    } == validation.REQUIRED_CANARY_CATEGORIES_BY_TARGET["agentcore-http"]
    assert all(result["passed"] for result in report["canaries"]["results"])
    launch_gates = report["launchGates"]
    assert launch_gates["status"] == "PASS"
    assert all(gate["passed"] for gate in launch_gates["scenarios"].values())
    assert launch_gates["concurrencyLoad"] == {
        "passed": True,
        "requestCountConfigured": 4,
        "requestCountCompleted": 4,
        "concurrency": 2,
        "maxErrorRate": 0.0,
        "maxP95LatencyMs": 100.0,
    }

    load = report["load"]
    assert load["status"] == "PASS"
    assert load["baseUrlsExercised"] == 2
    assert load["multipleHttpEndpointsExercised"] is True
    assert load["backingInstanceIdentityValidated"] is False
    assert {endpoint["baseUrl"]: endpoint["requestCount"] for endpoint in load["endpoints"]} == {
        ENDPOINTS[0]: 2,
        ENDPOINTS[1]: 2,
    }

    serialized = json.dumps(report)
    json.loads(serialized)
    for token in TOKENS.values():
        assert token not in serialized


def test_fargate_contract_does_not_require_or_call_query_route(
    tmp_path: Path,
) -> None:
    raw = _configuration(target="fargate")
    config = validation.parse_config(raw)
    transport = ContractTransport()
    collector = RecordingTargetHealthCollector()

    report = validation.run_validation(
        config,
        ENDPOINTS,
        environ=TOKENS,
        rollback=_rollback_journal(tmp_path),
        target_health_collector=collector,
        transport=transport,
        monotonic=iter([10.0, 12.0]).__next__,
    )

    required = validation.REQUIRED_CANARY_CATEGORIES_BY_TARGET["fargate"]
    assert report["overallStatus"] == "PASS"
    assert report["validationScope"] == ("source-policy-http-canary-and-load")
    assert report["claims"]["backingInstanceIdentityValidated"] is True
    assert report["load"]["backingInstanceIdentityValidated"] is True
    assert report["targetHealth"]["sameTargetSetAcrossLoad"] is True
    assert report["targetHealth"]["chronologyValidated"] is True
    assert report["targetHealth"]["preLoad"]["healthyTargetCount"] == 2
    assert report["targetHealth"]["preLoad"]["targetIdSha256"] == (report["targetHealth"]["postLoad"]["targetIdSha256"])
    assert all(target_id not in json.dumps(report["targetHealth"]) for target_id in ("10.0.1.10", "10.0.2.10"))
    assert collector.calls == ["pre-load", "post-load"]
    assert report["canaries"]["requiredCategories"] == sorted(required)
    assert report["launchGates"]["requiredScenarios"] == sorted(required)
    assert set(report["launchGates"]["scenarios"]) == required
    assert all(not request.url.endswith("/v1/query") for request in transport.requests)


def test_load_metrics_and_threshold_gates_are_deterministic() -> None:
    config = validation.parse_config(
        _configuration(
            max_error_rate=0.2,
            max_p95_latency_ms=39,
        )
    )
    transport = SequenceTransport(
        [
            validation.HttpObservation(200, 10),
            validation.HttpObservation(200, 20),
            validation.HttpObservation(500, 30),
            validation.HttpObservation(200, 40),
        ]
    )

    report = validation.run_load(
        config,
        ENDPOINTS,
        environ=TOKENS,
        transport=transport,
        monotonic=iter([100.0, 102.0]).__next__,
    )

    assert report["status"] == "FAIL"
    assert report["statusCounts"] == {"200": 3, "500": 1}
    assert report["errorCount"] == 1
    assert report["errorRate"] == 0.25
    assert report["throughputRequestsPerSecond"] == 2
    assert report["latencyMs"] == {
        "min": 10,
        "mean": 25,
        "p50": 20,
        "p95": 40,
        "p99": 40,
        "max": 40,
    }
    assert report["endpoints"][0]["statusCounts"] == {
        "200": 1,
        "500": 1,
    }
    assert report["endpoints"][1]["statusCounts"] == {"200": 2}
    gates = {gate["name"]: gate for gate in report["gates"]}
    assert gates["parallel_concurrency_configured"]["passed"] is True
    assert gates["all_endpoints_exercised"]["passed"] is True
    assert gates["error_rate"]["passed"] is False
    assert gates["p95_latency_ms"]["passed"] is False


def test_status_mismatch_fails_cli_and_skips_load(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "validation.json"
    config_path.write_text(
        json.dumps(_configuration()),
        encoding="utf-8",
    )
    statuses = dict(STATUS_BY_TOKEN)
    statuses[TOKENS["AXON_CANARY_CROSS_TENANT_TOKEN"]] = 404
    transport = ContractTransport(status_by_token=statuses)

    exit_code = validation.main(
        [
            "--config",
            str(config_path),
            "--base-url",
            ENDPOINTS[0],
            "--base-url",
            ENDPOINTS[1],
        ],
        environ=TOKENS,
        transport=transport,
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["overallStatus"] == "FAIL"
    assert report["canaries"]["status"] == "FAIL"
    assert report["load"]["status"] == "SKIPPED"
    assert report["load"]["reason"] == ("authorization_or_canary_prerequisite_failed")
    assert len(transport.requests) == 8


def test_missing_required_canary_fails_closed() -> None:
    raw = _configuration()
    raw["canaries"] = raw["canaries"][:-1]

    with pytest.raises(validation.ConfigurationError) as raised:
        validation.parse_config(raw)

    assert raised.value.code == "missing_required_canaries"


def test_load_requires_parallel_concurrency() -> None:
    raw = _configuration(concurrency=1)

    with pytest.raises(
        validation.ConfigurationError,
        match="concurrency must be between 2 and 1000",
    ):
        validation.parse_config(raw)


def test_denial_scenarios_require_distinct_identity_classes() -> None:
    raw = _configuration()
    cross_tenant = next(canary for canary in raw["canaries"] if canary["category"] == "cross_tenant_denied")
    cross_tenant["credentialEnv"] = "AXON_CANARY_VIEWER_TOKEN"

    with pytest.raises(
        validation.ConfigurationError,
        match="distinct identity classes",
    ):
        validation.parse_config(raw)


def test_single_endpoint_fails_cli_with_machine_readable_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "validation.json"
    config_path.write_text(
        json.dumps(_configuration()),
        encoding="utf-8",
    )
    transport = ContractTransport()

    exit_code = validation.main(
        [
            "--config",
            str(config_path),
            "--base-url",
            ENDPOINTS[0],
        ],
        environ=TOKENS,
        transport=transport,
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["overallStatus"] == "FAIL"
    assert report["error"]["code"] == "insufficient_endpoints"
    assert transport.requests == []


def test_single_load_balanced_endpoint_is_explicitly_supported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _configuration()
    raw["load"]["minimumEndpoints"] = 1
    config_path = tmp_path / "validation.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    transport = ContractTransport()

    exit_code = validation.main(
        [
            "--config",
            str(config_path),
            "--base-url",
            ENDPOINTS[0],
        ],
        environ=TOKENS,
        transport=transport,
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["overallStatus"] == "PASS"
    assert report["load"]["minimumEndpoints"] == 1
    assert report["load"]["baseUrlsExercised"] == 1
    assert report["load"]["multipleHttpEndpointsExercised"] is False


def test_missing_credential_fails_without_calling_transport() -> None:
    config = validation.parse_config(_configuration())
    transport = ContractTransport()
    environ = dict(TOKENS)
    missing_token = environ.pop("AXON_CANARY_VIEWER_TOKEN")

    report = validation.run_validation(
        config,
        ENDPOINTS,
        environ=environ,
        transport=transport,
    )

    assert report["overallStatus"] == "FAIL"
    viewer_results = [
        result for result in report["canaries"]["results"] if result["category"] == "viewer_mutation_denied"
    ]
    assert {result["failureReason"] for result in viewer_results} == {"credential_unavailable"}
    assert missing_token not in json.dumps(report)
    assert len(transport.requests) == 6


@pytest.mark.parametrize(
    ("filename", "credential_type"),
    [
        ("production_validation.example.json", "alb-session-cookie"),
        (
            "production_validation.cloudfront.example.json",
            "browser-session-cookie",
        ),
    ],
)
def test_example_configuration_is_valid_and_load_is_read_only(
    filename: str,
    credential_type: str,
) -> None:
    example = json.loads((OPERATIONS_DIR / filename).read_text(encoding="utf-8"))

    parsed = validation.parse_config(example)

    assert parsed.load.request.method == "GET"
    assert parsed.load.request.body is None
    assert parsed.load.minimum_endpoints == 1
    assert {canary["category"] for canary in example["canaries"]} == validation.REQUIRED_CANARY_CATEGORIES_BY_TARGET[
        "fargate"
    ]
    assert all(canary["category"] != "authenticated_query_allowed" for canary in example["canaries"])
    assert all(canary["credentialType"] == credential_type for canary in example["canaries"])
    viewer = next(canary for canary in example["canaries"] if canary["category"] == "viewer_mutation_denied")
    admin = next(canary for canary in example["canaries"] if canary["category"] == "tenant_admin_mutation_round_trip")
    assert viewer["method"] == "PUT"
    assert viewer["expectedErrorCode"] == "admin_access_denied"
    assert viewer["path"] == admin["path"]
    assert viewer["jsonBody"] == admin["jsonBody"]
    assert parsed.load.request.credential_type == credential_type


def test_fargate_validation_accepts_application_browser_sessions() -> None:
    parsed = validation.parse_config(
        _configuration(
            target="fargate",
            fargate_credential_type="browser-session-cookie",
        )
    )

    assert {
        request.credential_type
        for request in (
            *(canary.request for canary in parsed.canaries),
            parsed.load.request,
        )
    } == {"browser-session-cookie"}


def test_fargate_validation_rejects_non_browser_credentials() -> None:
    raw = _configuration(target="fargate")
    raw["canaries"][0]["credentialType"] = "bearer"

    with pytest.raises(
        validation.ConfigurationError,
        match="browser session cookies",
    ):
        validation.parse_config(raw)


def test_fargate_validation_rejects_mixed_cookie_modes() -> None:
    raw = _configuration(target="fargate")
    raw["load"]["request"]["credentialType"] = "browser-session-cookie"

    with pytest.raises(
        validation.ConfigurationError,
        match="one credential type",
    ):
        validation.parse_config(raw)


def test_config_rejects_inline_credentials_and_mutating_load() -> None:
    inline_secret = _configuration()
    inline_secret["canaries"][0]["headers"] = {"Authorization": "Bearer should-not-be-here"}
    with pytest.raises(
        validation.ConfigurationError,
        match="credential or routing headers",
    ):
        validation.parse_config(inline_secret)

    mutating_load = _configuration()
    mutating_load["load"]["request"]["method"] = "POST"
    mutating_load["load"]["request"]["jsonBody"] = {}
    with pytest.raises(
        validation.ConfigurationError,
        match="bodyless GET or HEAD",
    ):
        validation.parse_config(mutating_load)


@pytest.mark.parametrize(
    "credential_type",
    ["alb-session-cookie", "browser-session-cookie"],
)
def test_browser_session_cookie_is_loaded_only_from_environment(
    credential_type: str,
) -> None:
    raw = _configuration()
    raw["canaries"][1]["credentialType"] = credential_type
    raw["canaries"][1]["csrfTokenEnv"] = "AXON_CANARY_VIEWER_CSRF_TOKEN"
    config = validation.parse_config(raw)
    cookie = "AWSELBAuthSessionCookie-0=opaque; AWSELBAuthSessionCookie-1=opaque"
    csrf_token = TOKENS["AXON_CANARY_VIEWER_CSRF_TOKEN"]

    headers = validation._credential_headers(
        config.canaries[1].request,
        {
            "AXON_CANARY_VIEWER_TOKEN": cookie,
            "AXON_CANARY_VIEWER_CSRF_TOKEN": csrf_token,
        },
    )

    assert headers["Cookie"] == (f"{cookie}; __Host-axon-csrf={csrf_token}")
    assert headers["X-Axon-CSRF-Token"] == csrf_token
    assert "Authorization" not in headers


def test_viewer_csrf_denial_cannot_satisfy_rbac_canary(
    tmp_path: Path,
) -> None:
    config = validation.parse_config(_configuration(target="fargate"))
    transport = ContractTransport(viewer_error_code="csrf_validation_failed")

    report = validation.run_canaries(
        config,
        (ENDPOINTS[0],),
        environ=TOKENS,
        rollback=_rollback_journal(tmp_path),
        transport=transport,
    )

    viewer = next(result for result in report["results"] if result["category"] == "viewer_mutation_denied")
    assert viewer["statusCode"] == 403
    assert viewer["errorCodeValidated"] is False
    assert viewer["failureReason"] == "csrf_validation_failed"
    assert report["status"] == "FAIL"
    serialized = json.dumps(report)
    assert all(secret not in serialized for secret in TOKENS.values())


def test_cookie_write_rejects_invalid_csrf_token_without_sending(
    tmp_path: Path,
) -> None:
    config = validation.parse_config(_configuration(target="fargate"))
    transport = ContractTransport()
    environ = dict(TOKENS)
    environ["AXON_CANARY_VIEWER_CSRF_TOKEN"] = "invalid"

    report = validation.run_canaries(
        config,
        (ENDPOINTS[0],),
        environ=environ,
        rollback=_rollback_journal(tmp_path),
        transport=transport,
    )

    viewer = next(result for result in report["results"] if result["category"] == "viewer_mutation_denied")
    assert viewer["failureReason"] == "csrf_token_unavailable"
    assert viewer["statusCode"] is None
    assert len(transport.requests) == 9
    assert "invalid" not in json.dumps(report)


def test_admin_round_trip_rolls_back_after_verification_failure(
    tmp_path: Path,
) -> None:
    config = validation.parse_config(_configuration(target="fargate"))
    transport = ContractTransport(
        fail_changed_state_verification=True,
        fail_cleanup_read=True,
    )

    journal = _rollback_journal(tmp_path)
    report = validation.run_canaries(
        config,
        (ENDPOINTS[0],),
        environ=TOKENS,
        rollback=journal,
        transport=transport,
    )

    result = next(item for item in report["results"] if item["category"] == "tenant_admin_mutation_round_trip")
    assert result["passed"] is False
    assert result["failureReason"] == "transport_error"
    assert result["roundTrip"] == {
        "priorStateLoaded": True,
        "mutationApplied": True,
        "changedStateVerified": False,
        "rollbackAttempted": False,
        "rollbackSucceeded": False,
        "restorationVerified": False,
    }
    assert transport.project_states[ENDPOINTS[0]] == {
        "revision": 8,
        "cache_enabled": True,
    }
    assert journal.summary()["status"] == "PENDING"

    reconciliation = validation.reconcile_production_validation_rollbacks(
        journal,
        environ=TOKENS,
        transport=transport,
    )

    assert reconciliation["status"] == "COMPLETE"
    assert reconciliation["results"][0]["rollbackSucceeded"] is True
    assert transport.project_states[ENDPOINTS[0]] == {
        "revision": 9,
        "cache_enabled": False,
    }
    serialized = json.dumps(report)
    assert all(secret not in serialized for secret in TOKENS.values())


def _prepare_pending_rollback(
    journal: validation.rollback_journal.RollbackJournal,
) -> str:
    return journal.prepare(
        endpoint=ENDPOINTS[0],
        path="/admin/projects/launch-canary-project",
        credential_env="AXON_CANARY_TENANT_ADMIN_TOKEN",
        credential_type="alb-session-cookie",
        csrf_token_env="AXON_CANARY_TENANT_ADMIN_CSRF_TOKEN",
        timeout_seconds=5,
        prior_revision=7,
        prior_values={"cache_enabled": False},
        mutation_values={"cache_enabled": True},
    )


def test_rollback_intent_is_durable_before_mutation(
    tmp_path: Path,
) -> None:
    journal = _rollback_journal(tmp_path)

    class ObservingTransport(ContractTransport):
        observed_pending = False

        def __call__(
            self,
            request: validation.HttpRequest,
            timeout_seconds: float,
        ) -> validation.HttpObservation:
            if (
                request.method == "PUT"
                and request.body == b'{"cache_enabled":true}'
                and self._token(request) == TOKENS["AXON_CANARY_TENANT_ADMIN_TOKEN"]
            ):
                entries = journal.entries(pending_only=True)
                assert len(entries) == 1
                assert entries[0]["priorRevision"] == 7
                self.observed_pending = True
            return super().__call__(request, timeout_seconds)

    transport = ObservingTransport()
    report = validation.run_canaries(
        validation.parse_config(_configuration(target="fargate")),
        (ENDPOINTS[0],),
        environ=TOKENS,
        rollback=journal,
        transport=transport,
    )

    assert report["status"] == "PASS"
    assert transport.observed_pending is True
    assert journal.summary()["status"] == "COMPLETE"


def test_independent_reconciler_recovers_ambiguous_mutation_window(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "rollback.json"
    journal = validation.rollback_journal.RollbackJournal.create(
        path,
        clock=lambda: "2026-08-12T12:00:00+00:00",
    )
    _prepare_pending_rollback(journal)
    transport = ContractTransport()
    transport.project_states[ENDPOINTS[0]] = {
        "revision": 8,
        "cache_enabled": True,
    }

    exit_code = validation.main(
        ["--reconcile-rollback-journal", str(path)],
        environ=TOKENS,
        transport=transport,
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["status"] == "COMPLETE"
    assert result["results"][0]["rollbackAttempted"] is True
    assert transport.project_states[ENDPOINTS[0]] == {
        "revision": 9,
        "cache_enabled": False,
    }


def test_independent_reconciler_fails_closed_on_cas_race(
    tmp_path: Path,
) -> None:
    journal = _rollback_journal(tmp_path)
    _prepare_pending_rollback(journal)

    class RacingTransport(ContractTransport):
        raced = False

        def __call__(
            self,
            request: validation.HttpRequest,
            timeout_seconds: float,
        ) -> validation.HttpObservation:
            observation = super().__call__(request, timeout_seconds)
            if request.method == "GET" and not self.raced:
                self.project_states[ENDPOINTS[0]] = {
                    "revision": 9,
                    "cache_enabled": True,
                }
                self.raced = True
            return observation

    transport = RacingTransport()
    transport.project_states[ENDPOINTS[0]] = {
        "revision": 8,
        "cache_enabled": True,
    }

    first = validation.reconcile_production_validation_rollbacks(
        journal,
        environ=TOKENS,
        transport=transport,
    )
    second = validation.reconcile_production_validation_rollbacks(
        journal,
        environ=TOKENS,
        transport=transport,
    )

    assert first["status"] == "PENDING"
    assert first["results"][0]["reason"] == "rollback_failed"
    assert second["status"] == "PENDING"
    assert second["results"][0]["reason"] == "rollback_state_conflict"
    assert transport.project_states[ENDPOINTS[0]] == {
        "revision": 9,
        "cache_enabled": True,
    }


def test_independent_reconciler_confirms_ambiguous_rollback_response(
    tmp_path: Path,
) -> None:
    journal = _rollback_journal(tmp_path)
    _prepare_pending_rollback(journal)

    class AmbiguousRollbackTransport(ContractTransport):
        rollback_response_lost = False

        def __call__(
            self,
            request: validation.HttpRequest,
            timeout_seconds: float,
        ) -> validation.HttpObservation:
            if (
                request.method == "PUT"
                and request.body == b'{"cache_enabled":false}'
                and not self.rollback_response_lost
            ):
                super().__call__(request, timeout_seconds)
                self.rollback_response_lost = True
                return validation.HttpObservation(
                    None,
                    10,
                    error_type="transport_error",
                )
            return super().__call__(request, timeout_seconds)

    transport = AmbiguousRollbackTransport()
    transport.project_states[ENDPOINTS[0]] = {
        "revision": 8,
        "cache_enabled": True,
    }

    reconciliation = validation.reconcile_production_validation_rollbacks(
        journal,
        environ=TOKENS,
        transport=transport,
    )

    assert reconciliation["status"] == "COMPLETE"
    assert reconciliation["results"] == [
        {
            "entryId": journal.entries()[0]["id"],
            "status": "COMPLETE",
            "reason": None,
            "rollbackAttempted": True,
            "rollbackSucceeded": True,
            "restorationVerified": True,
        }
    ]
    assert transport.project_states[ENDPOINTS[0]] == {
        "revision": 9,
        "cache_enabled": False,
    }


def test_rollback_journal_rejects_mutation_and_insecure_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollback.json"
    journal = validation.rollback_journal.RollbackJournal.create(
        path,
        clock=lambda: "2026-08-12T12:00:00+00:00",
    )
    entry_id = _prepare_pending_rollback(journal)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["entries"][entry_id]["priorRevision"] = 6
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(
        validation.rollback_journal.RollbackJournalError,
        match="authentication",
    ):
        validation.rollback_journal.RollbackJournal.open(
            path,
            clock=lambda: "2026-08-12T12:00:01+00:00",
        )

    key_path = path.with_name(f".{path.name}.key")
    key_path.chmod(0o644)
    with pytest.raises(
        validation.rollback_journal.RollbackJournalError,
        match="key is unsafe",
    ):
        validation.rollback_journal.RollbackJournal.open(
            path,
            clock=lambda: "2026-08-12T12:00:01+00:00",
        )


def test_fargate_mutation_pair_requires_matching_body_and_csrf_envs() -> None:
    mismatched = _configuration(target="fargate")
    admin = next(
        canary for canary in mismatched["canaries"] if canary["category"] == "tenant_admin_mutation_round_trip"
    )
    admin["jsonBody"] = {"cache_enabled": False}
    with pytest.raises(
        validation.ConfigurationError,
        match="same project path and JSON body",
    ):
        validation.parse_config(mismatched)

    missing_csrf = _configuration(target="fargate")
    viewer = next(canary for canary in missing_csrf["canaries"] if canary["category"] == "viewer_mutation_denied")
    viewer.pop("csrfTokenEnv")
    with pytest.raises(
        validation.ConfigurationError,
        match="csrfTokenEnv",
    ):
        validation.parse_config(missing_csrf)


def test_fargate_requires_stable_two_target_health_snapshots(
    tmp_path: Path,
) -> None:
    config = validation.parse_config(_configuration(target="fargate"))
    transport = ContractTransport()

    with pytest.raises(
        validation.ConfigurationError,
        match="in-process ELB target-health collector",
    ):
        validation.run_validation(
            config,
            ENDPOINTS,
            environ=TOKENS,
            transport=transport,
        )
    assert transport.requests == []

    collector = RecordingTargetHealthCollector(
        post_target_ids=("10.0.1.10", "10.0.3.10"),
    )
    with pytest.raises(
        validation.ConfigurationError,
        match="same target group and healthy target set",
    ):
        validation.run_validation(
            config,
            ENDPOINTS,
            environ=TOKENS,
            rollback=_rollback_journal(tmp_path),
            target_health_collector=collector,
            transport=transport,
        )
    assert collector.calls == ["pre-load", "post-load"]
    assert len(transport.requests) > config.load.request_count

    with pytest.raises(
        validation.ConfigurationError,
        match="at least two distinct healthy target IDs",
    ):
        _target_health_snapshot(
            target_ids=("10.0.1.10",),
        )


def test_fargate_target_health_collection_brackets_the_same_http_load(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    config = validation.parse_config(_configuration(target="fargate"))
    transport = ContractTransport(events=events)
    collector = RecordingTargetHealthCollector(events=events)
    base_time = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    clock_values = iter(
        [
            base_time,
            base_time + timedelta(seconds=1),
            base_time + timedelta(seconds=2),
            base_time + timedelta(seconds=5),
            base_time + timedelta(seconds=6),
            base_time + timedelta(seconds=7),
        ]
    )

    report = validation.run_validation(
        config,
        ENDPOINTS,
        environ=TOKENS,
        rollback=_rollback_journal(tmp_path),
        target_health_collector=collector,
        transport=transport,
        monotonic=iter([10.0, 13.0]).__next__,
        now=clock_values.__next__,
    )

    pre_index = events.index("pre-load")
    assert events[pre_index:] == [
        "pre-load",
        *(["http"] * config.load.request_count),
        "post-load",
    ]
    target_health = report["targetHealth"]
    pre_collected_at = datetime.fromisoformat(target_health["preLoad"]["collectedAt"])
    load_started_at = datetime.fromisoformat(target_health["loadInterval"]["startedAt"])
    load_finished_at = datetime.fromisoformat(target_health["loadInterval"]["finishedAt"])
    post_collected_at = datetime.fromisoformat(target_health["postLoad"]["collectedAt"])
    assert pre_collected_at <= load_started_at <= load_finished_at <= post_collected_at
    assert pre_collected_at == base_time + timedelta(seconds=1)
    assert load_started_at == base_time + timedelta(seconds=2)
    assert load_finished_at == base_time + timedelta(seconds=5)
    assert post_collected_at == base_time + timedelta(seconds=6)
    assert target_health["preLoad"]["sourceSha256"] == target_health["postLoad"]["sourceSha256"]
    assert target_health["preLoad"]["observationSha256"] != target_health["postLoad"]["observationSha256"]
    bound_evidence = {
        "schemaVersion": validation.TARGET_HEALTH_SCHEMA,
        "preLoad": target_health["preLoad"],
        "loadInterval": target_health["loadInterval"],
        "postLoad": target_health["postLoad"],
    }
    canonical_evidence = json.dumps(
        bound_evidence,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert target_health["evidenceSha256"] == hashlib.sha256(canonical_evidence).hexdigest()


def test_cli_collects_live_target_health_around_the_same_load(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _configuration(target="fargate")
    raw["load"]["minimumEndpoints"] = 1
    config_path = tmp_path / "validation.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    events: list[str] = []
    payload = json.dumps(_target_health_payload()).encode()
    aws_calls: list[list[str]] = []

    def run_aws(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        aws_calls.append(command)
        events.append("pre-load" if len(aws_calls) == 1 else "post-load")
        assert kwargs == {
            "capture_output": True,
            "check": False,
            "timeout": 30,
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=payload,
            stderr=b"",
        )

    monkeypatch.setattr(validation.subprocess, "run", run_aws)

    exit_code = validation.main(
        [
            "--config",
            str(config_path),
            "--base-url",
            ENDPOINTS[0],
            "--target-group-arn",
            TARGET_GROUP_ARN,
            "--rollback-journal",
            str(tmp_path / "production-validation-rollback.json"),
        ],
        environ=TOKENS,
        transport=ContractTransport(events=events),
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["claims"]["backingInstanceIdentityValidated"] is True
    assert len(aws_calls) == 2
    assert all(
        command
        == [
            "aws",
            "elbv2",
            "describe-target-health",
            "--target-group-arn",
            TARGET_GROUP_ARN,
            "--output",
            "json",
            "--no-cli-pager",
        ]
        for command in aws_calls
    )
    pre_index = events.index("pre-load")
    assert events[pre_index:] == [
        "pre-load",
        *(["http"] * raw["load"]["requestCount"]),
        "post-load",
    ]
    assert report["targetHealth"]["preLoad"]["sourceSha256"] == report["targetHealth"]["postLoad"]["sourceSha256"]
    assert (
        report["targetHealth"]["preLoad"]["observationSha256"]
        != report["targetHealth"]["postLoad"]["observationSha256"]
    )
    serialized = json.dumps(report)
    assert "10.0.1.10" not in serialized
    assert "10.0.2.10" not in serialized


def test_non_fargate_never_claims_backing_instance_identity() -> None:
    config = validation.parse_config(_configuration())
    collector = RecordingTargetHealthCollector()
    transport = ContractTransport()

    with pytest.raises(
        validation.ConfigurationError,
        match="only for Fargate",
    ):
        validation.run_validation(
            config,
            ENDPOINTS,
            environ=TOKENS,
            target_health_collector=collector,
            transport=transport,
        )
    assert collector.calls == []
    assert transport.requests == []
