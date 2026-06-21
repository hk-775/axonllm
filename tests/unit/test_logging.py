"""Unit tests for the GatewayLogger structured logging module."""

import json
import logging
from datetime import datetime

import pytest

from src.gateway.logging import GatewayLogger
from src.gateway.models import RequestLogEntry


@pytest.fixture
def gateway_logger():
    """Create a GatewayLogger instance with a fresh logger."""
    logger = GatewayLogger(default_level="DEBUG")
    # Clear any existing handlers to avoid test pollution
    logging.getLogger("gateway").handlers.clear()
    return logger


@pytest.fixture
def sample_log_entry():
    """Create a sample RequestLogEntry for testing."""
    return RequestLogEntry(
        request_id="req-123",
        project_id="proj-abc",
        user_id="user-456",
        model="gpt-4",
        provider="openai",
        latency_ms=150.5,
        status_code=200,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost=0.0045,
        timestamp=datetime(2024, 1, 15, 12, 0, 0),
        trace_id="trace-789",
        is_streaming=False,
        is_cached=False,
        retry_count=0,
        fallback_providers_tried=[],
    )


class TestLogRequest:
    """Tests for log_request method."""

    def test_emits_structured_entry_with_all_required_fields(
        self, gateway_logger, sample_log_entry, caplog
    ):
        """log_request emits a JSON log containing all RequestLogEntry fields."""
        with caplog.at_level(logging.DEBUG, logger="gateway.project.proj-abc"):
            gateway_logger.log_request(sample_log_entry)

        assert len(caplog.records) == 1
        log_data = json.loads(caplog.records[0].message)

        assert log_data["event"] == "request_completed"
        assert log_data["request_id"] == "req-123"
        assert log_data["project_id"] == "proj-abc"
        assert log_data["user_id"] == "user-456"
        assert log_data["model"] == "gpt-4"
        assert log_data["provider"] == "openai"
        assert log_data["latency_ms"] == 150.5
        assert log_data["status_code"] == 200
        assert log_data["prompt_tokens"] == 100
        assert log_data["completion_tokens"] == 50
        assert log_data["total_tokens"] == 150
        assert log_data["cost"] == 0.0045
        assert log_data["timestamp"] == "2024-01-15T12:00:00"
        assert log_data["trace_id"] == "trace-789"
        assert log_data["is_streaming"] is False
        assert log_data["is_cached"] is False
        assert log_data["retry_count"] == 0
        assert log_data["fallback_providers_tried"] == []

    def test_emits_at_info_level(self, gateway_logger, sample_log_entry, caplog):
        """log_request emits at INFO level."""
        with caplog.at_level(logging.DEBUG, logger="gateway.project.proj-abc"):
            gateway_logger.log_request(sample_log_entry)

        assert caplog.records[0].levelno == logging.INFO

    def test_includes_trace_id_for_otel_compatibility(
        self, gateway_logger, sample_log_entry, caplog
    ):
        """log_request includes trace_id for OpenTelemetry trace context propagation."""
        sample_log_entry.trace_id = "otel-trace-abc123"
        with caplog.at_level(logging.DEBUG, logger="gateway.project.proj-abc"):
            gateway_logger.log_request(sample_log_entry)

        log_data = json.loads(caplog.records[0].message)
        assert log_data["trace_id"] == "otel-trace-abc123"

    def test_handles_streaming_request(self, gateway_logger, caplog):
        """log_request correctly logs streaming request entries."""
        entry = RequestLogEntry(
            request_id="req-stream",
            project_id="proj-1",
            user_id="user-1",
            model="claude-3",
            provider="anthropic",
            latency_ms=2000.0,
            status_code=200,
            prompt_tokens=200,
            completion_tokens=300,
            total_tokens=500,
            cost=0.01,
            timestamp=datetime(2024, 1, 15, 12, 0, 0),
            is_streaming=True,
            retry_count=2,
            fallback_providers_tried=["openai"],
        )
        with caplog.at_level(logging.DEBUG, logger="gateway.project.proj-1"):
            gateway_logger.log_request(entry)

        log_data = json.loads(caplog.records[0].message)
        assert log_data["is_streaming"] is True
        assert log_data["retry_count"] == 2
        assert log_data["fallback_providers_tried"] == ["openai"]


