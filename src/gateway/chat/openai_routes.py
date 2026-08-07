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

from src.gateway.chat.request_body import (
    DEFAULT_CHAT_REQUEST_MAX_BYTES,
    JSONBodyError,
    read_json_object,
)
from src.gateway.request_validator import RequestValidator

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


def _resolve_request_validator(client_agent: ClientAgent) -> RequestValidator:
    gateway_agent = getattr(client_agent, "gateway_agent", None)
    validator = getattr(gateway_agent, "request_validator", None)
    if isinstance(validator, RequestValidator):
        return validator
    return RequestValidator()


# OpenAI defines exactly four finish_reason values, and typed SDK clients
# deserialize the field into an enum — an unrecognized string is a validation
# error, not a curiosity. The adapters pass their provider's own stop reason
# through (Anthropic "end_turn", Gemini "MAX_TOKENS", Cohere "COMPLETE", …), so
# this boundary is where it has to become one of the four. Normalizing here
# rather than in each adapter keeps the internal API honest about what the
# provider actually said, while the OpenAI-compatible surface stays in spec.
_FINISH_REASONS = {
    # Anthropic / Bedrock Converse
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
    # Gemini (Google AI / Vertex) — uppercase
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    # Cohere
    "COMPLETE": "stop",
    "MAX_TOKENS_REACHED": "length",
    "ERROR_TOXIC": "content_filter",
    # Mantle's /openai/v1/responses route reports lifecycle status rather than a
    # stop reason (see mantle_provider). "incomplete" there most often means the
    # token cap was hit.
    "completed": "stop",
    "incomplete": "length",
}

_VALID_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter"}


def _finish_reason(raw: Any, has_tool_calls: bool) -> str:
    """Map a provider stop reason onto OpenAI's four legal values.

    ``has_tool_calls`` wins over everything: a response carrying tool calls is
    a tool call regardless of what the provider labeled the stop, and a client
    that reads anything else here ends its tool loop without running the tool.
    """
    if has_tool_calls:
        return "tool_calls"
    if not isinstance(raw, str) or not raw:
        return "stop"
    if raw in _VALID_FINISH_REASONS:
        return raw
    mapped = _FINISH_REASONS.get(raw)
    if mapped:
        return mapped
    # Unknown reason: "stop" is the safe default — it ends the turn cleanly
    # rather than making a client retry or reject the response outright.
    logger.debug("unmapped finish_reason %r from provider; reporting 'stop'", raw)
    return "stop"


