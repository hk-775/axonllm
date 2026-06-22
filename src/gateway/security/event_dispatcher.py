"""Event dispatcher — push security events to external systems.

Supports multiple destination types:
- Webhook (HTTP POST to any URL — Slack, PagerDuty, custom)
- CloudWatch Logs (via boto3)
- SNS (fan-out to SQS, Lambda, email, etc.)

Dispatching is async and fire-and-forget — failures are logged but
never block the request pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class DestinationType(Enum):
    WEBHOOK = "webhook"
    CLOUDWATCH = "cloudwatch"
    SNS = "sns"


@dataclass
class EventDestination:
    """A configured destination for security events."""

    name: str
    destination_type: DestinationType
    config: dict = field(default_factory=dict)
    event_filter: list[str] | None = None
    enabled: bool = True


@dataclass
class SecurityEvent:
    """A security event ready for dispatch."""

    event_id: str
    event_type: str
    timestamp: str
    source: str = "axonllm"
    severity: str = "info"
    user_id: str = ""
    project_id: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "source": self.source,
            "severity": self.severity,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "data": self.data,
        }


class EventDispatcher:
    """Dispatches security events to configured destinations.

    Events are dispatched asynchronously — dispatch failures never
    block the request pipeline.
    """

    def __init__(self) -> None:
        self._destinations: list[EventDestination] = []
        self._dispatch_count: int = 0
        self._error_count: int = 0
        self._http_client = None

    def add_destination(self, destination: EventDestination) -> None:
        self._destinations.append(destination)

    def remove_destination(self, name: str) -> bool:
        before = len(self._destinations)
        self._destinations = [d for d in self._destinations if d.name != name]
        return len(self._destinations) < before

    @property
    def destinations(self) -> list[EventDestination]:
        return list(self._destinations)

    async def dispatch(self, event: SecurityEvent) -> None:
        """Dispatch event to all matching destinations (fire-and-forget)."""
        tasks = []
        for dest in self._destinations:
            if not dest.enabled:
                continue
            if dest.event_filter and event.event_type not in dest.event_filter:
                continue
            tasks.append(self._send_to_destination(event, dest))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    self._error_count += 1
                    logger.error("Event dispatch failed: %s", r)
                else:
                    self._dispatch_count += 1

    async def dispatch_injection_event(
        self,
        event_id: str,
        user_id: str,
        project_id: str,
        threat_level: str,
        patterns: list[str],
        blocked: bool,
    ) -> None:
        severity = "critical" if blocked else "warning"
        event = SecurityEvent(
            event_id=event_id,
            event_type="injection_blocked" if blocked else "injection_detected",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity=severity,
            user_id=user_id,
            project_id=project_id,
            data={
                "threat_level": threat_level,
                "patterns": patterns,
                "blocked": blocked,
            },
        )
        await self.dispatch(event)

    async def dispatch_pii_event(
        self,
        event_id: str,
        user_id: str,
        project_id: str,
        redacted_types: list[str],
        count: int,
    ) -> None:
        event = SecurityEvent(
            event_id=event_id,
            event_type="pii_redaction",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="info",
            user_id=user_id,
            project_id=project_id,
            data={"redacted_types": redacted_types, "count": count},
        )
        await self.dispatch(event)

    async def dispatch_auth_failure(
        self,
        event_id: str,
        source_ip: str,
        reason: str,
    ) -> None:
        event = SecurityEvent(
            event_id=event_id,
            event_type="auth_failure",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="warning",
            data={"source_ip": source_ip, "reason": reason},
        )
        await self.dispatch(event)

    async def _send_to_destination(
        self, event: SecurityEvent, dest: EventDestination
    ) -> None:
        if dest.destination_type == DestinationType.WEBHOOK:
            await self._send_webhook(event, dest)
        elif dest.destination_type == DestinationType.SNS:
            await self._send_sns(event, dest)
        elif dest.destination_type == DestinationType.CLOUDWATCH:
            await self._send_cloudwatch(event, dest)

    def _get_http_client(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def _send_webhook(self, event: SecurityEvent, dest: EventDestination) -> None:
        url = dest.config.get("url", "")
        headers = dest.config.get("headers", {})
        timeout = dest.config.get("timeout", 5.0)

        if not url:
            return

        try:
            client = self._get_http_client()
            resp = await client.post(
                url,
                json=event.to_dict(),
                headers={"Content-Type": "application/json", **headers},
                timeout=timeout,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Webhook %s returned %d", dest.name, resp.status_code
                )
        except ImportError:
            logger.warning("httpx not installed — webhook dispatch unavailable")
        except Exception as e:
            raise RuntimeError(f"Webhook {dest.name} failed: {e}") from e

    async def _send_sns(self, event: SecurityEvent, dest: EventDestination) -> None:
        topic_arn = dest.config.get("topic_arn", "")
        region = dest.config.get("region", "us-east-1")

        if not topic_arn:
            return

        def _publish():
            import boto3
            client = boto3.client("sns", region_name=region)
            client.publish(
                TopicArn=topic_arn,
                Message=json.dumps(event.to_dict()),
                Subject=f"AxonLLM Security: {event.event_type}",
                MessageAttributes={
                    "event_type": {"DataType": "String", "StringValue": event.event_type},
                    "severity": {"DataType": "String", "StringValue": event.severity},
                },
            )

        await asyncio.to_thread(_publish)

    async def _send_cloudwatch(self, event: SecurityEvent, dest: EventDestination) -> None:
        log_group = dest.config.get("log_group", "/axonllm/security")
        log_stream = dest.config.get("log_stream", "events")
        region = dest.config.get("region", "us-east-1")

        def _put_log():
            import boto3
            client = boto3.client("logs", region_name=region)
            try:
                client.put_log_events(
                    logGroupName=log_group,
                    logStreamName=log_stream,
                    logEvents=[{
                        "timestamp": int(time.time() * 1000),
                        "message": json.dumps(event.to_dict()),
                    }],
                )
            except Exception:
                logger.debug("CloudWatch put_log_events failed", exc_info=True)

        await asyncio.to_thread(_put_log)

    @property
    def stats(self) -> dict:
        return {
            "destinations": len(self._destinations),
            "dispatched": self._dispatch_count,
            "errors": self._error_count,
        }
