#!/usr/bin/env python3
"""Verify AxonLLM serverless control-plane artifacts and their receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

from src.gateway.deployment.serverless_artifacts import verify_artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    try:
        receipt = verify_artifacts(
            args.directory,
            expected_source_revision=args.source_revision,
        )
    except (OSError, ValueError) as exc:
        print(f"serverless artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print(receipt.to_json().decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
