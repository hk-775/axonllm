"""Multi-strategy authentication middleware for AxonLLM.

Priority chain:
1. X-Amzn-Oidc-Data header (ALB OIDC JWT)
2. Authorization: Bearer <token> (OIDC JWT or API key if prefixed axon_)
3. X-Api-Key header
4. Anonymous -> 401
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.models import AuthMethod, RequestContext

if TYPE_CHECKING:
    from src.gateway.auth.api_key_service import APIKeyService
    from src.gateway.auth.oidc_service import OIDCService
    from src.gateway.auth.principal import PrincipalResolver

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({
    "/health",
    # The landing page. Anonymous by definition — gating it behind auth would
    # mean only signed-in users could read the pitch.
    "/",
    "/admin/dashboard",
    "/chat",
    "/playground",
    "/routing",
})


def _is_site_asset(path: str) -> bool:
    """True for the marketing site's pages and the assets they fetch.

    The landing page at "/" is public, and its nav links to architecture.html,
    which in turn fetches three SVGs plus the narration audio and its transcript
    from site/narration/. Gating those behind auth would serve the pitch to
    anonymous readers and then 401 the page it links to.

    Delegates the decision to ``_is_servable_site_path``, the same predicate the
    route handler applies, so "publicly routable" and "anonymous" cannot drift
    into a page that renders 200 with a 401 on the audio it plays. A path this
    admits but the handler rejects just 404s, so the coupling can only ever be
    too narrow, never too permissive.
    """
    from pathlib import PurePosixPath

    from src.gateway.admin.routes import _is_servable_site_path

    if not path.startswith("/"):
        return False
    return _is_servable_site_path(PurePosixPath(path.lstrip("/")))


class PolicyService(Protocol):
    """Interface for policy evaluation."""

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        ...


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticates requests via OIDC JWT, API key, or rejects as anonymous."""

    def __init__(
        self,
        app,
        oidc_service: OIDCService | None = None,
        api_key_service: APIKeyService | None = None,
        policy_service: PolicyService | None = None,
        mode: str = "ENFORCE",
        public_paths: frozenset[str] | None = None,
        config_sync: object | None = None,
        principal_resolver: PrincipalResolver | None = None,
        require_canonical_principal: bool = False,
    ):
        super().__init__(app)
        self.oidc_service = oidc_service
        self.api_key_service = api_key_service
        self.policy_service = policy_service
        self.mode = mode
        self.public_paths = public_paths or PUBLIC_PATHS
        # Optional so every existing caller constructs unchanged and never polls.
        # Refreshed here rather than in GatewayAgent because the project and user
        # config gate more than chat — the admin reads and /api/users need the
        # same converged view, and one refresh per request serves all of them.
        self.config_sync = config_sync
        self.principal_resolver = principal_resolver
        self.require_canonical_principal = require_canonical_principal

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip auth for public paths and static assets.
        # /scim/* and /saml/* carry their OWN auth (SCIM bearer token; SAML is
        # the login flow itself), so the gateway's user-auth chain is bypassed
        # for them — they must not require an existing session to authenticate.
        path = request.url.path
        if (
            path in self.public_paths
            or path.startswith("/admin/static")
            or path.startswith("/chat/static")
            or path.startswith("/scim/")
            or path.startswith("/saml/")
            or _is_site_asset(path)
        ):
            request.state.context = RequestContext(
                user_id="anonymous",
                project_id="",
                roles=[],
                scopes=[],
                auth_method=AuthMethod.ANONYMOUS,
            )
            request.state.principal = None
            return await call_next(request)

        context = None

        # 1. ALB OIDC headers. Their presence is authoritative: an invalid or
        # ambiguous ALB credential must not fall through to another auth method.
        alb_tokens = request.headers.getlist("x-amzn-oidc-data")
        alb_identities = request.headers.getlist("x-amzn-oidc-identity")
        alb_auth_attempted = bool(alb_tokens or alb_identities)
        authorization_headers = request.headers.getlist("authorization")
        api_key_headers = request.headers.getlist("x-api-key")
        header_auth_attempted = bool(
            authorization_headers or api_key_headers
        )
        competing_credentials = (
            (alb_auth_attempted and header_auth_attempted)
            or (authorization_headers and api_key_headers)
            or len(authorization_headers) > 1
            or len(api_key_headers) > 1
        )
        if (
            alb_auth_attempted
            and not competing_credentials
            and self.oidc_service
            and len(alb_tokens) == 1
            and len(alb_identities) == 1
            and alb_tokens[0]
            and alb_identities[0]
        ):
            context = await self.oidc_service.validate_alb_jwt(
                alb_tokens[0],
                expected_subject=alb_identities[0],
            )

        # 2. Authorization: Bearer <token>
        if (
            context is None
            and not alb_auth_attempted
            and not competing_credentials
            and len(authorization_headers) == 1
        ):
            auth_header = authorization_headers[0]
            if (
                auth_header.startswith("Bearer ")
                and auth_header[7:]
                and auth_header == auth_header.strip()
            ):
                token = auth_header[7:]
                if token.startswith("axon_"):
                    context = await self._authenticate_api_key(token)
                elif self.oidc_service:
                    context = await self.oidc_service.validate_oidc_jwt(token)

        # 3. X-Api-Key header
        if (
            context is None
            and not alb_auth_attempted
            and not competing_credentials
            and not authorization_headers
            and len(api_key_headers) == 1
        ):
            api_key_header = api_key_headers[0]
            if api_key_header and api_key_header == api_key_header.strip():
                context = await self._authenticate_api_key(api_key_header)

        # 4. No credentials — reject (or allow in LOG_ONLY mode)
        if context is None:
            if self.mode == "ENFORCE":
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "type": "authentication_error",
                            "message": "Missing or invalid credentials. Provide a Bearer token or X-Api-Key header.",
                        }
                    },
                )
            else:
                context = RequestContext(
                    user_id="anonymous",
                    project_id="",
                    roles=[],
                    scopes=[],
                    auth_method=AuthMethod.ANONYMOUS,
                )

        principal = None
        if context.auth_method is not AuthMethod.ANONYMOUS:
            if self.principal_resolver is not None:
                try:
                    principal = await self.principal_resolver.resolve(context)
                except Exception:
                    logger.exception(
                        "Canonical principal resolution is unavailable"
                    )
                    if self.mode == "ENFORCE":
                        return JSONResponse(
                            status_code=503,
                            content={
                                "error": {
                                    "type": "authorization_error",
                                    "message": (
                                        "Canonical principal resolution is "
                                        "temporarily unavailable."
                                    ),
                                    "code": "principal_resolver_unavailable",
                                }
                            },
                        )
                if principal is None:
                    if self.mode == "ENFORCE":
                        return JSONResponse(
                            status_code=403,
                            content={
                                "error": {
                                    "type": "authorization_error",
                                    "message": (
                                        "No active tenant membership exists for "
                                        "this credential."
                                    ),
                                    "code": "tenant_membership_required",
                                }
                            },
                        )
                    logger.warning(
                        "Canonical principal resolution failed user=%s tenant_hint=%s",
                        context.user_id,
                        context.tenant_id,
                    )
                else:
                    from src.gateway.auth.principal import canonical_request_context

                    context = canonical_request_context(context, principal)
            elif self.require_canonical_principal:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "configuration_error",
                            "message": "Canonical principal resolution is unavailable.",
                            "code": "principal_resolver_unavailable",
                        }
                    },
                )

        request.state.context = context
        request.state.principal = principal

        # Adopt any project or user config another instance wrote, before the
        # handler reads either. Both gate requests rather than decorate them — an
        # unresolved project means no budget limit, no allowed-models list and no
        # rate limit, and a missing user config means no per-user model
        # restriction — so a write that reached only one task made enforcement a
        # function of which task the balancer picked. Rate-limited to one counter
        # read per CONFIG_SYNC_TTL_SECONDS and a no-op without persistence.
        if self.config_sync is not None:
            try:
                await self.config_sync.refresh_if_stale()
            except Exception:
                # Never fail a request over a refresh; the loaded config still
                # decides it, which is what happened before this existed.
                logger.warning("Config refresh failed", exc_info=True)

        # Policy evaluation
        if self.policy_service:
            # Adopt any policy another instance wrote before deciding this
            # request. Statements are compiled once, so a policy written through
            # POST /admin/policies previously took effect only on the task that
            # served the write — behind desired_count=2 an operator's forbid was
            # enforced by one task and ignored by the other, per request, decided
            # by the load balancer. Rate-limited to one counter read per
            # POLICY_SYNC_TTL_SECONDS and a no-op without persistence.
            refresh = getattr(self.policy_service, "refresh_if_stale", None)
            if refresh is not None:
                try:
                    await refresh()
                except Exception:
                    # Never fail a request because the refresh failed; the
                    # already-compiled set still decides it.
                    logger.warning("Policy refresh failed", exc_info=True)
            action = request.method.lower()
            resource = path
            decision = await self.policy_service.evaluate(context, action, resource)

            if decision == "DENY":
                if self.mode == "ENFORCE":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "type": "authorization_error",
                                "message": "Access denied by policy.",
                            }
                        },
                    )
                else:
                    logger.warning(
                        "Policy DENY (LOG_ONLY) user=%s project=%s action=%s resource=%s",
                        context.user_id,
                        context.project_id,
                        action,
                        resource,
                    )

        return await call_next(request)

    async def _authenticate_api_key(self, raw_key: str) -> RequestContext | None:
        """Validate API key and return context."""
        if not self.api_key_service:
            return None

        key_record = await self.api_key_service.validate_key(raw_key)
        if key_record is None:
            return None

        return RequestContext(
            user_id=f"apikey:{key_record.key_id}",
            project_id=key_record.project_id,
            roles=["service"],
            scopes=key_record.scopes,
            auth_method=AuthMethod.API_KEY,
            tenant_id=key_record.tenant_id,
            api_key_id=key_record.key_id,
            issuer="urn:axonllm:api-key",
            subject=key_record.key_id,
        )
