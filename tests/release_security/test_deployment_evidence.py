from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import deployment_evidence  # noqa: E402
import certify_external_oidc_agentcore as external_oidc  # noqa: E402


ACCOUNT = "123456789012"
REGION = "us-east-1"
AGENTCORE_DIGEST = "sha256:" + "a" * 64
FARGATE_DIGEST = "sha256:" + "b" * 64
AGENTCORE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/agentcore@{AGENTCORE_DIGEST}"
FARGATE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/fargate@{FARGATE_DIGEST}"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/AxonRuntime-1234567890"
ENDPOINT_ARN = RUNTIME_ARN + "/runtime-endpoint/production"
CANDIDATE_ENDPOINT_NAME = "candidate_" + "a" * 32
CANDIDATE_ENDPOINT_ARN = RUNTIME_ARN + "/runtime-endpoint/" + CANDIDATE_ENDPOINT_NAME
EVIDENCE_BUCKET = "axonllm-production-evidence"
EVIDENCE_PREFIX = "deployment-evidence"
REHEARSAL_REPORT_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/"
    "launch-rehearsal-reports/owner/repo/55/1/"
    "agentcore-launch-rehearsal-evidence.json"
)
REHEARSAL_SIGNATURE_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/"
    "launch-rehearsal-reports/owner/repo/55/1/"
    "agentcore-launch-rehearsal-evidence-kms-signature.json"
)
REVIEWED_CONFIG_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/launch-rehearsal-gates/owner/repo/54/1/reviewed-launch-gates.json"
)
REVIEWED_CONFIG_VERSION_ID = "reviewed-config-version-1"
REVIEWED_CONFIG_SHA256 = "e" * 64
EXTERNAL_OIDC_EVIDENCE_PREFIX = "external-oidc-evidence"
EXTERNAL_OIDC_REPORT_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EXTERNAL_OIDC_EVIDENCE_PREFIX}/"
    f"owner/repo/{'1' * 40}/77/1/"
    "external-oidc-agentcore-report.json"
)
EXTERNAL_OIDC_SIGNATURE_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EXTERNAL_OIDC_EVIDENCE_PREFIX}/"
    f"owner/repo/{'1' * 40}/77/1/"
    "external-oidc-agentcore-kms-signature.json"
)
QUALIFICATION_TEARDOWN_RECEIPT_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/owner/repo/{'1' * 40}/99/1/qualification-teardown-receipt.json"
)
QUALIFICATION_TEARDOWN_SIGNATURE_URI = (
    f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/owner/repo/{'1' * 40}/99/1/qualification-teardown-signature.json"
)


