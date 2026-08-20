#!/usr/bin/env python3
"""Create and verify AxonLLM release evidence without registry publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_V3 = "https://axonllm.dev/schemas/release-evidence/v3"
SCHEMA = "https://axonllm.dev/schemas/release-evidence/v4"
PROVENANCE_TYPE = "https://slsa.dev/provenance/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
SIGNING_PROVIDER = "AWS_KMS"
SIGNING_ALGORITHM = "ECDSA_SHA_256"
BUILD_TYPE_V3 = "https://github.com/AxonLLM/axonllm/.github/workflows/release-security.yml@v3"
BUILD_TYPE = "https://github.com/AxonLLM/axonllm/.github/workflows/release-security.yml@v4"
WORKFLOW_PATH = ".github/workflows/release-security.yml"
SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9_./+-]+$")
KMS_KEY_ARN = re.compile(
    r"^arn:aws:kms:us-east-1:(?P<account_id>[0-9]{12}):key/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
AWS_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")

SOURCE_ARCHIVE = "axonllm-source.tar.gz"
IMAGE_ARCHIVE = "axonllm-linux-amd64.oci.tar"
BUILD_METADATA = "build-metadata.json"
SOURCE_SBOM = "source.cyclonedx.json"
IMAGE_SBOM = "image.cyclonedx.json"
SOURCE_SCAN = "source-security.json"
IMAGE_SCAN = "image-security.json"
AGENTCORE_IMAGE_ARCHIVE = "axonllm-agentcore-linux-arm64.oci.tar"
AGENTCORE_BUILD_METADATA = "agentcore-build-metadata.json"
AGENTCORE_IMAGE_SBOM = "agentcore-image.cyclonedx.json"
AGENTCORE_IMAGE_SCAN = "agentcore-image-security.json"
STANDALONE_ARM64_IMAGE_ARCHIVE = "axonllm-standalone-linux-arm64.oci.tar"
STANDALONE_ARM64_BUILD_METADATA = "standalone-arm64-build-metadata.json"
STANDALONE_ARM64_IMAGE_SBOM = "standalone-arm64-image.cyclonedx.json"
STANDALONE_ARM64_IMAGE_SCAN = "standalone-arm64-image-security.json"
PROVENANCE = "provenance.intoto.json"
MANIFEST = "release-manifest.json"
PROVENANCE_SIGNATURE = "provenance-kms-signature.json"
MANIFEST_SIGNATURE = "manifest-kms-signature.json"


@dataclass(frozen=True)
class TargetSpec:
    """Files and immutable identity expected for one deployment target."""

    subject: str
    platform: str
    archive: str
    metadata: str
    sbom: str
    scan: str


TARGETS_V3 = {
    "fargate": TargetSpec(
        subject="axonllm-linux-amd64",
        platform="linux/amd64",
        archive=IMAGE_ARCHIVE,
        metadata=BUILD_METADATA,
        sbom=IMAGE_SBOM,
        scan=IMAGE_SCAN,
    ),
    "agentcore": TargetSpec(
        subject="axonllm-agentcore-linux-arm64",
        platform="linux/arm64",
        archive=AGENTCORE_IMAGE_ARCHIVE,
        metadata=AGENTCORE_BUILD_METADATA,
        sbom=AGENTCORE_IMAGE_SBOM,
        scan=AGENTCORE_IMAGE_SCAN,
    ),
}
TARGETS = {
    **TARGETS_V3,
    "standalone-amd64": TargetSpec(
        subject="axonllm-standalone-linux-amd64",
        platform="linux/amd64",
        archive=IMAGE_ARCHIVE,
        metadata=BUILD_METADATA,
        sbom=IMAGE_SBOM,
        scan=IMAGE_SCAN,
    ),
    "standalone-arm64": TargetSpec(
        subject="axonllm-standalone-linux-arm64",
        platform="linux/arm64",
        archive=STANDALONE_ARM64_IMAGE_ARCHIVE,
        metadata=STANDALONE_ARM64_BUILD_METADATA,
        sbom=STANDALONE_ARM64_IMAGE_SBOM,
        scan=STANDALONE_ARM64_IMAGE_SCAN,
    ),
}
SOURCE_ARTIFACTS = (
    SOURCE_ARCHIVE,
    SOURCE_SBOM,
    SOURCE_SCAN,
)


def _target_artifacts(
    targets: dict[str, TargetSpec],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            artifact
            for target in targets.values()
            for artifact in (
                target.archive,
                target.metadata,
                target.sbom,
                target.scan,
            )
        )
    )


TARGET_ARTIFACTS = _target_artifacts(TARGETS)
INPUT_ARTIFACTS = SOURCE_ARTIFACTS + TARGET_ARTIFACTS
IMAGE_ARCHIVES = tuple(
    dict.fromkeys(target.archive for target in TARGETS.values())
)


class EvidenceError(RuntimeError):
    """Raised when release evidence is malformed or inconsistent."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n",
        encoding="utf-8",
    )


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceError(f"cannot read artifact: {path}") from exc
    return digest.hexdigest(), size


