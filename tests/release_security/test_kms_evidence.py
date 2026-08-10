from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "release"))

import kms_evidence  # noqa: E402


KEY_ARN = "arn:aws:kms:us-west-2:123456789012:key/11111111-2222-3333-4444-555555555555"
OTHER_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SIGNATURE = b"\x30\x44\x02\x20" + (b"\x01" * 64)


def _completed(value: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(value),
        stderr="",
    )


class KmsEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.artifact = self.directory / "release-evidence.tar.gz"
        self.bundle = self.directory / "release-evidence.kms.json"
        self.contents = b"immutable release evidence\n"
        self.artifact.write_bytes(self.contents)

    def _sign_response(
        self,
        *,
        key_arn: str = KEY_ARN,
        algorithm: str = kms_evidence.SIGNING_ALGORITHM,
        signature: str | None = None,
    ) -> dict[str, object]:
        return {
            "KeyId": key_arn,
            "SigningAlgorithm": algorithm,
            "Signature": (signature if signature is not None else base64.b64encode(SIGNATURE).decode("ascii")),
        }

    def _verify_response(
        self,
        *,
        key_arn: str = KEY_ARN,
        algorithm: str = kms_evidence.SIGNING_ALGORITHM,
        valid: object = True,
    ) -> dict[str, object]:
        return {
            "KeyId": key_arn,
            "SigningAlgorithm": algorithm,
            "SignatureValid": valid,
        }

    def _bundle_value(
        self,
        *,
        contents: bytes | None = None,
        key_arn: str = KEY_ARN,
        signature: str | None = None,
    ) -> dict[str, object]:
        artifact = self.contents if contents is None else contents
        return {
            "schema": kms_evidence.BUNDLE_SCHEMA,
            "artifact": {
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "size": len(artifact),
            },
            "signature": {
                "keyArn": key_arn,
                "messageType": kms_evidence.MESSAGE_TYPE,
                "signingAlgorithm": kms_evidence.SIGNING_ALGORITHM,
                "value": (signature if signature is not None else base64.b64encode(SIGNATURE).decode("ascii")),
            },
        }

    def _write_bundle(self, value: object | None = None) -> None:
        if value is None:
            value = self._bundle_value()
        self.bundle.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def _blob_path(command: list[str], option: str) -> Path:
        value = command[command.index(option) + 1]
        if not value.startswith("fileb://"):
            raise AssertionError(f"{option} does not use fileb://")
        return Path(value.removeprefix("fileb://"))

    def test_sign_and_verify_success(self) -> None:
        sign_commands: list[list[str]] = []

        def sign(command: list[str], **kwargs: object) -> object:
            sign_commands.append(command)
            self.assertEqual(command[:3], ["aws", "kms", "sign"])
            self.assertEqual(
                command[command.index("--region") + 1],
                "us-west-2",
            )
            self.assertEqual(
                command[command.index("--key-id") + 1],
                KEY_ARN,
            )
            self.assertEqual(
                command[command.index("--message-type") + 1],
                "DIGEST",
            )
            self.assertEqual(
                command[command.index("--signing-algorithm") + 1],
                "ECDSA_SHA_256",
            )
            self.assertEqual(
                self._blob_path(command, "--message").read_bytes(),
                hashlib.sha256(self.contents).digest(),
            )
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 60)
            return _completed(self._sign_response())

        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            side_effect=sign,
        ):
            value = kms_evidence.sign_artifact(
                self.artifact,
                self.bundle,
                KEY_ARN,
            )

        self.assertEqual(value, json.loads(self.bundle.read_text("utf-8")))
        self.assertEqual(
            set(value),
            {"schema", "artifact", "signature"},
        )
        self.assertEqual(len(sign_commands), 1)
        self.assertEqual(
            list(self.directory.glob(f".{self.bundle.name}.*.tmp")),
            [],
        )

        def verify(command: list[str], **kwargs: object) -> object:
            self.assertEqual(command[:3], ["aws", "kms", "verify"])
            self.assertEqual(
                command[command.index("--region") + 1],
                "us-west-2",
            )
            self.assertEqual(
                self._blob_path(command, "--message").read_bytes(),
                hashlib.sha256(self.contents).digest(),
            )
            self.assertEqual(
                self._blob_path(command, "--signature").read_bytes(),
                SIGNATURE,
            )
            return _completed(self._verify_response())

        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            side_effect=verify,
        ) as run:
            kms_evidence.verify_artifact(
                self.artifact,
                self.bundle,
                KEY_ARN,
            )
        run.assert_called_once()

    def test_tampered_artifact_fails_before_kms_verify(self) -> None:
        self._write_bundle()
        self.artifact.write_bytes(b"tampered\n")
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "does not match bundle",
            ):
                kms_evidence.verify_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )
        run.assert_not_called()

    def test_wrong_expected_key_fails_before_kms_verify(self) -> None:
        self._write_bundle()
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "does not match expected key",
            ):
                kms_evidence.verify_artifact(
                    self.artifact,
                    self.bundle,
                    OTHER_KEY_ARN,
                )
        run.assert_not_called()

    def test_kms_response_with_wrong_key_is_rejected(self) -> None:
        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            return_value=_completed(self._sign_response(key_arn=OTHER_KEY_ARN)),
        ):
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "different key ARN",
            ):
                kms_evidence.sign_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )
        self.assertFalse(self.bundle.exists())

    def test_malformed_bundles_are_rejected_before_kms(self) -> None:
        malformed_values = {
            "extra field": {
                **self._bundle_value(),
                "unexpected": True,
            },
            "wrong schema": {
                **self._bundle_value(),
                "schema": "https://axonllm.dev/schemas/kms-evidence-signature/v2",
            },
            "boolean size": {
                **self._bundle_value(),
                "artifact": {
                    "sha256": hashlib.sha256(self.contents).hexdigest(),
                    "size": True,
                },
            },
            "bad digest": {
                **self._bundle_value(),
                "artifact": {"sha256": "ABC", "size": len(self.contents)},
            },
            "bad base64": self._bundle_value(signature="not base64!"),
        }
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            for name, value in malformed_values.items():
                with self.subTest(name=name):
                    self._write_bundle(value)
                    with self.assertRaises(kms_evidence.KmsEvidenceError):
                        kms_evidence.verify_artifact(
                            self.artifact,
                            self.bundle,
                            KEY_ARN,
                        )
        run.assert_not_called()

    def test_invalid_and_duplicate_json_bundles_are_rejected(self) -> None:
        invalid_documents = (
            "{",
            '{"schema":"one","schema":"two"}',
            '{"schema":NaN}',
        )
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            for document in invalid_documents:
                with self.subTest(document=document):
                    self.bundle.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(
                        kms_evidence.KmsEvidenceError,
                        "not strict JSON",
                    ):
                        kms_evidence.verify_artifact(
                            self.artifact,
                            self.bundle,
                            KEY_ARN,
                        )
        run.assert_not_called()

    def test_malformed_sign_responses_are_rejected(self) -> None:
        responses: tuple[object, ...] = (
            [],
            {},
            self._sign_response(algorithm="RSASSA_PSS_SHA_256"),
            self._sign_response(signature="%%%not-base64%%%"),
        )
        for response in responses:
            with self.subTest(response=response):
                with mock.patch.object(
                    kms_evidence.subprocess,
                    "run",
                    return_value=_completed(response),
                ):
                    with self.assertRaises(kms_evidence.KmsEvidenceError):
                        kms_evidence.sign_artifact(
                            self.artifact,
                            self.bundle,
                            KEY_ARN,
                        )
                self.assertFalse(self.bundle.exists())

    def test_non_json_aws_response_is_rejected(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="{",
            stderr="",
        )
        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "not strict JSON",
            ):
                kms_evidence.sign_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )

    def test_malformed_verify_responses_are_rejected(self) -> None:
        self._write_bundle()
        responses: tuple[object, ...] = (
            [],
            {},
            self._verify_response(key_arn=OTHER_KEY_ARN),
            self._verify_response(algorithm="RSASSA_PSS_SHA_256"),
            self._verify_response(valid=None),
        )
        for response in responses:
            with self.subTest(response=response):
                with mock.patch.object(
                    kms_evidence.subprocess,
                    "run",
                    return_value=_completed(response),
                ):
                    with self.assertRaises(kms_evidence.KmsEvidenceError):
                        kms_evidence.verify_artifact(
                            self.artifact,
                            self.bundle,
                            KEY_ARN,
                        )

    def test_non_text_aws_response_is_rejected(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"{}",
            stderr=b"",
        )
        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "must be UTF-8 text",
            ):
                kms_evidence.sign_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )

    def test_subprocess_failure_preserves_existing_bundle(self) -> None:
        self.bundle.write_text("previous bundle\n", encoding="utf-8")
        failure = subprocess.CalledProcessError(
            returncode=1,
            cmd=["aws", "kms", "sign"],
        )
        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "CLI request failed",
            ):
                kms_evidence.sign_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )
        self.assertEqual(
            self.bundle.read_text(encoding="utf-8"),
            "previous bundle\n",
        )

    def test_symlink_artifacts_are_rejected_for_sign_and_verify(self) -> None:
        symlink = self.directory / "artifact-link"
        symlink.symlink_to(self.artifact)
        self._write_bundle()
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            for operation in (
                lambda: kms_evidence.sign_artifact(
                    symlink,
                    self.directory / "new-bundle.json",
                    KEY_ARN,
                ),
                lambda: kms_evidence.verify_artifact(
                    symlink,
                    self.bundle,
                    KEY_ARN,
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(
                        kms_evidence.KmsEvidenceError,
                        "not a symlink",
                    ):
                        operation()
        run.assert_not_called()

    def test_symlink_bundle_is_rejected(self) -> None:
        real_bundle = self.directory / "real-bundle.json"
        real_bundle.write_text(
            json.dumps(self._bundle_value()),
            encoding="utf-8",
        )
        self.bundle.symlink_to(real_bundle)
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "not a symlink",
            ):
                kms_evidence.verify_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )
        run.assert_not_called()

    def test_false_kms_verification_is_rejected(self) -> None:
        self._write_bundle()
        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            return_value=_completed(self._verify_response(valid=False)),
        ):
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "invalid signature",
            ):
                kms_evidence.verify_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )

    def test_non_boolean_kms_verification_is_rejected(self) -> None:
        self._write_bundle()
        with mock.patch.object(
            kms_evidence.subprocess,
            "run",
            return_value=_completed(self._verify_response(valid=1)),
        ):
            with self.assertRaisesRegex(
                kms_evidence.KmsEvidenceError,
                "malformed verification status",
            ):
                kms_evidence.verify_artifact(
                    self.artifact,
                    self.bundle,
                    KEY_ARN,
                )

    def test_key_must_be_full_key_arn(self) -> None:
        invalid_keys = (
            "alias/release-signing",
            "11111111-2222-3333-4444-555555555555",
            "arn:aws:kms:us-west-2:123456789012:alias/release",
        )
        with mock.patch.object(kms_evidence.subprocess, "run") as run:
            for key in invalid_keys:
                with self.subTest(key=key):
                    with self.assertRaisesRegex(
                        kms_evidence.KmsEvidenceError,
                        "full asymmetric key ARN",
                    ):
                        kms_evidence.sign_artifact(
                            self.artifact,
                            self.bundle,
                            key,
                        )
        run.assert_not_called()

    def test_cli_sign_and_verify_subcommands(self) -> None:
        arguments = [
            "--artifact",
            str(self.artifact),
            "--bundle",
            str(self.bundle),
            "--key-arn",
            KEY_ARN,
        ]
        with (
            mock.patch.object(kms_evidence, "sign_artifact") as sign,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(kms_evidence.main(["sign", *arguments]), 0)
        sign.assert_called_once_with(self.artifact, self.bundle, KEY_ARN)

        with (
            mock.patch.object(kms_evidence, "verify_artifact") as verify,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(kms_evidence.main(["verify", *arguments]), 0)
        verify.assert_called_once_with(self.artifact, self.bundle, KEY_ARN)


if __name__ == "__main__":
    unittest.main()
