"""Centralized bootstrap for AxonLLM gateway components.

Both ``serve_dashboard.py`` and ``agentcore_agent.py`` delegate to this
module instead of duplicating inline wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.admin.audit_routes import AuditAPI, create_audit_routes
from src.gateway.admin.key_routes import KeyManagementAPI, create_key_routes
from src.gateway.admin.policy_routes import PolicyHierarchyAPI, create_policy_hierarchy_routes
from src.gateway.admin.quota_routes import QuotaAPI, create_quota_routes
from src.gateway.admin.region_routes import RegionAPI, create_region_routes
from src.gateway.admin.routes import (
    AdminAPI,
    PROVIDER_MODEL_CATALOG,
    create_admin_routes,
    create_site_routes,
)
from src.gateway.admin.webhook_routes import WebhookAPI, create_webhook_routes
from src.gateway.agent import GatewayAgent
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.auth.cedar_policy import CedarPolicyService
from src.gateway.auth.oidc_service import OIDCConfig, OIDCService
from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.auth.saml_routes import SamlAPI, create_saml_routes
from src.gateway.auth.saml_service import SamlService, load_saml_config
from src.gateway.auth.scim_routes import ScimAPI, create_scim_routes
from src.gateway.auth.scim_service import ScimStore
from src.gateway.efficiency_analyzer import EfficiencyAnalyzer
from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
from src.gateway.multi_region.region_router import RegionRouter
from src.gateway.multi_region.spoke_loader import load_hub_config
from src.gateway.quota_enforcer import QuotaEnforcer
from src.gateway.middleware.security import SecurityMiddleware
from src.gateway.observability.otlp_exporter import OTLPSpanExporter
from src.gateway.observability.trace_forwarder import TraceForwarder
from src.gateway.security.audit_trail import AuditEventType, AuditTrail
from src.gateway.security.event_dispatcher import (
    DestinationType,
    EventDestination,
    EventDispatcher,
)
from src.gateway.security.injection_detector import PromptInjectionDetector
from src.gateway.security.pii_redactor import PIIRedactor
from src.gateway.cache_manager import CacheManager
from src.gateway.chat.client_agent import ClientAgent
from src.gateway.chat.routes import ChatAPI, create_chat_routes
from src.gateway.chat.openai_routes import OpenAICompatAPI, create_openai_routes
from src.gateway.config import AppConfig
from src.gateway.config_loader import (
    DemoSeedData,
    load_app_config,
    load_catalog_config,
    load_demo_seed_config,
    load_ensemble_config,
    load_pricing_config,
)
from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import PolicyNode, Project, RateLimitConfig, UsageRecord
from src.gateway.multi_provider_factory import MultiProviderFactory
from src.gateway.persistence import DynamoPersistence
from src.gateway.provider_config import ProviderConfig
from src.gateway.provider_loader import load_provider_configs
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.request_validator import RequestValidator
from src.gateway.router import Router
from src.gateway.semantic_efficiency import SemanticEfficiencyEngine
from src.gateway.smart_routing import SmartRoutingStrategy
from src.gateway.task_classifier import TaskClassifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GatewayComponents container
# ---------------------------------------------------------------------------


@dataclass
class GatewayComponents:
    """All constructed gateway components returned by the bootstrap."""

    cost_tracker: CostTracker
    health_tracker: ProviderHealthTracker
    registry: ModelRegistry
    router: Router
    rate_limiter: SlidingWindowRateLimiter
    guardrail_engine: GuardrailEngine
    cache_manager: CacheManager
    multi_factory: MultiProviderFactory
    request_validator: RequestValidator
    gateway_agent: GatewayAgent
    projects: dict[str, Project]
    user_configs: dict[str, dict]
    policies: list[dict]
    persistence: DynamoPersistence
    catalog: dict
    api_key_service: APIKeyService | None = None
    oidc_service: OIDCService | None = None
    scim_store: ScimStore | None = None
    saml_service: SamlService | None = None
    policy_resolver: PolicyHierarchyResolver | None = None
    quota_enforcer: QuotaEnforcer | None = None
    pii_redactor: PIIRedactor | None = None
    injection_detector: PromptInjectionDetector | None = None
    audit_trail: AuditTrail | None = None
    event_dispatcher: EventDispatcher | None = None
    region_router: RegionRouter | None = None
    health_monitor: SpokeHealthMonitor | None = None
    efficiency_analyzer: EfficiencyAnalyzer | None = None
    semantic_engine: SemanticEfficiencyEngine | None = None
    # Providers whose credentials loaded. load_provider_configs drops the rest,
    # so this is also the set the readiness checklist can distinguish
    # "configured" from "in models.yaml but unusable".
    provider_configs: dict[str, ProviderConfig] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_gateway_components(app_config: AppConfig | None = None) -> GatewayComponents:
    """Construct all gateway components from configuration.

    If *app_config* is ``None``, one is loaded from environment variables.
    """
    if app_config is None:
        app_config = load_app_config()

    # --- Pricing ---
    pricing = load_pricing_config(app_config.pricing_config_path)

    # --- Persistence ---
    persistence = DynamoPersistence(region=app_config.aws_region)
    if persistence.enabled:
        asyncio.run(
            persistence.create_table_if_not_exists()
        )

    # --- Auth services ---
    api_key_service = APIKeyService(persistence=persistence)
    oidc_config = OIDCConfig(
        issuer=app_config.oidc_issuer,
        audience=app_config.oidc_audience,
        alb_region=app_config.aws_region,
    )
    oidc_service = OIDCService(config=oidc_config)

    # --- Enterprise identity: SCIM provisioning + SAML SSO ---
    scim_store = ScimStore(persistence=persistence)
    if persistence.enabled:
        asyncio.run(scim_store.initialize())
    saml_service = SamlService(config=load_saml_config())

    policy_resolver = PolicyHierarchyResolver(persistence=persistence)
    if persistence.enabled:
        asyncio.run(policy_resolver.load_nodes())

    # --- Quota enforcement ---
    quota_enforcer = QuotaEnforcer()

    # --- Multi-region ---
    # Load a real hub/spoke topology from spokes.yaml when present; otherwise a
    # single-region default (single-region deploys need no config file).
    hub_config = load_hub_config(
        app_config.spokes_config_path, default_region=app_config.aws_region)
    region_router = RegionRouter(hub_config=hub_config)
    health_monitor = SpokeHealthMonitor(hub_config=hub_config)

    # --- Security services ---
    pii_redactor = PIIRedactor()
    injection_detector = PromptInjectionDetector()
    audit_trail = AuditTrail(persistence=persistence)
    # Reload the hash-chain head so audit continuity survives restarts. Loop-safe:
    # runs now when standalone, defers to the running loop when embedded (Ostiari)
    # — never calls asyncio.run inside an active loop.
    audit_trail.initialize_sync()
    event_dispatcher = EventDispatcher()
    # Forwards request traces to an embedding Ostiari when detected (OSTIARI_TRACES_URL
    # set, or an in-process sink registered via observability.trace_forwarder). No-op
    # for standalone AxonLLM.
    trace_forwarder = TraceForwarder()
    # Native OTLP span export for the STANDALONE deploy (opt-in via
    # OTEL_EXPORTER_OTLP_ENDPOINT). Suppressed by the agent when embedded in
    # Ostiari — Ostiari emits the governance span there. No-op if OTEL SDK absent.
    otlp_exporter = OTLPSpanExporter()

    # --- Budget threshold alerting ---
    def _budget_alert(project_id, threshold_pct, current_spend, budget_limit):
        import asyncio
        from src.gateway.security.event_dispatcher import SecurityEvent
        from datetime import timezone
        event = SecurityEvent(
            event_id=f"budget_{project_id}_{int(threshold_pct * 100)}",
            event_type="budget_threshold",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="warning" if threshold_pct < 1.0 else "critical",
            project_id=project_id,
            data={
                "threshold_pct": threshold_pct * 100,
                "current_spend": current_spend,
                "budget_limit": budget_limit,
            },
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(event_dispatcher.dispatch(event))
        except RuntimeError:
            logger.warning(
                "Budget alert dispatch skipped (no running loop) for %s at %d%%",
                project_id, int(threshold_pct * 100),
            )

    quota_enforcer.on_budget_alert(_budget_alert)

    # --- Core components ---
    cost_tracker = CostTracker(pricing_config=pricing, persistence=persistence)
    health_tracker = ProviderHealthTracker()

    registry = ModelRegistry()
    registry.load(app_config.models_config_path)
    all_model_names = list(registry.models.keys())

    # --- Demo seed data ---
    projects: dict[str, Project] = {}
    user_configs: dict[str, dict] = {}
    policies: list[dict] = []

    if app_config.load_demo_data:
        seed = load_demo_seed_config(app_config.demo_seed_config_path)
        projects, user_configs, policies = _apply_seed_data(
            seed, cost_tracker, health_tracker, all_model_names,
            quota_enforcer, policy_resolver,
            audit_trail, api_key_service, event_dispatcher,
        )

    # --- DynamoDB persisted state (merges on top of seed data) ---
    loaded_feedback: list = []
    if persistence.enabled:
        loaded_projects, loaded_user_configs, loaded_records, loaded_feedback = (
            asyncio.run(_load_persisted_state(persistence))
        )
        projects.update(loaded_projects)
        user_configs.update(loaded_user_configs)
        # Rehydrate via load_records so the running spend counters (which back
        # budget checks) are seeded from history, not just the record list.
        cost_tracker.load_records(loaded_records)

    # --- Smart routing components ---
    leaderboard = ModelLeaderboard()
    leaderboard.load("config/leaderboard.yaml", valid_models=set(all_model_names))

    task_classifier = TaskClassifier()
    feedback_tracker = FeedbackTracker(persistence=persistence)
    if loaded_feedback:
        feedback_tracker._records.extend(loaded_feedback)

    smart_strategy = SmartRoutingStrategy(
        classifier=task_classifier,
        leaderboard=leaderboard,
        model_registry=registry,
        health_tracker=health_tracker,
        cost_tracker=cost_tracker,
        feedback_tracker=feedback_tracker,
        confidence_threshold=leaderboard.config.get("confidence_threshold", 0.3),
        cost_quality_tradeoff=leaderboard.config.get("cost_quality_tradeoff", 0.3),
        default_model=leaderboard.config.get("default_model", "claude-sonnet"),
        # The same table CostTracker bills from. models.yaml carries no inline
        # pricing, so without this the cost half of cost_quality_tradeoff has
        # nothing to read and collapses to a constant.
        pricing_config=pricing,
    )

    # --- Routing / rate limiting / guardrails / cache ---
    ensemble_config = load_ensemble_config(app_config.ensemble_config_path)
    router = Router(
        model_registry=registry,
        health_tracker=health_tracker,
        smart_strategy=smart_strategy,
        ensemble_config=ensemble_config,
        cost_tracker=cost_tracker,
    )
    rate_limiter = SlidingWindowRateLimiter(config=RateLimitConfig())
    guardrail_engine = GuardrailEngine()
    cache_manager = CacheManager()

    # --- Multi-provider factory ---
    provider_configs = load_provider_configs(app_config.providers_config_path)
    multi_factory = MultiProviderFactory(
        provider_configs=provider_configs,
        bedrock_region=app_config.bedrock_region,
    )

    # --- Request validator ---
    request_validator = RequestValidator(model_registry=registry)

    # --- Gateway agent ---
    gateway_agent = GatewayAgent(
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        projects=projects,
        provider_fn_factory=multi_factory,
        user_configs=user_configs,
        request_validator=request_validator,
        smart_routing_enabled=True,
        quota_enforcer=quota_enforcer,
        policy_resolver=policy_resolver,
        pii_redactor=pii_redactor,
        injection_detector=injection_detector,
        audit_trail=audit_trail,
        event_dispatcher=event_dispatcher,
        region_router=region_router,
        trace_forwarder=trace_forwarder,
        otlp_exporter=otlp_exporter,
    )

    # --- Efficiency analysis ---
    efficiency_analyzer = EfficiencyAnalyzer(cost_tracker=cost_tracker)
    semantic_engine = SemanticEfficiencyEngine(
        task_classifier=task_classifier,
        cost_tracker=cost_tracker,
        model_registry=registry,
        leaderboard=leaderboard,
    )

    # --- Catalog ---
    catalog = load_catalog_config(
        app_config.catalog_config_path, fallback=PROVIDER_MODEL_CATALOG,
    )

    return GatewayComponents(
        cost_tracker=cost_tracker,
        health_tracker=health_tracker,
        registry=registry,
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        multi_factory=multi_factory,
        request_validator=request_validator,
        gateway_agent=gateway_agent,
        projects=projects,
        user_configs=user_configs,
        policies=policies,
        persistence=persistence,
        catalog=catalog,
        api_key_service=api_key_service,
        oidc_service=oidc_service,
        scim_store=scim_store,
        saml_service=saml_service,
        policy_resolver=policy_resolver,
        quota_enforcer=quota_enforcer,
        pii_redactor=pii_redactor,
        injection_detector=injection_detector,
        audit_trail=audit_trail,
        event_dispatcher=event_dispatcher,
        region_router=region_router,
        health_monitor=health_monitor,
        efficiency_analyzer=efficiency_analyzer,
        semantic_engine=semantic_engine,
        provider_configs=provider_configs,
    )


# ---------------------------------------------------------------------------
# Starlette app builder (used by serve_dashboard.py)
# ---------------------------------------------------------------------------


def build_starlette_app(app_config: AppConfig | None = None) -> Starlette:
    """Build a fully-wired Starlette application."""
    if app_config is None:
        app_config = load_app_config()

    comp = build_gateway_components(app_config)

    admin_api = AdminAPI(
        cost_tracker=comp.cost_tracker,
        health_tracker=comp.health_tracker,
        model_registry=comp.registry,
        projects=comp.projects,
        policies=comp.policies,
        user_configs=comp.user_configs,
        config_path=app_config.models_config_path,
        persistence=comp.persistence,
        catalog=comp.catalog,
        efficiency_analyzer=comp.efficiency_analyzer,
        semantic_engine=comp.semantic_engine,
        pricing_path=app_config.pricing_config_path,
        # For the production-readiness checklist: the settings this process booted
        # with, and the providers whose credentials actually loaded.
        app_config=app_config,
        provider_configs=comp.provider_configs,
    )

    # Key, policy, audit, webhook, region, and quota admin APIs
    key_api = KeyManagementAPI(api_key_service=comp.api_key_service)
    policy_api = PolicyHierarchyAPI(resolver=comp.policy_resolver)
    audit_api = AuditAPI(audit_trail=comp.audit_trail)
    webhook_api = WebhookAPI(dispatcher=comp.event_dispatcher)
    region_api = RegionAPI(router=comp.region_router, monitor=comp.health_monitor)
    quota_api = QuotaAPI(quota_enforcer=comp.quota_enforcer, policy_resolver=comp.policy_resolver)
    scim_api = ScimAPI(store=comp.scim_store)
    saml_api = SamlAPI(service=comp.saml_service)

    # Default chat project is the first demo project or "default"
    default_project = next(iter(comp.projects), "default")
    client_agent = ClientAgent(
        comp.gateway_agent,
        default_project_id=default_project,
        default_user_id="chat-user",
    )
    chat_api = ChatAPI(client_agent)
    openai_api = OpenAICompatAPI(client_agent)

    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    routes = (
        [Route("/health", health_check)]
        + create_admin_routes(admin_api)
        + create_key_routes(key_api)
        + create_policy_hierarchy_routes(policy_api)
        + create_audit_routes(audit_api)
        + create_webhook_routes(webhook_api)
        + create_region_routes(region_api)
        + create_quota_routes(quota_api)
        + create_scim_routes(scim_api)
        + create_saml_routes(saml_api)
        + create_chat_routes(chat_api)
        + create_openai_routes(openai_api)
        # Last: this one is a bare "/{path}" serving site/, and Starlette matches
        # in order, so anything after it would be unreachable.
        + create_site_routes(admin_api)
    )

    # Lifespan: run the spoke health monitor in the background for real
    # multi-region deployments. Single-region (one spoke) doesn't need it, so we
    # skip it there to avoid a pointless timer.
    monitor = comp.health_monitor
    multi_region = monitor is not None and not monitor.config.is_single_region

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        if multi_region:
            await monitor.start()
            logger.info("Spoke health monitor started (%d spokes)",
                        len(monitor.config.spokes))
        try:
            yield
        finally:
            if multi_region:
                await monitor.stop()

    app = Starlette(routes=routes, lifespan=_lifespan)

    # Security middleware (lightweight marker for LLM endpoints)
    app.add_middleware(SecurityMiddleware)

    # Admin RBAC (runs after auth, checks role/scope on /admin/* paths)
    app.add_middleware(AdminRBACMiddleware, mode=app_config.auth_mode)

    # Auth middleware (outermost — runs first on every request)
    app.add_middleware(
        AuthMiddleware,
        oidc_service=comp.oidc_service,
        api_key_service=comp.api_key_service,
        policy_service=CedarPolicyService(comp.policies) if comp.policies else None,
        mode=app_config.auth_mode,
    )

    return app


# ---------------------------------------------------------------------------
# Agent-only builder (used by agentcore_agent.py)
# ---------------------------------------------------------------------------


def build_gateway_agent(app_config: AppConfig | None = None) -> GatewayAgent:
    """Build and return just the GatewayAgent (no HTTP routes)."""
    comp = build_gateway_components(app_config)
    return comp.gateway_agent


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_seed_data(
    seed: DemoSeedData,
    cost_tracker: CostTracker,
    health_tracker: ProviderHealthTracker,
    all_model_names: list[str],
    quota_enforcer: QuotaEnforcer,
    policy_resolver: PolicyHierarchyResolver,
    audit_trail: AuditTrail,
    api_key_service: APIKeyService,
    event_dispatcher: EventDispatcher,
) -> tuple[dict[str, Project], dict[str, dict], list[dict]]:
    """Apply demo seed data to components. Returns (projects, user_configs, policies)."""
    projects: dict[str, Project] = {}
    for p in seed.projects:
        proj = Project(
            project_id=p["project_id"],
            name=p["name"],
            budget_limit=p.get("budget_limit"),
            alert_threshold=p.get("alert_threshold"),
            cache_enabled=p.get("cache_enabled", False),
            prompt_caching_enabled=p.get("prompt_caching_enabled", False),
            members=p.get("members", []),
            allowed_models=p.get("allowed_models"),
        )
        projects[proj.project_id] = proj
        if proj.budget_limit is not None or proj.alert_threshold is not None:
            cost_tracker.register_project(
                proj.project_id,
                budget_limit=proj.budget_limit,
                alert_threshold=proj.alert_threshold,
            )
        # Seed a policy node so the quota endpoint can resolve the project's
        # budget_limit (the resolver reads limits from PolicyNodes, not projects).
        if proj.budget_limit is not None:
            policy_resolver._nodes[proj.project_id] = PolicyNode(
                node_id=proj.project_id,
                node_type="project",
                parent_id=None,
                display_name=proj.name,
                limits={"budget_limit": proj.budget_limit},
            )

    # User budgets
    user_configs: dict[str, dict] = {}
    for ub in seed.user_budgets:
        cost_tracker.register_user(
            ub["user_id"],
            budget_limit=ub.get("budget_limit"),
            alert_threshold=ub.get("alert_threshold"),
        )

    # Usage seeds
    async def _seed_usage():
        now = datetime.now(timezone.utc)
        for i, s in enumerate(seed.usage_seeds):
            pt = s.get("prompt_tokens", 0)
            ct = s.get("completion_tokens", 0)
            await cost_tracker.record_usage(UsageRecord(
                # Indexed, because project+user+provider is not unique: several
                # seeded calls share all three, and identical request ids read as
                # one request retried rather than a populated trace log.
                request_id=f"req-{i:04d}-{s['project_id']}-{s['user_id']}",
                project_id=s["project_id"],
                user_id=s["user_id"],
                provider=s["provider"],
                model=s["model"],
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
                cost=s.get("cost", 0.0),
                # Spread over the window the seed asks for. Stamping every record
                # at import time puts the whole trace log on one clock minute,
                # which is not what a live gateway looks like.
                timestamp=now - timedelta(minutes=float(s.get("minutes_ago", 0))),
                # The dashboard shows an average latency tile; unset it reads
                # 0ms, i.e. a gateway that answered instantly.
                latency_ms=float(s.get("latency_ms", 0.0)),
                cached_tokens=s.get("cached_tokens", 0),
                cache_creation_tokens=s.get("cache_creation_tokens", 0),
            ))
            # Mirror spend into the quota enforcer so /admin/quotas reflects
            # seeded usage (enforcer tracks spend separately from cost_tracker).
            await quota_enforcer.record_spend(
                s["project_id"],
                s.get("cost", 0.0),
                budget_limit=projects[s["project_id"]].budget_limit
                if s["project_id"] in projects else None,
            )

    asyncio.run(_seed_usage())

    # Unhealthy providers
    for up in seed.unhealthy_providers:
        health_tracker.mark_unhealthy(
            up["provider"],
            cooldown_seconds=up.get("cooldown_seconds", 600),
        )

    # API keys. Issued through the real service so the dashboard sees genuine
    # hashed records; the raw key is discarded because nothing can display it
    # after issuance anyway.
    async def _seed_api_keys():
        for k in seed.api_keys:
            expires_at = None
            expires_in_days = k.get("expires_in_days")
            if expires_in_days is not None:
                expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            record, _raw = await api_key_service.issue_key(
                project_id=k["project_id"],
                name=k.get("name", "Unnamed key"),
                scopes=k.get("scopes", ["chat:invoke"]),
                created_by=k.get("created_by", "admin"),
                expires_at=expires_at,
            )
            if k.get("revoked"):
                await api_key_service.revoke_key(record.key_id)

    if seed.api_keys:
        asyncio.run(_seed_api_keys())

    # Audit events. Recorded through AuditTrail.record so each entry gets a real
    # hash-chain link -- the Audit Log page verifies chain integrity, and
    # hand-built records would fail that check.
    async def _seed_audit_events():
        for ev in seed.audit_events:
            try:
                event_type = AuditEventType(ev["event_type"])
            except (KeyError, ValueError):
                logger.warning(
                    "Demo seed: skipping audit event with invalid event_type %r",
                    ev.get("event_type"),
                )
                continue
            await audit_trail.record(
                event_type=event_type,
                user_id=ev.get("user_id", "unknown"),
                project_id=ev.get("project_id", "unknown"),
                request_id=ev.get("request_id", "req-demo"),
                data=ev.get("data", {}),
            )

    if seed.audit_events:
        asyncio.run(_seed_audit_events())

    # Webhook destinations
    for wd in seed.webhook_destinations:
        try:
            dest_type = DestinationType(wd.get("type", "webhook"))
        except ValueError:
            logger.warning(
                "Demo seed: skipping webhook destination %r with invalid type %r",
                wd.get("name"), wd.get("type"),
            )
            continue
        event_dispatcher.add_destination(EventDestination(
            name=wd["name"],
            destination_type=dest_type,
            config=wd.get("config", {}),
            event_filter=wd.get("event_filter"),
            enabled=wd.get("enabled", True),
        ))

    return projects, user_configs, seed.policies


async def _load_persisted_state(persistence: DynamoPersistence):
    """Load projects, user configs, usage records, and feedback from DynamoDB."""
    loaded_projects = await persistence.load_projects()
    loaded_user_configs = await persistence.load_user_configs()
    loaded_records = await persistence.load_usage_records()
    loaded_feedback = await persistence.load_feedback_records()
    return loaded_projects, loaded_user_configs, loaded_records, loaded_feedback