def _validate_source(
    repository: str,
    commit: str,
    ref: str,
    workflow_ref: str,
) -> None:
    if not REPOSITORY.fullmatch(repository):
        raise EvidenceError("repository must be owner/name")
    if not COMMIT.fullmatch(commit):
        raise EvidenceError("commit must be a full lowercase SHA")
    if not REF.fullmatch(ref) or ".." in ref:
        raise EvidenceError("invalid Git ref")
    expected = f"{repository}/{WORKFLOW_PATH}@{ref}"
    if workflow_ref != expected:
        raise EvidenceError(f"workflow ref must be {expected}")


def _validate_signing_key_arn(signing_key_arn: str) -> re.Match[str]:
    if not isinstance(signing_key_arn, str):
        raise EvidenceError("signing key must be a full us-east-1 AWS KMS key ARN")
    match = KMS_KEY_ARN.fullmatch(signing_key_arn)
    if match is None:
        raise EvidenceError("signing key must be a full us-east-1 AWS KMS key ARN")
    return match


def _trusted_signing_key_arn(
    signing: Any,
    *,
    signing_key_arn: str | None,
    signing_account_id: str | None,
) -> str:
    if (signing_key_arn is None) == (signing_account_id is None):
        raise EvidenceError("exactly one signing key ARN or signing account ID is required")
    if signing_key_arn is not None:
        _validate_signing_key_arn(signing_key_arn)
    if signing_account_id is not None and (
        not isinstance(signing_account_id, str) or not AWS_ACCOUNT_ID.fullmatch(signing_account_id)
    ):
        raise EvidenceError("signing account ID must be exactly 12 digits")

    if not isinstance(signing, dict) or set(signing) != {
        "provider",
        "algorithm",
        "keyArn",
        "provenanceSignature",
        "manifestSignature",
    }:
        raise EvidenceError("release signing identity is malformed")
    manifest_key_arn = signing.get("keyArn")
    if not isinstance(manifest_key_arn, str):
        raise EvidenceError("release signing key ARN is malformed")
    match = _validate_signing_key_arn(manifest_key_arn)
    expected_signing = {
        "provider": SIGNING_PROVIDER,
        "algorithm": SIGNING_ALGORITHM,
        "keyArn": manifest_key_arn,
        "provenanceSignature": PROVENANCE_SIGNATURE,
        "manifestSignature": MANIFEST_SIGNATURE,
    }
    if signing != expected_signing:
        raise EvidenceError("release signing identity does not match expectation")
    if signing_key_arn is not None and manifest_key_arn != signing_key_arn:
        raise EvidenceError("release signing identity does not match expectation")
    if signing_account_id is not None and match.group("account_id") != signing_account_id:
        raise EvidenceError("release signing key is not in the trusted AWS account")
    return manifest_key_arn


