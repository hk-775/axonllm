from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = REPO_ROOT / "scripts" / "operations"
sys.path.insert(0, str(OPERATIONS))

import certify_agentcore as certification


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm-AbCdEf1234"
TOKENS = {
    "ACTIVE_TOKEN": "active-secret-token",
    "INACTIVE_TOKEN": "inactive-secret-token",
    "UNGRANTED_TOKEN": "ungranted-secret-token",
    "CROSS_TOKEN": "cross-secret-token",
    "ADMIN_TOKEN": "admin-secret-token",
    "VIEWER_TOKEN": "viewer-secret-token",
}
CANARY_CONTENT = "AXON_CANARY_OK"


def _configuration() -> dict:
    return {
        "schemaVersion": 1,
        "region": "us-east-1",
        "runtimeArn": RUNTIME_ARN,
        "qualifier": "production",
        "timeoutSeconds": 30,
        "maxResponseBytes": 1024 * 1024,
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
        "providers": [
            {"provider": "openai", "model": "openai-certification"},
            {"provider": "bedrock", "model": "bedrock-certification"},
        ],
        "query": {
            "catalog": "AwsDataCatalog",
            "database": "default",
            "datasourceId": "launch-data",
            "region": "us-east-1",
            "roleArn": (
                "arn:aws:iam::123456789012:"
                "role/axon-athena-certification"
            ),
            "sql": "SELECT 1 AS ready",
            "maxRows": 10,
            "workgroup": "axon_read_only",
        },
    }


def _production_configuration(
    *,
    include_ai21: bool = False,
) -> dict:
    raw = _configuration()
    raw["profile"] = certification.PRODUCTION_LAUNCH_PROFILE
    providers = set(certification.PRODUCTION_LAUNCH_PROVIDERS)
    if include_ai21:
        providers.add("ai21")
    raw["providers"] = [
        {
            "provider": provider,
            "model": f"{provider}-certification",
            "features": sorted(
                certification.PRODUCTION_REQUIRED_PROVIDER_FEATURES
                if provider == "fireworks"
                else certification.SUPPORTED_PROVIDER_FEATURES
            ),
        }
        for provider in sorted(providers)
    ]
    return raw


def _observation(status: int, body: dict | bytes, content_type: str = "application/json"):
    encoded = json.dumps(body, separators=(",", ":")).encode() if isinstance(body, dict) else body
    return certification.InvocationObservation(
        status_code=status,
        latency_ms=12.5,
        content_type=content_type,
        body=encoded,
    )


def _completion_body(
    provider: str = "openai",
    model: str = "openai-certification",
    content: str = CANARY_CONTENT,
) -> dict:
    return {
        "provider": provider,
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                }
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 4,
            "total_tokens": 9,
        },
    }


def _tool_body(
    provider: str = "openai",
    model: str = "openai-certification",
) -> dict:
    body = _completion_body(provider, model)
    body["choices"] = [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-launch-probe",
                        "type": "function",
                        "function": {
                            "name": certification._TOOL_NAME,
                            "arguments": json.dumps(
                                {"token": certification._TOOL_VALUE}
                            ),
                        },
                    }
                ],
            },
        }
    ]
    return body


def _stream_events(
    provider: str = "openai",
    model: str = "openai-certification",
) -> list[dict]:
    return [
        {
            "data": {
                "provider": provider,
                "model": model,
                "choices": [{"delta": {"content": "AXON_"}}],
            }
        },
        {
            "data": {
                "choices": [{"delta": {"content": "CANARY_OK"}}],
            }
        },
        {"data": "[DONE]"},
    ]


