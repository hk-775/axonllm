from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))

import deployment_transition


def _intent(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "candidateRuntimeVersion": "7",
                "previousProductionRuntimeVersion": "6",
            }
        ),
        encoding="utf-8",
    )
    return path


def _args(tmp_path: Path, outcome: str) -> argparse.Namespace:
    return argparse.Namespace(
        intent=_intent(tmp_path / "promotion.json"),
        output=tmp_path / f"{outcome}.json",
        outcome=outcome,
        repository="owner/repo",
        run_id="42",
        run_attempt="1",
    )


@pytest.mark.parametrize(
    ("outcome", "resulting"),
    [("committed", "7"), ("rolled-back", "6")],
)
def test_terminal_record_is_bound_to_intent_and_outcome(
    tmp_path: Path,
    outcome: str,
    resulting: str,
) -> None:
    args = _args(tmp_path, outcome)
    record = deployment_transition.create_record(args)
    deployment_transition._atomic_write(args.output, record)

    verified = deployment_transition.verify_record(
        argparse.Namespace(
            intent=args.intent,
            record=args.output,
            outcome=outcome,
            repository=args.repository,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
    )

    assert verified["resultingProductionRuntimeVersion"] == resulting
    assert verified["intentSha256"]
    assert args.output.stat().st_mode & 0o777 == 0o600


def test_terminal_verification_rejects_tampered_intent(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "committed")
    deployment_transition._atomic_write(
        args.output,
        deployment_transition.create_record(args),
    )
    value = json.loads(args.intent.read_text(encoding="utf-8"))
    value["candidateRuntimeVersion"] = "8"
    args.intent.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        deployment_transition.TransitionRecordError,
        match="does not match",
    ):
        deployment_transition.verify_record(
            argparse.Namespace(
                intent=args.intent,
                record=args.output,
                outcome=None,
                repository=args.repository,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
            )
        )


def test_first_deployment_rollback_records_no_production_version(
    tmp_path: Path,
) -> None:
    intent = _intent(tmp_path / "promotion.json")
    value = json.loads(intent.read_text(encoding="utf-8"))
    value["previousProductionRuntimeVersion"] = None
    intent.write_text(json.dumps(value), encoding="utf-8")
    args = argparse.Namespace(
        intent=intent,
        output=tmp_path / "rolled-back.json",
        outcome="rolled-back",
        repository="owner/repo",
        run_id="42",
        run_attempt="1",
    )

    record = deployment_transition.create_record(args)

    assert record["resultingProductionRuntimeVersion"] is None


def test_create_reuses_exact_existing_terminal_bytes(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "committed")
    record = deployment_transition.create_record(args)
    record["recordedAt"] = "2026-08-12T12:00:00+00:00"
    deployment_transition._atomic_write(args.output, record)
    before = args.output.read_bytes()

    assert deployment_transition.materialize_record(args) is False
    assert args.output.read_bytes() == before


def test_create_refuses_to_replace_conflicting_terminal_bytes(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "committed")
    record = deployment_transition.create_record(args)
    record["outcome"] = "rolled-back"
    deployment_transition._atomic_write(args.output, record)
    before = args.output.read_bytes()

    with pytest.raises(
        deployment_transition.TransitionRecordError,
        match="does not match",
    ):
        deployment_transition.materialize_record(args)

    assert args.output.read_bytes() == before


class _S3Error(RuntimeError):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class _S3Client:
    def __init__(
        self,
        *,
        value: bytes | None = None,
        error: Exception | None = None,
        body: object | None = None,
    ) -> None:
        self.value = value
        self.error = error
        self.body = body

    def get_object(self, **kwargs: object) -> dict[str, object]:
        assert kwargs == {
            "Bucket": "evidence-bucket",
            "Key": "journal/terminal.json",
            "ChecksumMode": "ENABLED",
        }
        if self.error is not None:
            raise self.error
        value = self.value or b""
        return {
            "Body": self.body or io.BytesIO(value),
            "ChecksumSHA256": "checksum",
            "ContentLength": len(value),
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": "2033-08-12T00:00:00Z",
            "VersionId": "version-1",
        }


def _fetch_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        bucket="evidence-bucket",
        key="journal/terminal.json",
        output=tmp_path / "terminal.json",
    )


def test_s3_fetch_present_preserves_exact_bytes(tmp_path: Path) -> None:
    args = _fetch_args(tmp_path)
    value = b'{"recordedAt":"2026-08-12T12:00:00+00:00"}\n'

    state = deployment_transition.fetch_s3_object(
        args,
        s3_client=_S3Client(value=value),
    )

    assert state == deployment_transition.S3_PRESENT
    assert args.output.read_bytes() == value
    assert args.output.stat().st_mode & 0o777 == 0o600


def test_s3_fetch_only_nosuchkey_is_confirmed_absent(
    tmp_path: Path,
) -> None:
    args = _fetch_args(tmp_path)
    args.output.write_text("stale", encoding="utf-8")

    state = deployment_transition.fetch_s3_object(
        args,
        s3_client=_S3Client(error=_S3Error("NoSuchKey")),
    )

    assert state == deployment_transition.S3_ABSENT
    assert not args.output.exists()


