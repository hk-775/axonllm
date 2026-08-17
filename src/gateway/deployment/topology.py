"""Deployment topology shared by setup, runtime, and infrastructure code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


AXONLLM_EXPERIENCE = "axonllm"
OSTIARI_EXPERIENCE = "ostiari"
CONTAINER_EXECUTION = "container"
AGENTCORE_EXECUTION = "agentcore"

STANDALONE = "standalone"
STANDALONE_AGENTCORE = "standalone-agentcore"
OSTIARI_EMBEDDED = "ostiari-embedded"
OSTIARI_AGENTCORE = "ostiari-agentcore"

DEPLOYMENT_PROFILES = (
    STANDALONE,
    STANDALONE_AGENTCORE,
    OSTIARI_EMBEDDED,
    OSTIARI_AGENTCORE,
)
AGENTCORE_DEPLOYMENT_PROFILES = (
    STANDALONE_AGENTCORE,
    OSTIARI_AGENTCORE,
)
CONTAINER_DEPLOYMENT_PROFILES = (
    STANDALONE,
    OSTIARI_EMBEDDED,
)

_PROFILE_BY_DIMENSIONS = {
    (AXONLLM_EXPERIENCE, CONTAINER_EXECUTION): STANDALONE,
    (AXONLLM_EXPERIENCE, AGENTCORE_EXECUTION): STANDALONE_AGENTCORE,
    (OSTIARI_EXPERIENCE, CONTAINER_EXECUTION): OSTIARI_EMBEDDED,
    (OSTIARI_EXPERIENCE, AGENTCORE_EXECUTION): OSTIARI_AGENTCORE,
}
_DIMENSIONS_BY_PROFILE = {profile: dimensions for dimensions, profile in _PROFILE_BY_DIMENSIONS.items()}


@dataclass(frozen=True)
class DeploymentTopology:
    """Normalized experience owner and execution target."""

    experience: str
    execution: str

    def __post_init__(self) -> None:
        if (self.experience, self.execution) not in _PROFILE_BY_DIMENSIONS:
            raise ValueError(
                "deployment must combine experience 'axonllm' or 'ostiari' with execution 'container' or 'agentcore'"
            )

    @property
    def profile(self) -> str:
        return _PROFILE_BY_DIMENSIONS[(self.experience, self.execution)]

    def to_dict(self) -> dict[str, str]:
        return {
            "experience": self.experience,
            "execution": self.execution,
        }

    @classmethod
    def from_profile(cls, profile: str) -> DeploymentTopology:
        try:
            experience, execution = _DIMENSIONS_BY_PROFILE[profile]
        except (KeyError, TypeError) as exc:
            raise ValueError("deployment profile must be one of: " + ", ".join(DEPLOYMENT_PROFILES)) from exc
        return cls(
            experience=experience,
            execution=execution,
        )

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        name: str = "deployment",
    ) -> DeploymentTopology:
        if not isinstance(raw, Mapping) or set(raw) != {
            "experience",
            "execution",
        }:
            raise ValueError(f"{name} must contain exactly experience and execution")
        experience = raw["experience"]
        execution = raw["execution"]
        if not isinstance(experience, str) or not isinstance(
            execution,
            str,
        ):
            raise ValueError(f"{name}.experience and {name}.execution must be strings")
        return cls(
            experience=experience,
            execution=execution,
        )


DEFAULT_STANDALONE_AGENTCORE_TOPOLOGY = DeploymentTopology.from_profile(STANDALONE_AGENTCORE)
