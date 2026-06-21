"""Unit tests for src.gateway.bootstrap."""

from __future__ import annotations

import os

import pytest

from src.gateway.bootstrap import (
    GatewayComponents,
    build_gateway_components,
    build_starlette_app,
    build_gateway_agent,
)
from src.gateway.agent import GatewayAgent
from src.gateway.config import AppConfig


@pytest.fixture
def demo_app_config() -> AppConfig:
    """AppConfig pointing at real config files with demo data enabled."""
    return AppConfig(
        models_config_path="config/models.yaml",
        providers_config_path="config/providers.yaml",
        pricing_config_path="config/pricing.yaml",
        demo_seed_config_path="config/demo_seed.yaml",
        catalog_config_path="config/catalog.yaml",
        load_demo_data=True,
    )


@pytest.fixture
def minimal_app_config() -> AppConfig:
    """AppConfig with demo data disabled."""
    return AppConfig(
        models_config_path="config/models.yaml",
        providers_config_path="config/providers.yaml",
        pricing_config_path="config/pricing.yaml",
        demo_seed_config_path="config/demo_seed.yaml",
        catalog_config_path="config/catalog.yaml",
        load_demo_data=False,
    )


class TestBuildGatewayComponents:
    def test_returns_gateway_components(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        assert isinstance(comp, GatewayComponents)
        assert comp.cost_tracker is not None
        assert comp.health_tracker is not None
        assert comp.registry is not None
        assert comp.router is not None
        assert comp.gateway_agent is not None

    def test_demo_data_loaded(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        assert "proj-alpha" in comp.projects
        assert "proj-beta" in comp.projects
        assert len(comp.policies) > 0
        # Usage seeds should have been recorded
        assert len(comp.cost_tracker._records) > 0

    def test_no_demo_data_when_disabled(self, minimal_app_config: AppConfig):
        comp = build_gateway_components(minimal_app_config)
        assert len(comp.projects) == 0
        assert len(comp.policies) == 0
        assert len(comp.cost_tracker._records) == 0

    def test_pricing_loaded(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        # Pricing should have been loaded from config/pricing.yaml
        assert len(comp.cost_tracker.pricing_config) > 0
        assert "openai" in comp.cost_tracker.pricing_config

    def test_catalog_loaded(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        assert isinstance(comp.catalog, dict)
        assert len(comp.catalog) > 0


class TestBuildStaletteApp:
    def test_returns_starlette_app(self, demo_app_config: AppConfig):
        app = build_starlette_app(demo_app_config)
        assert app is not None
        assert hasattr(app, "routes")


class TestBuildGatewayAgent:
    def test_returns_gateway_agent(self, minimal_app_config: AppConfig):
        agent = build_gateway_agent(minimal_app_config)
        assert isinstance(agent, GatewayAgent)