@pytest.mark.parametrize(
    "client",
    [
        _S3Client(error=_S3Error("AccessDenied")),
        _S3Client(error=TimeoutError("timed out")),
        _S3Client(
            value=b"expected",
            body=type(
                "BrokenBody",
                (),
                {
                    "read": lambda self, _size: (_ for _ in ()).throw(OSError("connection reset")),
                    "close": lambda self: None,
                },
            )(),
        ),
    ],
)
def test_s3_fetch_aws_or_stream_failure_is_indeterminate(
    tmp_path: Path,
    client: _S3Client,
) -> None:
    args = _fetch_args(tmp_path)

    state = deployment_transition.fetch_s3_object(
        args,
        s3_client=client,
    )

    assert state == deployment_transition.S3_INDETERMINATE
    assert not args.output.exists()


def test_bound_intent_rejects_replay_under_another_journal_path(
    tmp_path: Path,
) -> None:
    intent = _intent(tmp_path / "promotion.json")
    value = json.loads(intent.read_text(encoding="utf-8"))
    value.update(
        {
            "schemaVersion": 3,
            "transition": {
                "changeId": "CHG-2026-001",
                "deploymentCommit": "a" * 40,
                "repository": "owner/repo",
                "rollbackNotBefore": "2026-08-11T16:00:00+00:00",
                "runAttempt": "1",
                "runId": "42",
                "transitionId": "b" * 64,
            },
        }
    )
    intent.write_text(json.dumps(value), encoding="utf-8")

    verified = deployment_transition.verify_intent(
        argparse.Namespace(
            intent=intent,
            repository="owner/repo",
            run_id="42",
            run_attempt="1",
        )
    )
    assert verified["transition"]["transitionId"] == "b" * 64

    with pytest.raises(
        deployment_transition.TransitionRecordError,
        match="does not match",
    ):
        deployment_transition.verify_intent(
            argparse.Namespace(
                intent=intent,
                repository="owner/repo",
                run_id="43",
                run_attempt="1",
            )
        )


def test_protected_intent_verification_rejects_legacy_schema(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        deployment_transition.TransitionRecordError,
        match="not bound",
    ):
        deployment_transition.verify_intent(
            argparse.Namespace(
                intent=_intent(tmp_path / "promotion.json"),
                repository="owner/repo",
                run_id="42",
                run_attempt="1",
            )
        )


def test_bound_intent_rejects_invalid_rollback_deadline(
    tmp_path: Path,
) -> None:
    intent = _bound_intent(tmp_path / "promotion.json")
    value = json.loads(intent.read_text(encoding="utf-8"))
    value["transition"]["rollbackNotBefore"] = "not-a-timestamp"
    intent.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        deployment_transition.TransitionRecordError,
        match="rollbackNotBefore",
    ):
        deployment_transition.verify_intent(
            argparse.Namespace(
                intent=intent,
                repository="owner/repo",
                run_id="42",
                run_attempt="1",
            )
        )


def _bound_intent(path: Path) -> Path:
    intent = _intent(path)
    value = json.loads(intent.read_text(encoding="utf-8"))
    value.update(
        {
            "schemaVersion": 3,
            "transition": {
                "changeId": "CHG-2026-001",
                "deploymentCommit": "a" * 40,
                "repository": "owner/repo",
                "rollbackNotBefore": "2026-08-11T16:00:00+00:00",
                "runAttempt": "1",
                "runId": "42",
                "transitionId": "b" * 64,
            },
        }
    )
    intent.write_text(json.dumps(value), encoding="utf-8")
    return intent


def test_recovery_binding_binds_exact_setup_and_intent(
    tmp_path: Path,
) -> None:
    intent = _bound_intent(tmp_path / "promotion.json")
    setup = tmp_path / "setup.json"
    setup.write_text('{"aws_region":"us-east-1"}\n', encoding="utf-8")
    binding = tmp_path / "recovery-binding.json"
    args = argparse.Namespace(
        intent=intent,
        setup_config=setup,
        binding=binding,
        repository="owner/repo",
        run_id="42",
        run_attempt="1",
    )
    deployment_transition._atomic_write(
        binding,
        deployment_transition.create_recovery_binding(args),
    )

    verified = deployment_transition.verify_recovery_binding(args)

    assert verified["schema"] == (deployment_transition.RECOVERY_BINDING_SCHEMA)
    assert verified["setupConfigSha256"] == hashlib.sha256(setup.read_bytes()).hexdigest()


@pytest.mark.parametrize("target", ["intent", "setup"])
def test_recovery_binding_rejects_substitution(
    tmp_path: Path,
    target: str,
) -> None:
    intent = _bound_intent(tmp_path / "promotion.json")
    setup = tmp_path / "setup.json"
    setup.write_text('{"aws_region":"us-east-1"}\n', encoding="utf-8")
    binding = tmp_path / "recovery-binding.json"
    args = argparse.Namespace(
        intent=intent,
        setup_config=setup,
        binding=binding,
        repository="owner/repo",
        run_id="42",
        run_attempt="1",
    )
    deployment_transition._atomic_write(
        binding,
        deployment_transition.create_recovery_binding(args),
    )
    if target == "intent":
        value = json.loads(intent.read_text(encoding="utf-8"))
        value["candidateRuntimeVersion"] = "8"
        intent.write_text(json.dumps(value), encoding="utf-8")
    else:
        setup.write_text('{"aws_region":"us-west-2"}\n', encoding="utf-8")

    with pytest.raises(
        deployment_transition.TransitionRecordError,
        match="does not match",
    ):
        deployment_transition.verify_recovery_binding(args)
