"""Fail-closed AxonLLM entrypoint for Bedrock AgentCore Runtime."""

import logging
from typing import Any

from starlette.exceptions import HTTPException

from src.gateway.agentcore.adapter import AgentCoreAdapter
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.runtime import RuntimeProvider
from src.gateway.agentcore.sdk_compat import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
logger = logging.getLogger(__name__)
_adapter = AgentCoreAdapter(RuntimeProvider())


@app.entrypoint
async def invoke(payload: Any, context: Any) -> Any:
    """Validate, authorize, and dispatch one AgentCore invocation."""
    try:
        return await _adapter.invoke(payload, context)
    except AgentCoreAdapterError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unhandled AgentCore invocation failure")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "internal_error",
                "message": "Internal server error.",
            },
        ) from exc


if __name__ == "__main__":
    app.run()
