from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "release"))

import release_evidence  # noqa: E402
import verify_ecr_image  # noqa: E402


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = 0o644
    archive.addfile(info, BytesIO(value))


def _write_oci(path: Path, architecture: str) -> str:
    config = _json_bytes({"architecture": architecture, "os": "linux"})
    config_digest = _digest(config)
    manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": [],
        }
    )
    manifest_digest = _digest(manifest)
    index = _json_bytes(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": len(manifest),
                    "platform": {"architecture": architecture, "os": "linux"},
                }
            ],
        }
    )
    with tarfile.open(path, "w") as archive:
        _add_bytes(
            archive,
            "oci-layout",
            _json_bytes({"imageLayoutVersion": "1.0.0"}),
        )
        _add_bytes(archive, "index.json", index)
        _add_bytes(
            archive,
            f"blobs/sha256/{config_digest.removeprefix('sha256:')}",
            config,
        )
        _add_bytes(
            archive,
            f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}",
            manifest,
        )
    return manifest_digest


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / release_evidence.SOURCE_ARCHIVE).write_bytes(b"source")
        (self.directory / release_evidence.SOURCE_SBOM).write_text(
            "{}\n",
            encoding="utf-8",
        )
        (self.directory / release_evidence.SOURCE_SCAN).write_text(
            '{"Results": []}\n',
            encoding="utf-8",
        )
        self.digests: dict[str, str] = {}
        for name, target in release_evidence.TARGETS.items():
            architecture = target.platform.removeprefix("linux/")
            digest = _write_oci(
                self.directory / target.archive,
                architecture,
            )
            self.digests[name] = digest
            (self.directory / target.metadata).write_text(
                json.dumps({"containerimage.digest": digest}),
                encoding="utf-8",
            )
            (self.directory / target.sbom).write_text(
                "{}\n",
                encoding="utf-8",
            )
            (self.directory / target.scan).write_text(
                '{"Results": []}\n',
                encoding="utf-8",
            )
        self.repository = "AxonLLM/axonllm"
        self.commit = "a" * 40
        self.ref = "refs/tags/v1.2.3"
        self.workflow_ref = f"{self.repository}/{release_evidence.WORKFLOW_PATH}@{self.ref}"
        self.digest = self.digests["fargate"]
        self.agentcore_digest = self.digests["agentcore"]
        self.standalone_amd64_digest = self.digests["standalone-amd64"]
        self.standalone_arm64_digest = self.digests["standalone-arm64"]
        self.signing_key_arn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc"
        self.signing_account_id = "123456789012"

    def _create(self) -> None:
        release_evidence.create_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            ref=self.ref,
            workflow_ref=self.workflow_ref,
            run_id="123",
            run_attempt="1",
            event_name="push",
            signing_key_arn=self.signing_key_arn,
        )

    def _downgrade_to_schema_v3(self) -> None:
        manifest_path = self.directory / release_evidence.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema"] = release_evidence.SCHEMA_V3
        manifest["targets"] = {name: manifest["targets"][name] for name in release_evidence.TARGETS_V3}
        input_artifacts = release_evidence.SOURCE_ARTIFACTS + release_evidence._target_artifacts(
            release_evidence.TARGETS_V3,
        )
        manifest["artifacts"] = {
            name: record
            for name, record in manifest["artifacts"].items()
            if name in {*input_artifacts, release_evidence.PROVENANCE}
        }
        source = manifest["source"]
        provenance = release_evidence._provenance_statement(
            repository=source["repository"],
            commit=source["commit"],
            ref=source["ref"],
            workflow_ref=source["workflowRef"],
            run_id=source["runId"],
            run_attempt=source["runAttempt"],
            event_name=source["eventName"],
            signing_key_arn=manifest["signing"]["keyArn"],
            artifacts=manifest["artifacts"],
            targets=manifest["targets"],
            target_specs=release_evidence.TARGETS_V3,
            input_artifacts=input_artifacts,
            build_type=release_evidence.BUILD_TYPE_V3,
        )
        release_evidence._write_json(
            self.directory / release_evidence.PROVENANCE,
            provenance,
        )
        digest, size = release_evidence._hash(
            self.directory / release_evidence.PROVENANCE,
        )
        manifest["artifacts"][release_evidence.PROVENANCE] = {
            "sha256": digest,
            "size": size,
        }
        release_evidence._write_json(manifest_path, manifest)

    def test_create_and_verify_release_evidence(self) -> None:
        self._create()
        manifest = release_evidence.verify_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            image_digest=self.digest,
            require_release_tag=True,
            signing_key_arn=self.signing_key_arn,
        )
        self.assertEqual(manifest["targets"]["fargate"]["digest"], self.digest)
        self.assertEqual(
            manifest["targets"]["agentcore"]["digest"],
            self.agentcore_digest,
        )
        self.assertEqual(
            manifest["targets"]["agentcore"]["platform"],
            "linux/arm64",
        )
        self.assertEqual(
            manifest["targets"]["standalone-amd64"]["digest"],
            self.digest,
        )
        self.assertEqual(
            manifest["targets"]["standalone-arm64"]["digest"],
            self.standalone_arm64_digest,
        )
        self.assertEqual(manifest["schema"], release_evidence.SCHEMA)
        self.assertEqual(
            manifest["source"]["workflowRef"],
            self.workflow_ref,
        )
        for target in release_evidence.TARGETS.values():
            self.assertIn(target.archive, manifest["artifacts"])
            self.assertIn(target.metadata, manifest["artifacts"])
            self.assertIn(target.scan, manifest["artifacts"])
            self.assertIn(target.sbom, manifest["artifacts"])
        provenance = json.loads((self.directory / release_evidence.PROVENANCE).read_text(encoding="utf-8"))
        parameters = provenance["predicate"]["buildDefinition"]["externalParameters"]
        self.assertEqual(parameters["workflowRef"], self.workflow_ref)
        self.assertEqual(
            parameters["signingKeyArn"],
            self.signing_key_arn,
        )
        self.assertEqual(
            set(parameters["targets"]),
            {
                "fargate",
                "agentcore",
                "standalone-amd64",
                "standalone-arm64",
            },
        )

    def test_account_trust_accepts_rotated_manifest_key(self) -> None:
        self.signing_key_arn = "arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._create()

        manifest = release_evidence.verify_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            image_digest=self.digest,
            require_release_tag=True,
            signing_account_id=self.signing_account_id,
        )

        self.assertEqual(
            manifest["signing"]["keyArn"],
            self.signing_key_arn,
        )

    def test_schema_v3_evidence_remains_verifiable_for_legacy_targets(self) -> None:
        self._create()
        self._downgrade_to_schema_v3()

        manifest = release_evidence.verify_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            image_digest=self.digest,
            require_release_tag=True,
            signing_key_arn=self.signing_key_arn,
            target="fargate",
        )

        self.assertEqual(manifest["schema"], release_evidence.SCHEMA_V3)
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "unknown deployment target",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
                target="standalone-amd64",
            )

    def test_account_trust_rejects_key_from_another_account(self) -> None:
        self._create()
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "not in the trusted AWS account",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_account_id="210987654321",
            )

    def test_account_trust_rejects_malformed_account_ids(self) -> None:
        self._create()
        for account_id in (
            "",
            "12345678901",
            "1234567890123",
            "12345678901a",
        ):
            with self.subTest(account_id=account_id):
                with self.assertRaisesRegex(
                    release_evidence.EvidenceError,
                    "exactly 12 digits",
                ):
                    release_evidence.verify_evidence(
                        self.directory,
                        repository=self.repository,
                        commit=self.commit,
                        image_digest=self.digest,
                        require_release_tag=True,
                        signing_account_id=account_id,
                    )

    def test_account_trust_rejects_malformed_manifest_key(self) -> None:
        self._create()
        manifest_path = self.directory / release_evidence.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["signing"]["keyArn"] = "arn:aws:kms:us-east-1:123456789012:key/not-a-key-id"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "signing key must be a full",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_account_id=self.signing_account_id,
            )

    def test_exactly_one_signing_trust_selector_is_required(self) -> None:
        self._create()
        common = {
            "repository": self.repository,
            "commit": self.commit,
            "image_digest": self.digest,
            "require_release_tag": True,
        }
        for selectors in (
            {},
            {
                "signing_key_arn": self.signing_key_arn,
                "signing_account_id": self.signing_account_id,
            },
        ):
            with self.subTest(selectors=selectors):
                with self.assertRaisesRegex(
                    release_evidence.EvidenceError,
                    "exactly one signing key ARN or signing account ID",
                ):
                    release_evidence.verify_evidence(
                        self.directory,
                        **common,
                        **selectors,
                    )

    def test_verify_cli_requires_exactly_one_signing_trust_selector(self) -> None:
        arguments = [
            "verify",
            "--directory",
            str(self.directory),
            "--repository",
            self.repository,
            "--commit",
            self.commit,
        ]
        parser = release_evidence._parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(arguments)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    *arguments,
                    "--signing-key-arn",
                    self.signing_key_arn,
                    "--signing-account-id",
                    self.signing_account_id,
                ]
            )

        by_key = parser.parse_args([*arguments, "--signing-key-arn", self.signing_key_arn])
        self.assertEqual(by_key.signing_key_arn, self.signing_key_arn)
        self.assertIsNone(by_key.signing_account_id)

        by_account = parser.parse_args([*arguments, "--signing-account-id", self.signing_account_id])
        self.assertEqual(
            by_account.signing_account_id,
            self.signing_account_id,
        )
        self.assertIsNone(by_account.signing_key_arn)

    def test_agentcore_target_selects_its_digest_and_signing_key(self) -> None:
        self._create()
        manifest = release_evidence.verify_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            image_digest=self.agentcore_digest,
            require_release_tag=True,
            signing_key_arn=self.signing_key_arn,
            target="agentcore",
        )
        output = self.directory / "github-output"
        release_evidence._write_github_output(
            output,
            target="agentcore",
            manifest=manifest,
        )
        values = dict(line.split("=", maxsplit=1) for line in output.read_text(encoding="utf-8").splitlines())
        self.assertEqual(values["target"], "agentcore")
        self.assertEqual(values["digest"], self.agentcore_digest)
        self.assertEqual(values["platform"], "linux/arm64")
        self.assertEqual(
            values["signing_key_arn"],
            self.signing_key_arn,
        )

    def test_standalone_targets_select_their_exact_platform_digest(self) -> None:
        self._create()
        for target, digest, platform in (
            (
                "standalone-amd64",
                self.standalone_amd64_digest,
                "linux/amd64",
            ),
            (
                "standalone-arm64",
                self.standalone_arm64_digest,
                "linux/arm64",
            ),
        ):
            with self.subTest(target=target):
                manifest = release_evidence.verify_evidence(
                    self.directory,
                    repository=self.repository,
                    commit=self.commit,
                    image_digest=digest,
                    require_release_tag=True,
                    signing_key_arn=self.signing_key_arn,
                    target=target,
                )
                self.assertEqual(
                    manifest["targets"][target]["platform"],
                    platform,
                )

    def test_tampered_agentcore_artifact_is_rejected_for_fargate(self) -> None:
        self._create()
        (self.directory / release_evidence.AGENTCORE_IMAGE_SBOM).write_text(
            '{"tampered": true}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "artifact digest mismatch",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
            )

    def test_tampered_signing_key_is_rejected(self) -> None:
        self._create()
        manifest_path = self.directory / release_evidence.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["signing"]["keyArn"] = "arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "signing identity does not match",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
            )

    def test_target_digest_cannot_be_used_for_other_target(self) -> None:
        self._create()
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "deployment image digest differs",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.agentcore_digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
                target="fargate",
            )

    def test_evidence_run_id_must_match_signed_source(self) -> None:
        self._create()
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "run ID does not match",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
                expected_run_id="456",
            )

    def test_agentcore_archive_must_be_linux_arm64(self) -> None:
        digest = _write_oci(
            self.directory / release_evidence.AGENTCORE_IMAGE_ARCHIVE,
            "amd64",
        )
        (self.directory / release_evidence.AGENTCORE_BUILD_METADATA).write_text(
            json.dumps({"containerimage.digest": digest}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "lacks a linux/arm64 image",
        ):
            self._create()

    def test_blocked_scan_finding_is_rejected(self) -> None:
        (self.directory / release_evidence.AGENTCORE_IMAGE_SCAN).write_text(
            json.dumps(
                {
                    "Results": [
                        {
                            "Target": "image",
                            "Vulnerabilities": [{"Severity": "CRITICAL"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "blocked findings",
        ):
            self._create()

    def test_workflow_identity_is_exact(self) -> None:
        self._create()
        manifest_path = self.directory / release_evidence.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["workflowRef"] = f"{self.repository}/.github/workflows/other.yml@{self.ref}"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "workflow ref must be",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
            )

    def test_non_release_ref_is_rejected_by_deployment_gate(self) -> None:
        self.ref = "refs/heads/main"
        self.workflow_ref = f"{self.repository}/{release_evidence.WORKFLOW_PATH}@{self.ref}"
        self._create()
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "must originate from a v\\* tag",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
                signing_key_arn=self.signing_key_arn,
            )


class EcrReferenceTests(unittest.TestCase):
    def test_parse_immutable_reference(self) -> None:
        digest = "sha256:" + "b" * 64
        image = verify_ecr_image.parse_reference(
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/axon/llm@" + digest,
            "us-east-1",
        )
        self.assertEqual(image.account_id, "123456789012")
        self.assertEqual(image.repository, "axon/llm")
        self.assertEqual(image.digest, digest)

    def test_mutable_tag_is_rejected(self) -> None:
        with self.assertRaises(verify_ecr_image.ImageVerificationError):
            verify_ecr_image.parse_reference(
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm:latest",
                "us-east-1",
            )


if __name__ == "__main__":
    unittest.main()
