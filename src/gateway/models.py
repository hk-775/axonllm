"""Core data models and types for the LLM-Router service."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# --- Enums ---


class RoutingStrategy(Enum):
    """Routing strategies for distributing requests across providers."""

    ROUND_ROBIN = "round-robin"
    WEIGHTED = "weighted"
    LEAST_LATENCY = "least-latency"
    COST_OPTIMIZED = "cost-optimized"
    SMART = "smart"
    ENSEMBLE = "ensemble"


class HealthStatus(Enum):
    """Health status for provider health checks."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


# --- Chat Completion ---


@dataclass
class ChatCompletionRequest:
    """OpenAI-compatible chat completion request."""

    messages: list[dict]
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    stream: bool = False
    system: str | None = None


@dataclass
class TokenUsage:
    """Token usage statistics for a completion request."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class ChatCompletionResponse:
    """Unified chat completion response in OpenAI-compatible format."""

    id: str
    choices: list[dict]
    usage: TokenUsage
    model: str
    provider: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class StreamChunk:
    """A single chunk in a streaming completion response."""

    id: str
    choices: list[dict]
    model: str
    is_final: bool = False


# --- Model Registry & Pricing ---


@dataclass
class TokenPricing:
    """Per-token pricing for a provider/model combination.

    Beyond basic input/output token costs, supports:
    - cached_token_cost: discounted rate for cached input tokens (OpenAI, Anthropic)
    - image_token_cost: rate for image/vision tokens (multimodal models)
    - reasoning_token_cost: rate for internal reasoning tokens (o1/o3 models)
    - per_request_cost: flat fee per API call (some providers charge this)
    """

    prompt_token_cost: float
    completion_token_cost: float
    cached_token_cost: float | None = None
    cache_creation_token_cost: float | None = None
    image_token_cost: float | None = None
    reasoning_token_cost: float | None = None
    per_request_cost: float = 0.0


@dataclass
class ProviderModelMapping:
    """Maps a virtual model to a specific provider and model identifier."""

    provider: str
    model_id: str
    weight: float = 1.0
    fallback_order: int = 0
    pricing: TokenPricing | None = None


@dataclass
class VirtualModelConfig:
    """Configuration for a virtual model with provider mappings."""

    name: str
    description: str
    providers: list[ProviderModelMapping]
    routing_strategy: RoutingStrategy = RoutingStrategy.ROUND_ROBIN
    capabilities: list[str] | None = None
    max_context_tokens: int | None = None


# --- Guardrails ---


@dataclass
class GuardrailRule:
    """A configurable rule for inspecting requests and responses."""

    name: str
    rule_type: str
    pattern: str | None
    action: str
    applies_to: str


@dataclass
class GuardrailResult:
    """Result of evaluating guardrail rules against a request or response."""

    passed: bool
    violated_rules: list[str]
    message: str | None = None


# --- Project & Configuration ---


@dataclass
class Project:
    """A logical grouping of users, budgets, and configuration."""

    project_id: str
    name: str
    budget_limit: float | None = None
    alert_threshold: float | None = None
    allowed_models: list[str] | None = None
    guardrail_rules: list[GuardrailRule] = field(default_factory=list)
    cache_enabled: bool = False
    cache_ttl_seconds: int = 300
    log_level: str = "INFO"
    log_destination: str | None = None
    prompt_caching_enabled: bool = False
    ltm_enabled: bool = False
    retention_period_hours: int = 24
    rate_limit_rpm: int | None = None
    members: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class RateLimitConfig:
    """Rate limit configuration for users and projects."""

    user_rpm: int = 60
    project_rpm: int = 600
    window_seconds: int = 60


# --- Provider Health ---


@dataclass
class ProviderHealth:
    """Health status of a provider."""

    provider: str
    status: HealthStatus
    latency_ms: float | None = None
    last_check: datetime | None = None
    error_message: str | None = None


# --- Usage & Cost ---


@dataclass
class UsageRecord:
    """A single usage record for a completed request."""

    request_id: str
    project_id: str
    user_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    timestamp: datetime
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    image_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass
class BudgetStatus:
    """Budget status for a project."""

    project_id: str
    current_spend: float
    budget_limit: float | None
    alert_threshold: float | None
    is_over_budget: bool
    is_alert_triggered: bool


@dataclass
class UsageFilters:
    """Filters for querying aggregated usage data."""

    start_time: datetime | None = None
    end_time: datetime | None = None
    provider: str | None = None
    model: str | None = None
    project_id: str | None = None
    user_id: str | None = None


@dataclass
class UsageBreakdown:
    """Usage breakdown by a grouping dimension."""

    group_key: str
    group_by: str
    requests: int
    tokens: int
    cost: float


@dataclass
class UsageReport:
    """Aggregated usage report with breakdown."""

    total_requests: int
    total_tokens: int
    total_cost: float
    breakdown: list[UsageBreakdown]


# --- Rate Limiting ---


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: datetime
    retry_after_seconds: int | None = None


# --- Auth ---


class AuthMethod(Enum):
    """How the request was authenticated."""

    OIDC_JWT = "oidc_jwt"
    API_KEY = "api_key"
    ANONYMOUS = "anonymous"


@dataclass
class RequestContext:
    """Extracted identity claims for request authorization."""

    user_id: str
    project_id: str
    roles: list[str]
    scopes: list[str]
    auth_method: AuthMethod = AuthMethod.ANONYMOUS
    tenant_id: str | None = None
    business_unit: str | None = None
    environment: str | None = None
    api_key_id: str | None = None
    email: str | None = None


@dataclass
class APIKey:
    """A project-scoped API key."""

    key_id: str
    key_hash: str
    project_id: str
    name: str
    scopes: list[str]
    created_by: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    revoked: bool = False
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass
class PolicyNode:
    """A single node in the hierarchical policy tree."""

    node_id: str
    node_type: str  # "org" | "business_unit" | "project" | "environment"
    parent_id: str | None
    display_name: str
    limits: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ResolvedPolicy:
    """Flattened effective policy after hierarchy walk."""

    rate_limit_rpm: int | None = None
    budget_limit: float | None = None
    allowed_models: list[str] | None = None
    max_tokens_per_request: int | None = None
    allowed_providers: list[str] | None = None
    pii_redaction_enabled: bool = False
    pii_redact_types: list[str] | None = None


# --- Validation ---


@dataclass
class ValidationError:
    """A validation error for configuration or request validation."""

    field: str
    message: str
    severity: str = "error"


# --- Logging ---


@dataclass
class RequestLogEntry:
    """Structured log entry for a processed request."""

    request_id: str
    project_id: str
    user_id: str
    model: str
    provider: str
    latency_ms: float
    status_code: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    timestamp: datetime
    trace_id: str | None = None
    is_streaming: bool = False
    is_cached: bool = False
    retry_count: int = 0
    fallback_providers_tried: list[str] = field(default_factory=list)


# --- Model Info ---


@dataclass
class ModelInfo:
    """Information about a provider-specific model."""

    model_id: str
    provider: str
    capabilities: list[str] = field(default_factory=list)


@dataclass
class VirtualModelInfo:
    """Public-facing information about a virtual model."""

    name: str
    description: str
    providers: list[str]
    capabilities: list[str]
    routing_strategy: str


# --- Smart Routing ---


@dataclass
class ClassificationResult:
    """Result of prompt task classification."""

    task_type: str
    confidence: float
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class ModelScore:
    """A model's benchmark score for a task type."""

    model_name: str
    score: float


