"""Admin API endpoints for the LLM-Router service.

Provides Starlette routes for:
- Overview dashboard (total requests, cost, active projects, active users)
- Project CRUD with hot-reload
- Aggregated usage queries with filters
- Cedar policy management
- Health status for providers and runtime
- Admin dashboard SPA
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

_STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent

from src.gateway.admin.pricing_drift import audit_pricing, render_drift_page
from src.gateway.admin.production_checklist import render_checklist_page, run_checklist
from src.gateway.config import AppConfig
from src.gateway.cost_tracker import CostTracker
from src.gateway.efficiency_analyzer import EfficiencyAnalyzer
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import GuardrailRule, Project, UsageFilters
from src.gateway.provider_config import ProviderConfig
from src.gateway.semantic_efficiency import SemanticEfficiencyEngine

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


PROVIDER_MODEL_CATALOG = {
    "openai": {
        "display_name": "OpenAI",
        "auth_type": "api_key",
        "models": [
            {"model_id": "gpt-4o", "name": "GPT-4o", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gpt-4o-mini", "name": "GPT-4o Mini", "capabilities": ["chat", "streaming"]},
            {"model_id": "gpt-4-turbo", "name": "GPT-4 Turbo", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "o3", "name": "o3 (Reasoning)", "capabilities": ["chat", "reasoning"]},
            {"model_id": "o4-mini", "name": "o4 Mini (Reasoning)", "capabilities": ["chat", "reasoning"]},
        ],
    },
    "anthropic": {
        "display_name": "Anthropic",
        "auth_type": "api_key",
        "models": [
            {"model_id": "claude-opus-4-20250514", "name": "Claude Opus 4", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku", "capabilities": ["chat", "streaming"]},
        ],
    },
    "bedrock": {
        "display_name": "AWS Bedrock",
        "auth_type": "aws_credentials",
        "models": [
            {"model_id": "us.anthropic.claude-opus-4-6-v1", "name": "Claude Opus 4.6", "capabilities": ["chat", "vision"]},
            {"model_id": "us.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "capabilities": ["chat", "vision"]},
            {"model_id": "us.amazon.nova-pro-v1:0", "name": "Amazon Nova Pro", "capabilities": ["chat", "vision"]},
            {"model_id": "us.amazon.nova-lite-v1:0", "name": "Amazon Nova Lite", "capabilities": ["chat"]},
            {"model_id": "us.amazon.nova-micro-v1:0", "name": "Amazon Nova Micro", "capabilities": ["chat"]},
            {"model_id": "us.deepseek.r1-v1:0", "name": "DeepSeek R1", "capabilities": ["chat", "reasoning"]},
        ],
    },
    "azure_openai": {
        "display_name": "Azure OpenAI",
        "auth_type": "azure_key",
        "models": [
            {"model_id": "gpt-4o", "name": "GPT-4o", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gpt-4o-mini", "name": "GPT-4o Mini", "capabilities": ["chat", "streaming"]},
            {"model_id": "gpt-4-turbo", "name": "GPT-4 Turbo", "capabilities": ["chat", "vision", "streaming"]},
        ],
    },
    "vertex_ai": {
        "display_name": "Google Vertex AI",
        "auth_type": "gcp_service_account",
        "models": [
            {"model_id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "capabilities": ["chat", "vision", "streaming"]},
        ],
    },
}


class AdminAPI:
    """Holds references to gateway components and exposes admin route handlers."""

    def __init__(
        self,
        cost_tracker: CostTracker,
        health_tracker: ProviderHealthTracker,
        model_registry: ModelRegistry,
        projects: dict[str, Project] | None = None,
        policies: list[dict] | None = None,
        user_configs: dict[str, dict] | None = None,
        config_path: str = "config/models.yaml",
        persistence: DynamoPersistence | None = None,
        catalog: dict | None = None,
        efficiency_analyzer: EfficiencyAnalyzer | None = None,
        semantic_engine: SemanticEfficiencyEngine | None = None,
        pricing_path: str = "config/pricing.yaml",
        app_config: AppConfig | None = None,
        provider_configs: dict[str, ProviderConfig] | None = None,
    ) -> None:
        self.cost_tracker = cost_tracker
        self.health_tracker = health_tracker
        self.model_registry = model_registry
        self.projects: dict[str, Project] = projects or {}
        self.policies: list[dict] = policies or []
        self._user_configs: dict[str, dict] = user_configs or {}
        self._config_path = config_path
        self._persistence = persistence
        self._catalog = catalog if catalog is not None else PROVIDER_MODEL_CATALOG
        self._efficiency_analyzer = efficiency_analyzer
        self._semantic_engine = semantic_engine
        # Shown on the pricing-coverage page so the operator knows which file to
        # edit; the table itself comes from the cost tracker, already loaded.
        self._pricing_path = pricing_path
        # For the production checklist. The typed AppConfig is threaded through
        # rather than read from the environment in the render path, so the page
        # reports the settings this process actually booted with — a later
        # os.environ mutation cannot make the checklist disagree with the running
        # gateway. Defaults to a fresh AppConfig so existing callers keep working;
        # that carries the fail-closed defaults (ENFORCE, no demo data), which is
        # the safe direction for a checklist to assume.
        self._app_config = app_config if app_config is not None else AppConfig()
        # Providers with credentials actually loaded. load_provider_configs drops
        # the rest, so this doubles as the credential check's input.
        self._provider_configs: dict[str, ProviderConfig] = provider_configs or {}

    # ------------------------------------------------------------------
    # GET /admin/overview
    # ------------------------------------------------------------------

    async def overview(self, request: Request) -> JSONResponse:
        """Total requests, cost, active projects, and active users."""
        records = self.cost_tracker._records
        total_requests = len(records)
        total_cost = sum(r.cost for r in records)
        active_projects = len({r.project_id for r in records})
        active_users = len({r.user_id for r in records})

        total_cached_tokens = sum(r.cached_tokens for r in records)
        total_cache_creation_tokens = sum(r.cache_creation_tokens for r in records)
        denom = total_cached_tokens + total_cache_creation_tokens
        cache_hit_rate = total_cached_tokens / denom if denom > 0 else 0.0

        return JSONResponse({
            "total_requests": total_requests,
            "total_cost": total_cost,
            "active_projects": active_projects,
            "active_users": active_users,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "cache_hit_rate": cache_hit_rate,
        })

    # ------------------------------------------------------------------
    # GET /admin/projects
    # ------------------------------------------------------------------

    async def list_projects(self, request: Request) -> JSONResponse:
        """List all projects with spend, budget utilization, and request counts."""
        result = []
        for project in self.projects.values():
            budget_status = await self.cost_tracker.check_budget(project.project_id)
            records = [
                r for r in self.cost_tracker._records
                if r.project_id == project.project_id
            ]
            utilization = (
                (budget_status.current_spend / project.budget_limit * 100)
                if project.budget_limit
                else None
            )
            result.append({
                "project_id": project.project_id,
                "name": project.name,
                "current_spend": budget_status.current_spend,
                "budget_limit": project.budget_limit,
                "budget_utilization_pct": utilization,
                "request_count": len(records),
            })
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/projects/{id}
    # ------------------------------------------------------------------

    async def get_project(self, request: Request) -> JSONResponse:
        """Project detail with users, usage breakdown, and config."""
        project_id = request.path_params["id"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        records = [
            r for r in self.cost_tracker._records
            if r.project_id == project_id
        ]
        users = list({r.user_id for r in records})

        # Usage breakdown by model
        model_breakdown: dict[str, dict] = {}
        for r in records:
            entry = model_breakdown.setdefault(r.model, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Usage breakdown by provider
        provider_breakdown: dict[str, dict] = {}
        for r in records:
            entry = provider_breakdown.setdefault(r.provider, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Usage breakdown by user
        user_breakdown: dict[str, dict] = {}
        for r in records:
            entry = user_breakdown.setdefault(r.user_id, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Cached token metrics for this project
        total_cached_tokens = sum(r.cached_tokens for r in records)
        total_cache_creation_tokens = sum(r.cache_creation_tokens for r in records)
        denom = total_cached_tokens + total_cache_creation_tokens
        cache_hit_rate = total_cached_tokens / denom if denom > 0 else 0.0

        return JSONResponse({
            "project_id": project.project_id,
            "name": project.name,
            "budget_limit": project.budget_limit,
            "alert_threshold": project.alert_threshold,
            "allowed_models": project.allowed_models,
            "guardrail_rules": [
                {"name": g.name, "rule_type": g.rule_type, "pattern": g.pattern, "action": g.action, "applies_to": g.applies_to}
                for g in project.guardrail_rules
            ],
            "cache_enabled": project.cache_enabled,
            "cache_ttl_seconds": project.cache_ttl_seconds,
            "log_level": project.log_level,
            "prompt_caching_enabled": project.prompt_caching_enabled,
            "users": users,
            "members": project.members,
            "usage_by_model": model_breakdown,
            "usage_by_provider": provider_breakdown,
            "usage_by_user": user_breakdown,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "cache_hit_rate": cache_hit_rate,
        })

    # ------------------------------------------------------------------
    # POST /admin/projects
    # ------------------------------------------------------------------

    async def create_project(self, request: Request) -> JSONResponse:
        """Create a new project from JSON body."""
        body = await request.json()

        project_id = body.get("project_id", str(uuid.uuid4()))
        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'name' is required"}},
                status_code=400,
            )

        guardrail_rules = [
            GuardrailRule(
                name=g["name"],
                rule_type=g.get("rule_type", "keyword_block"),
                pattern=g.get("pattern"),
                action=g.get("action", "block"),
                applies_to=g.get("applies_to", "both"),
            )
            for g in body.get("guardrail_rules", [])
        ]

        project = Project(
            project_id=project_id,
            name=name,
            budget_limit=body.get("budget_limit"),
            alert_threshold=body.get("alert_threshold"),
            allowed_models=body.get("allowed_models"),
            guardrail_rules=guardrail_rules,
            cache_enabled=body.get("cache_enabled", False),
            cache_ttl_seconds=body.get("cache_ttl_seconds", 300),
            log_level=body.get("log_level", "INFO"),
            log_destination=body.get("log_destination"),
            prompt_caching_enabled=body.get("prompt_caching_enabled", False),
            rate_limit_rpm=body.get("rate_limit_rpm"),
            members=body.get("members", []),
        )

        self.projects[project_id] = project

        # Persist to DynamoDB if enabled
        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_project(project)
            except Exception:
                logger.warning(
                    "Failed to persist project %s to DynamoDB",
                    project_id,
                    exc_info=True,
                )

        # Register budget with cost tracker if configured
        if project.budget_limit is not None or project.alert_threshold is not None:
            self.cost_tracker.register_project(
                project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
            )

        return JSONResponse(
            {"project_id": project_id, "name": project.name, "status": "created"},
            status_code=201,
        )

    # ------------------------------------------------------------------
    # PUT /admin/projects/{id}
    # ------------------------------------------------------------------

    async def update_project(self, request: Request) -> JSONResponse:
        """Update project config (hot-reload, no restart)."""
        project_id = request.path_params["id"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        body = await request.json()

        # Update mutable fields if present in the body
        if "name" in body:
            project.name = body["name"]
        if "budget_limit" in body:
            project.budget_limit = body["budget_limit"]
        if "alert_threshold" in body:
            project.alert_threshold = body["alert_threshold"]
        if "allowed_models" in body:
            project.allowed_models = body["allowed_models"]
        if "cache_enabled" in body:
            project.cache_enabled = body["cache_enabled"]
        if "cache_ttl_seconds" in body:
            project.cache_ttl_seconds = body["cache_ttl_seconds"]
        if "log_level" in body:
            project.log_level = body["log_level"]
        if "log_destination" in body:
            project.log_destination = body["log_destination"]
        if "rate_limit_rpm" in body:
            project.rate_limit_rpm = body["rate_limit_rpm"]
        if "members" in body:
            project.members = body["members"]
        if "prompt_caching_enabled" in body:
            project.prompt_caching_enabled = body["prompt_caching_enabled"]
        if "guardrail_rules" in body:
            project.guardrail_rules = [
                GuardrailRule(
                    name=g["name"],
                    rule_type=g.get("rule_type", "keyword_block"),
                    pattern=g.get("pattern"),
                    action=g.get("action", "block"),
                    applies_to=g.get("applies_to", "both"),
                )
                for g in body["guardrail_rules"]
            ]

        # Hot-reload budget in cost tracker
        if project.budget_limit is not None or project.alert_threshold is not None:
            self.cost_tracker.register_project(
                project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
            )

        # Persist to DynamoDB if enabled
        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_project(project)
            except Exception:
                logger.warning(
                    "Failed to persist project %s to DynamoDB",
                    project_id,
                    exc_info=True,
                )

        return JSONResponse({"project_id": project_id, "status": "updated"})

    # ------------------------------------------------------------------
    # POST /admin/projects/{id}/members
    # ------------------------------------------------------------------

    async def add_member(self, request: Request) -> JSONResponse:
        """Add a user to a project."""
        project_id = request.path_params["id"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )
        body = await request.json()
        user_id = body.get("user_id")
        if not user_id:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'user_id' is required"}},
                status_code=400,
            )
        if user_id not in project.members:
            project.members.append(user_id)
        return JSONResponse({"project_id": project_id, "user_id": user_id, "status": "added"})

    # ------------------------------------------------------------------
    # DELETE /admin/projects/{id}/members/{user_id}
    # ------------------------------------------------------------------

    async def remove_member(self, request: Request) -> JSONResponse:
        """Remove a user from a project."""
        project_id = request.path_params["id"]
        user_id = request.path_params["user_id"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )
        if user_id in project.members:
            project.members.remove(user_id)
            return JSONResponse({"project_id": project_id, "user_id": user_id, "status": "removed"})
        return JSONResponse(
            {"error": {"type": "not_found", "message": f"User '{user_id}' is not a member of project '{project_id}'"}},
            status_code=404,
        )

    # ------------------------------------------------------------------
    # GET /admin/usage
    # ------------------------------------------------------------------

    async def usage(self, request: Request) -> JSONResponse:
        """Aggregated usage with query-param filters."""
        params = request.query_params

        filters = UsageFilters(
            start_time=_parse_datetime(params.get("start_time")),
            end_time=_parse_datetime(params.get("end_time")),
            provider=params.get("provider"),
            model=params.get("model"),
            project_id=params.get("project_id"),
            user_id=params.get("user_id"),
        )

        report = await self.cost_tracker.get_aggregated_usage(filters)

        total_cached_tokens = sum(
            r.cached_tokens for r in self.cost_tracker._apply_filters(filters)
        )
        total_cache_creation_tokens = sum(
            r.cache_creation_tokens for r in self.cost_tracker._apply_filters(filters)
        )

        return JSONResponse({
            "total_requests": report.total_requests,
            "total_tokens": report.total_tokens,
            "total_cost": report.total_cost,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "breakdown": [
                {
                    "group_key": b.group_key,
                    "group_by": b.group_by,
                    "requests": b.requests,
                    "tokens": b.tokens,
                    "cost": b.cost,
                }
                for b in report.breakdown
            ],
        })

    # ------------------------------------------------------------------
    # GET /admin/usage/export
    # ------------------------------------------------------------------

    async def usage_export(self, request: Request):
        """Export usage for chargeback/FinOps.

        Same filters as /admin/usage, plus:
          - format=csv (default) | json
          - level=records (default, one row per request) | breakdown (aggregated)

        `records` is the detail an owner needs to attribute spend per request;
        `breakdown` is the aggregated summary (by provider/model/project/user).
        CSV streams as a file attachment.
        """
        params = request.query_params
        filters = UsageFilters(
            start_time=_parse_datetime(params.get("start_time")),
            end_time=_parse_datetime(params.get("end_time")),
            provider=params.get("provider"),
            model=params.get("model"),
            project_id=params.get("project_id"),
            user_id=params.get("user_id"),
        )
        fmt = (params.get("format") or "csv").lower()
        level = (params.get("level") or "records").lower()
        if fmt not in ("csv", "json"):
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "format must be csv or json"}},
                status_code=400,
            )
        if level not in ("records", "breakdown"):
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "level must be records or breakdown"}},
                status_code=400,
            )

        if level == "records":
            columns = [
                "request_id", "timestamp", "project_id", "user_id", "provider", "model",
                "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
                "cost", "latency_ms", "status", "routing_strategy",
            ]
            rows = [
                {
                    "request_id": r.request_id,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                    "project_id": r.project_id,
                    "user_id": r.user_id,
                    "provider": r.provider,
                    "model": r.model,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.total_tokens,
                    "cached_tokens": getattr(r, "cached_tokens", 0),
                    "cost": r.cost,
                    "latency_ms": getattr(r, "latency_ms", 0),
                    "status": getattr(r, "status", "success"),
                    "routing_strategy": getattr(r, "routing_strategy", ""),
                }
                for r in self.cost_tracker._apply_filters(filters)
            ]
        else:
            report = await self.cost_tracker.get_aggregated_usage(filters)
            columns = ["group_by", "group_key", "requests", "tokens", "cost"]
            rows = [
                {
                    "group_by": b.group_by,
                    "group_key": b.group_key,
                    "requests": b.requests,
                    "tokens": b.tokens,
                    "cost": b.cost,
                }
                for b in report.breakdown
            ]

        if fmt == "json":
            return JSONResponse({"level": level, "rows": rows})

        # CSV as a streamed file attachment
        def _csv_iter():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=columns)
            writer.writeheader()
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for row in rows:
                writer.writerow(row)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        filename = f"axonllm-usage-{level}.csv"
        return StreamingResponse(
            _csv_iter(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # GET /admin/policies
    # ------------------------------------------------------------------

    async def list_policies(self, request: Request) -> JSONResponse:
        """Return stored Cedar policies."""
        return JSONResponse(self.policies)

    # ------------------------------------------------------------------
    # POST /admin/policies
    # ------------------------------------------------------------------

    async def create_policy(self, request: Request) -> JSONResponse:
        """Store a new Cedar policy."""
        body = await request.json()

        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'name' is required"}},
                status_code=400,
            )

        policy = {
            "name": name,
            "description": body.get("description", ""),
            "policy_text": body.get("policy_text", ""),
            "mode": body.get("mode", "LOG_ONLY"),
        }

        # Update existing policy with the same name, or append
        for i, existing in enumerate(self.policies):
            if existing["name"] == name:
                self.policies[i] = policy
                return JSONResponse({"name": name, "status": "updated"})

        self.policies.append(policy)
        return JSONResponse({"name": name, "status": "created"}, status_code=201)

    # ------------------------------------------------------------------
    # GET /admin/health
    # ------------------------------------------------------------------

    async def health(self, request: Request) -> JSONResponse:
        """Per-provider health status and runtime agent status."""
        models = self.model_registry.list_models()
        providers: set[str] = set()
        for m in models:
            for p in m.providers:
                providers.add(p.provider)

        provider_health: dict[str, str] = {}
        for provider in sorted(providers):
            provider_health[provider] = (
                "healthy" if self.health_tracker.is_healthy(provider) else "unhealthy"
            )

        # Surface persistence reachability so a misconfigured/missing DynamoDB
        # table or IAM denial is visible here instead of silently dropping writes.
        persistence_health = None
        overall = "ok"
        if self._persistence is not None:
            persistence_health = await self._persistence.health_status()
            if persistence_health.get("enabled") and persistence_health.get("reachable") is False:
                overall = "degraded"

        return JSONResponse({
            "status": overall,
            "providers": provider_health,
            "persistence": persistence_health,
            "runtime": "running",
        })

    # ------------------------------------------------------------------
    # GET /admin/users
    # ------------------------------------------------------------------

    async def list_users(self, request: Request) -> JSONResponse:
        """List all users with aggregated usage stats and budget info."""
        records = self.cost_tracker._records
        user_data: dict[str, dict] = {}
        for r in records:
            entry = user_data.setdefault(r.user_id, {
                "user_id": r.user_id,
                "projects": set(),
                "requests": 0,
                "total_tokens": 0,
                "cost": 0.0,
            })
            entry["projects"].add(r.project_id)
            entry["requests"] += 1
            entry["total_tokens"] += r.total_tokens
            entry["cost"] += r.cost

        result = []
        for u in user_data.values():
            budget = self.cost_tracker.get_user_budget(u["user_id"])
            budget_limit = budget.get("budget_limit")
            current_spend = u["cost"]
            utilization = (current_spend / budget_limit * 100) if budget_limit else None
            result.append({
                "user_id": u["user_id"],
                "projects": sorted(u["projects"]),
                "requests": u["requests"],
                "total_tokens": u["total_tokens"],
                "cost": u["cost"],
                "budget_limit": budget_limit,
                "alert_threshold": budget.get("alert_threshold"),
                "budget_utilization_pct": utilization,
            })
        result.sort(key=lambda x: x["cost"], reverse=True)
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/users/{id}
    # ------------------------------------------------------------------

    async def get_user(self, request: Request) -> JSONResponse:
        """User detail with per-project and per-model breakdown, plus budget info."""
        user_id = request.path_params["id"]
        records = [r for r in self.cost_tracker._records if r.user_id == user_id]
        if not records:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"User '{user_id}' not found"}},
                status_code=404,
            )

        total_requests = len(records)
        total_tokens = sum(r.total_tokens for r in records)
        total_cost = sum(r.cost for r in records)
        projects = sorted({r.project_id for r in records})

        model_breakdown: dict[str, dict] = {}
        for r in records:
            entry = model_breakdown.setdefault(r.model, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        provider_breakdown: dict[str, dict] = {}
        for r in records:
            entry = provider_breakdown.setdefault(r.provider, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        project_breakdown: dict[str, dict] = {}
        for r in records:
            entry = project_breakdown.setdefault(r.project_id, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        budget = self.cost_tracker.get_user_budget(user_id)

        return JSONResponse({
            "user_id": user_id,
            "projects": projects,
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "budget_limit": budget.get("budget_limit"),
            "alert_threshold": budget.get("alert_threshold"),
            "allowed_models": self._user_configs.get(user_id, {}).get("allowed_models"),
            "usage_by_model": model_breakdown,
            "usage_by_provider": provider_breakdown,
            "usage_by_project": project_breakdown,
        })

    async def set_user_budget(self, request: Request) -> JSONResponse:
        """Set or update budget for a user."""
        user_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}

        budget_limit = body.get("budget_limit")
        alert_threshold = body.get("alert_threshold")

        self.cost_tracker.register_user(
            user_id=user_id,
            budget_limit=budget_limit,
            alert_threshold=alert_threshold,
        )

        # Persist user config to DynamoDB if enabled
        if self._persistence is not None and self._persistence.enabled:
            try:
                config = self._user_configs.get(user_id, {})
                config["budget_limit"] = budget_limit
                config["alert_threshold"] = alert_threshold
                await self._persistence.save_user_config(user_id, config)
            except Exception:
                logger.warning(
                    "Failed to persist user config for %s to DynamoDB",
                    user_id,
                    exc_info=True,
                )

        return JSONResponse({
            "user_id": user_id,
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
            "status": "updated",
        })

    # ------------------------------------------------------------------
    # PUT /admin/users/{id}/allowed-models
    # ------------------------------------------------------------------

    async def set_user_allowed_models(self, request: Request) -> JSONResponse:
        """Set or update allowed models for a user."""
        user_id = request.path_params["id"]
        body = await request.json()
        allowed_models = body.get("allowed_models")
        self._user_configs.setdefault(user_id, {})["allowed_models"] = allowed_models

        # Persist user config to DynamoDB if enabled
        if self._persistence is not None and self._persistence.enabled:
            try:
                config = self._user_configs.get(user_id, {})
                await self._persistence.save_user_config(user_id, config)
            except Exception:
                logger.warning(
                    "Failed to persist user config for %s to DynamoDB",
                    user_id,
                    exc_info=True,
                )

        return JSONResponse({
            "user_id": user_id,
            "allowed_models": allowed_models,
            "status": "updated",
        })


    # ------------------------------------------------------------------
    # GET /admin/projects/{id}/models
    # ------------------------------------------------------------------

    async def list_project_models(self, request: Request) -> JSONResponse:
        """Return the allowed models for a project."""
        project_id = request.path_params["id"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )
        return JSONResponse({
            "project_id": project_id,
            "allowed_models": project.allowed_models if project.allowed_models is not None else [],
        })

    # ------------------------------------------------------------------
    # POST /admin/projects/{id}/models
    # ------------------------------------------------------------------

    async def add_project_model(self, request: Request) -> JSONResponse:
        """Add a model to a project's allowed_models list."""
        project_id = request.path_params["id"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'model' is required"}},
                status_code=400,
            )

        if project.allowed_models is None:
            project.allowed_models = []

        if model not in project.allowed_models:
            project.allowed_models.append(model)

        # Persist to DynamoDB if enabled
        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_project(project)
            except Exception:
                logger.warning(
                    "Failed to persist project %s to DynamoDB",
                    project_id,
                    exc_info=True,
                )

        return JSONResponse({
            "project_id": project_id,
            "model": model,
            "allowed_models": project.allowed_models,
            "status": "added",
        })

    # ------------------------------------------------------------------
    # DELETE /admin/projects/{id}/models/{model_name}
    # ------------------------------------------------------------------

    async def remove_project_model(self, request: Request) -> JSONResponse:
        """Remove a model from a project's allowed_models list."""
        project_id = request.path_params["id"]
        model_name = request.path_params["model_name"]
        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        if project.allowed_models is None or model_name not in project.allowed_models:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Model '{model_name}' is not in project's allowed models"}},
                status_code=404,
            )

        project.allowed_models.remove(model_name)

        # Persist to DynamoDB if enabled
        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_project(project)
            except Exception:
                logger.warning(
                    "Failed to persist project %s to DynamoDB",
                    project_id,
                    exc_info=True,
                )

        return JSONResponse({
            "project_id": project_id,
            "model": model_name,
            "allowed_models": project.allowed_models,
            "status": "removed",
        })

    # ------------------------------------------------------------------
    # GET /admin/catalog
    # ------------------------------------------------------------------

    async def catalog(self, request: Request) -> JSONResponse:
        """Return the provider/model catalog."""
        return JSONResponse(self._catalog)

    # ------------------------------------------------------------------
    # GET /admin/models
    # ------------------------------------------------------------------

    async def list_models(self, request: Request) -> JSONResponse:
        """List all models with usage stats."""
        models = self.model_registry.list_models()
        records = self.cost_tracker._records

        result = []
        for m in models:
            # Match by model name OR any provider model_id
            model_ids = {m.name} | {p.model_id for p in m.providers}
            model_records = [r for r in records if r.model in model_ids]
            total_requests = len(model_records)
            total_tokens = sum(r.total_tokens for r in model_records)
            total_cost = sum(r.cost for r in model_records)

            result.append({
                "name": m.name,
                "description": m.description,
                "routing_strategy": m.routing_strategy.value,
                "capabilities": m.capabilities or [],
                "providers": [
                    {"provider": p.provider, "model_id": p.model_id, "weight": p.weight, "fallback_order": p.fallback_order}
                    for p in m.providers
                ],
                "total_requests": total_requests,
                "total_tokens": total_tokens,
                "total_cost": total_cost,
            })
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # POST /admin/models
    # ------------------------------------------------------------------

    async def create_model(self, request: Request) -> JSONResponse:
        """Create a new model."""
        body = await request.json()

        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'name' is required"}},
                status_code=400,
            )

        new_entry: dict = {
            "name": name,
            "description": body.get("description", ""),
            "routing_strategy": body.get("routing_strategy", "round-robin"),
            "providers": body.get("providers", []),
        }
        capabilities = body.get("capabilities")
        if capabilities is not None:
            new_entry["capabilities"] = capabilities

        # Build candidate config with all existing models + new entry
        existing_entries = []
        for m in self.model_registry.models.values():
            entry: dict = {
                "name": m.name,
                "description": m.description,
                "routing_strategy": m.routing_strategy.value,
                "providers": [
                    {"provider": p.provider, "model_id": p.model_id, "weight": p.weight, "fallback_order": p.fallback_order}
                    for p in m.providers
                ],
            }
            if m.capabilities is not None:
                entry["capabilities"] = m.capabilities
            existing_entries.append(entry)

        candidate = {"models": existing_entries + [new_entry]}
        errors = self.model_registry.validate(candidate)
        if errors:
            return JSONResponse(
                {"errors": [{"field": e.field, "message": e.message} for e in errors]},
                status_code=400,
            )

        # Parse and add to registry
        parsed = self.model_registry._parse_entry(new_entry)
        self.model_registry.models[name] = parsed

        # Persist
        try:
            pathlib.Path(self._config_path).write_text(
                self.model_registry.pretty_print(), encoding="utf-8"
            )
        except (IOError, OSError) as exc:
            # Rollback: remove from registry
            self.model_registry.models.pop(name, None)
            return JSONResponse(
                {"error": {"type": "server_error", "message": f"Failed to persist configuration: {exc}"}},
                status_code=500,
            )

        return JSONResponse({"name": name, "status": "created"}, status_code=201)

    # ------------------------------------------------------------------
    # PUT /admin/models/{name}
    # ------------------------------------------------------------------

    async def update_model(self, request: Request) -> JSONResponse:
        """Update a model's configuration."""
        model_name = request.path_params["name"]
        model_config = self.model_registry.models.get(model_name)
        if model_config is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Model '{model_name}' not found"}},
                status_code=404,
            )

        body = await request.json()

        # Build the updated entry dict from current config + body overrides
        updated_entry: dict = {
            "name": model_name,
            "description": body.get("description", model_config.description),
            "routing_strategy": body.get("routing_strategy", model_config.routing_strategy.value),
            "providers": body.get("providers", [
                {"provider": p.provider, "model_id": p.model_id, "weight": p.weight, "fallback_order": p.fallback_order}
                for p in model_config.providers
            ]),
        }
        capabilities = body.get("capabilities", model_config.capabilities)
        if capabilities is not None:
            updated_entry["capabilities"] = capabilities

        # Build candidate config with all models, replacing the updated one
        candidate_entries = []
        for m in self.model_registry.models.values():
            if m.name == model_name:
                candidate_entries.append(updated_entry)
            else:
                entry: dict = {
                    "name": m.name,
                    "description": m.description,
                    "routing_strategy": m.routing_strategy.value,
                    "providers": [
                        {"provider": p.provider, "model_id": p.model_id, "weight": p.weight, "fallback_order": p.fallback_order}
                        for p in m.providers
                    ],
                }
                if m.capabilities is not None:
                    entry["capabilities"] = m.capabilities
                candidate_entries.append(entry)

        candidate = {"models": candidate_entries}
        errors = self.model_registry.validate(candidate)
        if errors:
            return JSONResponse(
                {"errors": [{"field": e.field, "message": e.message} for e in errors]},
                status_code=400,
            )

        # Parse and replace in registry
        old_config = self.model_registry.models[model_name]
        parsed = self.model_registry._parse_entry(updated_entry)
        self.model_registry.models[model_name] = parsed

        # Persist
        try:
            pathlib.Path(self._config_path).write_text(
                self.model_registry.pretty_print(), encoding="utf-8"
            )
        except (IOError, OSError) as exc:
            # Rollback
            self.model_registry.models[model_name] = old_config
            return JSONResponse(
                {"error": {"type": "server_error", "message": f"Failed to persist configuration: {exc}"}},
                status_code=500,
            )

        return JSONResponse({"name": model_name, "status": "updated"})

    # ------------------------------------------------------------------
    # DELETE /admin/models/{name}
    # ------------------------------------------------------------------

    async def delete_model(self, request: Request) -> JSONResponse:
        """Delete a model."""
        model_name = request.path_params["name"]
        model_config = self.model_registry.models.get(model_name)
        if model_config is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Model '{model_name}' not found"}},
                status_code=404,
            )

        # Remove from registry
        del self.model_registry.models[model_name]

        # Persist
        try:
            pathlib.Path(self._config_path).write_text(
                self.model_registry.pretty_print(), encoding="utf-8"
            )
        except (IOError, OSError) as exc:
            # Rollback: restore the model
            self.model_registry.models[model_name] = model_config
            return JSONResponse(
                {"error": {"type": "server_error", "message": f"Failed to persist configuration: {exc}"}},
                status_code=500,
            )

        return JSONResponse({"name": model_name, "status": "deleted"})



    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # GET /admin/traces
    # ------------------------------------------------------------------

    async def traces(self, request: Request) -> JSONResponse:
        """Return recent request traces for the live traces view."""
        records = self.cost_tracker._records
        limit = int(request.query_params.get("limit", "100"))
        recent = records[-limit:] if len(records) > limit else records

        traces = []
        for r in reversed(recent):
            traces.append({
                "request_id": r.request_id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "model": r.model,
                "provider": r.provider,
                "user_id": r.user_id,
                "project_id": r.project_id,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "cost": r.cost,
                "latency_ms": getattr(r, "latency_ms", 0),
                "status": getattr(r, "status", "success"),
                "cached_tokens": r.cached_tokens,
                "routing_strategy": getattr(r, "routing_strategy", ""),
            })

        return JSONResponse({"traces": traces, "total": len(records)})

    # GET /admin/efficiency
    # ------------------------------------------------------------------

    async def efficiency_overview(self, request: Request) -> JSONResponse:
        """Token efficiency overview across all users."""
        if self._efficiency_analyzer is None:
            return JSONResponse(
                {"error": {"type": "not_configured", "message": "Efficiency analyzer not configured"}},
                status_code=501,
            )

        all_metrics = self._efficiency_analyzer.get_all_user_metrics()

        grade_distribution: dict[str, int] = {}
        for m in all_metrics:
            grade = m.grade.value
            grade_distribution[grade] = grade_distribution.get(grade, 0) + 1

        total_cost = sum(m.total_cost for m in all_metrics)
        avg_score = sum(m.score for m in all_metrics) / len(all_metrics) if all_metrics else 0.0

        wasteful_users = [
            {"user_id": m.entity_id, "score": m.score, "grade": m.grade.value, "cost": m.total_cost}
            for m in all_metrics if m.score < 50
        ]
        wasteful_users.sort(key=lambda x: x["score"])

        return JSONResponse({
            "total_users_analyzed": len(all_metrics),
            "avg_efficiency_score": round(avg_score, 1),
            "grade_distribution": grade_distribution,
            "total_cost": round(total_cost, 4),
            "wasteful_users": wasteful_users[:10],
            "users": [
                {
                    "user_id": m.entity_id,
                    "score": m.score,
                    "grade": m.grade.value,
                    "completion_prompt_ratio": m.completion_prompt_ratio,
                    "cache_utilization_rate": m.cache_utilization_rate,
                    "avg_cost_per_request": m.avg_cost_per_request,
                    "expensive_model_ratio": m.expensive_model_ratio,
                    "duplicate_request_rate": m.duplicate_request_rate,
                    "total_requests": m.total_requests,
                    "total_cost": m.total_cost,
                }
                for m in sorted(all_metrics, key=lambda x: x.score)
            ],
        })

    # ------------------------------------------------------------------
    # GET /admin/users/{id}/efficiency
    # ------------------------------------------------------------------

    async def user_efficiency(self, request: Request) -> JSONResponse:
        """Full efficiency report for a specific user."""
        user_id = request.path_params["id"]

        if self._efficiency_analyzer is None:
            return JSONResponse(
                {"error": {"type": "not_configured", "message": "Efficiency analyzer not configured"}},
                status_code=501,
            )

        report = self._efficiency_analyzer.analyze_user(user_id)

        result: dict = {
            "user_id": user_id,
            "metrics": {
                "score": report.metrics.score,
                "grade": report.metrics.grade.value,
                "completion_prompt_ratio": report.metrics.completion_prompt_ratio,
                "cache_utilization_rate": report.metrics.cache_utilization_rate,
                "avg_cost_per_request": report.metrics.avg_cost_per_request,
                "expensive_model_ratio": report.metrics.expensive_model_ratio,
                "token_velocity_per_hour": report.metrics.token_velocity_per_hour,
                "duplicate_request_rate": report.metrics.duplicate_request_rate,
                "avg_prompt_tokens": report.metrics.avg_prompt_tokens,
                "avg_completion_tokens": report.metrics.avg_completion_tokens,
                "total_requests": report.metrics.total_requests,
                "total_cost": report.metrics.total_cost,
            },
            "alerts": [
                {
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "message": a.message,
                    "metric_value": a.metric_value,
                    "threshold": a.threshold,
                }
                for a in report.alerts
            ],
            "recommendations": [
                {
                    "current_model": r.current_model,
                    "recommended_model": r.recommended_model,
                    "task_type": r.task_type,
                    "estimated_savings_pct": r.estimated_savings_pct,
                    "quality_impact": r.quality_impact,
                    "reason": r.reason,
                }
                for r in report.recommendations
            ],
            "peer_comparison": report.peer_comparison,
        }

        # Add semantic analysis if available
        if self._semantic_engine is not None:
            semantic = self._semantic_engine.generate_report(user_id=user_id)
            result["semantic"] = {
                "output_analysis": {
                    "avg_completion_tokens": semantic.output_analysis.avg_completion_tokens,
                    "estimated_utilization": semantic.output_analysis.estimated_utilization,
                    "recommendation": semantic.output_analysis.recommendation,
                },
                "waste_summary": semantic.waste_summary,
            }
            if semantic.user_profile:
                result["semantic"]["profile"] = {
                    "dominant_task_type": semantic.user_profile.dominant_task_type,
                    "avg_complexity": semantic.user_profile.avg_complexity,
                    "typical_model": semantic.user_profile.typical_model,
                    "optimal_model": semantic.user_profile.optimal_model,
                    "estimated_monthly_savings": semantic.user_profile.estimated_monthly_savings,
                    "patterns": semantic.user_profile.patterns,
                }
            if semantic.model_recommendations:
                result["semantic"]["model_recommendations"] = [
                    {
                        "current_model": r.current_model,
                        "recommended_model": r.recommended_model,
                        "task_type": r.task_type,
                        "estimated_savings_pct": r.estimated_savings_pct,
                        "quality_impact": r.quality_impact,
                        "reason": r.reason,
                    }
                    for r in semantic.model_recommendations
                ]

        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/projects/{id}/efficiency
    # ------------------------------------------------------------------

    async def project_efficiency(self, request: Request) -> JSONResponse:
        """Full efficiency report for a specific project."""
        project_id = request.path_params["id"]

        if self._efficiency_analyzer is None:
            return JSONResponse(
                {"error": {"type": "not_configured", "message": "Efficiency analyzer not configured"}},
                status_code=501,
            )

        project = self.projects.get(project_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        report = self._efficiency_analyzer.analyze_project(project_id)

        result: dict = {
            "project_id": project_id,
            "name": project.name,
            "metrics": {
                "score": report.metrics.score,
                "grade": report.metrics.grade.value,
                "completion_prompt_ratio": report.metrics.completion_prompt_ratio,
                "cache_utilization_rate": report.metrics.cache_utilization_rate,
                "avg_cost_per_request": report.metrics.avg_cost_per_request,
                "expensive_model_ratio": report.metrics.expensive_model_ratio,
                "token_velocity_per_hour": report.metrics.token_velocity_per_hour,
                "duplicate_request_rate": report.metrics.duplicate_request_rate,
                "avg_prompt_tokens": report.metrics.avg_prompt_tokens,
                "avg_completion_tokens": report.metrics.avg_completion_tokens,
                "total_requests": report.metrics.total_requests,
                "total_cost": report.metrics.total_cost,
            },
            "alerts": [
                {
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "message": a.message,
                    "metric_value": a.metric_value,
                    "threshold": a.threshold,
                }
                for a in report.alerts
            ],
            "recommendations": [
                {
                    "current_model": r.current_model,
                    "recommended_model": r.recommended_model,
                    "task_type": r.task_type,
                    "estimated_savings_pct": r.estimated_savings_pct,
                    "quality_impact": r.quality_impact,
                    "reason": r.reason,
                }
                for r in report.recommendations
            ],
            "user_comparison": report.peer_comparison,
        }

        # Add semantic waste analysis if available
        if self._semantic_engine is not None:
            semantic = self._semantic_engine.generate_report(project_id=project_id)
            result["semantic"] = {
                "output_analysis": {
                    "avg_completion_tokens": semantic.output_analysis.avg_completion_tokens,
                    "estimated_utilization": semantic.output_analysis.estimated_utilization,
                    "recommendation": semantic.output_analysis.recommendation,
                },
                "waste_summary": semantic.waste_summary,
            }

        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/dashboard
    # ------------------------------------------------------------------

    async def dashboard(self, request: Request) -> HTMLResponse:
        """Serve the admin dashboard SPA."""
        index_path = _STATIC_DIR / "index.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    # ------------------------------------------------------------------
    # GET /admin/static/{path}
    # ------------------------------------------------------------------

    async def static_asset(self, request: Request):
        """Serve vendored dashboard assets (React, Babel) from static/.

        Vendored locally so the dashboard works in air-gapped / offline
        deployments — no runtime dependency on unpkg.com. Path is confined to
        the static dir to prevent traversal.
        """
        from starlette.responses import PlainTextResponse, Response

        rel = request.path_params.get("path", "")
        # Confine to _STATIC_DIR — reject anything that escapes it.
        target = (_STATIC_DIR / rel).resolve()
        try:
            target.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            return PlainTextResponse("Not found", status_code=404)
        if not target.is_file():
            return PlainTextResponse("Not found", status_code=404)

        suffix = target.suffix.lower()
        media_type = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".html": "text/html",
        }.get(suffix, "application/octet-stream")
        return Response(
            target.read_bytes(),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def architecture(self, request: Request) -> HTMLResponse:
        """Serve the architecture diagram as an SVG embedded in a full-page viewer."""
        svg_path = _PROJECT_ROOT / "docs" / "architecture.svg"
        if not svg_path.exists():
            return HTMLResponse("<h1>Architecture diagram not found</h1>", status_code=404)

        svg_content = svg_path.read_text(encoding="utf-8")
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<title>AxonLLM Architecture</title>"
            "<style>"
            "body{margin:0;background:#f8f9fa;display:flex;flex-direction:column;min-height:100vh}"
            ".toolbar{background:#232F3E;padding:10px 20px;display:flex;align-items:center;gap:12px}"
            ".toolbar a{color:#fff;text-decoration:none;font-family:sans-serif;font-size:13px;"
            "padding:6px 14px;border-radius:4px;background:#FF9900;font-weight:600}"
            ".toolbar a:hover{background:#EC7211}"
            ".toolbar span{color:#fff;font-family:sans-serif;font-size:15px;font-weight:700;flex:1}"
            ".diagram{flex:1;display:flex;align-items:center;justify-content:center;padding:20px;overflow:auto}"
            ".diagram svg{max-width:100%;height:auto;border-radius:8px;"
            "box-shadow:0 4px 24px rgba(0,0,0,0.1)}"
            "</style></head><body>"
            '<div class="toolbar">'
            "<span>AxonLLM Architecture</span>"
            '<a href="/admin/dashboard">&larr; Dashboard</a>'
            "</div>"
            '<div class="diagram">' + svg_content + "</div>"
            "</body></html>"
        )
        return HTMLResponse(html)

    async def landing_page(self, request: Request) -> HTMLResponse:
        """Serve the marketing landing page at the gateway root.

        Read from disk per request rather than cached, matching `dashboard` and
        `architecture` — editing site/index.html and reloading shows the change
        without a restart, which is the whole point of a single-file page.

        site/ is outside the installed package and is not in package-data, so a
        pip-installed gateway will not have it. That is why this 404s with an
        explanation instead of raising: a missing landing page must not make the
        root path a 500 on an otherwise healthy deployment.
        """
        index_path = _PROJECT_ROOT / "site" / "index.html"
        if not index_path.exists():
            return HTMLResponse(
                "<h1>AxonLLM</h1><p>Landing page not found. The gateway is "
                'running — see <a href="/admin/dashboard">the dashboard</a>.</p>',
                status_code=404,
            )
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    async def pricing_drift(self, request: Request) -> HTMLResponse:
        """Report provider mappings with no price, and prices nothing uses.

        Rendered fresh on each request from the live registry and the pricing
        table the cost tracker bills from, so editing pricing.yaml and reloading
        shows the new coverage without a restart.
        """
        report = audit_pricing(self.model_registry, self.cost_tracker.pricing_config)
        return HTMLResponse(render_drift_page(report, self._pricing_path))

    async def production_checklist(self, request: Request) -> HTMLResponse:
        """Report whether this deployment is ready to carry real traffic.

        Every check behind this page covers something that fails silently — an
        unpriced model billing $0.00, LOG_ONLY auth admitting every request, a
        retired model id — so the state is only visible if something asks. Run
        fresh per request from the live config, so a fix shows on reload.

        Hidden in demo mode: ``run_checklist`` returns a did-not-run report and
        the page explains why, rather than listing failures that are correct for
        a demo and would train operators to ignore it.
        """
        report = await run_checklist(
            app_config=self._app_config,
            model_registry=self.model_registry,
            pricing_config=self.cost_tracker.pricing_config,
            provider_configs=self._provider_configs,
            persistence=self._persistence,
        )
        return HTMLResponse(render_checklist_page(report))


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning None on failure."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# Route factory
# ------------------------------------------------------------------


