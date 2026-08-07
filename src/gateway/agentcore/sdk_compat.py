"""Optional Bedrock AgentCore SDK import support.

The adapter's domain modules remain importable for local tests and tooling when
the deployment-only SDK is absent. Running the service still requires the SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
except ModuleNotFoundError as exc:
    if exc.name and not exc.name.startswith("bedrock_agentcore"):
        raise
    _SDK_IMPORT_ERROR = exc

    class BedrockAgentCoreApp:  # type: ignore[no-redef]
        """Minimal decorator-compatible placeholder used without the SDK."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.handlers: dict[str, Callable[..., Any]] = {}

        def entrypoint(
            self,
            function: Callable[..., Any],
        ) -> Callable[..., Any]:
            self.handlers["main"] = function
            return function

        def run(self, *args: Any, **kwargs: Any) -> NoReturn:
            raise RuntimeError("bedrock-agentcore is required to run the AgentCore entrypoint") from _SDK_IMPORT_ERROR


__all__ = ["BedrockAgentCoreApp"]