def _stream_body(
    provider: str,
    model: str,
) -> bytes:
    return b"".join(
        ("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
        for event in _stream_events(provider, model)
    )


def _tool_stream_body(
    provider: str,
    model: str,
) -> bytes:
    arguments = json.dumps(
        {"token": certification._TOOL_VALUE},
        separators=(",", ":"),
    )
    split_at = len(arguments) // 2
    events = [
        {
            "data": {
                "provider": provider,
                "model": model,
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-launch-probe",
                                    "type": "function",
                                    "function": {
                                        "name": certification._TOOL_NAME,
                                        "arguments": arguments[:split_at],
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
        },
        {
            "data": {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": arguments[split_at:],
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        },
        {"data": "[DONE]"},
    ]
    return b"".join(
        ("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
        for event in events
    )


class _Transport:
    def __init__(self) -> None:
        self.requests: list[certification.InvocationRequest] = []
        self.config_name = "Production"
        self.config_revision = 3

    def _tenant_config(self) -> dict:
        return {
            "tenant_id": "tenant-a",
            "project_id": "project-a",
            "revision": self.config_revision,
            "config": {
                "name": self.config_name,
                "budget_limit": None,
                "alert_threshold": None,
                "allowed_models": None,
                "guardrail_rules": [],
                "cache_enabled": False,
                "cache_ttl_seconds": 300,
                "semantic_cache_enabled": False,
                "semantic_cache_threshold": None,
                "log_level": "INFO",
                "log_destination": None,
                "prompt_caching_enabled": False,
                "ltm_enabled": False,
                "retention_period_hours": 24,
                "rate_limit_rpm": None,
            },
        }

    def __call__(self, request, timeout_seconds, max_response_bytes):
        assert timeout_seconds == 30
        assert max_response_bytes == 1024 * 1024
        self.requests.append(request)
        payload = json.loads(request.payload)
        authorization = request.headers.get("Authorization")
        if authorization is None:
            return _observation(401, {"message": "unauthorized"})
        token = authorization.removeprefix("Bearer ")
        if token == "invalid.jwt.signature":
            return _observation(401, {"message": "unauthorized"})
        if token == TOKENS["INACTIVE_TOKEN"]:
            return _observation(403, {"error": "denied"})
        if token in {
            TOKENS["UNGRANTED_TOKEN"],
            TOKENS["CROSS_TOKEN"],
        }:
            return _observation(404, {"error": "not found"})
        assert token in {
            TOKENS["ACTIVE_TOKEN"],
            TOKENS["ADMIN_TOKEN"],
            TOKENS["VIEWER_TOKEN"],
        }

        if "tenant_id" in payload:
            return _observation(
                400,
                {"error": {"code": "untrusted_identity_fields"}},
            )
        if payload["action"] == "health":
            return _observation(
                200,
                {
                    "status": "alive",
                    "ready": False,
                    "dependencies": "not_checked",
                },
            )
        if payload["action"] == "readiness":
            return _observation(
                200,
                {
                    "status": "ready",
                    "ready": True,
                    "state": "ready",
                    "dependencies": {
                        "runtime": "ready",
                        "dynamodb": "ready",
                    },
                },
            )
        if payload["action"] == "list_models":
            return _observation(
                200,
                {
                    "models": [
                        {
                            "name": "openai-certification",
                            "providers": ["openai"],
                        },
                        {
                            "name": "bedrock-certification",
                            "providers": ["bedrock"],
                        },
                        {
                            "name": "cohere-certification",
                            "providers": ["cohere"],
                        },
                    ]
                },
            )
        if payload["action"] == "get_tenant_config":
            return _observation(200, self._tenant_config())
        if payload["action"] == "update_tenant_config":
            if token == TOKENS["VIEWER_TOKEN"]:
                return _observation(
                    403,
                    {"error": {"code": "authorization_denied"}},
                )
            assert token == TOKENS["ADMIN_TOKEN"]
            if payload["expected_revision"] != self.config_revision:
                return _observation(
                    409,
                    {
                        "error": {
                            "code": "tenant_config_write_conflict",
                        }
                    },
                )
            self.config_revision += 1
            self.config_name = payload["config"]["name"]
            return _observation(200, self._tenant_config())
        if payload["action"] == "chat":
            provider = payload["provider"]
            assert provider == payload["model"].removesuffix(
                "-certification"
            )
            if payload["stream"]:
                if payload.get("tools"):
                    return _observation(
                        200,
                        _tool_stream_body(provider, payload["model"]),
                        "text/event-stream",
                    )
                return _observation(
                    200,
                    _stream_body(provider, payload["model"]),
                    "text/event-stream",
                )
            if payload.get("tools"):
                if any(
                    message.get("role") == "tool"
                    for message in payload["messages"]
                ):
                    return _observation(
                        200,
                        _completion_body(
                            provider,
                            payload["model"],
                            certification._TOOL_CONTINUATION_CONTENT,
                        ),
                    )
                if payload.get("tool_choice") == "none":
                    return _observation(
                        200,
                        _completion_body(provider, payload["model"]),
                    )
                if (
                    provider == "cohere"
                    and payload.get("tool_choice") == "required"
                ):
                    return _observation(
                        400,
                        {
                            "detail": {
                                "code": (
                                    certification
                                    ._UNSUPPORTED_PROVIDER_FEATURE
                                ),
                                "message": (
                                    "Required tool selection is unsupported."
                                ),
                            }
                        },
                    )
                return _observation(
                    200,
                    _tool_body(provider, payload["model"]),
                )
            return _observation(
                200,
                _completion_body(provider, payload["model"]),
            )
        assert payload["action"] == "query"
        if payload["sql"].startswith("DELETE"):
            return _observation(400, {"error": "select only"})
        return _observation(
            200,
            {
                "request_id": payload["request_id"],
                "datasource_id": payload["datasource_id"],
                "project_id": "project-a",
                "query_execution_id": "execution-1",
                "columns": [{"name": "ready", "type": "integer"}],
                "rows": [["1"]],
                "row_count": 1,
                "truncated": False,
                "statistics": {
                    "data_scanned_bytes": 0,
                    "engine_execution_ms": 1,
                    "result_bytes": 1,
                },
            },
        )


def _endpoint() -> dict[str, str]:
    return {
        "runtimeArn": RUNTIME_ARN,
        "endpointArn": f"{RUNTIME_ARN}/runtime-endpoint/production",
        "endpointName": "production",
        "status": "READY",
        "runtimeVersion": "7",
        "invocationUrl": (
            "https://bedrock-agentcore.us-east-1.amazonaws.com/"
            f"runtimes/{quote(RUNTIME_ARN, safe='')}/invocations"
            "?qualifier=production"
        ),
    }


def test_full_certification_covers_agentcore_rbac_providers_stream_and_query() -> None:
    config = certification.parse_config(_configuration())
    transport = _Transport()

    report = certification.run_certification(
        config,
        environ=TOKENS,
        transport=transport,
        endpoint_metadata=_endpoint(),
    )

    assert report["overallStatus"] == "PASS"
    assert report["summary"] == {
        "checkCount": 24,
        "passed": 24,
        "failed": 0,
        "providerCount": 2,
        "profile": "enabled-providers",
        "providerFeatures": {
            "bedrock": ["completion", "stream"],
            "openai": ["completion", "stream"],
        },
        "queryBackendExercised": True,
        "tenantConfigRbacExercised": True,
        "agentcoreHttpsInvoked": True,
    }
    assert len(transport.requests) == 24
    assert transport.config_name == "Production"
    assert transport.config_revision == 5
    chat_payloads = [
        json.loads(request.payload)
        for request in transport.requests
        if json.loads(request.payload).get("action") == "chat"
    ]
    assert chat_payloads
    assert all(
        payload["provider"]
        == payload["model"].removesuffix("-certification")
        for payload in chat_payloads
    )
    query_requests = [
        request
        for request in transport.requests
        if json.loads(request.payload).get("action") == "query"
    ]
    assert query_requests
    assert all(
        request.headers["Authorization"]
        == f"Bearer {TOKENS['VIEWER_TOKEN']}"
        for request in query_requests
    )
    assert {check["category"] for check in report["checks"]} >= {
        "missing_jwt_denied",
        "invalid_jwt_denied",
        "inactive_membership_denied",
        "missing_project_grant_denied",
        "cross_tenant_denied",
        "payload_identity_rejected",
        "dependency_readiness",
        "provider_completion",
        "provider_stream",
        "query_select",
        "query_mutation_denied",
        "admin_tenant_config_read",
        "viewer_tenant_config_read",
        "viewer_tenant_config_mutation_denied",
        "tenant_config_project_isolation",
        "tenant_config_tenant_isolation",
        "admin_tenant_config_cas_mutation",
        "admin_tenant_config_mutation_confirmed",
        "admin_tenant_config_cas_rollback",
        "admin_tenant_config_rollback_confirmed",
    }
    serialized = json.dumps(report)
    for token in TOKENS.values():
        assert token not in serialized
    assert "invalid.jwt.signature" not in serialized
    assert "Axon admin CAS " not in serialized
    assert "Axon viewer denial " not in serialized


def test_provider_tool_contract_is_exercised_end_to_end() -> None:
    raw = _configuration()
    raw["providers"][0]["features"] = [
        "completion",
        "stream",
        "tool_calling",
    ]
    report = certification.run_certification(
        certification.parse_config(raw),
        environ=TOKENS,
        transport=_Transport(),
        endpoint_metadata=_endpoint(),
    )

    tool_checks = {
        check["category"]: check
        for check in report["checks"]
        if check["category"].startswith("provider_tool_")
    }
    assert report["overallStatus"] == "PASS"
    assert set(tool_checks) == {
        "provider_tool_call",
        "provider_tool_required",
        "provider_tool_continuation",
        "provider_tool_none",
        "provider_tool_stream",
    }
    assert all(check["provider"] == "openai" for check in tool_checks.values())
    assert all(check["passed"] is True for check in tool_checks.values())


def test_cohere_tool_certification_proves_supported_and_rejected_controls() -> None:
    raw = _configuration()
    raw["providers"] = [
        {
            "provider": "cohere",
            "model": "cohere-certification",
            "features": ["completion", "stream", "tool_calling"],
        }
    ]
    report = certification.run_certification(
        certification.parse_config(raw),
        environ=TOKENS,
        transport=_Transport(),
        endpoint_metadata=_endpoint(),
    )

    tool_checks = {
        check["category"]: check
        for check in report["checks"]
        if check["category"].startswith("provider_tool_")
    }
    assert report["overallStatus"] == "PASS"
    assert set(tool_checks) == {
        "provider_tool_call",
        "provider_tool_required",
        "provider_tool_continuation",
        "provider_tool_none",
        "provider_tool_stream",
    }
    assert tool_checks["provider_tool_required"]["statusCode"] == 400
    assert (
        tool_checks["provider_tool_required"]["validation"]
        == "required_tool_selection_explicitly_unsupported"
    )
    assert all(check["passed"] is True for check in tool_checks.values())


def test_production_launch_profile_requires_the_mandatory_provider_baseline() -> None:
    raw = _production_configuration()
    parsed = certification.parse_config(raw)

    assert parsed.profile == certification.PRODUCTION_LAUNCH_PROFILE
    assert {
        case.provider for case in parsed.providers
    } == certification.PRODUCTION_LAUNCH_PROVIDERS
    features = {
        case.provider: case.features for case in parsed.providers
    }
    assert features["fireworks"] == (
        certification.PRODUCTION_REQUIRED_PROVIDER_FEATURES
    )
    assert features["anthropic"] == (
        certification.SUPPORTED_PROVIDER_FEATURES
    )


def test_production_launch_profile_accepts_optional_direct_ai21() -> None:
    parsed = certification.parse_config(
        _production_configuration(include_ai21=True)
    )

    assert {case.provider for case in parsed.providers} == (
        certification.PRODUCTION_ALLOWED_PROVIDERS
    )


def test_production_launch_profile_still_accepts_external_identity_contract() -> None:
    legacy = _production_configuration()
    legacy.pop("tenantConfig")
    legacy["identities"].pop("adminCredentialEnv")
    legacy["identities"].pop("viewerCredentialEnv")

    legacy_parsed = certification.parse_config(legacy)

    assert legacy_parsed.tenant_config is None
    assert legacy_parsed.identities.admin_env is None
    assert legacy_parsed.identities.viewer_env is None


def test_production_launch_profile_rejects_a_missing_mandatory_provider() -> None:
    raw = _production_configuration()
    raw["providers"] = [
        case
        for case in raw["providers"]
        if case["provider"] != "xai"
    ]

    with pytest.raises(
        certification.CertificationError,
        match="missing mandatory providers: xai",
    ):
        certification.parse_config(raw)


def test_production_launch_profile_rejects_an_unsupported_extra_provider() -> None:
    raw = _production_configuration()
    raw["providers"].append(
        {
            "provider": "unsupported",
            "model": "unsupported-certification",
            "features": ["completion", "stream"],
        }
    )

    with pytest.raises(
        certification.CertificationError,
        match="unsupported providers: unsupported",
    ):
        certification.parse_config(raw)


def test_production_launch_profile_requires_completion_and_stream() -> None:
    raw = _production_configuration()
    raw["providers"][0]["features"].remove("stream")

    with pytest.raises(
        certification.CertificationError,
        match="must include completion and stream",
    ):
        certification.parse_config(raw)


def test_production_launch_profile_requires_tools_when_supported() -> None:
    raw = _production_configuration()
    openai = next(
        case
        for case in raw["providers"]
        if case["provider"] == "openai"
    )
    openai["features"].remove("tool_calling")

    with pytest.raises(
        certification.CertificationError,
        match=(
            "must exactly match the production launch contract for "
            "openai"
        ),
    ):
        certification.parse_config(raw)


def test_production_launch_profile_rejects_unavailable_fireworks_tools() -> None:
    raw = _production_configuration()
    fireworks = next(
        case
        for case in raw["providers"]
        if case["provider"] == "fireworks"
    )
    fireworks["features"].append("tool_calling")

    with pytest.raises(
        certification.CertificationError,
        match=(
            "must exactly match the production launch contract for "
            "fireworks"
        ),
    ):
        certification.parse_config(raw)


def test_semantic_failure_report_does_not_retain_response_content() -> None:
    credential_like_content = "Bearer provider-secret-must-not-enter-report"

    class _Failing(_Transport):
        def __call__(self, request, timeout_seconds, max_response_bytes):
            result = super().__call__(
                request,
                timeout_seconds,
                max_response_bytes,
            )
            payload = json.loads(request.payload)
            if (
                payload.get("action") == "chat"
                and payload.get("model") == "openai-certification"
                and not payload.get("stream")
            ):
                body = _completion_body()
                body["choices"][0]["message"]["content"] = credential_like_content
                return _observation(200, body)
            return result

    report = certification.run_certification(
        certification.parse_config(_configuration()),
        environ=TOKENS,
        transport=_Failing(),
        endpoint_metadata=_endpoint(),
    )

    assert report["overallStatus"] == "FAIL"
    assert report["summary"]["failed"] == 1
    check = next(item for item in report["checks"] if item["name"] == "openai-completion")
    assert check["passed"] is False
    assert check["validation"] == "exact_provider_model_canary_and_usage"
    assert set(check) == {
        "name",
        "category",
        "passed",
        "validation",
        "statusCode",
        "latencyMs",
        "contentType",
        "responseBytes",
        "responseSha256",
        "transportError",
        "provider",
        "model",
    }
    serialized = json.dumps(report)
    assert credential_like_content not in serialized
    assert CANARY_CONTENT not in serialized


def test_failed_admin_rollback_aborts_without_credential_or_canary_leak() -> None:
    class _Unrestorable(_Transport):
        def __call__(self, request, timeout_seconds, max_response_bytes):
            payload = json.loads(request.payload)
            if (
                payload.get("action") == "update_tenant_config"
                and payload.get("config", {}).get("name") == "Production"
                and self.config_name.startswith("Axon admin CAS ")
            ):
                self.requests.append(request)
                return _observation(
                    409,
                    {
                        "error": {
                            "code": "tenant_config_write_conflict",
                        }
                    },
                )
            return super().__call__(
                request,
                timeout_seconds,
                max_response_bytes,
            )

    with pytest.raises(
        certification.CertificationError,
        match="rollback is incomplete",
    ) as failure:
        certification.run_certification(
            certification.parse_config(_configuration()),
            environ=TOKENS,
            transport=_Unrestorable(),
            endpoint_metadata=_endpoint(),
        )

    message = str(failure.value)
    assert "Axon admin CAS " not in message
    assert all(token not in message for token in TOKENS.values())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda body: body.update(provider="bedrock"),
            id="wrong-provider",
        ),
        pytest.param(
            lambda body: body.update(model="other-model"),
            id="wrong-model",
        ),
        pytest.param(
            lambda body: body["choices"][0]["message"].update(content="AXON_CANARY_OK "),
            id="wrong-content",
        ),
        pytest.param(
            lambda body: body.update(choices=[]),
            id="missing-choice",
        ),
        pytest.param(
            lambda body: body["choices"][0].update(message="malformed"),
            id="malformed-message",
        ),
        pytest.param(
            lambda body: body["usage"].update(total_tokens="9"),
            id="malformed-usage",
        ),
    ],
)
def test_completion_canary_requires_exact_semantics(mutate) -> None:
    case = certification.ProviderCase(
        provider="openai",
        model="openai-certification",
    )
    body = _completion_body()
    assert certification._valid_completion_canary(body, case)

    mutate(body)

    assert not certification._valid_completion_canary(body, case)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda events: events[0]["data"].update(provider="bedrock"),
            id="wrong-provider",
        ),
        pytest.param(
            lambda events: events[0]["data"].update(model="other-model"),
            id="wrong-model",
        ),
        pytest.param(
            lambda events: events[0]["data"].pop("provider"),
            id="missing-provider-proof",
        ),
        pytest.param(
            lambda events: events[1]["data"]["choices"][0]["delta"].update(content="CANARY_FAIL"),
            id="wrong-aggregate-content",
        ),
        pytest.param(
            lambda events: events[0]["data"].update(choices={}),
            id="malformed-choices",
        ),
        pytest.param(
            lambda events: events[0]["data"]["choices"][0].update(delta="malformed"),
            id="malformed-delta",
        ),
        pytest.param(
            lambda events: events[1]["data"]["choices"][0]["delta"].update(content=7),
            id="non-string-content",
        ),
        pytest.param(
            lambda events: events.insert(
                -1,
                {
                    "data": {
                        "error": {"message": "provider failed"},
                        "choices": [{"delta": {}}],
                    }
                },
            ),
            id="error-event",
        ),
        pytest.param(
            lambda events: events.pop(),
            id="missing-done",
        ),
        pytest.param(
            lambda events: events.append({"data": {"choices": [{"delta": {}}]}}),
            id="data-after-done",
        ),
        pytest.param(
            lambda events: events[1]["data"].update(provider="bedrock"),
            id="conflicting-provider",
        ),
    ],
)
def test_stream_canary_requires_exact_unambiguous_semantics(mutate) -> None:
    case = certification.ProviderCase(
        provider="openai",
        model="openai-certification",
    )
    events = _stream_events()
    assert certification._valid_stream_canary(events, case)

    mutate(events)

    assert not certification._valid_stream_canary(events, case)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda events: events[1]["data"]["choices"][0].update(
                finish_reason="stop"
            ),
            id="wrong-finish-reason",
        ),
        pytest.param(
            lambda events: events[1]["data"]["choices"][0]["delta"][
                "tool_calls"
            ][0]["function"].update(arguments='{"token":"WRONG"}'),
            id="wrong-arguments",
        ),
        pytest.param(
            lambda events: events[0]["data"]["choices"][0]["delta"].update(
                content="unexpected"
            ),
            id="unexpected-content",
        ),
        pytest.param(
            lambda events: events[1]["data"]["choices"][0]["delta"][
                "tool_calls"
            ].append(
                {
                    "index": 1,
                    "id": "call-second",
                    "type": "function",
                    "function": {
                        "name": certification._TOOL_NAME,
                        "arguments": '{"token":"AXON_TOOL_OK"}',
                    },
                }
            ),
            id="second-call",
        ),
    ],
)
def test_stream_tool_canary_requires_one_exact_call(mutate) -> None:
    case = certification.ProviderCase(
        provider="openai",
        model="openai-certification",
    )
    observation = _observation(
        200,
        _tool_stream_body(case.provider, case.model),
        "text/event-stream",
    )
    events = certification._sse_events(observation)
    assert certification._valid_stream_tool_canary(events, case)

    mutate(events)

    assert not certification._valid_stream_tool_canary(events, case)


