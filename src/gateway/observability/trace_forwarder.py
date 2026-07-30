"""Forward AxonLLM request traces to an embedding Ostiari instance.

When AxonLLM runs embedded inside Ostiari, each completed request (a UsageRecord)
is forwarded as a trace event so it shows up in Ostiari's Live Traces view. Two
delivery paths, either or both of which may be active ("Ostiari detected"):

1. HTTP sink — POST the event to Ostiari's control-plane ingest endpoint
   (`OSTIARI_TRACES_URL`, e.g. http://control-plane:8000/api/traces/ingest).
   Loosely coupled; works across processes/containers.
2. In-process sink — an embedding Ostiari registers a callback via
   register_sink(); AxonLLM calls it directly. No dependency on the `ostiari`
   package, no network hop.

Design rules:
- Forwarding is best-effort and MUST NOT affect the request path. Every failure
  is swallowed with a log; a slow/broken Ostiari never slows or fails a chat call.
- AxonLLM is a routing/cost layer, not a risk scorer. It sends neutral risk
  fields (tier="allow", score=0) and puts its real signal (tokens, cost,
  latency, provider) in params/metadata. Ostiari owns risk scoring.
- Standalone AxonLLM is unaffected: with no URL and no registered sink, the
  forwarder is a no-op.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.gateway.models import UsageRecord

logger = logging.getLogger("gateway.observability.traces")

# In-process sinks registered by an embedding host (e.g. Ostiari). Each receives
# the mapped trace-event dict. May be sync or async.
Sink = Callable[[dict[str, Any]], None] | Callable[[dict[str, Any]], Awaitable[None]]
_sinks: list[Sink] = []


def register_sink(sink: Sink) -> None:
    """Register an in-process trace sink (called by an embedding Ostiari).

    The sink receives the Ostiari-shaped trace-event dict for every forwarded
    request. Safe to call at startup; idempotent per distinct callable.
    """
    if sink not in _sinks:
        _sinks.append(sink)


def unregister_sink(sink: Sink) -> None:
    """Remove a previously registered in-process sink."""
    if sink in _sinks:
        _sinks.remove(sink)


def _ostiari_url() -> str | None:
    """The Ostiari trace-ingest URL, if configured."""
    url = os.environ.get("OSTIARI_TRACES_URL", "").strip()
    return url or None


def _gateway_id() -> str:
    """Identifier this AxonLLM instance reports as, in Ostiari's Live Traces."""
    return os.environ.get("OSTIARI_GATEWAY_ID", "axonllm").strip() or "axonllm"


def map_usage_to_trace_event(record: UsageRecord) -> dict[str, Any]:
    """Map an AxonLLM UsageRecord to Ostiari's trace-event shape.

    Matches the flat event dict Ostiari's control-plane `/api/traces/ingest`
    expects (see control-plane routers/traces.py). Neutral risk fields; AxonLLM
    specifics carried in params/metadata.
    """
    status = getattr(record, "status", "success")
    # AxonLLM does not compute risk; report a neutral tier. Surface an error tier
    # only when the request itself failed, without inventing a numeric score.
    tier = "error" if status not in ("success", "") else "allow"
    ts = record.timestamp.timestamp() if getattr(record, "timestamp", None) else None

    return {
        "sidecar_id": _gateway_id(),
        "gateway_id": _gateway_id(),
        "action": "chat.completion",
        "tier": tier,
        "score": 0,  # AxonLLM is a routing/cost layer; Ostiari owns risk scoring
        "duration_ms": getattr(record, "latency_ms", 0.0),
        "agent_id": record.user_id or "",
        "framework": "axonllm",
        "is_mcp": False,
        "endpoint": "",
        "session_id": "",
        "model": record.model,
        "params": {
            "provider": record.provider,
            "project_id": record.project_id,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "cached_tokens": getattr(record, "cached_tokens", 0),
            "cost": record.cost,
            "routing_strategy": getattr(record, "routing_strategy", ""),
            "status": status,
        },
        "metadata": {
            "source": "axonllm",
            "request_id": record.request_id,
            # The provider's own id for the upstream call, for cross-referencing
            # provider-side logs. Omitted when the provider didn't supply one.
            "provider_request_id": getattr(record, "provider_request_id", "") or "",
        },
        "timestamp": ts,
    }


class TraceForwarder:
    """Best-effort forwarder of AxonLLM request traces to Ostiari."""

    def __init__(self, url: str | None = None, gateway_id: str | None = None) -> None:
        self._url = url if url is not None else _ostiari_url()
        self._gateway_id = gateway_id or _gateway_id()
        self._http_client: Any = None

    @property
    def enabled(self) -> bool:
        """True when Ostiari is 'detected': a URL is set or a sink is registered."""
        return bool(self._url) or bool(_sinks)

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def forward(self, record: UsageRecord) -> None:
        """Forward one request trace. Never raises — failures are logged only."""
        if not self.enabled:
            return
        try:
            event = map_usage_to_trace_event(record)
        except Exception:
            logger.debug("failed to map usage record to trace event", exc_info=True)
            return

        await self._deliver_to_sinks(event)
        await self._deliver_http(event)

    async def _deliver_to_sinks(self, event: dict[str, Any]) -> None:
        import inspect

        for sink in list(_sinks):
            try:
                result = sink(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("in-process trace sink raised", exc_info=True)

    async def _deliver_http(self, event: dict[str, Any]) -> None:
        if not self._url:
            return
        headers = {"Content-Type": "application/json"}
        # Shared secret Ostiari requires when OSTIARI_INGEST_KEY is set on its side
        # (control-plane traces ingest). Sent as X-Ingest-Key; omitted if unset.
        ingest_key = os.environ.get("OSTIARI_INGEST_KEY", "").strip()
        if ingest_key:
            headers["X-Ingest-Key"] = ingest_key
        try:
            client = self._get_http_client()
            resp = await client.post(
                self._url,
                json=event,
                headers=headers,
                timeout=float(os.environ.get("OSTIARI_TRACES_TIMEOUT", "3.0")),
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Ostiari trace ingest returned %d for %s",
                    resp.status_code,
                    event.get("metadata", {}).get("request_id"),
                )
        except ImportError:
            logger.warning("httpx not installed — Ostiari trace HTTP forwarding unavailable")
        except Exception:
            logger.debug("Ostiari trace HTTP forward failed", exc_info=True)
