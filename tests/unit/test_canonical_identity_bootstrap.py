"""Deployment-gate tests for canonical tenant identity."""

from __future__ import annotations

import pytest

from src.gateway.bootstrap import build_gateway_components, build_starlette_app
from src.gateway.config import AppConfig
from src.gateway.config_loader import load_app_config


def _minimal_app_config() -> AppConfig:
    return AppConfig(
        models_config_path="config/models.yaml",
        providers_config_path="config/providers.yaml",
        pricing_config_path="config/pricing.yaml",
        demo_seed_config_path="config/demo_seed.yaml",
        catalog_config_path="config/catalog.yaml",
        load_demo_data=False,
    )


def test_canonical_identity_env_is_disabled_during_legacy_migration(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
    monkeypatch.delenv("AXON_REQUIRE_CANONICAL_IDENTITY", raising=False)

    config = load_app_config()

    assert config.deployment_profile == "development"
    assert config.canonical_identity_required is False


def test_canonical_identity_env_enables_the_gate(monkeypatch) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "true")

    assert load_app_config().canonical_identity_required is True


def test_ordinary_runtime_defaults_to_production_and_fails_closed(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AXON_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("AXON_LOAD_DEMO_DATA", raising=False)
    monkeypatch.delenv("AXON_REQUIRE_CANONICAL_IDENTITY", raising=False)
    monkeypatch.setenv("AXON_AUTH_MODE", "ENFORCE")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "true")

    with pytest.raises(
        RuntimeError,
        match="AXON_REQUIRE_CANONICAL_IDENTITY=true",
    ):
        load_app_config()


def test_demo_entrypoint_selects_the_development_profile(monkeypatch) -> None:
    monkeypatch.delenv("AXON_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.setenv("AXON_LOAD_DEMO_DATA", "true")
    monkeypatch.setenv("AXON_AUTH_MODE", "LOG_ONLY")
    monkeypatch.delenv("AXON_REQUIRE_CANONICAL_IDENTITY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)

    config = load_app_config()

    assert config.deployment_profile == "development"
    assert config.canonical_identity_required is False


def test_production_profile_requires_canonical_identity(monkeypatch) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("AXON_AUTH_MODE", "ENFORCE")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "true")
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "false")

    with pytest.raises(
        RuntimeError,
        match="AXON_REQUIRE_CANONICAL_IDENTITY=true",
    ):
        load_app_config()


def test_production_profile_requires_durable_persistence(monkeypatch) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("AXON_AUTH_MODE", "ENFORCE")
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "true")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "false")

    with pytest.raises(
        RuntimeError,
        match="LLM_ROUTER_DYNAMODB_ENABLED=true",
    ):
        load_app_config()


def test_production_profile_accepts_the_canonical_durable_contract(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("AXON_AUTH_MODE", "ENFORCE")
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "true")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "true")
    monkeypatch.setenv(
        "AXON_ROUTING_CONFIG_SIGNING_MODE",
        "verify",
    )
    monkeypatch.setenv(
        "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN",
        (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/11111111-2222-3333-4444-555555555555"
        ),
    )

    config = load_app_config()

    assert config.deployment_profile == "production"
    assert config.canonical_identity_required is True
    assert config.durable_persistence_enabled is True


def test_invalid_deployment_profile_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "prodution")

    with pytest.raises(ValueError, match="AXON_DEPLOYMENT_PROFILE"):
        load_app_config()


def test_canonical_identity_refuses_non_durable_startup() -> None:
    app_config = _minimal_app_config()
    app_config.canonical_identity_required = True

    with pytest.raises(
        RuntimeError,
        match="requires DynamoDB persistence",
    ):
        build_gateway_components(app_config)


def test_canonical_identity_refuses_log_only_auth() -> None:
    app_config = _minimal_app_config()
    app_config.canonical_identity_required = True
    app_config.auth_mode = "LOG_ONLY"

    with pytest.raises(
        RuntimeError,
        match="requires AXON_AUTH_MODE=ENFORCE",
    ):
        build_gateway_components(app_config)


def test_legacy_migration_mode_does_not_install_a_resolver() -> None:
    components = build_gateway_components(_minimal_app_config())

    assert components.principal_resolver is None


def test_starlette_auth_middleware_receives_canonical_gate() -> None:
    app = build_starlette_app(_minimal_app_config())
    auth = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls.__name__ == "AuthMiddleware"
    )

    assert auth.kwargs["principal_resolver"] is None
    assert auth.kwargs["require_canonical_principal"] is False
