"""Initialization-timeout and runtime-replacement launch domain."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker

from launch_domains.common import LaunchSession, copied_ownership, owned_id


OPERATIONS = framework.DOMAIN_OPERATIONS["initialization"]
CONTROL_GATE = "initializationTimeoutReplacement"
CONTROL_NAME = "startup-delay"


def _failure(code: str) -> worker.DomainTaskFailure:
    return worker.DomainTaskFailure(code, retryable=True)


def _parameters(task: worker.ActionTask) -> Mapping[str, Any]:
    value = task.payload.get("parameters")
    if type(value) is not dict:
        raise worker.HandlerContractError from None
    deadline = value.get("startupDeadlineSeconds")
    if isinstance(deadline, bool) or not isinstance(deadline, int) or not 1 <= deadline <= 300:
        raise worker.HandlerContractError from None
    return value


def _runtime_id(value: Any) -> str:
    if not isinstance(value, str) or worker.SAFE_ID.fullmatch(value) is None or len(value) > 128:
        raise worker.HandlerContractError from None
    return value


def _observed_attempts(session: LaunchSession) -> list[Mapping[str, Any]]:
    observations = session.observations("startup-attempt")
    return [observation.payload for observation in observations if observation.kind == "startup-attempt"]


def _timed_out_runtime(session: LaunchSession) -> str:
    attempts = _observed_attempts(session)
    timed_out = [
        value
        for value in attempts
        if value.get("phase") == "timed-out"
        and value.get("exit_code") == 124
        and value.get("boot_id") == session.binding.correlation_id
    ]
    if len(timed_out) != 1:
        raise _failure("InitializationTimeoutEvidenceUnavailable")
    runtime_id = _runtime_id(timed_out[0].get("runtime_id"))
    if not any(
        value.get("phase") == "started"
        and value.get("boot_id") == session.binding.correlation_id
        and value.get("runtime_id") == runtime_id
        for value in attempts
    ):
        raise _failure("InitializationTimeoutEvidenceUnavailable")
    return runtime_id


def _replacement_runtime(
    session: LaunchSession,
    *,
    timed_out_runtime_id: str,
) -> str:
    ready = [
        value
        for value in _observed_attempts(session)
        if value.get("phase") == "ready"
        and value.get("boot_id") == session.binding.correlation_id
        and value.get("runtime_id") != timed_out_runtime_id
    ]
    if not ready:
        raise _failure("RuntimeReplacementEvidenceUnavailable")
    return _runtime_id(ready[-1].get("runtime_id"))


def _observed_ready_runtime(
    session: LaunchSession,
    runtime_id: str,
) -> bool:
    return any(
        value.get("phase") == "ready"
        and value.get("boot_id") == session.binding.correlation_id
        and value.get("runtime_id") == runtime_id
        for value in _observed_attempts(session)
    )


def _session_state(task: worker.ActionTask, fence_token: int) -> dict[str, Any]:
    binding = task.payload.get("binding")
    release = task.payload.get("release")
    if type(binding) is not dict or type(release) is not dict:
        raise worker.HandlerContractError from None
    commit = release.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise worker.HandlerContractError from None
    return {
        "binding": dict(binding),
        "releaseCommit": commit,
        "controlGate": CONTROL_GATE,
        "fenceToken": fence_token,
    }


def _cleanup_task(
    *,
    owner: framework.OwnerBinding,
    state: Mapping[str, Any],
) -> worker.ActionTask:
    gate = state.get("controlGate")
    fence_token = state.get("fenceToken")
    binding = state.get("binding")
    commit = state.get("releaseCommit")
    if (
        gate != CONTROL_GATE
        or isinstance(fence_token, bool)
        or not isinstance(fence_token, int)
        or fence_token < 1
        or type(binding) is not dict
        or not isinstance(commit, str)
    ):
        raise worker.HandlerContractError from None
    correlation = hashlib.sha256(f"{owner.owner_id}:{gate}:cleanup".encode("ascii")).hexdigest()[:32]
    return worker.ActionTask(
        payload={
            "owner": {"expiresAt": owner.expires_at_text},
            "binding": dict(binding),
            "release": {"commit": commit},
            "parameters": {},
        },
        gate=gate,
        operation="cleanup",
        owner_id=owner.owner_id,
        correlation_id=correlation,
        idempotency_key=hashlib.sha256(f"{owner.owner_id}:{gate}:cleanup".encode("ascii")).hexdigest(),
        expires_at=owner.expires_at,
        fence_token=None,
        request_sha256="0" * 64,
    )


class InitializationDomain:
    """Execute the fenced initialization replacement scenario."""

    def __init__(
        self,
        session_factory: Callable[..., LaunchSession] = LaunchSession,
    ) -> None:
        self._session_factory = session_factory

    def _session(
        self,
        *,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        fence_token: int | None = None,
    ) -> LaunchSession:
        return self._session_factory(
            task=task,
            context=context,
            control_gate=CONTROL_GATE,
            fence_token=fence_token,
        )

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        parameters = _parameters(task)
        next_state = dict(state)
        completed = next_state.get("completed", [])
        if type(completed) is not list or completed != list(OPERATIONS[: len(completed)]):
            raise worker.HandlerContractError from None
        if len(completed) >= len(OPERATIONS) or OPERATIONS[len(completed)] != operation:
            raise worker.DomainTaskFailure("DomainActionOutOfOrder")
        next_ownership = copied_ownership(ownership)

        if operation == "induce-initialization-timeout":
            if context.fence_token is None:
                raise worker.HandlerContractError from None
            session = self._session(task=task, context=context)
            deadline = parameters["startupDeadlineSeconds"]
            session.write_control(
                control_type="fault",
                name=CONTROL_NAME,
                parameters={"delay_seconds": deadline},
                active=True,
            )
            observation = session.invoke(
                {"action": "readiness"},
                operation=operation,
                timeout_seconds=float(deadline + 10),
            )
            if not observation.transport_error:
                raise _failure("InitializationTimeoutNotObserved")
            runtime_id = _timed_out_runtime(session)
            fault_id = owned_id(task, CONTROL_NAME)
            next_ownership["faultIds"] = sorted({*next_ownership["faultIds"], fault_id})
            next_state.update(
                _session_state(task, context.fence_token)
                | {
                    "faultId": fault_id,
                    "startupDeadlineSeconds": deadline,
                    "timedOutRuntimeId": runtime_id,
                    "timeoutExitCode": 124,
                    "controlActive": True,
                }
            )
            evidence = {
                "startupDeadlineSeconds": deadline,
                "timedOutRuntimeId": runtime_id,
            }
        elif operation == "observe-exit-124":
            session = self._session(task=task, context=context)
            if _timed_out_runtime(session) != next_state.get("timedOutRuntimeId"):
                raise _failure("InitializationTimeoutEvidenceUnavailable")
            evidence = {"timeoutExitCode": 124}
        elif operation == "observe-runtime-replacement":
            session = self._session(task=task, context=context)
            observation = session.invoke(
                {"action": "readiness"},
                operation=operation,
            )
            if observation.status_code != 200 or observation.body is None:
                raise _failure("RuntimeReplacementUnavailable")
            replacement = _replacement_runtime(
                session,
                timed_out_runtime_id=_runtime_id(next_state.get("timedOutRuntimeId")),
            )
            session.write_control(
                control_type="fault",
                name=CONTROL_NAME,
                parameters={
                    "delay_seconds": next_state["startupDeadlineSeconds"],
                },
                active=False,
            )
            if session.ledger.read_active_fault(session.binding, CONTROL_NAME) is not None:
                raise _failure("RehearsalControlRemovalUnverified")
            fault_id = next_state.get("faultId")
            next_ownership["faultIds"] = [value for value in next_ownership["faultIds"] if value != fault_id]
            next_state.update(
                {
                    "replacementRuntimeId": replacement,
                    "controlActive": False,
                }
            )
            evidence = {"replacementRuntimeId": replacement}
        else:
            session = self._session(task=task, context=context)
            observation = session.invoke(
                {"action": "readiness"},
                operation=operation,
            )
            if (
                observation.status_code != 200
                or observation.body is None
                or observation.body.get("ready") is not True
                or not _observed_ready_runtime(
                    session,
                    _runtime_id(next_state.get("replacementRuntimeId")),
                )
            ):
                raise _failure("ReplacementReadinessUnavailable")
            evidence = {"replacementReadyStatusCode": 200}

        next_state["completed"] = [*completed, operation]
        return framework.DomainActionResult(
            evidence=evidence,
            state=next_state,
            ownership=next_ownership,
        )

    def cleanup(
        self,
        *,
        owner: framework.OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainCleanupResult:
        next_state = dict(state)
        endpoint_status: str | None = None
        verified_complete = "fenceToken" in next_state
        if "fenceToken" in next_state:
            task = _cleanup_task(owner=owner, state=next_state)
            session = self._session(
                task=task,
                context=context,
                fence_token=next_state["fenceToken"],
            )
            session.write_control(
                control_type="fault",
                name=CONTROL_NAME,
                parameters={
                    "delay_seconds": next_state["startupDeadlineSeconds"],
                },
                active=False,
            )
            if session.ledger.read_active_fault(session.binding, CONTROL_NAME) is not None:
                raise _failure("RehearsalControlRemovalUnverified")
            next_state["controlActive"] = False
            readiness = session.invoke(
                {"action": "readiness"},
                operation="verify-replacement-ready",
            )
            if readiness.status_code != 200 or readiness.body is None or readiness.body.get("ready") is not True:
                raise _failure("ReplacementReadinessUnavailable")
            next_state["cleanupReadyStatusCode"] = readiness.status_code
            endpoint_status = "READY"
        return framework.DomainCleanupResult(
            state=next_state,
            ownership={
                "faultIds": [],
                "fixtureIds": [],
                "dlqCorrelationIds": [],
                "snapshots": {"model": None, "tenantConfig": None},
            },
            verified_complete=verified_complete,
            cleared_fault_ids=list(ownership["faultIds"]),
            cleared_fixture_ids=list(ownership["fixtureIds"]),
            removed_dlq_correlation_ids=list(ownership["dlqCorrelationIds"]),
            production_endpoint_status=endpoint_status,
        )


def create_domain(
    *,
    aws: worker.AwsTransport,
    region: str,
    lease_table_arn: str,
) -> InitializationDomain:
    del aws, region, lease_table_arn
    return InitializationDomain()