def _write(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _rebind_target_health(report: dict[str, object]) -> None:
    target_health = report["targetHealth"]
    assert isinstance(target_health, dict)
    for name in ("preLoad", "postLoad"):
        observation = target_health[name]
        assert isinstance(observation, dict)
        canonical = {key: value for key, value in observation.items() if key != "observationSha256"}
        observation["observationSha256"] = hashlib.sha256(
            json.dumps(
                canonical,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    evidence = {
        "schemaVersion": deployment_evidence.TARGET_HEALTH_SCHEMA,
        "preLoad": target_health["preLoad"],
        "loadInterval": target_health["loadInterval"],
        "postLoad": target_health["postLoad"],
    }
    target_health["evidenceSha256"] = hashlib.sha256(
        json.dumps(
            evidence,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _fixtures(
    tmp_path: Path,
    *,
    include_ai21: bool = False,
) -> argparse.Namespace:
    launch_providers = set(deployment_evidence.PRODUCTION_LAUNCH_PROVIDERS)
    if include_ai21:
        launch_providers.add("ai21")
    provider_features = {
        provider: deployment_evidence.PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[provider]
        for provider in sorted(launch_providers)
    }
    provider_checks = sorted(deployment_evidence._expected_provider_checks(provider_features))
    release = {
        "schema": deployment_evidence.RELEASE_SCHEMA,
        "source": {
            "repository": "owner/repo",
            "commit": "1" * 40,
            "ref": "refs/tags/v1.2.3",
            "workflowRef": ("owner/repo/.github/workflows/release-security.yml@refs/tags/v1.2.3"),
            "runId": "41",
            "runAttempt": "1",
            "eventName": "push",
        },
        "signing": {"keyArn": (f"arn:aws:kms:{REGION}:{ACCOUNT}:key/11111111-1111-1111-1111-111111111111")},
        "targets": {
            "agentcore": {
                "digest": AGENTCORE_DIGEST,
                "platform": "linux/arm64",
            },
            "fargate": {
                "digest": FARGATE_DIGEST,
                "platform": "linux/amd64",
            },
        },
    }
    identity = {
        deployment_evidence.IDENTITY_STACK: {
            "OidcIssuer": "https://cognito-idp.us-east-1.amazonaws.com/pool",
            "OidcClientId": "client-id",
            "OidcAudience": "client-id",
            "UserPoolId": "us-east-1_pool",
            "EndpointMode": "custom-domain",
            "AlbClientId": "alb-client-id",
            "AlbClientSecretArn": (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:alb-client"),
            "ControlPlaneDomainName": "axon.example.com",
        }
    }
    runtime_outputs = {
        "RuntimeImageUri": AGENTCORE_IMAGE,
        "ProviderSecretVersion": "version-2",
        "RecoveryCutoverMode": "normal",
        "RuntimeEndpointName": "production",
        "CandidateRuntimeEndpointName": CANDIDATE_ENDPOINT_NAME,
        "EnabledProviders": ",".join(provider_features),
        "RuntimeVersion": "7",
        "CandidateRuntimeVersion": "7",
        "ProductionRuntimeVersion": "7",
        "StateTableName": "axonllm-agentcore-state",
        "SelectedRuntimeStateTableName": "axonllm-agentcore-state",
        "RecoveryApprovalId": "CHG-123",
        "RuntimeArn": RUNTIME_ARN,
        "RuntimeEndpointArn": ENDPOINT_ARN,
        "CandidateRuntimeEndpointArn": CANDIDATE_ENDPOINT_ARN,
        "ProviderSecretArn": (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:providers"),
    }
    runtime = {deployment_evidence.AGENTCORE_STACK: runtime_outputs}
    control_outputs = {
        "AgentCoreStackName": deployment_evidence.AGENTCORE_STACK,
        "PrimaryStateTableName": "axonllm-agentcore-state",
        "SelectedRuntimeStateTableName": "axonllm-agentcore-state",
        "RecoveryCutoverMode": "normal",
        "RecoveryApprovalId": "CHG-123",
        "ClusterName": "cluster",
        "ServiceName": "service",
        "ControlPlaneImageUri": FARGATE_IMAGE,
        "TaskDefinitionArn": (f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/axonllm-control-plane:7"),
        "TargetGroupArn": (
            f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/axonllm-control-plane/0123456789abcdef"
        ),
        "QueryPlaneEnabled": "true",
        "EndpointMode": "custom-domain",
        "ControlPlaneUrl": "https://axon.example.com",
        "ControlPlaneDomainName": "axon.example.com",
        "ControlPlaneAuthMode": "alb-cognito",
    }
    control = {deployment_evidence.CONTROL_PLANE_STACK: control_outputs}
    provider = {
        "secretArn": runtime_outputs["ProviderSecretArn"],
        "versionId": "version-2",
        "previousVersionId": "version-1",
        "changed": True,
        "configuredFields": ["OPENAI_API_KEY"],
        "fingerprint": "c" * 64,
    }
    recovery = {
        "tableArn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-agentcore-state"),
        "pointInTimeRecovery": "ENABLED",
        "latestRestorableAgeMinutes": 4.0,
        "backupVault": "axon-agent-vault",
        "backupVaultLocked": True,
        "backupVaultLockMode": "GOVERNANCE",
        "backupVaultMinRetentionDays": 30,
        "backupVaultMaxRetentionDays": 365,
        "latestBackupAgeHours": 2.0,
        "deploymentBackup": {
            "backupJobId": "backup-job-123",
            "status": "COMPLETED",
            "backupVault": "axon-agent-vault",
            "resourceArn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-agentcore-state"),
            "recoveryPointArn": (f"arn:aws:backup:{REGION}:{ACCOUNT}:recovery-point:backup-job-123"),
            "creationDate": "2026-08-11T11:55:00+00:00",
            "completionDate": "2026-08-11T11:59:00+00:00",
        },
        "restoreExercise": {
            "targetTable": ("axonllm-agentcore-state-restore-validation-20260811115900-a1b2c3"),
            "status": "validated",
            "retained": False,
            "pointInTimeRecovery": None,
            "timeToLive": None,
            "deletionProtection": False,
            "sampledItemCount": 25,
            "sampledItemsSha256": "d" * 64,
        },
    }
    transition = {
        "phase": "status",
        "approvalId": "CHG-123",
        "mode": "normal",
        "primaryTable": "axonllm-agentcore-state",
        "selectedTable": "axonllm-agentcore-state",
        "quiescedAt": "not-quiesced",
        "minimumQuiescenceSeconds": 14700,
        "endpoint": {
            "name": "production",
            "arn": ENDPOINT_ARN,
            "status": "READY",
            "version": "7",
        },
        "controlPlane": {
            "agentCoreStackName": deployment_evidence.AGENTCORE_STACK,
            "recoveryMode": "normal",
            "selectedTable": "axonllm-agentcore-state",
            "pendingCount": 0,
            "desiredCount": 2,
            "runningCount": 2,
        },
    }
    certification_checks = (
        [
            {
                "name": category,
                "category": category,
                "passed": True,
            }
            for category in (
                "cross_tenant_denied",
                "dependency_readiness",
                "inactive_membership_denied",
                "invalid_jwt_denied",
                "liveness",
                "missing_jwt_denied",
                "missing_project_grant_denied",
                "model_listing",
                "payload_identity_rejected",
                "query_mutation_denied",
                "query_select",
            )
        ]
        + [
            {
                "name": f"{provider}-{category}",
                "category": category,
                "provider": provider,
                "passed": True,
            }
            for provider, category in provider_checks
        ]
        + [
            {
                "name": "extra-contract",
                "category": "extra_contract",
                "passed": True,
            }
        ]
    )
    certification = {
        "schema": deployment_evidence.CERTIFICATION_SCHEMA,
        "generatedAt": "2026-08-11T12:00:00+00:00",
        "overallStatus": "PASS",
        "endpoint": {
            "runtimeArn": RUNTIME_ARN,
            "endpointArn": CANDIDATE_ENDPOINT_ARN,
            "endpointName": CANDIDATE_ENDPOINT_NAME,
            "status": "READY",
            "runtimeVersion": "7",
        },
        "summary": {
            "checkCount": len(certification_checks),
            "passed": len(certification_checks),
            "failed": 0,
            "providerCount": len(provider_features),
            "profile": "production-launch",
            "providerFeatures": {provider: sorted(features) for provider, features in provider_features.items()},
            "queryBackendExercised": True,
            "agentcoreHttpsInvoked": True,
        },
        "checks": certification_checks,
    }
    production_certification = json.loads(json.dumps(certification))
    production_certification["endpoint"]["endpointArn"] = ENDPOINT_ARN
    production_certification["endpoint"]["endpointName"] = "production"
    setup = {
        "schema_version": 2,
        "identity_mode": "managed-cognito",
        "aws_region": REGION,
        "runtime": {
            "verified_image_uri": AGENTCORE_IMAGE,
            "enabled_providers": sorted(provider_features),
        },
        "control_plane": {
            "verified_image_uri": FARGATE_IMAGE,
            "domain_name": "axon.example.com",
        },
    }
    required_control_categories = sorted(deployment_evidence.REQUIRED_CONTROL_PLANE_CATEGORIES)
    production_validation = {
        "schemaVersion": deployment_evidence.PRODUCTION_VALIDATION_SCHEMA,
        "target": "fargate",
        "startedAt": "2026-08-11T12:01:00+00:00",
        "finishedAt": "2026-08-11T12:02:00+00:00",
        "overallStatus": "PASS",
        "claims": {
            "agentcoreCutoverValidated": False,
            "queryBackendExercised": False,
            "backingInstanceIdentityValidated": True,
        },
        "httpEndpoints": ["https://axon.example.com"],
        "authorizationContract": {
            "status": "PASS",
            "sourcePolicyContractExercised": True,
        },
        "canaries": {
            "status": "PASS",
            "allEndpointsCovered": True,
            "allRequiredCanariesPassedOnAllEndpoints": True,
            "configuredCategories": required_control_categories,
            "results": [
                {
                    "category": category,
                    "baseUrl": "https://axon.example.com",
                    "credentialType": "alb-session-cookie",
                    "passed": True,
                }
                for category in required_control_categories
            ],
        },
        "load": {
            "status": "PASS",
            "credentialType": "alb-session-cookie",
            "requestCountConfigured": 4,
            "requestCountCompleted": 4,
            "concurrency": 2,
            "backingInstanceIdentityValidated": True,
        },
        "launchGates": {
            "status": "PASS",
            "scenarios": {category: {"passed": True} for category in required_control_categories},
            "concurrencyLoad": {
                "passed": True,
                "requestCountConfigured": 4,
                "requestCountCompleted": 4,
                "concurrency": 2,
            },
        },
    }
    target_group_sha256 = hashlib.sha256(control_outputs["TargetGroupArn"].encode("utf-8")).hexdigest()
    target_id_sha256 = ["1" * 64, "2" * 64]

    def target_observation(
        phase: str,
        collected_at: str,
        source_sha256: str,
    ) -> dict[str, object]:
        observation: dict[str, object] = {
            "schemaVersion": deployment_evidence.TARGET_HEALTH_SCHEMA,
            "phase": phase,
            "collectedAt": collected_at,
            "sourceSha256": source_sha256,
            "healthyTargetCount": 2,
            "targetIdSha256": target_id_sha256,
            "targetGroupArnSha256": target_group_sha256,
        }
        canonical = json.dumps(
            observation,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            **observation,
            "observationSha256": hashlib.sha256(canonical).hexdigest(),
        }

    pre_load = target_observation(
        "pre-load",
        "2026-08-11T12:01:30+00:00",
        "3" * 64,
    )
    post_load = target_observation(
        "post-load",
        "2026-08-11T12:01:55+00:00",
        "4" * 64,
    )
    load_interval = {
        "startedAt": "2026-08-11T12:01:35+00:00",
        "finishedAt": "2026-08-11T12:01:50+00:00",
    }
    target_evidence = {
        "schemaVersion": deployment_evidence.TARGET_HEALTH_SCHEMA,
        "preLoad": pre_load,
        "loadInterval": load_interval,
        "postLoad": post_load,
    }
    target_evidence_sha256 = hashlib.sha256(
        json.dumps(
            target_evidence,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    production_validation["targetHealth"] = {
        "status": "PASS",
        "minimumHealthyTargets": 2,
        "sameTargetSetAcrossLoad": True,
        "chronologyValidated": True,
        "backingInstanceIdentityValidated": True,
        "targetGroupArnSha256": target_group_sha256,
        "evidenceSha256": target_evidence_sha256,
        "loadInterval": load_interval,
        "preLoad": pre_load,
        "postLoad": post_load,
    }

    def rehearsal_reference(name: str, digest: str) -> dict[str, str]:
        return {
            "s3Uri": (f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/launch-rehearsal-gates/{name}.json"),
            "versionId": f"version-{name}",
            "sha256": digest,
        }

    launch_rehearsal = {
        "schema": deployment_evidence.LAUNCH_REHEARSAL_SCHEMA,
        "releaseCommit": "1" * 40,
        "region": REGION,
        "agentcoreImage": AGENTCORE_IMAGE,
        "controlPlaneImage": FARGATE_IMAGE,
        "generatedAt": "2026-08-11T12:03:00+00:00",
        "producer": {
            "repository": "owner/repo",
            "workflowRef": ("owner/repo/.github/workflows/agentcore-launch-rehearsal-evidence.yml@refs/heads/main"),
            "workflowCommit": "1" * 40,
            "parentWorkflowRef": ("owner/repo/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
            "parentWorkflowCommit": "1" * 40,
            "runId": "99",
            "runAttempt": "1",
        },
        "gateExecution": {
            "repository": "owner/repo",
            "workflowRef": ("owner/repo/.github/workflows/agentcore-launch-gates.yml@refs/heads/main"),
            "workflowCommit": "1" * 40,
            "parentWorkflowRef": ("owner/repo/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
            "parentWorkflowCommit": "1" * 40,
            "checkedOutCommit": "1" * 40,
            "runId": "99",
            "runAttempt": "1",
            "reviewedConfigS3Uri": REVIEWED_CONFIG_URI,
            "reviewedConfigVersionId": REVIEWED_CONFIG_VERSION_ID,
            "reviewedConfigSha256": REVIEWED_CONFIG_SHA256,
        },
        "sourceManifest": {
            "artifact": rehearsal_reference("manifest", "5" * 64),
            "signature": rehearsal_reference(
                "manifest-signature",
                "6" * 64,
            ),
        },
        "gates": {
            name: {
                "status": "PASS",
                "environment": "production",
                "startedAt": "2026-08-11T12:01:00+00:00",
                "completedAt": "2026-08-11T12:03:00+00:00",
                "commandReceiptsSha256": "7" * 64,
                "observationsSha256": "8" * 64,
                "artifact": rehearsal_reference(
                    f"{name}-receipt",
                    "9" * 64,
                ),
                "signature": rehearsal_reference(
                    f"{name}-signature",
                    "a" * 64,
                ),
            }
            for name in deployment_evidence.REQUIRED_REHEARSAL_GATES
        },
    }
    launch_rehearsal_report = _write(
        tmp_path / "launch-rehearsal-evidence.json",
        launch_rehearsal,
    )
    launch_rehearsal_signature = _write(
        tmp_path / "launch-rehearsal-evidence-signature.json",
        {
            "schema": "axonllm.kms-signature/v1",
            "signature": "not-secret-test-fixture",
        },
    )
    external_full_checks = [
        {
            "passed": True,
            "category": category,
            "responseSha256": "b" * 64,
            "transportError": None,
        }
        for category in sorted(deployment_evidence.REQUIRED_CERTIFICATION_CATEGORIES)
    ]
    external_full_checks.extend(
        {
            "passed": True,
            "category": category,
            "provider": provider_name,
            "responseSha256": "c" * 64,
            "transportError": None,
        }
        for provider_name, category in provider_checks
    )

    def issuer_evidence(
        issuer: str,
        digest_character: str,
    ) -> dict[str, object]:
        return {
            "issuer": issuer,
            "discoveryUrl": (f"{issuer}/.well-known/openid-configuration"),
            "jwksUri": f"{issuer}/keys",
            "discoverySha256": digest_character * 64,
            "jwksSha256": digest_character * 64,
            "keySetSha256": digest_character * 64,
            "keyCount": 1,
            "discoveryFreshness": {
                "date": "2026-08-11T12:00:00+00:00",
                "maxAgeSeconds": 3600,
                "currentAgeSeconds": 1.0,
            },
            "jwksFreshness": {
                "date": "2026-08-11T12:00:00+00:00",
                "maxAgeSeconds": 3600,
                "currentAgeSeconds": 1.0,
            },
        }

    http_check_contracts = {
        "admin_model_list": (
            [200],
            200,
            "canonical_membership_resolved_and_models_returned",
        ),
        "viewer_model_list": (
            [200],
            200,
            "canonical_membership_resolved_and_models_returned",
        ),
        "admin_tenant_config_read": (
            [200],
            200,
            "canonical_admin_config_read_over_agentcore_https",
        ),
        "viewer_tenant_config_read": (
            [200],
            200,
            "canonical_viewer_config_read_matches_admin_snapshot",
        ),
        "viewer_tenant_config_write_denied": (
            [403],
            403,
            "viewer_config_mutation_denied_by_runtime_rbac",
        ),
        "admin_tenant_config_mutation": (
            [200],
            200,
            "admin_config_cas_mutation_committed",
        ),
        "admin_tenant_config_mutation_confirmed": (
            [200],
            200,
            "admin_config_mutation_visible_on_strong_read",
        ),
        "admin_tenant_config_rollback": (
            [200],
            200,
            "admin_config_cas_rollback_committed",
        ),
        "admin_tenant_config_rollback_confirmed": (
            [200],
            200,
            "admin_config_rollback_visible_on_strong_read",
        ),
        "admin_query_select": (
            [200],
            200,
            "signed_claims_canonical_role_and_query_backend",
        ),
        "viewer_query_select": (
            [200],
            200,
            "signed_claims_canonical_role_and_query_backend",
        ),
        "viewer_query_mutation_denied": (
            [400, 403],
            400,
            "read_only_query_boundary_rejected_mutation",
        ),
        "viewer_payload_role_escalation_denied": (
            [400],
            400,
            "payload_authority_fields_rejected",
        ),
        "wrong_audience_denied": (
            [401, 403],
            401,
            "agentcore_authorizer_rejected_wrong_audience",
        ),
        "missing_tenant_claim_denied": (
            [401, 403],
            401,
            "runtime_rejected_missing_tenant_claim",
        ),
        "missing_project_claim_denied": (
            [401, 403],
            401,
            "runtime_rejected_missing_project_claim",
        ),
        "expired_identity_denied": (
            [401, 403],
            401,
            "agentcore_authorizer_rejected_expired_identity",
        ),
        "issuer_mixup_denied": (
            [401, 403],
            401,
            "agentcore_authorizer_rejected_other_issuer",
        ),
        "tampered_signature_denied": (
            [401, 403],
            401,
            "agentcore_authorizer_rejected_tampered_signature",
        ),
    }
    external_checks = [
        {
            "id": check_id,
            "kind": "agentcore_http",
            "passed": True,
            "expectedStatuses": expected_statuses,
            "statusCode": status_code,
            "latencyMs": 1.0,
            "contentType": "application/json",
            "responseBytes": 2,
            "responseSha256": "e" * 64,
            "transportError": None,
            "observedErrorCode": None,
            "validation": validation,
        }
        for check_id, (
            expected_statuses,
            status_code,
            validation,
        ) in http_check_contracts.items()
    ]
    policy_check_contracts = {
        "canonical_admin_config_read_allowed": (
            "tenant_admin",
            "tenant.config.read",
            True,
            200,
            "role_allowed",
        ),
        "canonical_admin_config_write_allowed": (
            "tenant_admin",
            "tenant.config.write",
            True,
            200,
            "role_allowed",
        ),
        "canonical_viewer_config_read_allowed": (
            "tenant_member",
            "tenant.config.read",
            True,
            200,
            "role_allowed",
        ),
        "canonical_viewer_config_write_denied": (
            "tenant_member",
            "tenant.config.write",
            False,
            403,
            "role_not_allowed",
        ),
        "canonical_admin_query_select_allowed": (
            "tenant_admin",
            "query.select",
            True,
            200,
            "role_allowed",
        ),
        "canonical_viewer_query_select_allowed": (
            "tenant_member",
            "query.select",
            True,
            200,
            "role_allowed",
        ),
        "canonical_cross_tenant_query_concealed": (
            "tenant_member",
            "query.select",
            False,
            404,
            "resource_not_found",
        ),
    }
    external_checks.extend(
        {
            "id": check_id,
            "kind": "canonical_policy",
            "passed": True,
            "role": role,
            "action": action,
            "expectedAllowed": allowed,
            "allowed": allowed,
            "expectedStatus": status,
            "statusCode": status,
            "reason": reason,
            "validation": "server_held_role_policy_decision",
        }
        for check_id, (
            role,
            action,
            allowed,
            status,
            reason,
        ) in policy_check_contracts.items()
    )

    external_oidc_report = {
        "schema": deployment_evidence.EXTERNAL_OIDC_CERTIFICATION_SCHEMA,
        "generatedAt": "2026-08-11T12:04:00+00:00",
        "overallStatus": "PASS",
        "producer": {
            "path": ("scripts/operations/certify_external_oidc_agentcore.py"),
            "sha256": "d" * 64,
            "mode": "live-probe-only",
        },
        "source": {
            "repository": "owner/repo",
            "workflowRef": ("owner/repo/.github/workflows/certify-agentcore-external-oidc.yml@refs/heads/main"),
            "parentWorkflowRef": ("owner/repo/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
            "runId": "99",
            "runAttempt": "1",
            "workflowCommit": "1" * 40,
            "parentWorkflowCommit": "1" * 40,
            "releaseCommit": "1" * 40,
            "agentcoreImage": AGENTCORE_IMAGE,
            "runtimeStackName": "AxonLLMAgentCoreStack-external",
        },
        "target": {
            "region": REGION,
            "stackName": "AxonLLMAgentCoreStack-external",
            "stackId": (f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/AxonLLMAgentCoreStack-external/1234"),
            "stackStatus": "UPDATE_COMPLETE",
            "runtimeArn": RUNTIME_ARN,
            "runtimeVersion": "7",
            "endpointName": CANDIDATE_ENDPOINT_NAME,
            "endpointArn": CANDIDATE_ENDPOINT_ARN,
            "endpointStatus": "READY",
            "image": AGENTCORE_IMAGE,
        },
        "oidc": {
            "identityMode": "external-oidc",
            "clientId": "external-client",
            "audience": "api://axonllm",
            "tenantClaim": "https://axonllm.example/tenant",
            "projectClaim": "https://axonllm.example/project",
            "expected": issuer_evidence(
                "https://idp.example.com/oauth2/default",
                "1",
            ),
            "mixup": issuer_evidence(
                "https://mixup.example.net/oauth2/default",
                "2",
            ),
        },
        "fixtures": {
            "fixtureIdSha256": "3" * 64,
            "challengeSha256": "4" * 64,
            "brokerResponseSha256": "5" * 64,
            "expiresAt": "2026-08-11T12:14:00+00:00",
            "canonicalPrincipalCount": 5,
            "datasourceId": "external-oidc-launch",
            "cleanup": {
                "status": "PASS",
                "complete": True,
                "localItemsRemoved": 6,
                "broker": {
                    "status": "PASS",
                    "complete": True,
                    "identitiesRevoked": True,
                    "responseSha256": "6" * 64,
                },
            },
        },
        "fullLaunchCertification": {
            "schema": deployment_evidence.CERTIFICATION_SCHEMA,
            "generatedAt": "2026-08-11T12:03:00+00:00",
            "overallStatus": "PASS",
            "endpoint": {
                "runtimeArn": RUNTIME_ARN,
                "endpointArn": CANDIDATE_ENDPOINT_ARN,
                "endpointName": CANDIDATE_ENDPOINT_NAME,
                "status": "READY",
                "runtimeVersion": "7",
                "invocationUrl": (
                    f"https://bedrock-agentcore.{REGION}.amazonaws.com/"
                    f"runtimes/{quote(RUNTIME_ARN, safe='')}/invocations"
                    f"?qualifier={CANDIDATE_ENDPOINT_NAME}"
                ),
            },
            "summary": {
                "profile": "production-launch",
                "providerCount": len(provider_features),
                "providerFeatures": {
                    provider_name: sorted(features) for provider_name, features in provider_features.items()
                },
                "agentcoreHttpsInvoked": True,
                "queryBackendExercised": True,
                "checkCount": len(external_full_checks),
                "passed": len(external_full_checks),
                "failed": 0,
            },
            "checks": external_full_checks,
        },
        "checks": external_checks,
        "summary": {
            "checkCount": len(deployment_evidence.REQUIRED_EXTERNAL_OIDC_CHECKS),
            "passed": len(deployment_evidence.REQUIRED_EXTERNAL_OIDC_CHECKS),
            "failed": 0,
            "expectedIssuerVerified": True,
            "mixupIssuerVerifiedAndRejected": True,
            "freshJwksVerified": True,
            "shortLivedIdentitiesVerified": True,
            "canonicalTenantRbacVerified": True,
            "agentcoreHttpsInvoked": True,
            "queryBackendExercised": True,
            "allLaunchProvidersExercised": True,
            "agentcoreTenantConfigMutationExercised": True,
            "fixturesCleaned": True,
        },
    }
    external_oidc_report_path = _write(
        tmp_path / "external-oidc-agentcore-report.json",
        external_oidc_report,
    )
    external_oidc_signature_path = _write(
        tmp_path / "external-oidc-agentcore-signature.json",
        {
            "schema": "axonllm.kms-signature/v1",
            "signature": "not-secret-test-fixture",
        },
    )
    revoked_payload_sha256 = hashlib.sha256(b'{"token":"revoked","expiresAtEpoch":0}\n').hexdigest()
    qualification_teardown_receipt_path = _write(
        tmp_path / "qualification-teardown-receipt.json",
        {
            "schema": (deployment_evidence.QUALIFICATION_TEARDOWN_SCHEMA),
            "generatedAt": "2026-08-11T12:05:00+00:00",
            "source": {
                "repository": "owner/repo",
                "workflowRef": ("owner/repo/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
                "workflowCommit": "1" * 40,
                "releaseCommit": "1" * 40,
                "runId": "99",
                "runAttempt": "1",
            },
            "accountId": ACCOUNT,
            "region": REGION,
            "namespace": "managed",
            "runtimeIdentity": {
                "secretArnSha256": "7" * 64,
                "versionId": "version-" + "8" * 32,
                "currentStageVerified": True,
                "revokedPayloadSha256": revoked_payload_sha256,
            },
            "fixtures": {
                "controlPlaneStatePresent": True,
                "certificationStatePresent": True,
                "stateAbsentAfterCleanup": True,
            },
            "workers": [
                {
                    "serviceName": ("axonllm-launch-cleanup-worker-managed"),
                    "desiredCount": 0,
                    "runningCount": 0,
                    "pendingCount": 0,
                },
                {
                    "serviceName": ("axonllm-launch-action-worker-managed"),
                    "desiredCount": 0,
                    "runningCount": 0,
                    "pendingCount": 0,
                },
            ],
            "stacks": [
                {
                    "name": "AxonLLMIdentityStack-managed",
                    "absent": True,
                },
                {
                    "name": "AxonLLMAgentCoreStack-managed",
                    "absent": True,
                },
                {
                    "name": "AxonLLMControlPlaneStack-managed",
                    "absent": True,
                },
                {
                    "name": "AxonLLMLaunchWorkersStack-managed",
                    "absent": True,
                },
            ],
        },
    )
    qualification_teardown_signature_path = _write(
        tmp_path / "qualification-teardown-signature.json",
        {
            "schema": "axonllm.kms-signature/v1",
            "signature": "not-secret-test-fixture",
        },
    )

    return argparse.Namespace(
        output=tmp_path / "deployment.json",
        repository="owner/repo",
        deployment_commit="1" * 40,
        release_commit="1" * 40,
        release_run_id="41",
        release_manifest=_write(tmp_path / "release.json", release),
        workflow_ref=("owner/repo/.github/workflows/deploy-agentcore-production.yml@refs/heads/main"),
        workflow_commit="1" * 40,
        parent_workflow_ref=("owner/repo/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
        parent_workflow_commit="1" * 40,
        run_id="99",
        run_attempt="1",
        actor="operator",
        actor_id="123",
        triggering_actor="operator",
        change_id="CHG-123",
        operation="deploy",
        region=REGION,
        agentcore_image=AGENTCORE_IMAGE,
        fargate_image=FARGATE_IMAGE,
        setup_config=_write(tmp_path / "setup.json", setup),
        certification_config=_write(
            tmp_path / "certification-config.json",
            {
                "runtimeArn": RUNTIME_ARN,
                "qualifier": CANDIDATE_ENDPOINT_NAME,
                "providers": [
                    {
                        "provider": provider_name,
                        "model": f"{provider_name}-certification",
                        "features": sorted(features),
                    }
                    for provider_name, features in provider_features.items()
                ],
            },
        ),
        production_validation_config=_write(
            tmp_path / "production-validation-config.json",
            {
                "schemaVersion": 1,
                "target": "fargate",
            },
        ),
        identity_outputs=_write(tmp_path / "identity.json", identity),
        runtime_outputs=_write(tmp_path / "runtime.json", runtime),
        control_outputs=_write(tmp_path / "control.json", control),
        provider_secret=_write(tmp_path / "provider.json", provider),
        recovery_report=_write(tmp_path / "recovery.json", recovery),
        transition_report=_write(tmp_path / "transition.json", transition),
        certification_report=_write(
            tmp_path / "certification.json",
            certification,
        ),
        production_certification_report=_write(
            tmp_path / "production-certification.json",
            production_certification,
        ),
        production_validation_report=_write(
            tmp_path / "production-validation.json",
            production_validation,
        ),
        launch_rehearsal_report=launch_rehearsal_report,
        launch_rehearsal_signature=launch_rehearsal_signature,
        evidence_bucket=EVIDENCE_BUCKET,
        evidence_prefix=EVIDENCE_PREFIX,
        launch_rehearsal_report_uri=REHEARSAL_REPORT_URI,
        launch_rehearsal_report_version_id="report-version-1",
        launch_rehearsal_report_sha256=hashlib.sha256(launch_rehearsal_report.read_bytes()).hexdigest(),
        launch_rehearsal_signature_uri=REHEARSAL_SIGNATURE_URI,
        launch_rehearsal_signature_version_id="signature-version-1",
        launch_rehearsal_signature_sha256=hashlib.sha256(launch_rehearsal_signature.read_bytes()).hexdigest(),
        external_oidc_certification_report=external_oidc_report_path,
        external_oidc_certification_signature=(external_oidc_signature_path),
        external_oidc_evidence_prefix=EXTERNAL_OIDC_EVIDENCE_PREFIX,
        external_oidc_report_uri=EXTERNAL_OIDC_REPORT_URI,
        external_oidc_report_version_id="external-report-version-1",
        external_oidc_report_sha256=hashlib.sha256(external_oidc_report_path.read_bytes()).hexdigest(),
        external_oidc_signature_uri=EXTERNAL_OIDC_SIGNATURE_URI,
        external_oidc_signature_version_id=("external-signature-version-1"),
        external_oidc_signature_sha256=hashlib.sha256(external_oidc_signature_path.read_bytes()).hexdigest(),
        qualification_teardown_receipt=(qualification_teardown_receipt_path),
        qualification_teardown_signature=(qualification_teardown_signature_path),
        qualification_teardown_receipt_uri=(QUALIFICATION_TEARDOWN_RECEIPT_URI),
        qualification_teardown_receipt_version_id=("qualification-teardown-receipt-version-1"),
        qualification_teardown_receipt_sha256=hashlib.sha256(
            qualification_teardown_receipt_path.read_bytes()
        ).hexdigest(),
        qualification_teardown_signature_uri=(QUALIFICATION_TEARDOWN_SIGNATURE_URI),
        qualification_teardown_signature_version_id=("qualification-teardown-signature-version-1"),
        qualification_teardown_signature_sha256=hashlib.sha256(
            qualification_teardown_signature_path.read_bytes()
        ).hexdigest(),
    )


def _use_cloudfront_endpoint(args: argparse.Namespace) -> None:
    domain = "d111111abcdef8.cloudfront.net"
    setup = json.loads(args.setup_config.read_text(encoding="utf-8"))
    setup["control_plane"]["endpoint_mode"] = "cloudfront"
    setup["control_plane"].pop("domain_name")
    _write(args.setup_config, setup)

    identity = json.loads(
        args.identity_outputs.read_text(encoding="utf-8")
    )
    identity_outputs = identity[deployment_evidence.IDENTITY_STACK]
    identity_outputs["EndpointMode"] = "cloudfront"
    identity_outputs["AlbClientId"] = ""
    identity_outputs.pop("AlbClientSecretArn")
    identity_outputs["ControlPlaneDomainName"] = ""
    _write(args.identity_outputs, identity)

    control = json.loads(
        args.control_outputs.read_text(encoding="utf-8")
    )
    control_outputs = control[deployment_evidence.CONTROL_PLANE_STACK]
    control_outputs.update(
        {
            "EndpointMode": "cloudfront",
            "ControlPlaneUrl": f"https://{domain}",
            "ControlPlaneDomainName": domain,
            "ControlPlaneAuthMode": "application-oidc",
            "BrowserClientId": "browser-client-id",
        }
    )
    _write(args.control_outputs, control)

    report = json.loads(
        args.production_validation_report.read_text(encoding="utf-8")
    )
    report["httpEndpoints"] = [f"https://{domain}"]
    for result in report["canaries"]["results"]:
        result["baseUrl"] = f"https://{domain}"
        result["credentialType"] = "browser-session-cookie"
    report["load"]["credentialType"] = "browser-session-cookie"
    _write(args.production_validation_report, report)


def _verify_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )


def test_create_evidence_binds_release_runtime_secret_and_canaries(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)

    evidence = deployment_evidence.create_evidence(args)
    deployment_evidence._atomic_write(args.output, evidence)

    assert evidence["deployment"]["commit"] == "1" * 40
    assert evidence["release"]["commit"] == "1" * 40
    assert evidence["images"]["agentcore"]["digest"] == AGENTCORE_DIGEST
    assert evidence["stacks"]["runtime"]["RuntimeVersion"] == "7"
    assert evidence["providerSecret"]["versionId"] == "version-2"
    assert evidence["recovery"]["transition"]["mode"] == "normal"
    assert evidence["recovery"]["validation"]["restoreExercise"]["sampledItemCount"] == 25
    assert evidence["certification"]["overallStatus"] == "PASS"
    assert evidence["certification"]["endpoint"]["endpointName"] == CANDIDATE_ENDPOINT_NAME
    assert evidence["productionCertification"]["overallStatus"] == "PASS"
    assert evidence["productionCertification"]["endpoint"]["endpointName"] == "production"
    assert evidence["productionValidation"]["overallStatus"] == "PASS"
    assert evidence["stacks"]["controlPlane"]["EndpointMode"] == (
        "custom-domain"
    )
    assert evidence["productionValidation"]["httpEndpoints"] == [
        "https://axon.example.com"
    ]
    assert evidence["launchRehearsal"]["gates"]["securityEventDeliveryAndDlq"]["environment"] == "production"
    assert set(evidence["launchRehearsal"]["gates"]) == (deployment_evidence.REQUIRED_REHEARSAL_GATES)
    assert evidence["launchRehearsalSource"]["artifact"] == {
        "s3Uri": REHEARSAL_REPORT_URI,
        "versionId": "report-version-1",
        "sha256": args.launch_rehearsal_report_sha256,
    }
    assert evidence["externalOidcCertification"]["overallStatus"] == "PASS"
    assert evidence["externalOidcCertificationSource"]["artifact"] == {
        "s3Uri": EXTERNAL_OIDC_REPORT_URI,
        "versionId": "external-report-version-1",
        "sha256": args.external_oidc_report_sha256,
    }
    assert evidence["qualificationTeardown"]["fixtures"] == {
        "controlPlaneStatePresent": True,
        "certificationStatePresent": True,
        "stateAbsentAfterCleanup": True,
    }
    assert evidence["qualificationTeardownSource"]["artifact"] == {
        "s3Uri": QUALIFICATION_TEARDOWN_RECEIPT_URI,
        "versionId": "qualification-teardown-receipt-version-1",
        "sha256": args.qualification_teardown_receipt_sha256,
    }
    assert len(evidence["configuration"]["productionValidationConfigSha256"]) == 64
    assert len(evidence["configuration"]["launchRehearsalSignatureSha256"]) == 64
    assert "AlbClientSecretArn" in evidence["stacks"]["identity"]

    serialized = args.output.read_text(encoding="utf-8")
    assert "actual-provider-secret" not in serialized
    assert stat_mode(args.output) == 0o600


def test_create_and_verify_bind_cloudfront_endpoint_mode(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    _use_cloudfront_endpoint(args)

    evidence = deployment_evidence.create_evidence(args)
    deployment_evidence._atomic_write(args.output, evidence)
    verified = deployment_evidence.verify_evidence(_verify_args(args))

    assert verified["stacks"]["controlPlane"][
        "ControlPlaneAuthMode"
    ] == "application-oidc"
    assert verified["productionValidation"]["httpEndpoints"] == [
        "https://d111111abcdef8.cloudfront.net"
    ]
    assert {
        result["credentialType"]
        for result in verified["productionValidation"]["canaries"][
            "results"
        ]
    } == {"browser-session-cookie"}


def test_create_defaults_omitted_setup_endpoint_mode_to_custom_domain(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    setup = json.loads(args.setup_config.read_text(encoding="utf-8"))
    assert "endpoint_mode" not in setup["control_plane"]

    evidence = deployment_evidence.create_evidence(args)

    assert evidence["stacks"]["controlPlane"]["EndpointMode"] == (
        "custom-domain"
    )


def test_create_rejects_setup_and_control_endpoint_mode_mismatch(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    setup = json.loads(args.setup_config.read_text(encoding="utf-8"))
    setup["control_plane"]["endpoint_mode"] = "cloudfront"
    setup["control_plane"].pop("domain_name")
    _write(args.setup_config, setup)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="control-plane endpoint",
    ):
        deployment_evidence.create_evidence(args)


def test_verify_rejects_tampered_endpoint_auth_binding(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    evidence = deployment_evidence.create_evidence(args)
    evidence["stacks"]["controlPlane"][
        "ControlPlaneAuthMode"
    ] = "application-oidc"
    deployment_evidence._atomic_write(args.output, evidence)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="endpoint outputs are inconsistent",
    ):
        deployment_evidence.verify_evidence(_verify_args(args))


@pytest.mark.parametrize("include_ai21", [False, True])
def test_create_evidence_accepts_canonical_provider_selections(
    tmp_path: Path,
    include_ai21: bool,
) -> None:
    args = _fixtures(tmp_path, include_ai21=include_ai21)

    evidence = deployment_evidence.create_evidence(args)

    expected_providers = set(deployment_evidence.PRODUCTION_LAUNCH_PROVIDERS)
    if include_ai21:
        expected_providers.add("ai21")
    provider_features = evidence["certification"]["summary"]["providerFeatures"]
    assert set(provider_features) == expected_providers
    assert provider_features["fireworks"] == ["completion", "stream"]
    assert (
        set(evidence["externalOidcCertification"]["fullLaunchCertification"]["summary"]["providerFeatures"])
        == expected_providers
    )


def _commit_args(
    args: argparse.Namespace,
    signature: Path,
    commit_record: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        evidence=args.output,
        evidence_signature=signature,
        commit_record=commit_record,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )


def test_commit_record_binds_exact_evidence_pair_and_candidate(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    deployment_evidence._atomic_write(
        args.output,
        deployment_evidence.create_evidence(args),
    )
    signature = _write(
        tmp_path / "agentcore-deployment-kms-signature.json",
        {"fixture": "signature"},
    )
    commit_record = tmp_path / "agentcore-deployment-commit.json"
    commit_args = _commit_args(args, signature, commit_record)

    value = deployment_evidence.create_commit_record(commit_args)
    deployment_evidence._atomic_write(commit_record, value)

    assert value["schema"] == deployment_evidence.COMMIT_SCHEMA
    assert value["deployment"] == {
        "repository": args.repository,
        "commit": args.deployment_commit,
        "runId": args.run_id,
        "runAttempt": args.run_attempt,
    }
    assert deployment_evidence.verify_commit_record(commit_args) == value


@pytest.mark.parametrize("tampered", ["evidence", "signature", "identity"])
def test_commit_record_rejects_artifact_or_candidate_substitution(
    tmp_path: Path,
    tampered: str,
) -> None:
    args = _fixtures(tmp_path)
    deployment_evidence._atomic_write(
        args.output,
        deployment_evidence.create_evidence(args),
    )
    signature = _write(
        tmp_path / "agentcore-deployment-kms-signature.json",
        {"fixture": "signature"},
    )
    commit_record = tmp_path / "agentcore-deployment-commit.json"
    commit_args = _commit_args(args, signature, commit_record)
    deployment_evidence._atomic_write(
        commit_record,
        deployment_evidence.create_commit_record(commit_args),
    )

    if tampered == "evidence":
        args.output.write_bytes(args.output.read_bytes() + b" ")
    elif tampered == "signature":
        _write(signature, {"fixture": "different-signature"})
    else:
        value = json.loads(commit_record.read_text(encoding="utf-8"))
        value["deployment"]["runAttempt"] = "2"
        _write(commit_record, value)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="commit record",
    ):
        deployment_evidence.verify_commit_record(commit_args)


def test_external_oidc_fixture_satisfies_published_evidence_contract(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.external_oidc_certification_report.read_text(encoding="utf-8"))

    validated = external_oidc.validate_published_report(
        report,
        repository=args.repository,
        release_commit=args.release_commit,
        agentcore_image=args.agentcore_image,
        region=args.region,
        runtime_stack_name=deployment_evidence.EXTERNAL_OIDC_STACK,
        clock=lambda: datetime(
            2026,
            8,
            11,
            12,
            5,
            tzinfo=timezone.utc,
        ).timestamp(),
    )

    assert validated == report


@pytest.mark.parametrize("mode", ["GOVERNANCE", "COMPLIANCE"])
def test_create_evidence_accepts_supported_vault_lock_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    args = _fixtures(tmp_path)
    recovery = json.loads(args.recovery_report.read_text(encoding="utf-8"))
    recovery["backupVaultLockMode"] = mode
    _write(args.recovery_report, recovery)

    evidence = deployment_evidence.create_evidence(args)

    assert evidence["recovery"]["validation"]["backupVaultLockMode"] == mode


def test_create_evidence_rejects_missing_vault_lock_state(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    recovery = json.loads(args.recovery_report.read_text(encoding="utf-8"))
    recovery.pop("backupVaultLocked")
    _write(args.recovery_report, recovery)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="recovery validation",
    ):
        deployment_evidence.create_evidence(args)


def test_create_evidence_requires_completed_deployment_backup(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    recovery = json.loads(args.recovery_report.read_text(encoding="utf-8"))
    recovery.pop("deploymentBackup")
    _write(args.recovery_report, recovery)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="deployment backup",
    ):
        deployment_evidence.create_evidence(args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sampledItemCount", 0),
        ("sampledItemCount", 26),
        ("sampledItemsSha256", ""),
        ("sampledItemsSha256", "not-a-sha256"),
        ("status", "started"),
        ("retained", True),
    ],
)
def test_create_evidence_requires_validated_restore_sample(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    args = _fixtures(tmp_path)
    recovery = json.loads(args.recovery_report.read_text(encoding="utf-8"))
    recovery["restoreExercise"][field] = value
    _write(args.recovery_report, recovery)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="restore exercise",
    ):
        deployment_evidence.create_evidence(args)


def test_create_evidence_requires_restore_exercise(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    recovery = json.loads(args.recovery_report.read_text(encoding="utf-8"))
    recovery["restoreExercise"] = None
    _write(args.recovery_report, recovery)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="restore exercise",
    ):
        deployment_evidence.create_evidence(args)


def test_create_evidence_rejects_invalid_vault_lock_mode(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    recovery = json.loads(args.recovery_report.read_text(encoding="utf-8"))
    recovery["backupVaultLockMode"] = "UNLOCKED"
    _write(args.recovery_report, recovery)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="recovery validation",
    ):
        deployment_evidence.create_evidence(args)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_verify_rejects_a_different_expected_image(tmp_path: Path) -> None:
    args = _fixtures(tmp_path)
    deployment_evidence._atomic_write(
        args.output,
        deployment_evidence.create_evidence(args),
    )
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=AGENTCORE_IMAGE.replace("a" * 64, "d" * 64),
        fargate_image=FARGATE_IMAGE,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="images do not match",
    ):
        deployment_evidence.verify_evidence(verify)


def test_create_rejects_failed_certification(tmp_path: Path) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.certification_report.read_text(encoding="utf-8"))
    report["overallStatus"] = "FAIL"
    _write(args.certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="certification",
    ):
        deployment_evidence.create_evidence(args)


def test_create_rejects_certification_of_production_instead_of_candidate(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.certification_report.read_text(encoding="utf-8"))
    report["endpoint"]["endpointName"] = "production"
    report["endpoint"]["endpointArn"] = ENDPOINT_ARN
    _write(args.certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="certification",
    ):
        deployment_evidence.create_evidence(args)


def test_create_rejects_failed_production_certification(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_certification_report.read_text(encoding="utf-8"))
    report["overallStatus"] = "FAIL"
    _write(args.production_certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="production certification",
    ):
        deployment_evidence.create_evidence(args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtimeArn", RUNTIME_ARN.replace("1234567890", "abcdefghij")),
        ("endpointArn", CANDIDATE_ENDPOINT_ARN),
        ("endpointName", CANDIDATE_ENDPOINT_NAME),
        ("runtimeVersion", "6"),
    ],
)
def test_create_rejects_unbound_production_certification(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_certification_report.read_text(encoding="utf-8"))
    report["endpoint"][field] = value
    _write(args.production_certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="production certification",
    ):
        deployment_evidence.create_evidence(args)


@pytest.mark.parametrize(
    ("category", "provider"),
    [
        ("query_select", None),
        ("provider_stream", "openai"),
    ],
)
def test_create_requires_complete_production_certification(
    tmp_path: Path,
    category: str,
    provider: str | None,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_certification_report.read_text(encoding="utf-8"))
    report["checks"] = [
        check
        for check in report["checks"]
        if not (check.get("category") == category and check.get("provider") == provider)
    ]
    report["summary"]["checkCount"] -= 1
    report["summary"]["passed"] -= 1
    _write(args.production_certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="every launch contract and enabled provider",
    ):
        deployment_evidence.create_evidence(args)


def test_create_requires_exact_production_provider_feature_matrix(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    runtime = json.loads(args.runtime_outputs.read_text(encoding="utf-8"))
    runtime[deployment_evidence.AGENTCORE_STACK]["EnabledProviders"] = "openai"
    _write(args.runtime_outputs, runtime)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="production launch contract",
    ):
        deployment_evidence.create_evidence(args)

    feature_path = tmp_path / "feature"
    feature_path.mkdir()
    args = _fixtures(feature_path)
    report = json.loads(args.production_certification_report.read_text(encoding="utf-8"))
    report["summary"]["providerFeatures"]["openai"].remove("tool_calling")
    _write(args.production_certification_report, report)
    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="provider feature matrix",
    ):
        deployment_evidence.create_evidence(args)


@pytest.mark.parametrize(
    "source",
    ["setup", "certification", "external-oidc"],
)
def test_create_requires_every_provider_source_to_match_runtime(
    tmp_path: Path,
    source: str,
) -> None:
    args = _fixtures(tmp_path, include_ai21=True)
    if source == "setup":
        setup = json.loads(args.setup_config.read_text(encoding="utf-8"))
        setup["runtime"]["enabled_providers"].remove("ai21")
        _write(args.setup_config, setup)
        expected = "setup configuration"
    elif source == "certification":
        config = json.loads(args.certification_config.read_text(encoding="utf-8"))
        config["providers"] = [case for case in config["providers"] if case["provider"] != "ai21"]
        _write(args.certification_config, config)
        expected = "certification configuration"
    else:
        report = json.loads(args.external_oidc_certification_report.read_text(encoding="utf-8"))
        full = report["fullLaunchCertification"]
        full["summary"]["providerFeatures"].pop("ai21")
        full["summary"]["providerCount"] -= 1
        removed = [check for check in full["checks"] if check.get("provider") == "ai21"]
        full["checks"] = [check for check in full["checks"] if check.get("provider") != "ai21"]
        full["summary"]["checkCount"] -= len(removed)
        full["summary"]["passed"] -= len(removed)
        _write(args.external_oidc_certification_report, report)
        expected = "provider feature matrix"

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match=expected,
    ):
        deployment_evidence.create_evidence(args)


def test_create_redacts_production_certification(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_certification_report.read_text(encoding="utf-8"))
    report["privateFixture"] = "must-not-persist"
    report["endpoint"]["invocationUrl"] = "must-not-persist"
    report["checks"][0]["responseBody"] = "must-not-persist"
    _write(args.production_certification_report, report)

    evidence = deployment_evidence.create_evidence(args)

    serialized = json.dumps(evidence["productionCertification"])
    assert "must-not-persist" not in serialized


def test_verify_requires_redacted_production_certification_schema(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    evidence = deployment_evidence.create_evidence(args)
    evidence["productionCertification"]["privateFixture"] = "not-redacted"
    deployment_evidence._atomic_write(args.output, evidence)
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="not redacted",
    ):
        deployment_evidence.verify_evidence(verify)


def test_create_cli_requires_production_certification_report() -> None:
    create_parser = next(
        action for action in deployment_evidence._parser()._actions if action.dest == "command"
    ).choices["create"]
    action = next(action for action in create_parser._actions if action.dest == "production_certification_report")

    assert action.required is True


@pytest.mark.parametrize(
    "argument",
    [
        "production_certification_report",
        "production_validation_config",
        "production_validation_report",
        "launch_rehearsal_report",
        "launch_rehearsal_signature",
        "evidence_bucket",
        "evidence_prefix",
        "launch_rehearsal_report_uri",
        "launch_rehearsal_report_version_id",
        "launch_rehearsal_report_sha256",
        "launch_rehearsal_signature_uri",
        "launch_rehearsal_signature_version_id",
        "launch_rehearsal_signature_sha256",
        "external_oidc_certification_report",
        "external_oidc_certification_signature",
        "external_oidc_evidence_prefix",
        "external_oidc_report_uri",
        "external_oidc_report_version_id",
        "external_oidc_report_sha256",
        "external_oidc_signature_uri",
        "external_oidc_signature_version_id",
        "external_oidc_signature_sha256",
    ],
)
def test_create_cli_requires_post_promotion_launch_evidence(
    argument: str,
) -> None:
    create_parser = next(
        action for action in deployment_evidence._parser()._actions if action.dest == "command"
    ).choices["create"]
    action = next(action for action in create_parser._actions if action.dest == argument)

    assert action.required is True


def test_create_rejects_failed_production_validation(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["overallStatus"] = "FAIL"
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="control-plane RBAC and load",
    ):
        deployment_evidence.create_evidence(args)


def test_create_requires_every_control_plane_launch_gate(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["launchGates"]["scenarios"].pop("cross_tenant_denied")
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="control-plane RBAC and load",
    ):
        deployment_evidence.create_evidence(args)


def test_create_requires_tenant_admin_mutation_round_trip(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["canaries"]["configuredCategories"].remove("tenant_admin_mutation_round_trip")
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="control-plane RBAC and load",
    ):
        deployment_evidence.create_evidence(args)


def test_create_requires_two_stable_backing_target_identities(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["targetHealth"]["postLoad"]["targetIdSha256"] = [
        "1" * 64,
        "5" * 64,
    ]
    _rebind_target_health(report)
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="stable backing instances",
    ):
        deployment_evidence.create_evidence(args)


def test_create_requires_target_health_to_bracket_load(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["targetHealth"]["loadInterval"]["startedAt"] = "2026-08-11T12:01:20+00:00"
    _rebind_target_health(report)
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="stable backing instances",
    ):
        deployment_evidence.create_evidence(args)


def test_create_binds_target_health_to_deployed_target_group(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["targetHealth"]["targetGroupArnSha256"] = "9" * 64
    for phase in ("preLoad", "postLoad"):
        report["targetHealth"][phase]["targetGroupArnSha256"] = "9" * 64
    _rebind_target_health(report)
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="target-health observation is invalid",
    ):
        deployment_evidence.create_evidence(args)


def test_create_recomputes_target_health_observation_digest(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.production_validation_report.read_text(encoding="utf-8"))
    report["targetHealth"]["preLoad"]["sourceSha256"] = "8" * 64
    _write(args.production_validation_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="observation digest",
    ):
        deployment_evidence.create_evidence(args)


@pytest.mark.parametrize(
    ("gate", "field", "value"),
    [
        ("initializationTimeoutReplacement", "status", "FAIL"),
        (
            "securityEventDeliveryAndDlq",
            "environment",
            "production-like",
        ),
        ("providerFallbackRecovery", "status", "FAIL"),
    ],
)
def test_create_rejects_failed_launch_rehearsal_gate(
    tmp_path: Path,
    gate: str,
    field: str,
    value: str,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.launch_rehearsal_report.read_text(encoding="utf-8"))
    report["gates"][gate][field] = value
    _write(args.launch_rehearsal_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="gate .* is invalid",
    ):
        deployment_evidence.create_evidence(args)


@pytest.mark.parametrize(
    "gate",
    [
        "queryBoundaryLimitsAndReconciliation",
        "providerRoutingStrategies",
        "controlPlaneFaultRecovery",
    ],
)
def test_create_rejects_missing_launch_rehearsal_gate(
    tmp_path: Path,
    gate: str,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.launch_rehearsal_report.read_text(encoding="utf-8"))
    report["gates"].pop(gate)
    _write(args.launch_rehearsal_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="missing required gates",
    ):
        deployment_evidence.create_evidence(args)


def test_create_binds_fetched_rehearsal_hashes(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    args.launch_rehearsal_report_sha256 = "0" * 64

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="source hashes do not match",
    ):
        deployment_evidence.create_evidence(args)


def test_create_rejects_unversioned_reviewed_config_provenance(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.launch_rehearsal_report.read_text(encoding="utf-8"))
    report["gateExecution"]["reviewedConfigVersionId"] = "null"
    _write(args.launch_rehearsal_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="reviewed config binding is malformed",
    ):
        deployment_evidence.create_evidence(args)


def test_create_rejects_compatibility_projection_location(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    args.launch_rehearsal_report_uri = f"s3://{EVIDENCE_BUCKET}/{EVIDENCE_PREFIX}/agentcore-launch-rehearsal.json"

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="detailed report",
    ):
        deployment_evidence.create_evidence(args)


def test_verify_revalidates_per_gate_signature_binding(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    evidence = deployment_evidence.create_evidence(args)
    evidence["launchRehearsal"]["gates"]["providerFallbackRecovery"]["signature"]["versionId"] = "null"
    deployment_evidence._atomic_write(args.output, evidence)
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="immutable binding",
    ):
        deployment_evidence.verify_evidence(verify)


def test_create_requires_complete_external_oidc_certification(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.external_oidc_certification_report.read_text(encoding="utf-8"))
    report["checks"] = [check for check in report["checks"] if check["id"] != "issuer_mixup_denied"]
    _write(args.external_oidc_certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="checks are incomplete",
    ):
        deployment_evidence.create_evidence(args)


def test_create_binds_external_oidc_release_and_image(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.external_oidc_certification_report.read_text(encoding="utf-8"))
    report["source"]["releaseCommit"] = "9" * 40
    _write(args.external_oidc_certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="not bound to this release",
    ):
        deployment_evidence.create_evidence(args)


def test_create_binds_external_oidc_source_hashes(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    args.external_oidc_signature_sha256 = "0" * 64

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="source hashes do not match",
    ):
        deployment_evidence.create_evidence(args)


def test_create_requires_complete_qualification_teardown(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    receipt = json.loads(args.qualification_teardown_receipt.read_text(encoding="utf-8"))
    receipt["fixtures"]["certificationStatePresent"] = False
    _write(args.qualification_teardown_receipt, receipt)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="fixture cleanup is incomplete",
    ):
        deployment_evidence.create_evidence(args)


def test_standalone_qualification_teardown_verifier(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    verify = argparse.Namespace(
        receipt=args.qualification_teardown_receipt,
        repository=args.repository,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        account_id=ACCOUNT,
        region=REGION,
    )

    receipt = deployment_evidence.verify_qualification_teardown(verify)

    assert receipt["source"]["runId"] == args.run_id
    assert receipt["fixtures"]["stateAbsentAfterCleanup"] is True


def test_create_requires_all_qualification_workers_stopped(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    receipt = json.loads(args.qualification_teardown_receipt.read_text(encoding="utf-8"))
    receipt["workers"][0]["runningCount"] = 1
    _write(args.qualification_teardown_receipt, receipt)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="worker shutdown is incomplete",
    ):
        deployment_evidence.create_evidence(args)


def test_create_binds_qualification_teardown_to_exact_run(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    receipt = json.loads(args.qualification_teardown_receipt.read_text(encoding="utf-8"))
    receipt["source"]["runAttempt"] = "2"
    _write(args.qualification_teardown_receipt, receipt)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="not bound to this deployment",
    ):
        deployment_evidence.create_evidence(args)


def test_create_binds_qualification_teardown_source_hashes(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    args.qualification_teardown_signature_sha256 = "0" * 64

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="teardown source hashes do not match",
    ):
        deployment_evidence.create_evidence(args)


def test_verify_revalidates_embedded_qualification_teardown(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    evidence = deployment_evidence.create_evidence(args)
    evidence["qualificationTeardown"]["stacks"][0]["absent"] = False
    deployment_evidence._atomic_write(args.output, evidence)
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="stack teardown is incomplete",
    ):
        deployment_evidence.verify_evidence(verify)


def test_verify_revalidates_external_oidc_summary(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    evidence = deployment_evidence.create_evidence(args)
    evidence["externalOidcCertification"]["summary"]["allLaunchProvidersExercised"] = False
    deployment_evidence._atomic_write(args.output, evidence)
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="summary is incomplete",
    ):
        deployment_evidence.verify_evidence(verify)


def test_verify_revalidates_embedded_launch_evidence(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    evidence = deployment_evidence.create_evidence(args)
    evidence["productionValidation"]["launchGates"]["scenarios"]["viewer_mutation_denied"]["passed"] = False
    deployment_evidence._atomic_write(args.output, evidence)
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="control-plane RBAC and load",
    ):
        deployment_evidence.verify_evidence(verify)


def test_create_rejects_predictable_candidate_endpoint(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    runtime = json.loads(args.runtime_outputs.read_text(encoding="utf-8"))
    outputs = runtime[deployment_evidence.AGENTCORE_STACK]
    outputs["CandidateRuntimeEndpointName"] = "candidate"
    outputs["CandidateRuntimeEndpointArn"] = RUNTIME_ARN + "/runtime-endpoint/candidate"
    _write(args.runtime_outputs, runtime)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="outputs",
    ):
        deployment_evidence.create_evidence(args)


def test_create_rejects_unbound_certification_configuration(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    config = json.loads(args.certification_config.read_text(encoding="utf-8"))
    config["qualifier"] = "candidate_" + "b" * 32
    _write(args.certification_config, config)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="certification configuration",
    ):
        deployment_evidence.create_evidence(args)


def test_stack_outputs_reject_secret_values_but_allow_secret_metadata(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    identity = json.loads(args.identity_outputs.read_text(encoding="utf-8"))
    identity[deployment_evidence.IDENTITY_STACK]["ApiKey"] = "must-not-persist"
    _write(args.identity_outputs, identity)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="unsafe output",
    ):
        deployment_evidence.create_evidence(args)