def _tar_members(archive: Path) -> dict[str, tarfile.TarInfo]:
    try:
        with tarfile.open(archive, mode="r:*") as image:
            members: dict[str, tarfile.TarInfo] = {}
            for member in image.getmembers():
                name = member.name.removeprefix("./")
                if name in members:
                    raise EvidenceError(f"OCI archive contains duplicate path: {name}")
                if member.issym() or member.islnk():
                    raise EvidenceError(f"OCI archive contains link: {name}")
                members[name] = member
            return members
    except (OSError, tarfile.TarError) as exc:
        raise EvidenceError(f"invalid OCI archive: {archive}") from exc


def _tar_json(
    image: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> dict[str, Any]:
    member = members.get(name)
    if member is None or not member.isfile() or member.size > 16 * 1024 * 1024:
        raise EvidenceError(f"invalid OCI JSON member: {name}")
    stream = image.extractfile(member)
    if stream is None:
        raise EvidenceError(f"cannot read OCI member: {name}")
    try:
        payload = stream.read()
        if name.startswith("blobs/sha256/"):
            expected = name.removeprefix("blobs/sha256/")
            if hashlib.sha256(payload).hexdigest() != expected:
                raise EvidenceError(f"OCI blob digest mismatch: {name}")
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid OCI JSON: {name}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"OCI JSON must be an object: {name}")
    return value


def _oci_blob_name(digest: str) -> str:
    match = SHA256.fullmatch(digest)
    if not match:
        raise EvidenceError(f"invalid OCI digest: {digest}")
    return f"blobs/sha256/{match.group(1)}"


def _verify_descriptor_blob(
    image: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    descriptor: dict[str, Any],
) -> None:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise EvidenceError("OCI descriptor lacks a valid digest or size")
    name = _oci_blob_name(digest)
    member = members.get(name)
    if member is None or not member.isfile() or member.size != size:
        raise EvidenceError(f"OCI descriptor does not match blob: {digest}")
    stream = image.extractfile(member)
    if stream is None:
        raise EvidenceError(f"cannot read OCI blob: {digest}")
    actual = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        actual.update(chunk)
    if f"sha256:{actual.hexdigest()}" != digest:
        raise EvidenceError(f"OCI blob digest mismatch: {digest}")


def _verify_oci_platform(
    image: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    descriptor: dict[str, Any],
    seen: set[str],
    expected: tuple[str, str],
) -> bool:
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or digest in seen:
        return False
    seen.add(digest)
    _verify_descriptor_blob(image, members, descriptor)
    value = _tar_json(image, members, _oci_blob_name(digest))
    manifests = value.get("manifests")
    if isinstance(manifests, list):
        candidates = []
        for child in manifests:
            if isinstance(child, dict):
                platform = child.get("platform")
                if (
                    not isinstance(platform, dict)
                    or (
                        platform.get("os"),
                        platform.get("architecture"),
                    )
                    == expected
                ):
                    candidates.append(child)
        return any(_verify_oci_platform(image, members, child, seen, expected) for child in candidates)

    platform = descriptor.get("platform")
    if (
        isinstance(platform, dict)
        and (
            platform.get("os"),
            platform.get("architecture"),
        )
        != expected
    ):
        return False
    config_descriptor = value.get("config")
    layers = value.get("layers")
    if not isinstance(config_descriptor, dict) or not isinstance(layers, list):
        return False
    _verify_descriptor_blob(image, members, config_descriptor)
    for layer in layers:
        if not isinstance(layer, dict):
            return False
        _verify_descriptor_blob(image, members, layer)
    config_digest = config_descriptor.get("digest")
    if not isinstance(config_digest, str):
        return False
    config = _tar_json(image, members, _oci_blob_name(config_digest))
    return (config.get("os"), config.get("architecture")) == expected


