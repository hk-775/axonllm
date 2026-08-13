#!/usr/bin/env python3
"""Compatibility launcher for the release-foundation bootstrap command."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.gateway.deployment.release_foundation_bootstrap import main


if __name__ == "__main__":
    raise SystemExit(main())
