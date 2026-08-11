"""Centralized configuration for the LLM-Router.

All magic numbers, default values, and provider metadata live here.
Modules import from this file instead of hardcoding values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


_MAX_ATHENA_QUERY_BINDINGS_CHARACTERS = 2_048


# ---------------------------------------------------------------------------
# Retry / Fallback
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryConfig:
    """Configuration for retry and fallback behaviour in the Router."""

    max_retries: int = 3
    base_delay: float = 1.0
    cooldown_seconds: int = 60
    # Fraction of the backoff delay that is randomized, to avoid synchronized
    # retry storms (thundering herd). 0.0 = no jitter (fixed exponential),
    # 0.5 = delay drawn from [0.5, 1.0] * base*2**attempt.
    jitter: float = 0.5
    retryable_status_codes: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    non_retryable_status_codes: frozenset[int] = frozenset({400, 401, 403})


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RateLimitDefaults:
    """Default rate-limit values used when no per-project override exists."""

    user_rpm: int = 60
    project_rpm: int = 600
    window_seconds: int = 60


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CacheDefaults:
    """Default cache settings."""

    ttl_seconds: int = 300


# ---------------------------------------------------------------------------
# Cost / Token Estimation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenEstimationConfig:
    """Configuration for token estimation fallback."""

    fallback_encoding: str = "cl100k_base"


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdapterDefaults:
    """Shared defaults across provider adapters."""

    default_max_tokens: int = 4096


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

VALID_PROVIDERS: frozenset[str] = frozenset({
    "openai",
    "anthropic",
    "bedrock",
    "bedrock-mantle",
    "azure_openai",
    "vertex_ai",
    "google_ai",
    "cohere",
    "xai",
    "groq",
    "together",
    "fireworks",
    "ai21",
})


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoggingDefaults:
    """Default logging configuration."""

    default_level: str = "INFO"
    logger_name: str = "gateway"


# ---------------------------------------------------------------------------
# Composite gateway config
# ---------------------------------------------------------------------------

@dataclass
class GatewayConfig:
    """Top-level configuration object aggregating all sub-configs."""

    retry: RetryConfig = field(default_factory=RetryConfig)
    rate_limit: RateLimitDefaults = field(default_factory=RateLimitDefaults)
    cache: CacheDefaults = field(default_factory=CacheDefaults)
    token_estimation: TokenEstimationConfig = field(default_factory=TokenEstimationConfig)
    adapter: AdapterDefaults = field(default_factory=AdapterDefaults)
    logging: LoggingDefaults = field(default_factory=LoggingDefaults)
    valid_providers: frozenset[str] = field(default_factory=lambda: VALID_PROVIDERS)


# Module-level default instance — importable as a convenience.
DEFAULT_CONFIG = GatewayConfig()


# ---------------------------------------------------------------------------
# Application Config (env-var driven)
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    """Runtime application settings loaded from environment variables."""

    deployment_profile: str = "development"
    aws_region: str = "us-east-1"
    bedrock_region: str = "us-east-1"
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    models_config_path: str = "config/models.yaml"
    providers_config_path: str = "config/providers.yaml"
    enabled_providers: frozenset[str] | None = None
    pricing_config_path: str = "config/pricing.yaml"
    demo_seed_config_path: str = "config/demo_seed.yaml"
    catalog_config_path: str = "config/catalog.yaml"
    ensemble_config_path: str = "config/ensemble.yaml"
    spokes_config_path: str = "config/spokes.yaml"
    load_demo_data: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_tenant_claim: str = "custom:tenant_id"
    oidc_project_claim: str = "custom:project_id"
    alb_signer_arn: str = ""
    alb_client_id: str = ""
    alb_issuer: str = ""
    auth_mode: str = "ENFORCE"  # fail-closed by default; set AXON_AUTH_MODE=LOG_ONLY for local dev only
    # Migration gate for server-held tenant memberships. Once enabled, every
    # authenticated credential must resolve through durable canonical identity
    # storage; startup refuses an in-memory-only configuration.
    canonical_identity_required: bool = False
    durable_persistence_enabled: bool = False
    # Semantic cache. Off by default at the gateway level *as well as* per
    # project: a project flag can only take effect once an embedder exists, and
    # building one costs a Bedrock dependency at startup. Both must say yes.
    semantic_cache_enabled: bool = False
    semantic_cache_region: str = "us-east-1"
    semantic_cache_model: str = ""  # "" means the embeddings module default
    # None means "use semantic_cache.DEFAULT_SIMILARITY_THRESHOLD". Not 0.0,
    # which would make every comparison a hit.
    semantic_cache_threshold: float | None = None
    # Athena query-plane settings. Query execution and datasource routes are
    # absent unless explicitly enabled.
    athena_query_enabled: bool = False
    athena_query_bindings: str = ""
    athena_query_timeout_seconds: float = 30.0
    athena_query_max_rows: int = 1000
    athena_query_max_result_bytes: int = 1024 * 1024
    athena_query_max_bytes_scanned: int = 1024 * 1024 * 1024
    athena_query_poll_interval_seconds: float = 0.25
    athena_query_project_rpm: int = 30
    athena_query_principal_rpm: int = 10
    athena_query_project_concurrency: int = 5
    athena_query_principal_concurrency: int = 2
    athena_query_project_scan_bytes_per_minute: int = (
        5 * 1024 * 1024 * 1024
    )
    athena_query_principal_scan_bytes_per_minute: int = (
        2 * 1024 * 1024 * 1024
    )
    athena_query_max_datasources_per_tenant: int = 500
    # A dedicated control-plane process serves health and administration only.
    control_plane_only: bool = False

    def __post_init__(self) -> None:
        for field_name, claim_name in (
            ("oidc_tenant_claim", self.oidc_tenant_claim),
            ("oidc_project_claim", self.oidc_project_claim),
        ):
            if (
                not isinstance(claim_name, str)
                or not claim_name
                or len(claim_name) > 256
                or any(character.isspace() for character in claim_name)
            ):
                raise ValueError(
                    f"{field_name} must be a non-empty claim name without "
                    "whitespace"
                )
        if self.enabled_providers is not None:
            if not self.enabled_providers:
                raise ValueError("enabled_providers must not be empty")
            unknown = self.enabled_providers.difference(VALID_PROVIDERS)
            if unknown:
                raise ValueError(
                    "enabled_providers contains unknown providers: "
                    + ", ".join(sorted(unknown))
                )
        if self.deployment_profile not in {"development", "production"}:
            raise ValueError(
                "deployment_profile must be 'development' or 'production'"
            )
        if not isinstance(self.athena_query_bindings, str):
            raise ValueError("athena_query_bindings must be JSON text")
        if (
            len(self.athena_query_bindings)
            > _MAX_ATHENA_QUERY_BINDINGS_CHARACTERS
        ):
            raise ValueError(
                "athena_query_bindings must not exceed the AgentCore "
                "2,048-character environment value limit"
            )
        if (
            isinstance(self.athena_query_timeout_seconds, bool)
            or not isinstance(
                self.athena_query_timeout_seconds,
                (int, float),
            )
            or not math.isfinite(self.athena_query_timeout_seconds)
            or not 0 < self.athena_query_timeout_seconds <= 300
        ):
            raise ValueError(
                "athena_query_timeout_seconds must be between 0 and 300"
            )
        if (
            isinstance(self.athena_query_max_rows, bool)
            or not isinstance(self.athena_query_max_rows, int)
            or not 1 <= self.athena_query_max_rows <= 10_000
        ):
            raise ValueError(
                "athena_query_max_rows must be between 1 and 10000"
            )
        if (
            isinstance(self.athena_query_max_result_bytes, bool)
            or not isinstance(self.athena_query_max_result_bytes, int)
            or not 1024
            <= self.athena_query_max_result_bytes
            <= 16 * 1024 * 1024
        ):
            raise ValueError(
                "athena_query_max_result_bytes must be between 1 KiB "
                "and 16 MiB"
            )
        if (
            isinstance(self.athena_query_max_bytes_scanned, bool)
            or not isinstance(self.athena_query_max_bytes_scanned, int)
            or self.athena_query_max_bytes_scanned <= 0
        ):
            raise ValueError(
                "athena_query_max_bytes_scanned must be positive"
            )
        if (
            isinstance(
                self.athena_query_poll_interval_seconds,
                bool,
            )
            or not isinstance(
                self.athena_query_poll_interval_seconds,
                (int, float),
            )
            or not math.isfinite(
                self.athena_query_poll_interval_seconds
            )
            or not 0.05
            <= self.athena_query_poll_interval_seconds
            <= 5
        ):
            raise ValueError(
                "athena_query_poll_interval_seconds must be between "
                "0.05 and 5"
            )
        admission_limits = (
            "athena_query_project_rpm",
            "athena_query_principal_rpm",
            "athena_query_project_concurrency",
            "athena_query_principal_concurrency",
            "athena_query_project_scan_bytes_per_minute",
            "athena_query_principal_scan_bytes_per_minute",
            "athena_query_max_datasources_per_tenant",
        )
        for field_name in admission_limits:
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ValueError(
                    f"{field_name} must be a positive integer"
                )
        if self.athena_query_max_datasources_per_tenant > 10_000:
            raise ValueError(
                "athena_query_max_datasources_per_tenant must not exceed "
                "10000"
            )
        if (
            self.athena_query_principal_rpm
            > self.athena_query_project_rpm
        ):
            raise ValueError(
                "athena_query_principal_rpm must not exceed "
                "athena_query_project_rpm"
            )
        if (
            self.athena_query_principal_concurrency
            > self.athena_query_project_concurrency
        ):
            raise ValueError(
                "athena_query_principal_concurrency must not exceed "
                "athena_query_project_concurrency"
            )
        if (
            self.athena_query_principal_scan_bytes_per_minute
            > self.athena_query_project_scan_bytes_per_minute
        ):
            raise ValueError(
                "principal query scan budget must not exceed the project "
                "query scan budget"
            )
        if (
            self.athena_query_max_bytes_scanned
            > self.athena_query_principal_scan_bytes_per_minute
        ):
            raise ValueError(
                "athena_query_max_bytes_scanned must fit within the "
                "principal aggregate scan budget"
            )
        if self.athena_query_enabled and (
            self.auth_mode != "ENFORCE"
            or not self.canonical_identity_required
            or not self.durable_persistence_enabled
        ):
            raise RuntimeError(
                "Athena queries require enforced canonical identity and "
                "durable DynamoDB persistence"
            )
        if self.deployment_profile != "production":
            return
        if self.auth_mode != "ENFORCE":
            raise RuntimeError(
                "production profile requires AXON_AUTH_MODE=ENFORCE"
            )
        if not self.canonical_identity_required:
            raise RuntimeError(
                "production profile requires "
                "AXON_REQUIRE_CANONICAL_IDENTITY=true"
            )
        if not self.durable_persistence_enabled:
            raise RuntimeError(
                "production profile requires "
                "LLM_ROUTER_DYNAMODB_ENABLED=true"
            )
