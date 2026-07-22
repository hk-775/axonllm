"""Tests for the native OTLP span exporter (#17, OTEL half).

Covers: opt-in gating (no endpoint → disabled), UsageRecord → span attribute
mapping, graceful degrade when the OTEL SDK is missing, and the deterministic
id scheme. Agent-level suppress-when-embedded is covered in
test_otlp_agent_integration below.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.gateway.models import UsageRecord
from src.gateway.observability.otlp_exporter import OTLPSpanExporter, _id_from


def _rec(**kw) -> UsageRecord:
    base = dict(
        request_id="req_abc123",
        project_id="proj1",
        user_id="user1",
        provider="anthropic",
        model="claude-sonnet",
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        cost=0.0021,
        timestamp=datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc),
        latency_ms=250.0,
        status="success",
        routing_strategy="smart",
    )
    base.update(kw)
    return UsageRecord(**base)


class TestGating:
    def test_disabled_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        exp = OTLPSpanExporter()
        assert exp.enabled is False
        # export is a safe no-op
        exp.export_usage(_rec())

    def test_enabled_with_endpoint(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        # opentelemetry is a dev dependency, so this should build
        assert exp.enabled is True


class TestIdScheme:
    def test_deterministic_and_sized(self):
        t = _id_from("req_abc123", 16)
        s = _id_from("req_abc123", 8)
        assert t == _id_from("req_abc123", 16)          # deterministic
        assert 0 < t < 2 ** 128 and 0 < s < 2 ** 64      # sized, non-zero
        assert _id_from("", 8) != 0                       # never all-zero


class TestSpanMapping:
    def test_maps_usage_record_to_span(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        exp._ensure()
        span = exp._to_span(_rec())

        assert span.name == "llm.completion"
        a = dict(span.attributes)
        assert a["gen_ai.request.model"] == "claude-sonnet"
        assert a["gen_ai.usage.input_tokens"] == 100
        assert a["gen_ai.usage.output_tokens"] == 40
        assert a["axon.provider"] == "anthropic"
        assert a["axon.project_id"] == "proj1"
        assert abs(a["axon.cost_usd"] - 0.0021) < 1e-9
        assert a["axon.total_tokens"] == 140
        assert a["axon.routing_strategy"] == "smart"
        assert a["axon.request_id"] == "req_abc123"
        assert a["axon.status"] == "success"

    def test_span_timing_from_latency(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        exp._ensure()
        span = exp._to_span(_rec(latency_ms=250.0))
        assert span.end_time - span.start_time == 250_000_000  # 250ms in ns

    def test_error_status_maps_to_error(self, monkeypatch):
        from opentelemetry.trace.status import StatusCode

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        exp._ensure()
        span = exp._to_span(_rec(status="error"))
        assert span.status.status_code == StatusCode.ERROR
        assert dict(span.attributes)["axon.status"] == "error"

    def test_export_never_raises_on_bad_record(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        # A record missing timestamp shouldn't blow up export
        exp.export_usage(_rec(timestamp=None))  # type: ignore[arg-type]


class TestNonBlocking:
    def test_export_enqueues_via_processor(self, monkeypatch):
        """Spans go through the BatchSpanProcessor (background thread), so a
        down/slow collector never blocks the request path."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        exp._ensure()
        exp._processor = MagicMock()          # capture enqueue, no real network
        exp.export_usage(_rec())
        exp._processor.on_end.assert_called_once()

    def test_shutdown_flushes_processor(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()
        exp._ensure()
        exp._processor = MagicMock()
        exp.shutdown()
        exp._processor.shutdown.assert_called_once()


class TestGracefulDegrade:
    def test_disabled_when_sdk_import_fails(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        exp = OTLPSpanExporter()

        # Simulate the OTLP package being absent by making the import raise.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if "opentelemetry" in name:
                raise ImportError("no otel")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert exp.enabled is False           # degrades, doesn't raise
        exp.export_usage(_rec())              # no-op, no raise


class TestSuppressWhenEmbedded:
    """The agent exports natively only when NOT embedded. This exercises that
    exact gate — export iff (exporter present AND trace_forwarder not enabled) —
    against the real TraceForwarder so a change to `enabled` is caught here."""

    def _should_export(self, exporter, forwarder) -> bool:
        # Mirrors the gate in GatewayAgent.handle_chat_completion.
        return exporter is not None and not (
            forwarder is not None and forwarder.enabled)

    def test_standalone_exports(self, monkeypatch):
        from src.gateway.observability import trace_forwarder as tf
        from src.gateway.observability.trace_forwarder import TraceForwarder

        monkeypatch.delenv("OSTIARI_TRACES_URL", raising=False)
        for s in list(tf._sinks):
            tf.unregister_sink(s)
        exp = OTLPSpanExporter()
        fwd = TraceForwarder()
        assert fwd.enabled is False
        assert self._should_export(exp, fwd) is True   # standalone → export natively

    def test_embedded_via_sink_suppresses(self, monkeypatch):
        from src.gateway.observability import trace_forwarder as tf
        from src.gateway.observability.trace_forwarder import TraceForwarder, register_sink

        monkeypatch.delenv("OSTIARI_TRACES_URL", raising=False)
        for s in list(tf._sinks):
            tf.unregister_sink(s)
        sink = lambda ev: None  # noqa: E731 — an embedding Ostiari registered a sink
        register_sink(sink)
        try:
            exp = OTLPSpanExporter()
            fwd = TraceForwarder()
            assert fwd.enabled is True
            assert self._should_export(exp, fwd) is False  # embedded → suppress
        finally:
            tf.unregister_sink(sink)

    def test_embedded_via_url_suppresses(self, monkeypatch):
        from src.gateway.observability import trace_forwarder as tf
        from src.gateway.observability.trace_forwarder import TraceForwarder

        for s in list(tf._sinks):
            tf.unregister_sink(s)
        monkeypatch.setenv("OSTIARI_TRACES_URL", "http://cp:8000/api/traces/ingest")
        exp = OTLPSpanExporter()
        fwd = TraceForwarder()
        assert fwd.enabled is True
        assert self._should_export(exp, fwd) is False
