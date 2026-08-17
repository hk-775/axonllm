"""Compatibility export for the packaged application-state CDK stack."""

from pathlib import Path
import sys


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.gateway.deployment.infra.application_state_stack import (
    AxonLLMApplicationStateStack,
)


__all__ = ["AxonLLMApplicationStateStack"]
