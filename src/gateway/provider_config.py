"""Provider configuration for the HTTP Client Layer."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.gateway.models import ProviderModelMapping
from src.gateway.router import ProviderError

SUPPORTED_AUTH_TYPES: frozenset[str] = frozenset(
    {"api_key", "aws_credentials", "azure_key", "gcp_service_account"}
)


@dataclass
class ProviderConfig:
    """Connection configuration for a single LLM provider."""

    provider_name: str
    base_url: str
    auth_type: str  # "api_key" | "aws_credentials" | "azure_key" | "gcp_service_account"
    credentials: dict[str, str]
    connect_timeout: float = 30.0
    read_timeout: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.auth_type not in SUPPORTED_AUTH_TYPES:
            raise ProviderError(
                status_code=400,
                provider=self.provider_name,
                message=(
                    f"Unsupported auth_type '{self.auth_type}'. "
                    f"Must be one of: {', '.join(sorted(SUPPORTED_AUTH_TYPES))}"
                ),
            )

# ---------------------------------------------------------------------------
# Authentication header generation
# ---------------------------------------------------------------------------


def _require_credential(config: ProviderConfig, key: str) -> str:
    """Return a credential value or raise ProviderError(401) if missing/empty."""
    value = config.credentials.get(key, "")
    if not value:
        raise ProviderError(
            status_code=401,
            provider=config.provider_name,
            message=f"Missing or empty credential '{key}' for provider '{config.provider_name}'",
        )
    return value


def _api_key_headers(config: ProviderConfig) -> dict[str, str]:
    """Produce auth headers for the ``api_key`` auth type.

    Anthropic uses ``x-api-key``; Google AI uses query param (no header needed);
    all other providers use ``Authorization: Bearer``.
    """
    api_key = _require_credential(config, "api_key")
    if config.provider_name == "anthropic":
        return {"x-api-key": api_key}
    if config.provider_name == "google_ai":
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _azure_key_headers(config: ProviderConfig) -> dict[str, str]:
    """Produce auth headers for the ``azure_key`` auth type."""
    api_key = _require_credential(config, "api_key")
    return {"api-key": api_key}


def _aws_sig_v4_headers(config: ProviderConfig) -> dict[str, str]:
    """Produce a simplified AWS SigV4 ``Authorization`` header.

    This is *not* a full SigV4 implementation — it emits a header with the
    ``AWS4-HMAC-SHA256`` prefix and the configured credential components so
    that downstream code (or tests) can verify the right pieces are present.
    """
    access_key = _require_credential(config, "access_key")
    secret_key = _require_credential(config, "secret_key")
    region = _require_credential(config, "region")
    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{region}/bedrock/aws4_request, "
            f"SignedHeaders=host;x-amz-date, "
            f"Signature={secret_key}"
        ),
    }


def _gcp_bearer_headers(config: ProviderConfig) -> dict[str, str]:
    """Produce auth headers for the ``gcp_service_account`` auth type."""
    access_token = _require_credential(config, "access_token")
    return {"Authorization": f"Bearer {access_token}"}


# Dispatch table: auth_type → handler
_AUTH_DISPATCH: dict[str, callable] = {
    "api_key": _api_key_headers,
    "azure_key": _azure_key_headers,
    "aws_credentials": _aws_sig_v4_headers,
    "gcp_service_account": _gcp_bearer_headers,
}


def get_auth_headers(config: ProviderConfig) -> dict[str, str]:
    """Return authentication headers for the given provider configuration.

    Dispatches to the appropriate handler based on ``config.auth_type``.
    Raises ``ProviderError(401)`` when credentials are missing or empty.
    """
    handler = _AUTH_DISPATCH.get(config.auth_type)
    if handler is None:
        raise ProviderError(
            status_code=401,
            provider=config.provider_name,
            message=f"No auth handler for auth_type '{config.auth_type}'",
        )
    return handler(config)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def _openai_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    return f"{config.base_url}/v1/chat/completions"


def _anthropic_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    return f"{config.base_url}/v1/messages"


def _azure_openai_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    return (
        f"{config.base_url}/openai/deployments/{mapping.model_id}"
        f"/chat/completions?api-version=2024-02-01"
    )


def _bedrock_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    return f"{config.base_url}/model/{mapping.model_id}/invoke"


def _mantle_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    return f"{config.base_url}/v1/chat/completions"


def _vertex_ai_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    project = config.extra_params.get("project", "")
    location = config.extra_params.get("location", "")
    return (
        f"{config.base_url}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{mapping.model_id}:generateContent"
    )


def _cohere_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    return f"{config.base_url}/v1/chat"


def _google_ai_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    api_key = config.credentials.get("api_key", "")
    return (
        f"{config.base_url}/v1beta/models/{mapping.model_id}:generateContent"
        f"?key={api_key}"
    )


def _google_ai_stream_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    api_key = config.credentials.get("api_key", "")
    return (
        f"{config.base_url}/v1beta/models/{mapping.model_id}:streamGenerateContent"
        f"?alt=sse&key={api_key}"
    )


_URL_DISPATCH: dict[str, callable] = {
    "openai": _openai_url,
    "anthropic": _anthropic_url,
    "azure_openai": _azure_openai_url,
    "bedrock": _bedrock_url,
    "bedrock-mantle": _mantle_url,
    "vertex_ai": _vertex_ai_url,
    "google_ai": _google_ai_url,
    "cohere": _cohere_url,
    "xai": _openai_url,
    "groq": _openai_url,
    "together": _openai_url,
    "fireworks": _openai_url,
    "ai21": _openai_url,
}


def build_provider_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    """Construct the full endpoint URL for the given provider and model.

    Uses a dispatch dict keyed by provider name. Each entry is a callable
    ``(config, mapping) -> str``. Provider-specific path parameters (project,
    location) come from ``config.extra_params``.

    Raises ``ProviderError(400)`` for unsupported provider names.
    """
    builder = _URL_DISPATCH.get(config.provider_name)
    if builder is None:
        raise ProviderError(
            status_code=400,
            provider=config.provider_name,
            message=f"No URL builder for provider '{config.provider_name}'",
        )
    return builder(config, mapping)


_STREAM_URL_DISPATCH: dict[str, callable] = {
    "google_ai": _google_ai_stream_url,
}


def build_provider_stream_url(config: ProviderConfig, mapping: ProviderModelMapping) -> str:
    """Construct the streaming endpoint URL. Falls back to the standard URL."""
    builder = _STREAM_URL_DISPATCH.get(config.provider_name)
    if builder is not None:
        return builder(config, mapping)
    return build_provider_url(config, mapping)
