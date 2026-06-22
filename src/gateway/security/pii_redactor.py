"""PII redaction engine — policy-driven, configurable per org/BU/project.

Replaces detected PII with indexed tokens ([EMAIL_1], [SSN_2], etc.) before
the prompt reaches the LLM. Stores a reversible mapping so originals can be
re-injected into the response for the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gateway.models import ResolvedPolicy

# Regex patterns per PII type — intentionally conservative to reduce false positives
PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "aws_account_id": re.compile(r"\b\d{12}\b"),
    "medical_record": re.compile(r"\b(?:MRN|mrn)[:\s#]*\d{6,10}\b"),
}


@dataclass
class RedactionMapping:
    """Stores the mapping between tokens and original values for re-injection."""

    _forward: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def add(self, pii_type: str, original: str) -> str:
        for token, val in self._forward.items():
            if val == original:
                return token
        count = self._counters.get(pii_type, 0) + 1
        self._counters[pii_type] = count
        token = f"[{pii_type.upper()}_{count}]"
        self._forward[token] = original
        return token

    def reinject(self, text: str) -> str:
        for token, original in self._forward.items():
            text = text.replace(token, original)
        return text

    @property
    def redacted_count(self) -> int:
        return len(self._forward)


class PIIRedactor:
    """Policy-driven PII redaction engine.

    If the resolved policy has pii_redaction_enabled=False, all methods
    are no-ops with zero overhead.
    """

    def redact(self, text: str, policy: ResolvedPolicy) -> tuple[str, RedactionMapping]:
        """Redact PII from text based on policy. Returns (redacted_text, mapping)."""
        mapping = RedactionMapping()

        if not policy.pii_redaction_enabled:
            return text, mapping

        active_types = policy.pii_redact_types or []
        if not active_types:
            return text, mapping

        # Collect all matches first, then replace from right to left
        # to avoid index shifting and token-on-token false matches
        matches: list[tuple[int, int, str, str]] = []
        for pii_type in active_types:
            pattern = PII_PATTERNS.get(pii_type)
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                matches.append((match.start(), match.end(), pii_type, match.group()))

        # Sort by start position descending so replacements don't shift earlier indices
        matches.sort(key=lambda m: m[0], reverse=True)
        for start, end, pii_type, original in matches:
            token = mapping.add(pii_type, original)
            text = text[:start] + token + text[end:]

        return text, mapping

    def redact_messages(
        self, messages: list[dict], policy: ResolvedPolicy
    ) -> tuple[list[dict], RedactionMapping]:
        """Redact PII across all message contents. Returns (redacted_messages, mapping)."""
        mapping = RedactionMapping()

        if not policy.pii_redaction_enabled:
            return messages, mapping

        active_types = policy.pii_redact_types or []
        if not active_types:
            return messages, mapping

        redacted = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                # Collect all matches first, replace right-to-left
                matches: list[tuple[int, int, str, str]] = []
                for pii_type in active_types:
                    pattern = PII_PATTERNS.get(pii_type)
                    if pattern is None:
                        continue
                    for match in pattern.finditer(content):
                        matches.append((match.start(), match.end(), pii_type, match.group()))
                matches.sort(key=lambda m: m[0], reverse=True)
                redacted_content = content
                for start, end, pii_type, original in matches:
                    token = mapping.add(pii_type, original)
                    redacted_content = redacted_content[:start] + token + redacted_content[end:]
                redacted.append({**msg, "content": redacted_content})
            else:
                redacted.append(msg)

        return redacted, mapping

    def reinject_response(self, text: str, mapping: RedactionMapping) -> str:
        """Re-inject original PII values into the LLM response."""
        return mapping.reinject(text)