def verify_oci_archive(archive: Path, digest: str, platform: str) -> None:
    try:
        operating_system, architecture = platform.split("/", maxsplit=1)
    except ValueError as exc:
        raise EvidenceError(f"invalid target platform: {platform}") from exc
    blob_name = _oci_blob_name(digest)
    members = _tar_members(archive)
    if "oci-layout" not in members or "index.json" not in members:
        raise EvidenceError("OCI archive lacks oci-layout or index.json")
    member = members.get(blob_name)
    if member is None or not member.isfile():
        raise EvidenceError(f"OCI archive lacks signed image digest: {digest}")
    try:
        with tarfile.open(archive, mode="r:*") as image:
            index = _tar_json(image, members, "index.json")
            descriptors = index.get("manifests")
            if not isinstance(descriptors, list):
                raise EvidenceError("OCI index does not reference the signed digest")
            matching = [item for item in descriptors if isinstance(item, dict) and item.get("digest") == digest]
            if len(matching) != 1:
                raise EvidenceError("OCI index does not reference the signed digest")
            if not _verify_oci_platform(
                image,
                members,
                matching[0],
                set(),
                (operating_system, architecture),
            ):
                raise EvidenceError(f"OCI archive lacks a {platform} image")
    except (OSError, tarfile.TarError) as exc:
        raise EvidenceError(f"cannot inspect OCI archive: {archive}") from exc


def _metadata_image_digest(directory: Path, target: TargetSpec) -> str:
    metadata = _read_json(directory / target.metadata)
    if not isinstance(metadata, dict):
        raise EvidenceError("BuildKit metadata must be an object")
    digest = metadata.get("containerimage.digest")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise EvidenceError("BuildKit metadata lacks containerimage.digest")
    return digest


def _image_digest(directory: Path, target: TargetSpec) -> str:
    digest = _metadata_image_digest(directory, target)
    verify_oci_archive(directory / target.archive, digest, target.platform)
    return digest


def _assert_clean_scan(path: Path) -> None:
    report = _read_json(path)
    if not isinstance(report, dict) or not isinstance(report.get("Results", []), list):
        raise EvidenceError(f"invalid Trivy report: {path}")
    findings: list[str] = []
    for result in report.get("Results", []):
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target", "unknown"))
        for key in ("Vulnerabilities", "Secrets", "Misconfigurations"):
            entries = result.get(key) or []
            if entries:
                findings.append(f"{target}:{key}={len(entries)}")
    if findings:
        raise EvidenceError(f"release scan contains blocked findings in {path.name}: " + ", ".join(findings))


def _target_record(spec: TargetSpec, digest: str) -> dict[str, str]:
    return {
        "archive": spec.archive,
        "digest": digest,
        "metadata": spec.metadata,
        "platform": spec.platform,
        "sbom": spec.sbom,
        "scan": spec.scan,
        "subject": spec.subject,
    }


def _provenance_statement(
    *,
    repository: str,
    commit: str,
    ref: str,
    workflow_ref: str,
    run_id: str,
    run_attempt: str,
    event_name: str,
    signing_key_arn: str,
    artifacts: dict[str, dict[str, Any]],
    targets: dict[str, dict[str, str]],
    target_specs: dict[str, TargetSpec],
    input_artifacts: tuple[str, ...],
    build_type: str,
) -> dict[str, Any]:
    subjects = [
        {
            "name": name,
            "digest": {"sha256": artifacts[name]["sha256"]},
        }
        for name in input_artifacts
    ]
    subjects.extend(
        {
            "name": target_specs[name].subject,
            "digest": {"sha256": targets[name]["digest"].removeprefix("sha256:")},
        }
        for name in target_specs
    )
    return {
        "_type": STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": PROVENANCE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": build_type,
                "externalParameters": {
                    "commit": commit,
                    "ref": ref,
                    "repository": repository,
                    "signingKeyArn": signing_key_arn,
                    "targets": {
                        name: {
                            "digest": targets[name]["digest"],
                            "platform": target_specs[name].platform,
                        }
                        for name in target_specs
                    },
                    "workflowRef": workflow_ref,
                },
                "internalParameters": {
                    "eventName": event_name,
                    "runAttempt": run_attempt,
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+https://github.com/{repository}@{commit}",
                        "digest": {"gitCommit": commit},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": f"https://github.com/{workflow_ref}"},
                "metadata": {
                    "invocationId": (f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{run_attempt}")
                },
            },
        },
    }