@dataclass
class SmartRoutingDecision:
    """Metadata about a smart routing decision for observability."""

    task_type: str
    confidence: float
    selected_model: str
    benchmark_score: float
    candidates_considered: list[dict]
    used_fallback: bool
    cost_quality_tradeoff: float


@dataclass
class FeedbackRecord:
    """Record of a smart routing decision for feedback tracking."""

    request_id: str
    timestamp: datetime
    task_type: str
    confidence: float
    selected_model: str
    benchmark_score: float


# --- Token Efficiency ---


class EfficiencyGrade(Enum):
    """Token efficiency grades for users and projects."""

    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    WASTEFUL = "wasteful"


@dataclass
class EfficiencyMetrics:
    """Per-user or per-project token efficiency metrics (Level 1 — ratio-based)."""

    entity_id: str
    entity_type: str
    completion_prompt_ratio: float
    cache_utilization_rate: float
    avg_cost_per_request: float
    expensive_model_ratio: float
    token_velocity_per_hour: float
    duplicate_request_rate: float
    avg_prompt_tokens: float
    avg_completion_tokens: float
    total_requests: int
    total_cost: float
    grade: EfficiencyGrade
    score: float


@dataclass
class EfficiencyAlert:
    """Alert raised when a user or project exceeds efficiency thresholds."""

    entity_id: str
    entity_type: str
    alert_type: str
    severity: str
    message: str
    metric_value: float
    threshold: float
    timestamp: datetime


@dataclass
class ModelRecommendation:
    """Recommendation to use a different model for cost efficiency."""

    current_model: str
    recommended_model: str
    task_type: str
    estimated_savings_pct: float
    quality_impact: str
    reason: str


@dataclass
class EfficiencyReport:
    """Full efficiency report combining metrics, alerts, and recommendations."""

    metrics: EfficiencyMetrics
    alerts: list[EfficiencyAlert]
    recommendations: list[ModelRecommendation]
    peer_comparison: dict


# --- Ensemble Routing ---


@dataclass
class EnsemblePreset:
    """A named ensemble configuration: a panel, a judge, and policy knobs."""

    name: str
    panel: list[str]  # 1..10 model identifiers
    judge: str  # exactly one judge/synthesis model
    quorum: int = 1  # 1..len(panel); default 1
    fallback_policy: str = "error"  # "best-single" | "error"; default "error"
    cost_ceiling: float | None = None  # per-request ceiling in USD; None = no ceiling
    ranking_criteria: str = "length"  # how to rank survivors for best-single fallback


@dataclass
class PanelMemberResult:
    """Outcome of a single panel member call within an ensemble request."""

    model: str
    status: str  # "succeeded" | "failed"
    response: ChatCompletionResponse | None = None
    cost: float = 0.0
    failure_reason: str | None = None  # populated when status == "failed"
    latency_ms: float | None = None


@dataclass
class EnsembleDecision:
    """Observability metadata returned with an ensemble response."""

    preset_name: str
    panel_members: list[str]  # every panel model used
    judge_model: str
    succeeded: list[str]  # survivor model identifiers
    failed: list[dict]  # [{"model": str, "reason": str}, ...]
    quorum_met: bool
    succeeded_count: int
    quorum_threshold: int
    total_cost: float  # sum(survivor costs) + judge cost
    cost_multiplier: float  # N + 1
    fallback_used: bool = False  # True when best-single fallback returned
    judge_invoked: bool = False
    error: str | None = None  # set when quorum not met / synthesis failed
