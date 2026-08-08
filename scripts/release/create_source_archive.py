#!/usr/bin/env python3
"""Create a deterministic gzip-compressed archive from a Git revision."""

from __future__ import annotations

import argparse
import gzip
import re
import shutil
import subprocess
import sys
from pathlib import Path


COMMIT = re.compile(r"^[0-9a-f]{40}$")


def create_archive(repository: Path, revision: str, output: Path) -> None:
    if not COMMIT.fullmatch(revision):
        raise ValueError("revision must be a full lowercase Git commit SHA")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"axonllm-{revision[:12]}/"
    process = subprocess.Popen(
        [
            "git",
            "-C",
            str(repository),
            "archive",
            "--format=tar",
            f"--prefix={prefix}",
            revision,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        with output.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                mtime=0,
            ) as compressed:
                shutil.copyfileobj(process.stdout, compressed)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        output.unlink(missing_ok=True)
        raise
    if return_code:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"git archive failed: {stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        create_archive(args.repository.resolve(), args.revision, args.output)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"source archive failed: {exc}", file=sys.stderr)
        return 1
    print(f"source archive created: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