def create_evidence(
    directory: Path,
    *,
    repository: str,
    commit: str,
    ref: str,
    workflow_ref: str,
    run_id: str,
    run_attempt: str,
    event_name: str,
    signing_key_arn: str,
) -> dict[str, Any]:
    _validate_source(repository, commit, ref, workflow_ref)
    _validate_signing_key_arn(signing_key_arn)
    if not run_id.isdigit() or not run_attempt.isdigit():
        raise EvidenceError("run ID and attempt must be numeric")
    if not re.fullmatch(r"[A-Za-z0-9_]+", event_name):
        raise EvidenceError("event name is malformed")
    for name in INPUT_ARTIFACTS:
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise EvidenceError(f"missing release artifact: {name}")
    _assert_clean_scan(directory / SOURCE_SCAN)
    target_records: dict[str, dict[str, str]] = {}
    for name, spec in TARGETS.items():
        _assert_clean_scan(directory / spec.scan)
        target_records[name] = _target_record(
            spec,
            _image_digest(directory, spec),
        )

    artifact_records: dict[str, dict[str, Any]] = {}
    for name in INPUT_ARTIFACTS:
        digest, size = _hash(directory / name)
        artifact_records[name] = {"sha256": digest, "size": size}

    provenance = _provenance_statement(
        repository=repository,
        commit=commit,
        ref=ref,
        workflow_ref=workflow_ref,
        run_id=run_id,
        run_attempt=run_attempt,
        event_name=event_name,
        signing_key_arn=signing_key_arn,
        artifacts=artifact_records,
        targets=target_records,
        target_specs=TARGETS,
        input_artifacts=INPUT_ARTIFACTS,
        build_type=BUILD_TYPE,
    )
    _write_json(directory / PROVENANCE, provenance)
    provenance_digest, provenance_size = _hash(directory / PROVENANCE)
    artifact_records[PROVENANCE] = {
        "sha256": provenance_digest,
        "size": provenance_size,
    }

    manifest = {
        "schema": SCHEMA,
        "source": {
            "repository": repository,
            "commit": commit,
            "ref": ref,
            "workflowRef": workflow_ref,
            "runId": run_id,
            "runAttempt": run_attempt,
            "eventName": event_name,
        },
        "signing": {
            "provider": SIGNING_PROVIDER,
            "algorithm": SIGNING_ALGORITHM,
            "keyArn": signing_key_arn,
            "provenanceSignature": PROVENANCE_SIGNATURE,
            "manifestSignature": MANIFEST_SIGNATURE,
        },
        "targets": target_records,
        "artifacts": artifact_records,
    }
    _write_json(directory / MANIFEST, manifest)
    return manifest


