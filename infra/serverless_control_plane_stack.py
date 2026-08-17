"""Compatibility export for the serverless AgentCore control plane."""

from pathlib import Path
import sys


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.gateway.deployment.infra.serverless_control_plane_stack import (  # noqa: E402
    AxonLLMServerlessControlPlaneStack,
)

__all__ = ["AxonLLMServerlessControlPlaneStack"]