def create_admin_routes(admin_api: AdminAPI) -> list[Route]:
    """Return Starlette Route objects for the admin API."""
    return [
        Route("/", admin_api.landing_page, methods=["GET"]),
        Route("/admin/dashboard", admin_api.dashboard, methods=["GET"]),
        Route("/admin/static/{path:path}", admin_api.static_asset, methods=["GET"]),
        Route("/admin/architecture", admin_api.architecture, methods=["GET"]),
        Route("/admin/pricing-drift", admin_api.pricing_drift, methods=["GET"]),
        Route("/admin/production-checklist", admin_api.production_checklist, methods=["GET"]),
        Route("/admin/overview", admin_api.overview, methods=["GET"]),
        Route("/admin/projects", admin_api.list_projects, methods=["GET"]),
        Route("/admin/projects", admin_api.create_project, methods=["POST"]),
        Route("/admin/projects/{id}/members", admin_api.add_member, methods=["POST"]),
        Route("/admin/projects/{id}/members/{user_id}", admin_api.remove_member, methods=["DELETE"]),
        Route("/admin/projects/{id}/models", admin_api.list_project_models, methods=["GET"]),
        Route("/admin/projects/{id}/models", admin_api.add_project_model, methods=["POST"]),
        Route("/admin/projects/{id}/models/{model_name}", admin_api.remove_project_model, methods=["DELETE"]),
        Route("/admin/projects/{id}", admin_api.get_project, methods=["GET"]),
        Route("/admin/projects/{id}", admin_api.update_project, methods=["PUT"]),
        Route("/admin/usage", admin_api.usage, methods=["GET"]),
        Route("/admin/usage/export", admin_api.usage_export, methods=["GET"]),
        Route("/admin/users", admin_api.list_users, methods=["GET"]),
        Route("/admin/users/{id:path}/allowed-models", admin_api.set_user_allowed_models, methods=["PUT"]),
        Route("/admin/users/{id:path}/budget", admin_api.set_user_budget, methods=["PUT"]),
        Route("/admin/users/{id:path}/efficiency", admin_api.user_efficiency, methods=["GET"]),
        Route("/admin/users/{id:path}", admin_api.get_user, methods=["GET"]),
        Route("/admin/catalog", admin_api.catalog, methods=["GET"]),
        Route("/admin/models", admin_api.list_models, methods=["GET"]),
        Route("/admin/models", admin_api.create_model, methods=["POST"]),
        Route("/admin/models/{name}", admin_api.update_model, methods=["PUT"]),
        Route("/admin/models/{name}", admin_api.delete_model, methods=["DELETE"]),
        Route("/admin/policies", admin_api.list_policies, methods=["GET"]),
        Route("/admin/policies", admin_api.create_policy, methods=["POST"]),
        Route("/admin/health", admin_api.health, methods=["GET"]),
        Route("/admin/traces", admin_api.traces, methods=["GET"]),
        Route("/admin/efficiency", admin_api.efficiency_overview, methods=["GET"]),
        Route("/admin/projects/{id}/efficiency", admin_api.project_efficiency, methods=["GET"]),
    ]
