#!/usr/bin/env python3
"""Build content-addressed AxonLLM serverless control-plane artifacts."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

from src.gateway.deployment.serverless_artifacts import build_artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=_REPOSITORY)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--dependency-root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    try:
        receipt = build_artifacts(
            args.repository,
            args.output_directory,
            args.dependency_root,
            args.source_revision,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"serverless artifact build failed: {exc}", file=sys.stderr)
        return 1
    print(receipt.to_json().decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
