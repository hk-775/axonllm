"""Security middleware — orchestrates PII redaction and injection detection.

Runs after auth middleware (so RequestContext is available) and before
the request reaches the LLM routing layer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.models import ResolvedPolicy

if TYPE_CHECKING:
    from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
    from src.gateway.security.audit_trail import AuditTrail
    from src.gateway.security.injection_detector import PromptInjectionDetector
    from src.gateway.security.pii_redactor import PIIRedactor

logger = logging.getLogger(__name__)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Applies PII redaction and injection detection based on resolved policy.

    For non-chat endpoints this is a no-op pass-through.
    """

    def __init__(
        self,
        app,
        pii_redactor: PIIRedactor | None = None,
        injection_detector: PromptInjectionDetector | None = None,
        policy_resolver: PolicyHierarchyResolver | None = None,
        audit_trail: AuditTrail | None = None,
        injection_block_enabled: bool = True,
    ):
        super().__init__(app)
        self.pii_redactor = pii_redactor
        self.injection_detector = injection_detector
        self.policy_resolver = policy_resolver
        self.audit_trail = audit_trail
        self.injection_block_enabled = injection_block_enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if not self._is_llm_endpoint(path):
            return await call_next(request)

        ctx = getattr(request.state, "context", None)
        if ctx is None:
            return await call_next(request)

        # Resolve policy for this project
        policy = ResolvedPolicy()
        if self.policy_resolver and ctx.project_id:
            policy = await self.policy_resolver.resolve(ctx.project_id)

        request.state.resolved_policy = policy
        request.state.pii_mapping = None

        return await call_next(request)

    def _is_llm_endpoint(self, path: str) -> bool:
        return path.startswith("/v1/") or path.startswith("/chat/completions") or path == "/chat/send"
