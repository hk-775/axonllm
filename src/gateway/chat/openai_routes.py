"""OpenAI-compatible ingress for AxonLLM.

Exposes ``POST /v1/chat/completions`` and ``GET /v1/models`` in the shape the
OpenAI SDK (and the many tools built on it) expect, so a client can point at
AxonLLM with nothing more than a ``base_url`` swap:

    from openai import OpenAI
    client = OpenAI(base_url="https://<gateway>/v1", api_key="axon_...")

This reuses the internal GatewayAgent pipeline (routing, quotas, guardrails,
cost tracking) — it is a thin translation layer over ``handle_chat_completion``,
not a second implementation. Identity for attribution comes from the
authenticated request context (see AuthMiddleware / task #3), never the body.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from src.gateway.chat.client_agent import ClientAgent

logger = logging.getLogger("gateway.openai")


def _identity(request: Request) -> tuple[str | None, str | None]:
    """Trustworthy (user_id, project_id) from the authenticated context.

    Mirrors chat/routes.py::_identity_from_context — identity comes from the
    token, not the request body. ANONYMOUS (dev/LOG_ONLY) returns (None, None)
    so ClientAgent falls back to its configured defaults.
    """
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None
    if getattr(ctx.auth_method, "value", None) == "anonymous":
        return None, None
    return (ctx.user_id or None), (ctx.project_id or None)


def _error(status_code: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    """OpenAI-shaped error envelope."""
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "param": None, "code": None}},
        status_code=status_code,
    )


class OpenAICompatAPI:
    """OpenAI-compatible route handlers backed by the internal ClientAgent."""

    def __init__(self, client_agent: ClientAgent) -> None:
        self.client_agent = client_agent

    # ------------------------------------------------------------------
    # POST /v1/chat/completions
    # ------------------------------------------------------------------

    async def chat_completions(self, request: Request):
        try:
            body = await request.json()
        except Exception:
            return _error(400, "Invalid JSON in request body")

        model = body.get("model")
        if not model or not isinstance(model, str):
            return _error(400, "you must provide a model parameter")
        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return _error(400, "you must provide a messages parameter")

        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        stream = bool(body.get("stream", False))
        user_id, project_id = _identity(request)

        if stream:
            return await self._stream(model, messages, temperature, max_tokens, user_id, project_id)
        return await self._complete(model, messages, temperature, max_tokens, user_id, project_id)

    async def _complete(self, model, messages, temperature, max_tokens, user_id, project_id):
        try:
            resp = await self.client_agent.chat(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                user_id=user_id, project_id=project_id,
            )
        except Exception:
            logger.exception("chat completion failed")
            return _error(500, "Internal server error", err_type="server_error")

        resp.pop("_rate_limit_headers", None)
        if "error" in resp:
            err = resp["error"]
            msg = err.get("message", "request failed") if isinstance(err, dict) else str(err)
            return _error(resp.get("status_code", 500), msg, err_type="server_error")

        usage = resp.get("usage", {}) or {}
        completion = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp.get("model", model),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": resp.get("content", "")},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        return JSONResponse(completion)

    async def _stream(self, model, messages, temperature, max_tokens, user_id, project_id):
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        try:
            chunks = self.client_agent.chat_stream(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                user_id=user_id, project_id=project_id,
            )
        except Exception:
            logger.exception("stream setup failed")
            return _error(500, "Internal server error", err_type="server_error")

        async def event_generator():
            resolved_model = model
            first = True
            try:
                async for chunk in chunks:
                    if "_rate_limit_headers" in chunk:
                        continue
                    if "error" in chunk:
                        err = chunk["error"]
                        msg = err.get("message", "stream error") if isinstance(err, dict) else str(err)
                        yield f"data: {json.dumps({'error': {'message': msg, 'type': 'server_error'}})}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    if chunk.get("done"):
                        break
                    resolved_model = chunk.get("model") or resolved_model
                    delta: dict[str, Any] = {"content": chunk.get("content", "")}
                    if first:
                        delta["role"] = "assistant"
                        first = False
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": resolved_model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                # Final chunk with finish_reason, then the [DONE] sentinel
                final = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception:
                logger.exception("error during stream")
                yield f"data: {json.dumps({'error': {'message': 'stream failed', 'type': 'server_error'}})}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # GET /v1/models
    # ------------------------------------------------------------------

    async def list_models(self, request: Request) -> JSONResponse:
        user_id, project_id = _identity(request)
        try:
            models = await self.client_agent.list_models(project_id=project_id, user_id=user_id)
        except Exception:
            logger.exception("list models failed")
            return _error(500, "Internal server error", err_type="server_error")

        created = int(time.time())
        data = [
            {
                "id": m["name"] if isinstance(m, dict) else str(m),
                "object": "model",
                "created": created,
                "owned_by": "axonllm",
            }
            for m in models
        ]
        return JSONResponse({"object": "list", "data": data})


def create_openai_routes(api: OpenAICompatAPI) -> list[Route]:
    """Return Starlette routes for the OpenAI-compatible surface."""
    return [
        Route("/v1/chat/completions", api.chat_completions, methods=["POST"]),
        Route("/v1/models", api.list_models, methods=["GET"]),
    ]
