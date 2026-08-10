from __future__ import annotations

import json
import sys
import threading
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
TOKENS = {
    "AXON_CANARY_MEMBER_TOKEN": "member-production-secret",
    "AXON_CANARY_VIEWER_TOKEN": "viewer-production-secret",
    "AXON_CANARY_CROSS_TENANT_TOKEN": "cross-production-secret",
    "AXON_CANARY_UNGRANTED_PROJECT_TOKEN": "ungranted-production-secret",
}
STATUS_BY_TOKEN = {
    TOKENS["AXON_CANARY_MEMBER_TOKEN"]: 200,
    TOKENS["AXON_CANARY_VIEWER_TOKEN"]: 403,
    TOKENS["AXON_CANARY_CROSS_TENANT_TOKEN"]: 403,
    TOKENS["AXON_CANARY_UNGRANTED_PROJECT_TOKEN"]: 403,
}


def _configuration(
    *,
    request_count: int = 4,
    concurrency: int = 1,
    max_error_rate: float = 0,
    max_p95_latency_ms: float = 100,
) -> dict:
    return {
        "schemaVersion": 1,
        "target": "agentcore-http",
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
                "method": "POST",
                "path": "/admin/projects",
                "credentialEnv": "AXON_CANARY_VIEWER_TOKEN",
                "credentialType": "bearer",
                "expectedStatuses": [403],
                "jsonBody": {},
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


class ContractTransport:
    def __init__(
        self,
        *,
        status_by_token: dict[str, int] | None = None,
    ) -> None:
        self.status_by_token = status_by_token or STATUS_BY_TOKEN
        self.requests: list[validation.HttpRequest] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        request: validation.HttpRequest,
        timeout_seconds: float,
    ) -> validation.HttpObservation:
        assert timeout_seconds == 5
        with self._lock:
            self.requests.append(request)
        authorization = request.headers["Authorization"]
        assert authorization.startswith("Bearer ")
        token = authorization.removeprefix("Bearer ")
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


def test_local_contract_covers_select_mutation_and_tenant_writes() -> None:
    report = validation.evaluate_authorization_contract()

    assert report["status"] == "PASS"
    assert report["sourcePolicyContractExercised"] is True
    assert report["queryBackendExercised"] is False

    checks = report["checks"]
    select_checks = [
        check
        for check in checks
        if check["action"] == "query.select"
        and check["id"].endswith("_query_select_allowed")
    ]
    assert {check["role"] for check in select_checks} == {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
        "service",
    }
    assert all(check["allowed"] for check in select_checks)

    mutation_checks = [
        check
        for check in checks
        if check["action"] == "query.mutate"
    ]
    assert {check["role"] for check in mutation_checks} == {
        role.value for role in validation.TenantRole
    }
    assert all(not check["allowed"] for check in mutation_checks)
    assert {
        check["reason"] for check in mutation_checks
    } == {"query_mutation_not_supported"}

    write_checks = {
        check["role"]: check
        for check in checks
        if check["id"].endswith("_tenant_config_write")
    }
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
        "queryBackendExercised": False,
        "backingInstanceIdentityValidated": False,
    }
    assert report["canaries"]["status"] == "PASS"
    assert report["canaries"]["requestCount"] == 8
    assert {
        result["category"] for result in report["canaries"]["results"]
    } == validation.REQUIRED_CANARY_CATEGORIES
    assert all(
        result["passed"] for result in report["canaries"]["results"]
    )

    load = report["load"]
    assert load["status"] == "PASS"
    assert load["baseUrlsExercised"] == 2
    assert load["multipleHttpEndpointsExercised"] is True
    assert load["backingInstanceIdentityValidated"] is False
    assert {
        endpoint["baseUrl"]: endpoint["requestCount"]
        for endpoint in load["endpoints"]
    } == {
        ENDPOINTS[0]: 2,
        ENDPOINTS[1]: 2,
    }

    serialized = json.dumps(report)
    json.loads(serialized)
    for token in TOKENS.values():
        assert token not in serialized


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
    assert report["load"]["reason"] == (
        "authorization_or_canary_prerequisite_failed"
    )
    assert len(transport.requests) == 8


def test_missing_required_canary_fails_closed() -> None:
    raw = _configuration()
    raw["canaries"] = raw["canaries"][:-1]

    with pytest.raises(validation.ConfigurationError) as raised:
        validation.parse_config(raw)

    assert raised.value.code == "missing_required_canaries"


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
    assert (
        report["load"]["multipleHttpEndpointsExercised"] is False
    )


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
        result
        for result in report["canaries"]["results"]
        if result["category"] == "viewer_mutation_denied"
    ]
    assert {result["failureReason"] for result in viewer_results} == {
        "credential_unavailable"
    }
    assert missing_token not in json.dumps(report)
    assert len(transport.requests) == 6


def test_example_configuration_is_valid_and_load_is_read_only() -> None:
    example = json.loads(
        (
            OPERATIONS_DIR / "production_validation.example.json"
        ).read_text(encoding="utf-8")
    )

    parsed = validation.parse_config(example)

    assert parsed.load.request.method == "GET"
    assert parsed.load.request.body is None
    assert parsed.load.minimum_endpoints == 1
    assert example["canaries"][1]["credentialType"] == (
        "alb-session-cookie"
    )


def test_config_rejects_inline_credentials_and_mutating_load() -> None:
    inline_secret = _configuration()
    inline_secret["canaries"][0]["headers"] = {
        "Authorization": "Bearer should-not-be-here"
    }
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


def test_alb_session_cookie_is_loaded_only_from_environment() -> None:
    raw = _configuration()
    raw["canaries"][1]["credentialType"] = "alb-session-cookie"
    config = validation.parse_config(raw)
    cookie = "AWSELBAuthSessionCookie-0=opaque; AWSELBAuthSessionCookie-1=opaque"

    headers = validation._credential_headers(
        config.canaries[1].request,
        {"AXON_CANARY_VIEWER_TOKEN": cookie},
    )

    assert headers["Cookie"] == cookie
    assert "Authorization" not in headers
