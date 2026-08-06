"""Admin API routes for quota and usage control."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
    from src.gateway.quota_enforcer import QuotaEnforcer


class QuotaAPI:
    """Query and manage quota enforcement state."""

    def __init__(
        self,
        quota_enforcer: QuotaEnforcer,
        policy_resolver: PolicyHierarchyResolver,
    ) -> None:
        self.enforcer = quota_enforcer
        self.resolver = policy_resolver

    async def get_project_quota(self, request: Request) -> JSONResponse:
        """GET /admin/quotas/{project_id} — current quota state for a project."""
        project_id = request.path_params["project_id"]
        environment = request.query_params.get("env")

        policy = await self.resolver.resolve(project_id, environment)
        # Read through to the shared counter rather than this instance's own: an
        # operator checking a budget cannot tell which task answered, and a task
        # that has not served this project since starting would report $0 for a
        # project the fleet is already blocking.
        current_spend = await self.enforcer.current_spend(project_id)

        return JSONResponse(content={
            "project_id": project_id,
            "environment": environment,
            "policy_limits": {
                "rate_limit_rpm": policy.rate_limit_rpm,
                "budget_limit": policy.budget_limit,
                "max_tokens_per_request": policy.max_tokens_per_request,
                "allowed_models": policy.allowed_models,
                "allowed_providers": policy.allowed_providers,
                "pii_redaction_enabled": policy.pii_redaction_enabled,
                "pii_redact_types": policy.pii_redact_types,
                # Another whitelist rebuild that dropped fields it didn't know
                # about: a project with entity detection on reported the same
                # policy as one without, so the only per-request paid feature in
                # the hierarchy was invisible here.
                "pii_reinject": policy.pii_reinject,
                "pii_ner_enabled": policy.pii_ner_enabled,
                "pii_ner_types": policy.pii_ner_types,
            },
            "usage": {
                "current_spend": round(current_spend, 4),
                "budget_remaining": round(policy.budget_limit - current_spend, 4) if policy.budget_limit else None,
                "budget_utilization_pct": round((current_spend / policy.budget_limit) * 100, 1) if policy.budget_limit else None,
            },
        })

    async def reset_spend(self, request: Request) -> JSONResponse:
        """POST /admin/quotas/{project_id}/reset — reset spend counter (billing cycle)."""
        project_id = request.path_params["project_id"]
        old_spend = await self.enforcer.current_spend(project_id)
        fleet_wide = await self.enforcer.reset_spend(project_id)
        if not fleet_wide:
            # 503, not 200: the shared counter still holds the old total, so every
            # other instance keeps blocking the project. Reporting "reset" here
            # would tell an operator their unblock worked when the next request
            # will still be refused.
            return JSONResponse(
                status_code=503,
                content={
                    "project_id": project_id,
                    "previous_spend": round(old_spend, 4),
                    "current_spend": round(old_spend, 4),
                    "status": "reset_failed",
                    "detail": (
                        "Local spend was cleared but the shared counter could not be "
                        "reset, so other instances still hold the old total. Retry."
                    ),
                },
            )
        return JSONResponse(content={
            "project_id": project_id,
            "previous_spend": round(old_spend, 4),
            "current_spend": 0.0,
            "status": "reset",
        })

    async def simulate_request(self, request: Request) -> JSONResponse:
        """POST /admin/quotas/simulate — test whether a request would be allowed."""
        body = await request.json()
        project_id = body.get("project_id", "")
        model = body.get("model", "")
        provider = body.get("provider")
        max_tokens = body.get("max_tokens")
        estimated_cost = body.get("estimated_cost", 0.0)
        environment = body.get("environment")

        policy = await self.resolver.resolve(project_id, environment)
        decision = await self.enforcer.enforce_all(
            project_id=project_id,
            model=model,
            provider=provider,
            max_tokens=max_tokens,
            estimated_cost=estimated_cost,
            policy=policy,
        )

        return JSONResponse(content={
            "allowed": decision.allowed,
            "reason": decision.reason,
            "limit_type": decision.limit_type,
            "limit_value": decision.limit_value,
            "current_value": decision.current_value,
            "resolved_policy": {
                "rate_limit_rpm": policy.rate_limit_rpm,
                "budget_limit": policy.budget_limit,
                "max_tokens_per_request": policy.max_tokens_per_request,
                "allowed_models": policy.allowed_models,
                "allowed_providers": policy.allowed_providers,
            },
        })


def create_quota_routes(quota_api: QuotaAPI) -> list[Route]:
    """Create Starlette routes for quota management."""
    return [
        Route("/admin/quotas/simulate", quota_api.simulate_request, methods=["POST"]),
        Route("/admin/quotas/{project_id}", quota_api.get_project_quota, methods=["GET"]),
        Route("/admin/quotas/{project_id}/reset", quota_api.reset_spend, methods=["POST"]),
    ]
