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
    monkeypatch.delenv("AXON_REQUIRE_CANONICAL_IDENTITY", raising=False)

    assert load_app_config().canonical_identity_required is False


def test_canonical_identity_env_enables_the_gate(monkeypatch) -> None:
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "true")

    assert load_app_config().canonical_identity_required is True


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
