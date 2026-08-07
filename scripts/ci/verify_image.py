#!/usr/bin/env python3
"""Inspect and smoke-test the production image without credentials or networking."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def verify_image(image: str) -> None:
    inspection = json.loads(_run("docker", "image", "inspect", image).stdout)[0]
    config = inspection.get("Config", {})

    user = config.get("User", "")
    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)", user)
    if not match:
        raise RuntimeError(f"image has invalid non-root UID:GID: {user!r}")
    if inspection.get("Os") != "linux" or inspection.get("Architecture") != "amd64":
        raise RuntimeError("release image must target linux/amd64")

    environment = set(config.get("Env") or [])
    required_environment = {
        "AXON_AUTH_MODE=ENFORCE",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
    }
    missing_environment = required_environment - environment
    if missing_environment:
        raise RuntimeError(
            f"image lacks required environment: {sorted(missing_environment)}"
        )

    smoke_test = r"""
import os
from pathlib import Path

assert os.getuid() != 0
assert os.getgid() != 0
assert Path.cwd() == Path("/app")
assert not os.access("/app", os.W_OK)

required = (
    "/app/src/gateway/__init__.py",
    "/app/config/models.yaml",
    "/app/site/index.html",
)
for path in required:
    assert Path(path).is_file(), path

forbidden = (
    "/app/.env",
    "/app/.git",
    "/app/.bedrock_agentcore.yaml",
    "/app/config/providers.yaml",
    "/app/infra",
    "/app/tests",
)
for path in forbidden:
    assert not Path(path).exists(), path

import src.gateway  # noqa: F401
"""
    _run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--entrypoint",
        "python",
        image,
        "-c",
        smoke_test,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()

    try:
        verify_image(args.image)
    except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"image verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"image verified: {args.image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
