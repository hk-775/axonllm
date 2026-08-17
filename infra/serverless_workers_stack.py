"""Compatibility export for request-independent serverless workers."""

from pathlib import Path
import sys


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.gateway.deployment.infra.serverless_workers_stack import (  # noqa: E402
    AxonLLMServerlessWorkersStack,
)

__all__ = ["AxonLLMServerlessWorkersStack"]