def test_sse_parser_supports_framed_multiline_json_events() -> None:
    body = (
        b": keepalive\r\n"
        b"event: message\r\n"
        b'data: {"data":{"provider":"openai",\r\n'
        b'data: "model":"openai-certification",'
        b'"choices":[{"delta":{"content":"AXON_"}}]}}\r\n\r\n'
        b'data: {"data":{"choices":[{"delta":'
        b'{"content":"CANARY_OK"}}]}}\r\n\r\n'
        b'data: {"data":"[DONE]"}\r\n\r\n'
    )
    observation = _observation(200, body, "text/event-stream")
    case = certification.ProviderCase(
        provider="openai",
        model="openai-certification",
    )

    events = certification._sse_events(observation)

    assert certification._valid_stream_canary(events, case)


@pytest.mark.parametrize(
    "body",
    [
        b'data: {"data":not-json}\n\n',
        b'data: {"data":"[DONE]"}\n',
        b"data: \xff\n\n",
    ],
)
def test_sse_parser_rejects_malformed_or_unframed_events(body) -> None:
    observation = _observation(200, body, "text/event-stream")

    assert certification._sse_events(observation) is None


def test_invocation_url_encodes_the_full_arn_and_uses_endpoint_qualifier() -> None:
    config = certification.parse_config(_configuration())

    url = certification.invocation_url(config)

    assert quote(RUNTIME_ARN, safe="") in url
    assert url.startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/")
    assert url.endswith("/invocations?qualifier=production")


