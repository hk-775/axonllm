#!/usr/bin/env python3
"""Validate an immutable private ECR reference and its remote manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ECR_REFERENCE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z]{2}(?:-gov)?-[a-z]+-[0-9])\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)


class ImageVerificationError(RuntimeError):
    """Raised when an ECR image does not match its immutable reference."""


@dataclass(frozen=True)
class EcrImage:
    account_id: str
    region: str
    repository: str
    digest: str

    @property
    def registry(self) -> str:
        return f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"


def parse_reference(reference: str, expected_region: str) -> EcrImage:
    match = ECR_REFERENCE.fullmatch(reference)
    if not match:
        raise ImageVerificationError(
            "image must be an immutable private ECR URI ending in @sha256:<digest>"
        )
    image = EcrImage(
        account_id=match.group("account"),
        region=match.group("region"),
        repository=match.group("repository"),
        digest=match.group("digest"),
    )
    if image.region != expected_region:
        raise ImageVerificationError(
            f"image region {image.region} does not match {expected_region}"
        )
    return image


def _aws_json(*arguments: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["aws", *arguments, "--output", "json", "--no-cli-pager"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ImageVerificationError("AWS ECR query failed") from exc
    if not isinstance(value, dict):
        raise ImageVerificationError("AWS ECR response must be an object")
    return value


def verify_remote(image: EcrImage) -> str:
    response = _aws_json(
        "ecr",
        "batch-get-image",
        "--region",
        image.region,
        "--repository-name",
        image.repository,
        "--image-ids",
        f"imageDigest={image.digest}",
        "--accepted-media-types",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
    failures = response.get("failures") or []
    images = response.get("images") or []
    if failures or len(images) != 1 or not isinstance(images[0], dict):
        raise ImageVerificationError("ECR did not return exactly one image manifest")
    result = images[0]
    if result.get("imageId", {}).get("imageDigest") != image.digest:
        raise ImageVerificationError("ECR returned a different image digest")
    manifest = result.get("imageManifest")
    if not isinstance(manifest, str):
        raise ImageVerificationError("ECR response lacks an image manifest")
    actual = "sha256:" + hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    if actual != image.digest:
        raise ImageVerificationError("ECR manifest bytes do not match image digest")
    media_type = result.get("imageManifestMediaType")
    if not isinstance(media_type, str) or not media_type:
        raise ImageVerificationError("ECR response lacks an image media type")
    return media_type


def _write_github_output(path: Path, image: EcrImage) -> None:
    values = {
        "account_id": image.account_id,
        "registry": image.registry,
        "repository": image.repository,
        "digest": image.digest,
    }
    with path.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image_reference")
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-digest")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--verify-remote", action="store_true")
    args = parser.parse_args()
    try:
        image = parse_reference(args.image_reference, args.region)
        if args.expected_digest and image.digest != args.expected_digest:
            raise ImageVerificationError(
                "ECR reference digest differs from release evidence"
            )
        if args.github_output:
            _write_github_output(args.github_output, image)
        if args.verify_remote:
            media_type = verify_remote(image)
            print(f"ECR image verified: {image.digest} ({media_type})")
        else:
            print(f"ECR reference verified: {image.digest}")
    except ImageVerificationError as exc:
        print(f"ECR image verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
