#!/usr/bin/env python3
"""Sign and verify release evidence with an asymmetric AWS KMS key."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Sequence


BUNDLE_SCHEMA = "https://axonllm.dev/schemas/kms-evidence-signature/v1"
SIGNING_ALGORITHM = "ECDSA_SHA_256"
MESSAGE_TYPE = "DIGEST"
AWS_TIMEOUT_SECONDS = 60
MAX_BUNDLE_BYTES = 64 * 1024
MAX_AWS_RESPONSE_BYTES = 64 * 1024

KEY_ARN = re.compile(
    r"^arn:(?:aws|aws-[a-z0-9-]+):kms:"
    r"(?P<region>[a-z0-9]+(?:-[a-z0-9]+)+):"
    r"[0-9]{12}:key/[A-Za-z0-9-]{1,256}$"
)
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class KmsEvidenceError(RuntimeError):
    """Raised when KMS evidence cannot be safely created or verified."""


def _key_region(key_arn: str) -> str:
    if not isinstance(key_arn, str):
        raise KmsEvidenceError("KMS key ARN must be a string")
    match = KEY_ARN.fullmatch(key_arn)
    if match is None:
        raise KmsEvidenceError("KMS key must be a full asymmetric key ARN, not an alias or key ID")
    return match.group("region")


def _open_regular(path: Path) -> tuple[BinaryIO, os.stat_result]:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise KmsEvidenceError(f"cannot inspect regular file: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise KmsEvidenceError(f"file must be regular and not a symlink: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KmsEvidenceError(f"cannot open regular file: {path}") from exc

    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            raise KmsEvidenceError(f"file changed while opening: {path}")
        return os.fdopen(descriptor, "rb"), opened_stat
    except Exception:
        os.close(descriptor)
        raise


def _ensure_unchanged(
    path: Path,
    initial: os.stat_result,
    final: os.stat_result,
    bytes_read: int,
) -> None:
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_dev != initial.st_dev
        or final.st_ino != initial.st_ino
        or final.st_size != initial.st_size
        or final.st_size != bytes_read
        or final.st_mtime_ns != initial.st_mtime_ns
    ):
        raise KmsEvidenceError(f"file changed while reading: {path}")


def _hash_regular_file(path: Path) -> tuple[bytes, int]:
    digest = hashlib.sha256()
    bytes_read = 0
    stream, initial = _open_regular(path)
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            bytes_read += len(chunk)
            digest.update(chunk)
        final = os.fstat(stream.fileno())
    except OSError as exc:
        raise KmsEvidenceError(f"cannot read regular file: {path}") from exc
    finally:
        stream.close()
    _ensure_unchanged(path, initial, final, bytes_read)
    return digest.digest(), bytes_read


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    stream, initial = _open_regular(path)
    try:
        if initial.st_size > maximum:
            raise KmsEvidenceError(f"file is too large: {path}")
        value = stream.read(maximum + 1)
        final = os.fstat(stream.fileno())
    except OSError as exc:
        raise KmsEvidenceError(f"cannot read regular file: {path}") from exc
    finally:
        stream.close()
    if len(value) > maximum:
        raise KmsEvidenceError(f"file is too large: {path}")
    _ensure_unchanged(path, initial, final, len(value))
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise KmsEvidenceError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _invalid_json_constant(value: str) -> None:
    raise KmsEvidenceError(f"invalid JSON constant: {value}")


def _parse_json(value: str, *, context: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_json_constant,
        )
    except (json.JSONDecodeError, KmsEvidenceError) as exc:
        raise KmsEvidenceError(f"{context} is not strict JSON") from exc


def _aws_json(*arguments: str) -> dict[str, Any]:
    command = [
        "aws",
        "kms",
        *arguments,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=AWS_TIMEOUT_SECONDS,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        UnicodeError,
    ) as exc:
        raise KmsEvidenceError("AWS KMS CLI request failed") from exc
    if not isinstance(completed.stdout, str):
        raise KmsEvidenceError("AWS KMS response must be UTF-8 text")
    if len(completed.stdout.encode("utf-8")) > MAX_AWS_RESPONSE_BYTES:
        raise KmsEvidenceError("AWS KMS response is too large")
    response = _parse_json(completed.stdout, context="AWS KMS response")
    if not isinstance(response, dict):
        raise KmsEvidenceError("AWS KMS response must be a JSON object")
    return response


def _decode_signature(value: Any, *, context: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 16 * 1024:
        raise KmsEvidenceError(f"{context} signature is not valid base64")
    try:
        signature = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise KmsEvidenceError(f"{context} signature is not valid base64") from exc
    if not signature or base64.b64encode(signature).decode("ascii") != value:
        raise KmsEvidenceError(f"{context} signature is not canonical base64")
    return signature


def _validate_sign_response(
    response: dict[str, Any],
    *,
    expected_key_arn: str,
) -> bytes:
    if response.get("KeyId") != expected_key_arn:
        raise KmsEvidenceError("AWS KMS returned a different key ARN")
    if response.get("SigningAlgorithm") != SIGNING_ALGORITHM:
        raise KmsEvidenceError("AWS KMS returned a different signing algorithm")
    return _decode_signature(response.get("Signature"), context="AWS KMS")


def _validate_verify_response(
    response: dict[str, Any],
    *,
    expected_key_arn: str,
) -> None:
    if response.get("KeyId") != expected_key_arn:
        raise KmsEvidenceError("AWS KMS returned a different key ARN")
    if response.get("SigningAlgorithm") != SIGNING_ALGORITHM:
        raise KmsEvidenceError("AWS KMS returned a different signing algorithm")
    signature_valid = response.get("SignatureValid")
    if signature_valid is False:
        raise KmsEvidenceError("AWS KMS reported an invalid signature")
    if signature_valid is not True:
        raise KmsEvidenceError("AWS KMS returned malformed verification status")


def _blob_uri(path: Path) -> str:
    return f"fileb://{path}"


def _kms_sign(digest: bytes, *, key_arn: str, region: str) -> bytes:
    try:
        with tempfile.TemporaryDirectory(prefix="axonllm-kms-sign-") as directory:
            digest_path = Path(directory) / "digest.bin"
            digest_path.write_bytes(digest)
            response = _aws_json(
                "sign",
                "--region",
                region,
                "--key-id",
                key_arn,
                "--message",
                _blob_uri(digest_path),
                "--message-type",
                MESSAGE_TYPE,
                "--signing-algorithm",
                SIGNING_ALGORITHM,
            )
    except OSError as exc:
        raise KmsEvidenceError("cannot prepare KMS signing request") from exc
    return _validate_sign_response(response, expected_key_arn=key_arn)


def _kms_verify(
    digest: bytes,
    signature: bytes,
    *,
    key_arn: str,
    region: str,
) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="axonllm-kms-verify-") as directory:
            temporary = Path(directory)
            digest_path = temporary / "digest.bin"
            signature_path = temporary / "signature.bin"
            digest_path.write_bytes(digest)
            signature_path.write_bytes(signature)
            response = _aws_json(
                "verify",
                "--region",
                region,
                "--key-id",
                key_arn,
                "--message",
                _blob_uri(digest_path),
                "--message-type",
                MESSAGE_TYPE,
                "--signature",
                _blob_uri(signature_path),
                "--signing-algorithm",
                SIGNING_ALGORITHM,
            )
    except OSError as exc:
        raise KmsEvidenceError("cannot prepare KMS verification request") from exc
    _validate_verify_response(response, expected_key_arn=key_arn)


def _require_fields(
    value: Any,
    expected: set[str],
    *,
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise KmsEvidenceError(f"{context} fields do not match schema")
    return value


def _validate_bundle(
    value: Any,
    *,
    expected_key_arn: str,
) -> tuple[str, int, bytes]:
    bundle = _require_fields(
        value,
        {"schema", "artifact", "signature"},
        context="bundle",
    )
    if bundle["schema"] != BUNDLE_SCHEMA:
        raise KmsEvidenceError("unsupported evidence signature schema")

    artifact = _require_fields(
        bundle["artifact"],
        {"sha256", "size"},
        context="artifact",
    )
    digest = artifact["sha256"]
    size = artifact["size"]
    if not isinstance(digest, str) or SHA256_HEX.fullmatch(digest) is None:
        raise KmsEvidenceError("artifact SHA-256 digest is malformed")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise KmsEvidenceError("artifact size is malformed")

    signature = _require_fields(
        bundle["signature"],
        {"keyArn", "messageType", "signingAlgorithm", "value"},
        context="signature",
    )
    if signature["keyArn"] != expected_key_arn:
        raise KmsEvidenceError("bundle KMS key ARN does not match expected key")
    if signature["messageType"] != MESSAGE_TYPE:
        raise KmsEvidenceError("bundle KMS message type is not DIGEST")
    if signature["signingAlgorithm"] != SIGNING_ALGORITHM:
        raise KmsEvidenceError("bundle KMS signing algorithm is not ECDSA_SHA_256")
    decoded_signature = _decode_signature(
        signature["value"],
        context="bundle",
    )
    return digest, size, decoded_signature


def _ensure_distinct(artifact: Path, bundle: Path) -> None:
    if os.path.abspath(artifact) == os.path.abspath(bundle):
        raise KmsEvidenceError("artifact and bundle paths must be different")
    try:
        if artifact.samefile(bundle):
            raise KmsEvidenceError("artifact and bundle paths must be different")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise KmsEvidenceError("cannot compare artifact and bundle paths") from exc


def _atomic_write_bundle(path: Path, value: dict[str, Any]) -> None:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise KmsEvidenceError(f"cannot inspect bundle output: {path}") from exc
    if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        raise KmsEvidenceError(f"bundle output must be regular and not a symlink: {path}")

    encoded = (json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n").encode("utf-8")
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise KmsEvidenceError(f"cannot atomically write bundle: {path}") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def sign_artifact(
    artifact: Path,
    bundle: Path,
    expected_key_arn: str,
) -> dict[str, Any]:
    """Sign an artifact digest and atomically write its evidence bundle."""

    artifact = Path(artifact)
    bundle = Path(bundle)
    _ensure_distinct(artifact, bundle)
    region = _key_region(expected_key_arn)
    digest, size = _hash_regular_file(artifact)
    signature = _kms_sign(
        digest,
        key_arn=expected_key_arn,
        region=region,
    )
    value = {
        "schema": BUNDLE_SCHEMA,
        "artifact": {
            "sha256": digest.hex(),
            "size": size,
        },
        "signature": {
            "keyArn": expected_key_arn,
            "messageType": MESSAGE_TYPE,
            "signingAlgorithm": SIGNING_ALGORITHM,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }
    _atomic_write_bundle(bundle, value)
    return value


def verify_artifact(
    artifact: Path,
    bundle: Path,
    expected_key_arn: str,
) -> None:
    """Verify an artifact against a strict KMS evidence signature bundle."""

    artifact = Path(artifact)
    bundle = Path(bundle)
    _ensure_distinct(artifact, bundle)
    region = _key_region(expected_key_arn)
    try:
        bundle_bytes = _read_regular_file(bundle, maximum=MAX_BUNDLE_BYTES)
        bundle_text = bundle_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise KmsEvidenceError("bundle is not valid UTF-8") from exc
    value = _parse_json(bundle_text, context="bundle")
    expected_digest, expected_size, signature = _validate_bundle(
        value,
        expected_key_arn=expected_key_arn,
    )
    digest, size = _hash_regular_file(artifact)
    if size != expected_size or digest.hex() != expected_digest:
        raise KmsEvidenceError("artifact digest or size does not match bundle")
    _kms_verify(
        digest,
        signature,
        key_arn=expected_key_arn,
        region=region,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sign or verify an artifact digest with AWS KMS.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("sign", "verify"):
        subparser = subcommands.add_parser(command)
        subparser.add_argument("--artifact", required=True, type=Path)
        subparser.add_argument("--bundle", required=True, type=Path)
        subparser.add_argument("--key-arn", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "sign":
            sign_artifact(args.artifact, args.bundle, args.key_arn)
            print(f"KMS evidence signature written: {args.bundle}")
        else:
            verify_artifact(args.artifact, args.bundle, args.key_arn)
            print(f"KMS evidence signature verified: {args.artifact}")
    except KmsEvidenceError as exc:
        print(f"KMS evidence operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
