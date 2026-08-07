#!/usr/bin/env python3
"""Verify that a synthesized CDK Docker asset contains no local secret paths."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath


REQUIRED_PATHS = {
    ".dockerignore",
    "Dockerfile",
    "pyproject.toml",
    "site/index.html",
    "src/gateway/__init__.py",
    "uv.lock",
}
FORBIDDEN_PARTS = {
    ".aws",
    ".direnv",
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".tox",
    ".venv",
    "__pycache__",
    "cdk.out",
    "node_modules",
    "tests",
    "venv",
}
FORBIDDEN_NAMES = {
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "config/providers.yaml",
}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
PUBLIC_ENV_SUFFIXES = (".example", ".sample", ".template")


class AssetVerificationError(RuntimeError):
    """Raised when a synthesized Docker context violates release policy."""


def _is_forbidden(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return True
    if relative_path in FORBIDDEN_NAMES:
        return True
    if any(part.startswith(".bedrock_agentcore") for part in path.parts):
        return True
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    if path.name == ".env" or path.name.startswith(".env."):
        return not path.name.endswith(PUBLIC_ENV_SUFFIXES)
    return False


def _asset_manifest_path(out_dir: Path) -> Path:
    manifest_path = out_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetVerificationError(f"invalid CDK manifest: {manifest_path}") from exc

    asset_files = [
        artifact.get("properties", {}).get("file")
        for artifact in manifest.get("artifacts", {}).values()
        if artifact.get("type") == "cdk:asset-manifest"
    ]
    asset_files = [name for name in asset_files if isinstance(name, str)]
    if len(asset_files) != 1:
        raise AssetVerificationError(
            f"expected one CDK asset manifest, found {len(asset_files)}"
        )
    return out_dir / asset_files[0]


def verify_cdk_asset(out_dir: Path) -> int:
    out_dir = out_dir.resolve()
    asset_manifest_path = _asset_manifest_path(out_dir)
    try:
        asset_manifest = json.loads(asset_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetVerificationError(
            f"invalid asset manifest: {asset_manifest_path}"
        ) from exc

    docker_images = asset_manifest.get("dockerImages", {})
    if not isinstance(docker_images, dict) or len(docker_images) != 1:
        count = len(docker_images) if isinstance(docker_images, dict) else 0
        raise AssetVerificationError(f"expected one Docker asset, found {count}")

    image = next(iter(docker_images.values()))
    source_name = image.get("source", {}).get("directory")
    if not isinstance(source_name, str) or not source_name:
        raise AssetVerificationError("Docker asset has no source directory")

    source_dir = (out_dir / source_name).resolve()
    if not source_dir.is_relative_to(out_dir) or not source_dir.is_dir():
        raise AssetVerificationError("Docker asset source escapes the CDK output")

    included: set[str] = set()
    forbidden: list[str] = []
    symlinks: list[str] = []
    for path in source_dir.rglob("*"):
        relative = path.relative_to(source_dir).as_posix()
        if path.is_symlink():
            symlinks.append(relative)
            continue
        if path.is_file():
            included.add(relative)
            if _is_forbidden(relative):
                forbidden.append(relative)

    if symlinks:
        raise AssetVerificationError(
            f"CDK Docker asset contains symlinks: {', '.join(sorted(symlinks))}"
        )
    if forbidden:
        raise AssetVerificationError(
            "CDK Docker asset contains prohibited paths: "
            + ", ".join(sorted(forbidden))
        )

    missing = REQUIRED_PATHS - included
    if missing:
        raise AssetVerificationError(
            f"CDK Docker asset is missing required paths: {', '.join(sorted(missing))}"
        )

    return len(included)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()

    try:
        file_count = verify_cdk_asset(args.out_dir)
    except AssetVerificationError as exc:
        print(f"CDK asset verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"CDK Docker asset verified ({file_count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