def verify_evidence(
    directory: Path,
    *,
    repository: str,
    commit: str,
    image_digest: str | None,
    require_release_tag: bool,
    signing_key_arn: str | None = None,
    signing_account_id: str | None = None,
    target: str = "fargate",
    expected_run_id: str | None = None,
    allow_missing_image_archives: bool = False,
) -> dict[str, Any]:
    if allow_missing_image_archives and (
        image_digest is None or not require_release_tag
    ):
        raise EvidenceError(
            "archive-free verification requires an exact image digest "
            "and tagged release"
        )
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise EvidenceError("release manifest is missing or unsafe")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema") not in {
        SCHEMA_V3,
        SCHEMA,
    }:
        raise EvidenceError("unsupported release manifest schema")
    schema = manifest["schema"]
    target_specs = TARGETS_V3 if schema == SCHEMA_V3 else TARGETS
    input_artifacts = SOURCE_ARTIFACTS + _target_artifacts(target_specs)
    image_archives = {
        spec.archive
        for spec in target_specs.values()
    }
    build_type = BUILD_TYPE_V3 if schema == SCHEMA_V3 else BUILD_TYPE
    if target not in target_specs:
        raise EvidenceError(f"unknown deployment target: {target}")
    if set(manifest) != {
        "schema",
        "source",
        "signing",
        "targets",
        "artifacts",
    }:
        raise EvidenceError("release manifest structure is unexpected")
    manifest_signing_key_arn = _trusted_signing_key_arn(
        manifest.get("signing"),
        signing_key_arn=signing_key_arn,
        signing_account_id=signing_account_id,
    )
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise EvidenceError("release manifest lacks source identity")
    if set(source) != {
        "repository",
        "commit",
        "ref",
        "workflowRef",
        "runId",
        "runAttempt",
        "eventName",
    }:
        raise EvidenceError("release source identity is malformed")
    if source.get("repository") != repository or source.get("commit") != commit:
        raise EvidenceError("release source identity does not match expectation")
    ref = source.get("ref")
    workflow_ref = source.get("workflowRef")
    run_id = source.get("runId")
    run_attempt = source.get("runAttempt")
    event_name = source.get("eventName")
    if (
        not isinstance(ref, str)
        or not isinstance(workflow_ref, str)
        or not isinstance(run_id, str)
        or not run_id.isdigit()
        or not isinstance(run_attempt, str)
        or not run_attempt.isdigit()
        or not isinstance(event_name, str)
        or not re.fullmatch(r"[A-Za-z0-9_]+", event_name)
    ):
        raise EvidenceError("release source ref is malformed")
    if expected_run_id is not None and (not expected_run_id.isdigit() or expected_run_id != run_id):
        raise EvidenceError("release workflow run ID does not match expectation")
    _validate_source(repository, commit, ref, workflow_ref)
    if require_release_tag and not re.fullmatch(r"refs/tags/v[0-9A-Za-z._+-]+", ref):
        raise EvidenceError("deployment evidence must originate from a v* tag")

    target_records = manifest.get("targets")
    if not isinstance(target_records, dict) or set(target_records) != set(target_specs):
        raise EvidenceError("release manifest target set is incomplete or unexpected")
    target_digests: dict[str, str] = {}
    for name, spec in target_specs.items():
        record = target_records.get(name)
        if not isinstance(record, dict):
            raise EvidenceError(f"release target is malformed: {name}")
        digest = record.get("digest")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise EvidenceError(f"release target digest is malformed: {name}")
        if record != _target_record(spec, digest):
            raise EvidenceError(f"release target identity is malformed: {name}")
        target_digests[name] = digest
    signed_digest = target_digests[target]
    if image_digest is not None and (not SHA256.fullmatch(image_digest) or image_digest != signed_digest):
        raise EvidenceError("deployment image digest differs from release evidence")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise EvidenceError("release manifest lacks artifacts")
    expected_artifacts = {*input_artifacts, PROVENANCE}
    if set(artifacts) != expected_artifacts:
        raise EvidenceError("release manifest artifact set is incomplete or unexpected")
    for name, record in artifacts.items():
        if Path(name).name != name or not isinstance(record, dict):
            raise EvidenceError(f"unsafe artifact name: {name}")
        if (
            set(record) != {"sha256", "size"}
            or not isinstance(record.get("sha256"), str)
            or not isinstance(record.get("size"), int)
            or isinstance(record.get("size"), bool)
        ):
            raise EvidenceError(f"malformed artifact record: {name}")
        path = directory / name
        if path.is_symlink():
            raise EvidenceError(f"missing signed artifact: {name}")
        if not path.is_file():
            if (
                allow_missing_image_archives
                and name in image_archives
                and not path.exists()
            ):
                continue
            raise EvidenceError(f"missing signed artifact: {name}")
        digest, size = _hash(path)
        if record != {"sha256": digest, "size": size}:
            raise EvidenceError(f"artifact digest mismatch: {name}")

    _assert_clean_scan(directory / SOURCE_SCAN)
    for name, spec in target_specs.items():
        _assert_clean_scan(directory / spec.scan)
        metadata_digest = _metadata_image_digest(directory, spec)
        if metadata_digest != target_digests[name]:
            raise EvidenceError(f"OCI digest differs from release manifest: {name}")
        archive = directory / spec.archive
        if archive.is_file():
            verify_oci_archive(
                archive,
                metadata_digest,
                spec.platform,
            )

    provenance = _read_json(directory / PROVENANCE)
    if not isinstance(provenance, dict):
        raise EvidenceError("provenance statement must be an object")
    expected_provenance = _provenance_statement(
        repository=repository,
        commit=commit,
        ref=ref,
        workflow_ref=workflow_ref,
        run_id=run_id,
        run_attempt=run_attempt,
        event_name=event_name,
        signing_key_arn=manifest_signing_key_arn,
        artifacts=artifacts,
        targets=target_records,
        target_specs=target_specs,
        input_artifacts=input_artifacts,
        build_type=build_type,
    )
    if provenance != expected_provenance:
        raise EvidenceError("provenance does not match signed release manifest")
    return manifest


