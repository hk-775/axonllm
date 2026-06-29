"""Chat API endpoints for the LLM-Router service.

Provides Starlette routes for:
- Model listing (GET /api/models)
- Non-streaming chat completion (POST /api/chat)
- Streaming chat completion via SSE (POST /api/chat/stream)
- Chat UI page (GET /chat)
"""

from __future__ import annotations

import json
import logging
import pathlib
import traceback
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from src.gateway.chat.client_agent import ClientAgent

_STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


class ChatAPI:
    """Route handlers for the client-facing chat interface."""

    def __init__(self, client_agent: ClientAgent) -> None:
        self.client_agent = client_agent

    # ------------------------------------------------------------------
    # GET /api/models
    # ------------------------------------------------------------------

    async def list_models(self, request: Request) -> JSONResponse:
        """Return available models as a JSON array."""
        try:
            user_id = request.query_params.get("user_id")
            project_id = request.query_params.get("project_id")
            models = await self.client_agent.list_models(
                project_id=project_id, user_id=user_id,
            )
            return JSONResponse(models)
        except Exception:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

    # ------------------------------------------------------------------
    # GET /api/users
    # ------------------------------------------------------------------

    async def list_users(self, request: Request) -> JSONResponse:
        """Return available user IDs for the user selector."""
        try:
            users = self.client_agent.get_available_users()
            return JSONResponse(users)
        except Exception:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

    # ------------------------------------------------------------------
    # POST /api/chat
    # ------------------------------------------------------------------

    async def chat(self, request: Request) -> JSONResponse:
        """Non-streaming chat completion."""
        # Parse and validate request body
        body, error_response = await _parse_chat_body(request)
        if error_response is not None:
            return error_response

        model = body.get("model", "")
        messages = body["messages"]
        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        user_id = body.get("user_id")
        provider = body.get("provider")
        # Extract smart_routing flag from context
        context = body.get("context", {})
        smart_routing = context.get("smart_routing", False) if isinstance(context, dict) else False

        try:
            response = await self.client_agent.chat(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                user_id=user_id, provider=provider, smart_routing=smart_routing,
            )
        except Exception:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

        # Extract rate limit headers from the response (if present)
        rate_limit_headers = response.pop("_rate_limit_headers", None)

        # Error response from the gateway
        if "error" in response:
            status_code = response.get("status_code", 500)
            json_response = JSONResponse(
                {"error": response["error"]},
                status_code=status_code,
            )
            if rate_limit_headers:
                for header_name, header_value in rate_limit_headers.items():
                    json_response.headers[header_name] = str(header_value)
            return json_response

        json_response = JSONResponse(response)
        if rate_limit_headers:
            for header_name, header_value in rate_limit_headers.items():
                json_response.headers[header_name] = str(header_value)
        return json_response

    # ------------------------------------------------------------------
    # POST /api/chat/stream
    # ------------------------------------------------------------------

    async def chat_stream(self, request: Request) -> StreamingResponse:
        """Streaming chat completion via SSE."""
        # Parse and validate request body (return 400 JSON, not SSE)
        body, error_response = await _parse_chat_body(request)
        if error_response is not None:
            return error_response

        model = body["model"]
        messages = body["messages"]
        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        user_id = body.get("user_id")
        provider = body.get("provider")

        # Collect the first response to check for errors / rate limit headers
        # before starting the SSE stream
        rate_limit_headers: dict[str, str] = {}

        # Try to get the stream result
        try:
            result = self.client_agent.chat_stream(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                user_id=user_id, provider=provider,
            )
        except Exception as exc:
            logging.getLogger("gateway.chat").error("Stream error: %s\n%s", exc, traceback.format_exc())
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

        async def event_generator():
            try:
                async for chunk in result:
                    # Extract rate limit headers from metadata chunk
                    if "_rate_limit_headers" in chunk:
                        rate_limit_headers.update(chunk["_rate_limit_headers"])
                        # If this chunk ONLY has rate limit headers, skip it
                        if "error" not in chunk and "done" not in chunk and "content" not in chunk and "id" not in chunk:
                            continue

                    # Error chunk
                    if "error" in chunk:
                        yield f"data: {json.dumps({'error': chunk['error']})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    # Done sentinel
                    if chunk.get("done"):
                        yield "data: [DONE]\n\n"
                        return

                    # Normal content chunk
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as exc:
                logging.getLogger("gateway.chat").error("Stream error: %s\n%s", exc, traceback.format_exc())
                yield f"data: {json.dumps({'error': {'type': 'server_error', 'message': str(exc)}})}\n\n"
                yield "data: [DONE]\n\n"

        streaming_response = StreamingResponse(event_generator(), media_type="text/event-stream")
        # Rate limit headers will be set on the response if available
        # For streaming, we set them eagerly from any pre-stream error response
        if rate_limit_headers:
            for header_name, header_value in rate_limit_headers.items():
                streaming_response.headers[header_name] = str(header_value)
        return streaming_response

    # ------------------------------------------------------------------
    # GET /chat
    # ------------------------------------------------------------------

    async def chat_page(self, request: Request) -> HTMLResponse:
        """Serve the chat UI SPA."""
        index_path = _STATIC_DIR / "index.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def playground_page(self, request: Request) -> HTMLResponse:
        """Serve the customer-facing playground SPA."""
        index_path = _STATIC_DIR / "playground.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def routing_page(self, request: Request) -> HTMLResponse:
        """Serve the routing explorer SPA."""
        index_path = _STATIC_DIR / "routing.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)


# ------------------------------------------------------------------
# Validation helper
# ------------------------------------------------------------------


async def _parse_chat_body(request: Request) -> tuple[dict, JSONResponse | None]:
    """Parse and validate the chat request body.

    Returns (body_dict, None) on success, or (empty_dict, error_response) on failure.
    """
    try:
        body = await request.json()
    except Exception:
        return {}, JSONResponse(
            {"error": {"type": "invalid_request", "message": "Invalid JSON"}},
            status_code=400,
        )

    model = body.get("model")
    # Allow empty model when smart_routing context is present (auto-select mode)
    context = body.get("context", {})
    smart_routing = context.get("smart_routing", False) if isinstance(context, dict) else False
    if not smart_routing:
        if not model or not isinstance(model, str) or not model.strip():
            return {}, JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'model' is required"}},
                status_code=400,
            )

    messages = body.get("messages")
    if not messages or not isinstance(messages, list) or len(messages) == 0:
        return {}, JSONResponse(
            {"error": {"type": "invalid_request", "message": "Field 'messages' is required and must be non-empty"}},
            status_code=400,
        )

    return body, None


# ------------------------------------------------------------------
# Route factory
# ------------------------------------------------------------------


def create_chat_routes(chat_api: ChatAPI) -> list[Route]:
    """Return Starlette Route objects for the chat API."""
    return [
        Route("/api/models", chat_api.list_models, methods=["GET"]),
        Route("/api/users", chat_api.list_users, methods=["GET"]),
        Route("/api/chat", chat_api.chat, methods=["POST"]),
        Route("/api/chat/stream", chat_api.chat_stream, methods=["POST"]),
        Route("/chat", chat_api.chat_page, methods=["GET"]),
        Route("/playground", chat_api.playground_page, methods=["GET"]),
        Route("/routing", chat_api.routing_page, methods=["GET"]),
    ]