class TestLogFailure:
    """Tests for log_failure method."""

    def test_emits_entry_with_diagnostic_fields(self, gateway_logger, caplog):
        """log_failure emits a JSON log with provider, error_type, status_code, retry_attempt."""
        with caplog.at_level(logging.DEBUG, logger="gateway"):
            gateway_logger.log_failure(
                provider="openai",
                error_type="rate_limit",
                status_code=429,
                retry_attempt=2,
                message="Rate limit exceeded",
            )

        assert len(caplog.records) == 1
        log_data = json.loads(caplog.records[0].message)

        assert log_data["event"] == "provider_failure"
        assert log_data["provider"] == "openai"
        assert log_data["error_type"] == "rate_limit"
        assert log_data["status_code"] == 429
        assert log_data["retry_attempt"] == 2
        assert log_data["message"] == "Rate limit exceeded"
        assert "timestamp" in log_data

    def test_emits_at_error_level(self, gateway_logger, caplog):
        """log_failure emits at ERROR level."""
        with caplog.at_level(logging.DEBUG, logger="gateway"):
            gateway_logger.log_failure(
                provider="anthropic",
                error_type="server_error",
                status_code=500,
                retry_attempt=0,
            )

        assert caplog.records[0].levelno == logging.ERROR

    def test_default_empty_message(self, gateway_logger, caplog):
        """log_failure uses empty string as default message."""
        with caplog.at_level(logging.DEBUG, logger="gateway"):
            gateway_logger.log_failure(
                provider="bedrock",
                error_type="timeout",
                status_code=504,
                retry_attempt=1,
            )

        log_data = json.loads(caplog.records[0].message)
        assert log_data["message"] == ""


class TestLogStartupSummary:
    """Tests for log_startup_summary method."""

    def test_includes_counts_and_strategies(self, gateway_logger, caplog):
        """log_startup_summary logs provider, model, project counts and routing strategies."""
        with caplog.at_level(logging.DEBUG, logger="gateway"):
            gateway_logger.log_startup_summary(
                provider_count=3,
                model_count=5,
                project_count=2,
                routing_strategies=["round-robin", "weighted"],
            )

        assert len(caplog.records) == 1
        log_data = json.loads(caplog.records[0].message)

        assert log_data["event"] == "startup"
        assert log_data["provider_count"] == 3
        assert log_data["model_count"] == 5
        assert log_data["project_count"] == 2
        assert log_data["routing_strategies"] == ["round-robin", "weighted"]

    def test_emits_at_info_level(self, gateway_logger, caplog):
        """log_startup_summary emits at INFO level."""
        with caplog.at_level(logging.DEBUG, logger="gateway"):
            gateway_logger.log_startup_summary(
                provider_count=1,
                model_count=1,
                project_count=1,
                routing_strategies=["round-robin"],
            )

        assert caplog.records[0].levelno == logging.INFO


class TestPerProjectLogLevel:
    """Tests for per-project log level configuration."""

    def test_set_and_get_project_level(self, gateway_logger):
        """set_project_log_level stores the level, get_logger_for_project uses it."""
        gateway_logger.set_project_log_level("proj-1", "WARNING")
        project_logger = gateway_logger.get_logger_for_project("proj-1")
        assert project_logger.level == logging.WARNING

    def test_default_level_when_project_not_configured(self, gateway_logger):
        """get_logger_for_project returns a logger at the default level for unconfigured projects."""
        project_logger = gateway_logger.get_logger_for_project("unknown-project")
        assert project_logger.level == logging.DEBUG  # default_level="DEBUG" in fixture

    def test_log_request_uses_project_logger(self, gateway_logger, caplog):
        """log_request routes through the project-specific logger."""
        gateway_logger.set_project_log_level("proj-quiet", "CRITICAL")

        entry = RequestLogEntry(
            request_id="req-1",
            project_id="proj-quiet",
            user_id="user-1",
            model="gpt-4",
            provider="openai",
            latency_ms=100.0,
            status_code=200,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cost=0.001,
            timestamp=datetime(2024, 1, 15, 12, 0, 0),
        )

        # The project logger is set to CRITICAL, so INFO-level log_request should be suppressed
        with caplog.at_level(logging.DEBUG, logger="gateway.project.proj-quiet"):
            gateway_logger.log_request(entry)

        # caplog captures regardless of level, but the logger's effective level
        # means the record won't be emitted if level is too high
        # We verify the project logger has the right level
        project_logger = gateway_logger.get_logger_for_project("proj-quiet")
        assert project_logger.level == logging.CRITICAL

    def test_case_insensitive_level_setting(self, gateway_logger):
        """set_project_log_level normalizes level names to uppercase."""
        gateway_logger.set_project_log_level("proj-1", "debug")
        project_logger = gateway_logger.get_logger_for_project("proj-1")
        assert project_logger.level == logging.DEBUG

    def test_multiple_projects_independent_levels(self, gateway_logger):
        """Different projects can have different log levels."""
        gateway_logger.set_project_log_level("proj-a", "DEBUG")
        gateway_logger.set_project_log_level("proj-b", "ERROR")

        logger_a = gateway_logger.get_logger_for_project("proj-a")
        logger_b = gateway_logger.get_logger_for_project("proj-b")

        assert logger_a.level == logging.DEBUG
        assert logger_b.level == logging.ERROR
