"""Standalone container host contracts."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.gateway.config import AppConfig
from src.gateway import standalone


def test_build_app_uses_fail_closed_runtime_configuration(monkeypatch) -> None:
    config = AppConfig(
        deployment_profile="development",
        auth_mode="LOG_ONLY",
    )
    app = object()
    loader = Mock(return_value=config)
    builder = Mock(return_value=app)
    monkeypatch.setattr(standalone, "load_app_config", loader)
    monkeypatch.setattr(standalone, "build_starlette_app", builder)

    built_app, built_config = standalone.build_app()

    assert built_app is app
    assert built_config is config
    loader.assert_called_once_with()
    builder.assert_called_once_with(config)


@pytest.mark.parametrize("value", ("0", "121", "1.5", "invalid"))
def test_graceful_shutdown_timeout_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="AXON_GRACEFUL_SHUTDOWN_SECONDS"):
        standalone._graceful_shutdown_seconds({"AXON_GRACEFUL_SHUTDOWN_SECONDS": value})


def test_graceful_shutdown_timeout_defaults_and_accepts_fargate_limit() -> None:
    assert standalone._graceful_shutdown_seconds({}) == 30
    assert standalone._graceful_shutdown_seconds({"AXON_GRACEFUL_SHUTDOWN_SECONDS": "120"}) == 120


def test_main_binds_configured_listener_and_graceful_timeout(
    monkeypatch,
) -> None:
    app = object()
    config = AppConfig(
        deployment_profile="development",
        auth_mode="LOG_ONLY",
        server_host="127.0.0.1",
        server_port=8123,
    )
    runner = Mock()
    monkeypatch.setattr(standalone, "build_app", lambda: (app, config))
    monkeypatch.setattr(standalone.uvicorn, "run", runner)
    monkeypatch.setenv("AXON_GRACEFUL_SHUTDOWN_SECONDS", "45")

    standalone.main()

    runner.assert_called_once_with(
        app,
        host="127.0.0.1",
        port=8123,
        timeout_graceful_shutdown=45,
    )
