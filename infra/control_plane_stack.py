"""Compatibility export for the packaged AgentCore control-plane stack."""

from pathlib import Path
import sys


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.gateway.deployment.infra.control_plane_stack import (  # noqa: E402
    AxonLLMControlPlaneStack,
)

__all__ = ["AxonLLMControlPlaneStack"]
