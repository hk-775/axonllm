"""Compatibility exports for the packaged AgentCore CDK stack."""

from pathlib import Path
import sys


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.gateway.deployment.infra.agentcore_stack import (  # noqa: E402
    AxonLLMAgentCoreStack,
    load_athena_infrastructure_config,
)

__all__ = [
    "AxonLLMAgentCoreStack",
    "load_athena_infrastructure_config",
]
