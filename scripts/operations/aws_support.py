"""Small AWS CLI adapter shared by operational validation scripts."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any


class AwsError(RuntimeError):
    """Raised when an AWS CLI request fails or returns malformed JSON."""


class AwsCli:
    def __init__(self, region: str) -> None:
        self.region = region

    def json(self, service: str, operation: str, *arguments: str) -> dict[str, Any]:
        command = [
            "aws",
            service,
            operation,
            *arguments,
            "--region",
            self.region,
            "--output",
            "json",
            "--no-cli-pager",
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            value = json.loads(completed.stdout)
        except OSError as exc:
            raise AwsError("AWS CLI is not installed or executable") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip().splitlines()
            detail = message[-1] if message else "unknown AWS CLI error"
            raise AwsError(f"{service} {operation} failed: {detail}") from exc
        except json.JSONDecodeError as exc:
            raise AwsError(f"{service} {operation} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AwsError(f"{service} {operation} returned a non-object response")
        return value


def parse_aws_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise AwsError(f"{label} is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AwsError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AwsError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)
