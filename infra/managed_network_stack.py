"""Compatibility export for the packaged AgentCore managed network."""

from pathlib import Path
import sys


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.gateway.deployment.infra.managed_network_stack import (  # noqa: E402
    AxonLLMManagedNetworkStack,
)

__all__ = ["AxonLLMManagedNetworkStack"]
