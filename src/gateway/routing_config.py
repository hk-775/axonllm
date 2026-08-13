"""Versioned, credential-free routing configuration snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.gateway.model_registry import ModelRegistry

ROUTING_CONFIG_SCHEMA = "axonllm.routing-config/v1"


@dataclass(frozen=True)
class RoutingConfigSnapshot:
    """An immutable control-plane snapshot consumed by router instances.

    This document owns logical models, provider mappings, and routing policy.
    Provider endpoints and credentials remain deployment-owned secrets and are
    intentionally excluded.
    """

    revision: int
    document: str
    sha256: str

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        revision: int,
    ) -> RoutingConfigSnapshot:
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError(
                "routing configuration revision must be non-negative"
            )
        ModelRegistry.from_config(config, revision=revision)
        document = json.dumps(
            config,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return cls(
            revision=revision,
            document=document,
            sha256=hashlib.sha256(
                document.encode("utf-8")
            ).hexdigest(),
        )

    @classmethod
    def from_registry(
        cls,
        registry: ModelRegistry,
    ) -> RoutingConfigSnapshot:
        return cls.from_config(
            registry.to_config(),
            revision=registry.revision,
        )

    @property
    def config(self) -> dict[str, Any]:
        """Return a detached copy safe for validation and atomic adoption."""
        value = json.loads(self.document)
        if not isinstance(value, dict):
            raise RuntimeError(
                "routing configuration document is malformed"
            )
        return value

    def apply(self, registry: ModelRegistry) -> None:
        """Atomically replace one live registry with this validated snapshot."""
        registry.replace_config(
            self.config,
            revision=self.revision,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ROUTING_CONFIG_SCHEMA,
            "revision": self.revision,
            "sha256": self.sha256,
            "config": self.config,
        }