def test_endpoint_metadata_requires_ready_stable_exact_runtime() -> None:
    class _Control:
        def get_agent_runtime_endpoint(self, **kwargs):
            assert kwargs == {
                "agentRuntimeId": "axonllm-AbCdEf1234",
                "endpointName": "production",
            }
            return {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeEndpointArn": (f"{RUNTIME_ARN}/runtime-endpoint/production"),
                "name": "production",
                "status": "READY",
                "liveVersion": "7",
                "targetVersion": "7",
            }

    metadata = certification.resolve_endpoint_metadata(
        _Control(),
        certification.parse_config(_configuration()),
    )

    assert metadata == _endpoint()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(runtimeArn="arn:aws:other"),
        lambda value: value["providers"].append({"provider": "openai", "model": "duplicate"}),
        lambda value: value["query"].update(sql="DELETE FROM users"),
        lambda value: value["identities"].update(activeCredentialEnv="literal-secret-value"),
        lambda value: value["identities"].pop("viewerCredentialEnv"),
        lambda value: value.pop("tenantConfig"),
        lambda value: value["tenantConfig"].update(tenantId="tenant other"),
        lambda value: value.update(apiKey="must-not-appear"),
    ],
)
def test_config_rejects_unsafe_or_incomplete_certification_scenarios(mutate) -> None:
    value = _configuration()
    mutate(value)

    with pytest.raises(certification.CertificationError):
        certification.parse_config(value)