def _create_command(args: argparse.Namespace) -> int:
    manifest = create_evidence(
        args.directory,
        repository=args.repository,
        commit=args.commit,
        ref=args.ref,
        workflow_ref=args.workflow_ref,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        event_name=args.event_name,
        signing_key_arn=args.signing_key_arn,
    )
    identities = ", ".join(f"{name}={manifest['targets'][name]['digest']}" for name in TARGETS)
    print(f"release evidence created: {identities}")
    return 0


def _write_github_output(
    path: Path,
    *,
    target: str,
    manifest: dict[str, Any],
) -> None:
    record = manifest["targets"][target]
    values = {
        "digest": record["digest"],
        "platform": record["platform"],
        "release_ref": manifest["source"]["ref"],
        "run_attempt": manifest["source"]["runAttempt"],
        "run_id": manifest["source"]["runId"],
        "event_name": manifest["source"]["eventName"],
        "subject": record["subject"],
        "target": target,
        "signing_key_arn": manifest["signing"]["keyArn"],
    }
    try:
        with path.open("a", encoding="utf-8") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
    except OSError as exc:
        raise EvidenceError(f"cannot write GitHub output: {path}") from exc


def _verify_command(args: argparse.Namespace) -> int:
    manifest = verify_evidence(
        args.directory,
        repository=args.repository,
        commit=args.commit,
        image_digest=args.image_digest,
        require_release_tag=args.require_release_tag,
        signing_key_arn=args.signing_key_arn,
        signing_account_id=args.signing_account_id,
        target=args.target,
        expected_run_id=args.run_id,
        allow_missing_image_archives=args.allow_missing_image_archives,
    )
    if args.github_output:
        _write_github_output(
            args.github_output,
            target=args.target,
            manifest=manifest,
        )
    print(
        "release evidence verified: "
        f"{manifest['source']['ref']} {args.target} "
        f"{manifest['targets'][args.target]['digest']}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--directory", type=Path, required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--ref", required=True)
    create.add_argument("--workflow-ref", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--run-attempt", required=True)
    create.add_argument("--event-name", required=True)
    create.add_argument("--signing-key-arn", required=True)
    create.set_defaults(handler=_create_command)
    verify = commands.add_parser("verify")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--image-digest")
    verify.add_argument("--require-release-tag", action="store_true")
    signing_trust = verify.add_mutually_exclusive_group(required=True)
    signing_trust.add_argument("--signing-key-arn")
    signing_trust.add_argument("--signing-account-id")
    verify.add_argument("--target", choices=tuple(TARGETS), default="fargate")
    verify.add_argument("--run-id")
    verify.add_argument(
        "--allow-missing-image-archives",
        action="store_true",
    )
    verify.add_argument("--github-output", type=Path)
    verify.set_defaults(handler=_verify_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return args.handler(args)
    except EvidenceError as exc:
        print(f"release evidence failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
