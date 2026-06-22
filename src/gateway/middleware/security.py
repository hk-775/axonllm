"""Security middleware — lightweight pass-through that marks LLM endpoints.

The actual security logic (PII redaction, injection detection, audit trail)
is now handled inside GatewayAgent.handle_chat_completion() which has full
access to the parsed request body. This middleware only attaches a marker
so downstream code can detect LLM-bound requests if needed.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Marks LLM endpoints on request.state for downstream awareness.

    All security enforcement (injection blocking, PII redaction, audit)
    is handled in GatewayAgent to avoid double policy resolution and
    to operate on the parsed message body rather than raw HTTP.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        request.state.is_llm_endpoint = self._is_llm_endpoint(path)
        return await call_next(request)

    def _is_llm_endpoint(self, path: str) -> bool:
        return path.startswith("/v1/") or path.startswith("/chat/completions") or path == "/chat/send"
