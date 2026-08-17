"""Tests for forwarding AxonLLM request traces to an embedding Ostiari."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.gateway.models import UsageRecord
from src.gateway.observability.trace_forwarder import (
    TraceForwarder,
    map_usage_to_trace_event,
)


def _record(status: str = "success") -> UsageRecord:
    return UsageRecord(
        request_id="req-1", project_id="proj-b", user_id="alice",
        provider="bedrock", model="claude-sonnet",
        prompt_tokens=100, completion_tokens=50, total_tokens=150, cost=0.0123,
        timestamp=datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc),
        latency_ms=142.5, status=status,
    )


class TestActivation:
    def test_disabled_when_standalone(self):
        assert TraceForwarder().enabled is False

    def test_enabled_by_explicit_url(self):
        assert TraceForwarder(
            url="http://cp:8000/api/traces/ingest"
        ).enabled is True

    def test_environment_does_not_implicitly_enable_embedded_forwarding(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv(
            "OSTIARI_TRACES_URL",
            "http://cp:8000/api/traces/ingest",
        )
        assert TraceForwarder().enabled is False

    def test_enabled_by_injected_sink(self):
        assert TraceForwarder(sinks=[lambda ev: None]).enabled is True

    def test_forward_is_noop_when_disabled(self):
        # No sink, no URL → forward does nothing and does not raise.
        asyncio.run(TraceForwarder().forward(_record()))


class TestMapping:
    def test_event_shape_matches_ostiari(self):
        ev = map_usage_to_trace_event(_record())
        # Fields Ostiari's control-plane trace event expects.
        for key in ("sidecar_id", "gateway_id", "action", "tier", "score",
                    "duration_ms", "agent_id", "framework", "model", "params",
                    "metadata", "timestamp"):
            assert key in ev
        assert ev["action"] == "chat.completion"
        assert ev["framework"] == "axonllm"
        assert ev["tier"] == "allow"       # success → allow
        assert ev["score"] == 0            # AxonLLM does not score risk
        assert ev["model"] == "claude-sonnet"
        assert ev["agent_id"] == "alice"
        assert ev["params"]["cost"] == 0.0123
        assert ev["params"]["total_tokens"] == 150
        assert ev["metadata"]["request_id"] == "req-1"

    def test_error_status_maps_to_error_tier(self):
        assert map_usage_to_trace_event(_record(status="error"))["tier"] == "error"

    def test_gateway_id_override(self):
        assert map_usage_to_trace_event(
            _record(),
            gateway_id="axon-prod-1",
        )["gateway_id"] == "axon-prod-1"


class TestSinkDelivery:
    def test_sync_sink_receives_event(self):
        captured = []
        forwarder = TraceForwarder(sinks=[lambda ev: captured.append(ev)])
        asyncio.run(forwarder.forward(_record()))
        assert len(captured) == 1
        assert captured[0]["metadata"]["request_id"] == "req-1"

    def test_async_sink_receives_event(self):
        captured = []

        async def sink(ev):
            captured.append(ev)

        asyncio.run(TraceForwarder(sinks=[sink]).forward(_record()))
        assert len(captured) == 1

    def test_failing_sink_does_not_raise(self):
        def bad(ev):
            raise RuntimeError("boom")

        # Must not propagate — forwarding is best-effort.
        asyncio.run(TraceForwarder(sinks=[bad]).forward(_record()))

    def test_protocol_sink_receives_event(self):
        class Sink:
            def __init__(self):
                self.events = []

            async def emit(self, event):
                self.events.append(event)

        sink = Sink()
        asyncio.run(TraceForwarder(sinks=[sink]).forward(_record()))
        assert sink.events[0]["metadata"]["request_id"] == "req-1"


class TestHttpDelivery:
    def test_posts_to_url(self):
        fwd = TraceForwarder(url="http://cp:8000/api/traces/ingest")
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)
        fwd._http_client = mock_client

        asyncio.run(fwd.forward(_record()))

        mock_client.post.assert_awaited_once()
        call = mock_client.post.call_args
        assert call.args[0] == "http://cp:8000/api/traces/ingest"
        assert call.kwargs["json"]["action"] == "chat.completion"
        assert call.kwargs["json"]["params"]["cost"] == 0.0123
        # No ingest key configured → no X-Ingest-Key header.
        assert "X-Ingest-Key" not in call.kwargs["headers"]

    def test_sends_explicit_ingest_key_header(self):
        fwd = TraceForwarder(
            url="http://cp:8000/api/traces/ingest",
            ingest_key="s3cret",
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)
        fwd._http_client = mock_client

        asyncio.run(fwd.forward(_record()))

        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["X-Ingest-Key"] == "s3cret"

    def test_http_failure_is_swallowed(self):
        fwd = TraceForwarder(url="http://cp:8000/api/traces/ingest")
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("connection refused"))
        fwd._http_client = mock_client
        # Best-effort: a broken Ostiari must never fail the request path.
        asyncio.run(fwd.forward(_record()))

    def test_close_releases_lazy_http_client(self):
        fwd = TraceForwarder(url="http://cp:8000/api/traces/ingest")
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        fwd._http_client = mock_client

        asyncio.run(fwd.close())

        mock_client.aclose.assert_awaited_once()
        assert fwd._http_client is None
