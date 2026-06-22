"""Immutable audit trail for compliance recording.

Every LLM request/response pair is recorded with:
- Who made the request (user, project, auth method)
- What was sent (redacted prompt) and received
- Security events (injection attempts, PII redactions)
- Policy state at time of request

Records are append-only and include a hash chain for tamper detection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


class AuditEventType(Enum):
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    PII_REDACTION = "pii_redaction"
    INJECTION_DETECTED = "injection_detected"
    INJECTION_BLOCKED = "injection_blocked"
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    POLICY_DENY = "policy_deny"
    KEY_ISSUED = "key_issued"
    KEY_REVOKED = "key_revoked"
    KEY_ROTATED = "key_rotated"


@dataclass
class AuditRecord:
    """A single immutable audit record."""

    record_id: str
    event_type: AuditEventType
    timestamp: datetime
    user_id: str
    project_id: str
    request_id: str
    data: dict = field(default_factory=dict)
    prev_hash: str = ""
    record_hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps({
            "record_id": self.record_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "user_id": self.user_id,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "data": self.data,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


class AuditTrail:
    """Append-only audit trail with hash-chain integrity.

    Records are stored in DynamoDB (when enabled) and kept in an
    in-memory buffer for recent queries. The hash chain allows
    tamper detection during compliance audits.
    """

    def __init__(
        self,
        persistence: DynamoPersistence | None = None,
        buffer_size: int = 10000,
    ) -> None:
        self._persistence = persistence
        self._buffer: deque[AuditRecord] = deque(maxlen=buffer_size)
        self._buffer_size = buffer_size
        self._last_hash = "genesis"
        self._lock = asyncio.Lock()

    async def record(
        self,
        event_type: AuditEventType,
        user_id: str,
        project_id: str,
        request_id: str,
        data: dict | None = None,
    ) -> AuditRecord:
        """Append a new audit record."""
        async with self._lock:
            record = AuditRecord(
                record_id=f"aud_{uuid.uuid4().hex[:16]}",
                event_type=event_type,
                timestamp=datetime.now(timezone.utc),
                user_id=user_id,
                project_id=project_id,
                request_id=request_id,
                data=data or {},
                prev_hash=self._last_hash,
            )
            record.record_hash = record.compute_hash()
            self._last_hash = record.record_hash

            self._buffer.append(record)

        if self._persistence and self._persistence.enabled:
            await self._persist(record)

        return record

    async def record_llm_request(
        self,
        user_id: str,
        project_id: str,
        request_id: str,
        model: str,
        provider: str,
        message_count: int,
        pii_redacted_count: int = 0,
        injection_score: float = 0.0,
    ) -> AuditRecord:
        """Record an LLM request with security metadata."""
        return await self.record(
            event_type=AuditEventType.LLM_REQUEST,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={
                "model": model,
                "provider": provider,
                "message_count": message_count,
                "pii_redacted_count": pii_redacted_count,
                "injection_score": injection_score,
            },
        )

    async def record_injection_event(
        self,
        user_id: str,
        project_id: str,
        request_id: str,
        threat_level: str,
        patterns: list[str],
        blocked: bool,
    ) -> AuditRecord:
        event_type = (
            AuditEventType.INJECTION_BLOCKED if blocked
            else AuditEventType.INJECTION_DETECTED
        )
        return await self.record(
            event_type=event_type,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={
                "threat_level": threat_level,
                "patterns": patterns,
                "blocked": blocked,
            },
        )

    async def record_pii_redaction(
        self,
        user_id: str,
        project_id: str,
        request_id: str,
        redacted_types: list[str],
        count: int,
    ) -> AuditRecord:
        return await self.record(
            event_type=AuditEventType.PII_REDACTION,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={"redacted_types": redacted_types, "count": count},
        )

    def query_recent(
        self,
        project_id: str | None = None,
        event_type: AuditEventType | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """Query recent records from the in-memory buffer."""
        results: list[AuditRecord] = list(self._buffer)
        if project_id:
            results = [r for r in results if r.project_id == project_id]
        if event_type:
            results = [r for r in results if r.event_type == event_type]
        return results[-limit:]

    def verify_chain(self, records: list[AuditRecord] | None = None) -> bool:
        """Verify hash chain integrity. Returns False if tampered."""
        records = records if records is not None else list(self._buffer)
        if not records:
            return True

        for i, record in enumerate(records):
            expected_hash = record.compute_hash()
            if record.record_hash != expected_hash:
                return False
            if i > 0 and record.prev_hash != records[i - 1].record_hash:
                return False

        return True

    async def _persist(self, record: AuditRecord) -> None:
        """Persist audit record to DynamoDB."""
        try:
            item = {
                "PK": f"AUDIT#{record.project_id}",
                "SK": f"AUDIT#{record.timestamp.isoformat()}#{record.record_id}",
                "record_id": record.record_id,
                "event_type": record.event_type.value,
                "timestamp": record.timestamp.isoformat(),
                "user_id": record.user_id,
                "project_id": record.project_id,
                "request_id": record.request_id,
                "data": json.dumps(record.data),
                "prev_hash": record.prev_hash,
                "record_hash": record.record_hash,
            }
            if self._persistence:
                await self._persistence.put_item(item)
        except Exception:
            logger.error("Failed to persist audit record %s", record.record_id, exc_info=True)
