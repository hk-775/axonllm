"""Centralized configuration for the LLM-Router.

All magic numbers, default values, and provider metadata live here.
Modules import from this file instead of hardcoding values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    aws_region: str = "us-east-1"
    bedrock_region: str = "us-east-1"
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    models_config_path: str = "config/models.yaml"
    providers_config_path: str = "config/providers.yaml"
    pricing_config_path: str = "config/pricing.yaml"
    demo_seed_config_path: str = "config/demo_seed.yaml"
    catalog_config_path: str = "config/catalog.yaml"
    ensemble_config_path: str = "config/ensemble.yaml"
    spokes_config_path: str = "config/spokes.yaml"
    load_demo_data: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = ""
    auth_mode: str = "ENFORCE"  # fail-closed by default; set AXON_AUTH_MODE=LOG_ONLY for local dev only
