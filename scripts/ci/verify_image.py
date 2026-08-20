#!/usr/bin/env python3
"""Inspect and smoke-test the production image without credentials or networking."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


_READINESS_PROBE = """
import http.client

connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=1.0)
try:
    connection.request("GET", "/ready")
    status = connection.getresponse().status
except (OSError, http.client.HTTPException):
    status = 0
finally:
    connection.close()
raise SystemExit(0 if status == 200 else 1)
"""


def _replica_ready(name: str) -> bool:
    result = subprocess.run(
        ("docker", "exec", name, "python", "-c", _READINESS_PROBE),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _replica_logs(name: str) -> str:
    result = subprocess.run(
        ("docker", "logs", "--tail", "80", name),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout


def _verify_two_replica_startup(
    image: str,
    timeout_seconds: float = 30.0,
) -> None:
    """Require two hardened replicas to bind without external network access."""
    suffix = f"{os.getpid()}-{time.time_ns()}"
    network = f"axonllm-ci-{suffix}"
    names = [f"axonllm-ci-a-{suffix}", f"axonllm-ci-b-{suffix}"]
    started: list[str] = []

    _run("docker", "network", "create", "--internal", network)
    try:
        started_at = time.monotonic()
        for name in names:
            _run(
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--env",
                "AXON_AUTH_MODE=LOG_ONLY",
                "--env",
                "AXON_DEPLOYMENT_PROFILE=development",
                "--env",
                "AXON_REQUIRE_CANONICAL_IDENTITY=false",
                "--env",
                "AXON_LOAD_DEMO_DATA=false",
                "--env",
                "AXON_CHECK_MODEL_AVAILABILITY=true",
                "--env",
                "LLM_ROUTER_DYNAMODB_ENABLED=false",
                "--env",
                "AXON_NO_BROWSER=true",
                "--env",
                "AXON_SERVER_HOST=0.0.0.0",
                "--env",
                "AXON_SERVER_PORT=8000",
                "--env",
                "AWS_DEFAULT_REGION=us-east-1",
                "--env",
                "AWS_ACCESS_KEY_ID=ci-fake-access-key",
                "--env",
                "AWS_SECRET_ACCESS_KEY=ci-fake-secret-key",
                "--env",
                "AWS_EC2_METADATA_DISABLED=true",
                "--env",
                "HOME=/tmp",
                image,
            )
            started.append(name)

        deadline = started_at + timeout_seconds
        pending = set(names)
        while pending and time.monotonic() < deadline:
            for name in tuple(pending):
                running = _run(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    name,
                ).stdout.strip()
                if running != "true":
                    raise RuntimeError(
                        f"replica {name} exited before readiness:\n"
                        f"{_replica_logs(name)}"
                    )
                if _replica_ready(name):
                    pending.remove(name)
            if pending:
                time.sleep(0.25)

        if pending:
            details = "\n".join(
                f"--- {name} ---\n{_replica_logs(name)}"
                for name in sorted(pending)
            )
            raise RuntimeError(
                "two-replica startup exceeded "
                f"{timeout_seconds:.0f}s for {sorted(pending)}:\n{details}"
            )
        print(
            "two-replica startup verified in "
            f"{time.monotonic() - started_at:.2f}s"
        )
    finally:
        if started:
            subprocess.run(
                ("docker", "rm", "--force", *started),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ("docker", "network", "rm", network),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def verify_image(
    image: str,
    platform: str = "linux/amd64",
    *,
    verify_startup: bool = False,
) -> None:
    inspection = json.loads(_run("docker", "image", "inspect", image).stdout)[0]
    config = inspection.get("Config", {})

    user = config.get("User", "")
    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)", user)
    if not match:
        raise RuntimeError(f"image has invalid non-root UID:GID: {user!r}")
    try:
        expected_os, expected_architecture = platform.split("/", maxsplit=1)
    except ValueError as exc:
        raise RuntimeError(f"invalid expected platform: {platform}") from exc
    if inspection.get("Os") != expected_os or inspection.get("Architecture") != expected_architecture:
        raise RuntimeError(f"release image must target {platform}")

    environment = set(config.get("Env") or [])
    required_environment = {
        "AXON_AUTH_MODE=ENFORCE",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "UV_NO_CACHE=1",
    }
    missing_environment = required_environment - environment
    if missing_environment:
        raise RuntimeError(f"image lacks required environment: {sorted(missing_environment)}")

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
    _run(
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "--entrypoint",
        "sh",
        image,
        "-c",
        "test ! -e /root/.cache/uv",
    )
    if verify_startup:
        _verify_two_replica_startup(image)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument(
        "--platform",
        choices=("linux/amd64", "linux/arm64"),
        default="linux/amd64",
    )
    parser.add_argument(
        "--verify-startup",
        action="store_true",
        help="require two isolated replicas to reach /ready within 30 seconds",
    )
    args = parser.parse_args()

    try:
        verify_image(
            args.image,
            args.platform,
            verify_startup=args.verify_startup,
        )
    except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"image verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"image verified: {args.image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
