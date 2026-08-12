"""Security and failure-semantics tests for the rehearsal-control ledger."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from src.gateway.rehearsal_control import (
    CHECKPOINTS,
    FAULTS,
    MAX_OBSERVATIONS,
    OBSERVATIONS,
    TABLE_ENV,
    TTL_ATTRIBUTE,
    RehearsalBinding,
    RehearsalControlLedger,
    RehearsalEvidenceUnavailable,
    parse_rehearsal_correlation_id,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
CORRELATION_ID = "a" * 32
OWNER_ID = "b" * 64
RELEASE_COMMIT = "c" * 40


class _AwsError(RuntimeError):
    def __init__(self, code: str, message: str = "suppressed-secret") -> None:
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(message)


class _FakeTable:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.get_error: Exception | None = None
        self.put_error: Exception | None = None
        self.before_put: Callable[["_FakeTable", dict[str, Any], str], None] | None = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(copy.deepcopy(kwargs))
        assert kwargs["ConsistentRead"] is True
        if self.get_error is not None:
            raise self.get_error
        key = kwargs["Key"]["ledger_key"]
        item = self.rows.get(key)
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(copy.deepcopy(kwargs))
        if self.put_error is not None:
            raise self.put_error
        item = kwargs["Item"]
        key = item["ledger_key"]
        hook = self.before_put
        self.before_put = None
        if hook is not None:
            hook(self, kwargs, key)
        current = self.rows.get(key)
        condition = kwargs["ConditionExpression"]
        if condition == "attribute_not_exists(#ledger_key)":
            if current is not None:
                raise _AwsError("ConditionalCheckFailedException")
        else:
            if current is None:
                raise _AwsError("ConditionalCheckFailedException")
            names = kwargs["ExpressionAttributeNames"]
            values = kwargs["ExpressionAttributeValues"]
            for token, field in names.items():
                expected_token = f":{token[1:]}"
                if current.get(field) != values[expected_token]:
                    raise _AwsError("ConditionalCheckFailedException")
        self.rows[key] = copy.deepcopy(item)
        return {}


@dataclass
class _Clock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _binding(
    *,
    clock: _Clock,
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
    fence_token: int = 7,
    ttl_seconds: int = 1800,
) -> RehearsalBinding:
    return RehearsalBinding(
        tenant_id=tenant_id,
        project_id=project_id,
        correlation_id=CORRELATION_ID,
        owner_id=OWNER_ID,
        release_commit=RELEASE_COMMIT,
        fence_token=fence_token,
        expires_at_epoch=int(clock().timestamp()) + ttl_seconds,
    )


def _ledger(
    table: _FakeTable,
    clock: _Clock,
    *,
    enabled: bool = True,
) -> RehearsalControlLedger:
    environment = {TABLE_ENV: "rehearsal-table"} if enabled else {}
    return RehearsalControlLedger(
        table=table,
        environ=environment,
        now=clock,
    )


def _routing_payload(request_id: str = "request-1") -> dict[str, Any]:
    return {
        "candidate_count": 2,
        "provider": "bedrock",
        "request_id": request_id,
        "strategy": "weighted",
    }


def _only_row(table: _FakeTable) -> dict[str, Any]:
    assert len(table.rows) == 1
    return next(iter(table.rows.values()))


def _assert_evidence_unavailable(
    ledger: RehearsalControlLedger,
    binding: RehearsalBinding,
) -> None:
    with pytest.raises(
        RehearsalEvidenceUnavailable,
        match=r"^rehearsal evidence is unavailable$",
    ) as raised:
        ledger.collect_observations(binding)
    assert raised.value.__cause__ is None


def test_unset_environment_is_inert_but_evidence_fails_closed() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock, enabled=False)

    assert ledger.enabled is False
    assert ledger.claim(binding) is None
    assert (
        ledger.write_control(
            binding,
            control_type="fault",
            name="startup-delay",
            parameters={"delay_seconds": 30},
            active=True,
            expected_revision=1,
        )
        is None
    )
    assert ledger.read_active_fault(binding, "startup-delay") is None
    assert (
        ledger.read_active_checkpoint(
            binding,
            "startup-before-ready",
        )
        is None
    )
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            _routing_payload(),
        )
        is False
    )
    _assert_evidence_unavailable(ledger, binding)
    assert table.get_calls == []
    assert table.put_calls == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "g" * 32,
        "a" * 31 + " ",
        123,
    ],
)
def test_request_correlation_parser_rejects_noncanonical_values(
    value: object,
) -> None:
    assert parse_rehearsal_correlation_id(value) is None


def test_authenticated_binding_rejects_noncanonical_scope() -> None:
    clock = _Clock()
    valid = _binding(clock=clock)
    assert parse_rehearsal_correlation_id(valid.correlation_id) == (valid.correlation_id)
    assert (
        RehearsalBinding.from_authenticated_request(
            tenant_id=" tenant-a",
            project_id=valid.project_id,
            correlation_id=valid.correlation_id,
            owner_id=valid.owner_id,
            release_commit=valid.release_commit,
            fence_token=valid.fence_token,
            expires_at_epoch=valid.expires_at_epoch,
        )
        is None
    )
    with pytest.raises(ValueError, match="project_id"):
        replace(valid, project_id="project/a")


def test_fixed_control_vocabulary_and_schemas() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    revision = ledger.claim(binding)
    assert revision == 1

    controls = [
        ("fault", "startup-delay", {"delay_seconds": 30}),
        (
            "fault",
            "provider-unavailable",
            {"provider": "bedrock", "status_code": 503},
        ),
        (
            "fault",
            "dependency-unavailable",
            {"dependency": "dynamodb"},
        ),
        (
            "checkpoint",
            "query-after-reservation",
            {"hold_seconds": 10},
        ),
        (
            "checkpoint",
            "startup-before-ready",
            {"hold_seconds": 10},
        ),
    ]
    assert {name for control_type, name, _ in controls if control_type == "fault"} == FAULTS
    assert {name for control_type, name, _ in controls if control_type == "checkpoint"} == CHECKPOINTS
    for control_type, name, parameters in controls:
        revision = ledger.write_control(
            binding,
            control_type=control_type,
            name=name,
            parameters=parameters,
            active=True,
            expected_revision=revision,
        )
        assert isinstance(revision, int)

    fault = ledger.read_active_fault(binding, "provider-unavailable")
    checkpoint = ledger.read_active_checkpoint(
        binding,
        "query-after-reservation",
    )
    assert fault is not None
    assert dict(fault.parameters) == {
        "provider": "bedrock",
        "status_code": 503,
    }
    assert checkpoint is not None
    assert dict(checkpoint.parameters) == {"hold_seconds": 10}
    assert ledger.read_active_fault(binding, "run-python") is None
    assert (
        ledger.write_control(
            binding,
            control_type="fault",
            name="provider-unavailable",
            parameters={"provider": "bedrock", "error": "arbitrary"},
            active=True,
            expected_revision=revision,
        )
        is None
    )


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (
            "startup-attempt",
            {"boot_id": "d" * 32, "phase": "timed-out", "exit_code": 124},
        ),
        (
            "query-lifecycle",
            {
                "phase": "reconciled",
                "request_id": "query-1",
                "reservation_units": 0,
                "terminal_state": "CANCELLED",
            },
        ),
        (
            "provider-attempt",
            {
                "attempt": 1,
                "outcome": "success",
                "provider": "bedrock",
                "request_id": "request-1",
                "status_code": 200,
            },
        ),
        ("routing-decision", _routing_payload()),
        (
            "dependency-call",
            {
                "dependency": "dynamodb",
                "outcome": "available",
                "request_id": "request-1",
                "status_code": 200,
            },
        ),
    ],
)
def test_fixed_observation_vocabulary_accepts_only_typed_payloads(
    kind: str,
    payload: dict[str, Any],
) -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    assert ledger.append_observation(binding, kind, payload) is True
    observations = ledger.collect_observations(
        binding,
        required_kinds={kind},
    )
    assert len(observations) == 1
    assert observations[0].kind == kind
    assert dict(observations[0].payload) == payload


def test_tenant_and_project_scopes_are_isolated() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    assert ledger.append_observation(
        binding,
        "routing-decision",
        _routing_payload(),
    )

    for foreign in (
        replace(binding, tenant_id="tenant-b"),
        replace(binding, project_id="project-b"),
    ):
        assert (
            ledger.read_active_fault(
                foreign,
                "provider-unavailable",
            )
            is None
        )
        assert (
            ledger.append_observation(
                foreign,
                "routing-decision",
                _routing_payload(),
            )
            is False
        )
        _assert_evidence_unavailable(ledger, foreign)


def test_absolute_ttl_is_bounded_and_enforced_before_dynamodb_ttl() -> None:
    table = _FakeTable()
    clock = _Clock()
    ledger = _ledger(table, clock)
    too_long = _binding(clock=clock, ttl_seconds=48 * 60 * 60 + 1)
    assert ledger.claim(too_long) is None
    assert table.get_calls == []

    binding = _binding(clock=clock, ttl_seconds=60)
    assert ledger.claim(binding) == 1
    revision = ledger.write_control(
        binding,
        control_type="fault",
        name="startup-delay",
        parameters={"delay_seconds": 30},
        active=True,
        expected_revision=1,
    )
    assert revision == 2
    assert _only_row(table)[TTL_ATTRIBUTE] == binding.expires_at_epoch

    clock.advance(60)
    assert ledger.read_active_fault(binding, "startup-delay") is None
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            _routing_payload(),
        )
        is False
    )
    _assert_evidence_unavailable(ledger, binding)


def test_higher_fence_clears_state_and_rejects_lost_fence() -> None:
    table = _FakeTable()
    clock = _Clock()
    old = _binding(clock=clock, fence_token=7)
    ledger = _ledger(table, clock)
    assert ledger.claim(old) == 1
    assert ledger.append_observation(
        old,
        "routing-decision",
        _routing_payload(),
    )
    assert (
        ledger.write_control(
            old,
            control_type="fault",
            name="provider-unavailable",
            parameters={"provider": "bedrock", "status_code": 503},
            active=True,
            expected_revision=2,
        )
        == 3
    )

    current = replace(old, fence_token=8)
    assert ledger.claim(current) == 4
    assert ledger.collect_observations(current) == ()
    assert (
        ledger.read_active_fault(
            current,
            "provider-unavailable",
        )
        is None
    )
    assert ledger.read_active_fault(old, "provider-unavailable") is None
    assert (
        ledger.append_observation(
            old,
            "routing-decision",
            _routing_payload(),
        )
        is False
    )
    _assert_evidence_unavailable(ledger, old)
    assert ledger.claim(replace(old, fence_token=6)) is None


def test_malformed_or_foreign_records_fail_open_for_hooks() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    assert ledger.append_observation(
        binding,
        "routing-decision",
        _routing_payload(),
    )
    key = next(iter(table.rows))
    baseline = copy.deepcopy(table.rows[key])

    def extra_field(row: dict[str, Any]) -> None:
        row["unexpected"] = True

    def foreign_owner(row: dict[str, Any]) -> None:
        row["owner_id"] = "f" * 64

    def invalid_expiry(row: dict[str, Any]) -> None:
        row[TTL_ATTRIBUTE] = "never"

    def arbitrary_error(row: dict[str, Any]) -> None:
        row["observations"][0]["payload"] = {"error": "provider key leaked"}

    def sequence_gap(row: dict[str, Any]) -> None:
        row["observations"][0]["sequence"] = 2

    for mutate in (
        extra_field,
        foreign_owner,
        invalid_expiry,
        arbitrary_error,
        sequence_gap,
    ):
        table.rows[key] = copy.deepcopy(baseline)
        mutate(table.rows[key])
        assert (
            ledger.read_active_fault(
                binding,
                "provider-unavailable",
            )
            is None
        )
        assert (
            ledger.append_observation(
                binding,
                "routing-decision",
                _routing_payload(),
            )
            is False
        )
        _assert_evidence_unavailable(ledger, binding)


def test_observations_reject_unknown_freeform_and_unbounded_values() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    assert OBSERVATIONS == {
        "dependency-call",
        "provider-attempt",
        "query-lifecycle",
        "routing-decision",
        "startup-attempt",
    }
    assert (
        ledger.append_observation(
            binding,
            "arbitrary-code",
            {},
        )
        is False
    )
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            {**_routing_payload(), "error": "do not persist this"},
        )
        is False
    )
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            {**_routing_payload(), "strategy": []},
        )
        is False
    )
    assert ledger.append_observation(binding, [], {}) is False
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            _routing_payload("x" * 129),
        )
        is False
    )

    for index in range(MAX_OBSERVATIONS):
        assert ledger.append_observation(
            binding,
            "routing-decision",
            _routing_payload(f"request-{index}"),
        )
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            _routing_payload("request-overflow"),
        )
        is False
    )
    assert len(ledger.collect_observations(binding)) == MAX_OBSERVATIONS


def test_control_write_uses_revision_and_full_binding_cas() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    assert (
        ledger.write_control(
            binding,
            control_type="fault",
            name="startup-delay",
            parameters={"delay_seconds": 30},
            active=True,
            expected_revision=1,
        )
        == 2
    )
    assert (
        ledger.write_control(
            binding,
            control_type="checkpoint",
            name="startup-before-ready",
            parameters={"hold_seconds": 10},
            active=True,
            expected_revision=1,
        )
        is None
    )
    row = _only_row(table)
    assert row["revision"] == 2
    assert row["checkpoints"] == {}
    replacement = table.put_calls[-1]
    assert "#revision = :revision" in replacement["ConditionExpression"]
    assert "#fence = :fence" in replacement["ConditionExpression"]
    assert "#tenant = :tenant" in replacement["ConditionExpression"]
    assert "#project = :project" in replacement["ConditionExpression"]


def test_observation_append_retries_one_cas_race_without_overwrite() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1

    def competing_write(
        fake: _FakeTable,
        _request: dict[str, Any],
        key: str,
    ) -> None:
        row = fake.rows[key]
        row["observations"].append(
            {
                "kind": "dependency-call",
                "observed_at_epoch": int(clock().timestamp()),
                "payload": {
                    "dependency": "dynamodb",
                    "outcome": "available",
                    "request_id": "competing-request",
                },
                "sequence": 1,
            }
        )
        row["revision"] += 1

    table.before_put = competing_write
    assert ledger.append_observation(
        binding,
        "routing-decision",
        _routing_payload(),
    )
    observations = ledger.collect_observations(binding)
    assert [item.sequence for item in observations] == [1, 2]
    assert [item.kind for item in observations] == [
        "dependency-call",
        "routing-decision",
    ]
    assert _only_row(table)["revision"] == 3


def test_transport_failures_are_suppressed_for_hooks_and_redacted_for_evidence() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    table.get_error = _AwsError(
        "InternalServerError",
        "api_key=must-never-escape",
    )

    assert (
        ledger.read_active_fault(
            binding,
            "provider-unavailable",
        )
        is None
    )
    assert (
        ledger.append_observation(
            binding,
            "routing-decision",
            _routing_payload(),
        )
        is False
    )
    with pytest.raises(RehearsalEvidenceUnavailable) as raised:
        ledger.collect_observations(binding)
    assert str(raised.value) == "rehearsal evidence is unavailable"
    assert "api_key" not in str(raised.value)
    assert raised.value.__cause__ is None

    table.get_error = None
    table.put_error = _AwsError(
        "InternalServerError",
        "token=must-never-escape",
    )
    assert (
        ledger.write_control(
            binding,
            control_type="fault",
            name="startup-delay",
            parameters={"delay_seconds": 30},
            active=True,
            expected_revision=1,
        )
        is None
    )


def test_evidence_collection_requires_declared_observation_kinds() -> None:
    table = _FakeTable()
    clock = _Clock()
    binding = _binding(clock=clock)
    ledger = _ledger(table, clock)
    assert ledger.claim(binding) == 1
    with pytest.raises(RehearsalEvidenceUnavailable):
        ledger.collect_observations(
            binding,
            required_kinds={"provider-attempt"},
        )
    with pytest.raises(RehearsalEvidenceUnavailable):
        ledger.collect_observations(
            binding,
            required_kinds={"arbitrary"},
        )
    with pytest.raises(RehearsalEvidenceUnavailable):
        ledger.collect_observations(
            binding,
            required_kinds=["provider-attempt"] * 6,
        )
