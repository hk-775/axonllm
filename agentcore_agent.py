"""Fail-closed AxonLLM entrypoint for Bedrock AgentCore Runtime."""

import contextlib
import logging
from typing import Any

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from src.gateway.agentcore.adapter import AgentCoreAdapter
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.runtime import RuntimeProvider
from src.gateway.agentcore.sdk_compat import BedrockAgentCoreApp

logger = logging.getLogger(__name__)
_adapter = AgentCoreAdapter(RuntimeProvider())


@contextlib.asynccontextmanager
async def _lifespan(_app: Any):
    try:
        await _adapter.initialize()
        logger.info("AgentCore runtime initialized and ready")
        yield
    finally:
        await _adapter.close()


app = BedrockAgentCoreApp(lifespan=_lifespan)


async def readiness(_request: Any) -> JSONResponse:
    """Report dependency readiness independently from AgentCore liveness."""
    try:
        report = await _adapter.readiness()
    except Exception:
        logger.exception("AgentCore readiness check failed")
        report = {
            "status": "not_ready",
            "ready": False,
            "state": "failed",
            "dependencies": {"runtime": "readiness_failed"},
        }
    return JSONResponse(
        report,
        status_code=200 if report["ready"] else 503,
        headers={"Cache-Control": "no-store"},
    )


app.add_route("/ready", readiness, methods=["GET"])


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