class OpenAICompatAPI:
    """OpenAI-compatible route handlers backed by the internal ClientAgent."""

    def __init__(
        self,
        client_agent: ClientAgent,
        *,
        max_request_bytes: int = DEFAULT_CHAT_REQUEST_MAX_BYTES,
        request_validator: RequestValidator | None = None,
    ) -> None:
        self.client_agent = client_agent
        self.max_request_bytes = max_request_bytes
        self.request_validator = (
            request_validator
            if request_validator is not None
            else _resolve_request_validator(client_agent)
        )

    # ------------------------------------------------------------------
    # POST /v1/chat/completions
    # ------------------------------------------------------------------

    async def chat_completions(self, request: Request):
        try:
            body = await read_json_object(
                request,
                max_bytes=self.max_request_bytes,
            )
        except JSONBodyError as exc:
            return _error(exc.status_code, exc.message)

        raw_model = body.get("model")
        # Smart routing (auto model selection): model == "auto" or empty/missing.
        # Otherwise a concrete model string is required. Lets standard OpenAI
        # clients opt into task-aware routing via `model: "auto"`.
        smart_routing = "model" not in body or (
            isinstance(raw_model, str)
            and raw_model.strip().lower() in ("", "auto")
        )
        errors = self.request_validator.validate_payload(
            body,
            allow_empty_model=smart_routing,
            check_model=False,
        )
        if errors:
            return _error(400, errors[0].message)

        model = body.get("model", "")
        assert isinstance(model, str)
        smart_routing = model.strip().lower() in ("", "auto")
        if smart_routing:
            model = ""
        messages = body.get("messages")
        assert isinstance(messages, list)

        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        top_p = body.get("top_p")
        stop = body.get("stop")
        system = body.get("system")
        stream = body.get("stream", False)
        # The pipeline translates tools per-provider, but this route never read
        # them off the body — so an OpenAI SDK client got a fluent 200 in which
        # the model states it has no such tool, with no error to notice it by.
        # The one failure mode worse than a 400.
        tools = body.get("tools")
        tool_choice = body.get("tool_choice")
        user_id, project_id = _identity(request)

        if stream:
            return await self._stream(model, messages, temperature, max_tokens,
                                      top_p, stop, system,
                                      user_id, project_id, smart_routing,
                                      tools, tool_choice)
        return await self._complete(model, messages, temperature, max_tokens,
                                    top_p, stop, system,
                                    user_id, project_id, smart_routing,
                                    tools, tool_choice)

    async def _complete(self, model, messages, temperature, max_tokens, top_p,
                        stop, system, user_id, project_id, smart_routing=False,
                        tools=None, tool_choice=None):
        try:
            resp = await self.client_agent.chat(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                top_p=top_p, stop=stop, system=system,
                user_id=user_id, project_id=project_id, smart_routing=smart_routing,
                tools=tools, tool_choice=tool_choice,
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
        tool_calls = resp.get("tool_calls")
        message: dict[str, Any] = {
            "role": "assistant",
            # "" not None when there are no tool_calls: a plain response has
            # always sent a string here and clients rely on it.
            "content": resp.get("content") if tool_calls else (resp.get("content") or ""),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        # Was hardcoded "stop", which is what an OpenAI client reads as "the turn
        # is over" — so even once tool_calls were forwarded, a tool loop would
        # stop before running the tool.
        finish_reason = _finish_reason(resp.get("finish_reason"), bool(tool_calls))

        completion = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp.get("model", model),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        # Surface the smart-routing decision (task_type + selected model) as an
        # extension field. Standard OpenAI clients ignore unknown keys.
        if "smart_routing" in resp:
            completion["x_smart_routing"] = resp["smart_routing"]
        # Same for the cache. This route rebuilds the response rather than
        # passing the pipeline dict through, so without this a cached response
        # is indistinguishable from a fresh one: the id is a new uuid either
        # way. It went unnoticed because nothing ever wrote to the cache, so
        # is_cached was unreachable — now that it isn't, a caller comparing two
        # responses needs to be able to tell a hit from a provider call, and a
        # semantic hit (an answer to a question judged equivalent) from an exact
        # one.
        if resp.get("is_cached"):
            completion["x_cached"] = True
            completion["x_cache_type"] = resp.get("cache_type", "exact")
        return JSONResponse(completion)

    async def _stream(self, model, messages, temperature, max_tokens, top_p,
                      stop, system, user_id, project_id, smart_routing=False,
                      tools=None, tool_choice=None):
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        try:
            chunks = self.client_agent.chat_stream(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                top_p=top_p, stop=stop, system=system,
                user_id=user_id, project_id=project_id, smart_routing=smart_routing,
                tools=tools, tool_choice=tool_choice,
            )
        except Exception:
            logger.exception("stream setup failed")
            return _error(500, "Internal server error", err_type="server_error")

        async def event_generator():
            resolved_model = model
            first = True
            observed_finish: str | None = None
            saw_tool_call = False
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
                    if chunk.get("tool_calls"):
                        delta["tool_calls"] = chunk["tool_calls"]
                        saw_tool_call = True
                        # A tool-call delta carries no text. OpenAI sends
                        # content: null there, and clients accumulating
                        # `content or ""` would otherwise see "" and treat the
                        # turn as plain prose.
                        delta["content"] = chunk.get("content") or None
                    if chunk.get("finish_reason"):
                        observed_finish = chunk["finish_reason"]
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
                # Final chunk with finish_reason, then the [DONE] sentinel.
                # Carry the provider's reason when it gave one: a client driving
                # a tool loop branches on this, and a hardcoded "stop" ends the
                # loop before the tool ever runs.
                final = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": _finish_reason(observed_finish,
                                                                 saw_tool_call)}],
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
