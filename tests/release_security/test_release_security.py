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
        self.workflow_ref = (
            f"{self.repository}/{release_evidence.WORKFLOW_PATH}@{self.ref}"
        )
        self.digest = self.digests["fargate"]
        self.agentcore_digest = self.digests["agentcore"]

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
        )

    def test_create_and_verify_release_evidence(self) -> None:
        self._create()
        manifest = release_evidence.verify_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            image_digest=self.digest,
            require_release_tag=True,
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
            manifest["source"]["workflowRef"],
            self.workflow_ref,
        )
        for target in release_evidence.TARGETS.values():
            self.assertIn(target.archive, manifest["artifacts"])
            self.assertIn(target.metadata, manifest["artifacts"])
            self.assertIn(target.scan, manifest["artifacts"])
            self.assertIn(target.sbom, manifest["artifacts"])
        provenance = json.loads(
            (self.directory / release_evidence.PROVENANCE).read_text(
                encoding="utf-8"
            )
        )
        parameters = provenance["predicate"]["buildDefinition"][
            "externalParameters"
        ]
        self.assertEqual(parameters["workflowRef"], self.workflow_ref)
        self.assertEqual(
            set(parameters["targets"]),
            {"fargate", "agentcore"},
        )

    def test_agentcore_target_selects_its_digest_and_bundle(self) -> None:
        self._create()
        manifest = release_evidence.verify_evidence(
            self.directory,
            repository=self.repository,
            commit=self.commit,
            image_digest=self.agentcore_digest,
            require_release_tag=True,
            target="agentcore",
        )
        output = self.directory / "github-output"
        release_evidence._write_github_output(
            output,
            target="agentcore",
            manifest=manifest,
        )
        values = dict(
            line.split("=", maxsplit=1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(values["target"], "agentcore")
        self.assertEqual(values["digest"], self.agentcore_digest)
        self.assertEqual(values["platform"], "linux/arm64")
        self.assertEqual(
            values["bundle"],
            release_evidence.AGENTCORE_IMAGE_BUNDLE,
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
            )

    def test_tampered_target_bundle_name_is_rejected(self) -> None:
        self._create()
        manifest_path = self.directory / release_evidence.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["targets"]["agentcore"]["attestationBundle"] = "caller.jsonl"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            release_evidence.EvidenceError,
            "target identity is malformed: agentcore",
        ):
            release_evidence.verify_evidence(
                self.directory,
                repository=self.repository,
                commit=self.commit,
                image_digest=self.digest,
                require_release_tag=True,
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
        manifest["source"]["workflowRef"] = (
            f"{self.repository}/.github/workflows/other.yml@{self.ref}"
        )
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
            )

    def test_non_release_ref_is_rejected_by_deployment_gate(self) -> None:
        self.ref = "refs/heads/main"
        self.workflow_ref = (
            f"{self.repository}/{release_evidence.WORKFLOW_PATH}@{self.ref}"
        )
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
