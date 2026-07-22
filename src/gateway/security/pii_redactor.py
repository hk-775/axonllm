"""PII redaction engine — policy-driven, configurable per org/BU/project.

Replaces detected PII with indexed tokens ([EMAIL_1], [SSN_2], etc.) before
the prompt reaches the LLM. Stores a reversible mapping so originals can be
re-injected into the response for the caller.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gateway.models import ResolvedPolicy

logger = logging.getLogger(__name__)

# Regex patterns per PII type — intentionally conservative to reduce false positives
PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "aws_account_id": re.compile(r"\b\d{12}\b"),
    "medical_record": re.compile(r"\b(?:MRN|mrn)[:\s#]*\d{6,10}\b"),
    # International / broader coverage (the set was previously US-centric).
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "passport": re.compile(r"\b[A-Z]{1,2}\d{6,9}\b"),
    "ipv6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
}


def env_default_enabled() -> bool:
    """True when AXON_PII_REDACTION_DEFAULT opts a deploy into safe-by-default.

    Backward-compatible: unset → False (redaction stays opt-in via policy). When
    set truthy, any request whose resolved policy does NOT explicitly configure
    redaction gets redaction ON with a default type set. This makes a standalone
    AxonLLM deploy safe-by-default with one env flag, without changing behavior
    for deploys that don't set it (e.g. the Ostiari embed, which governs PII at
    its own layer before the request reaches the embedded agent).
    """
    return os.environ.get("AXON_PII_REDACTION_DEFAULT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def env_default_types() -> list[str]:
    """Default PII types used when the env default is on and policy sets none.

    AXON_PII_REDACT_TYPES (comma-separated) narrows the set; unset → all known
    patterns. Unknown type names are dropped with a warning.
    """
    raw = os.environ.get("AXON_PII_REDACT_TYPES", "").strip()
    if not raw:
        return list(PII_PATTERNS.keys())
    types, unknown = [], []
    for t in (p.strip() for p in raw.split(",")):
        if not t:
            continue
        (types if t in PII_PATTERNS else unknown).append(t)
    if unknown:
        logger.warning("AXON_PII_REDACT_TYPES has unknown types (ignored): %s", unknown)
    return types or list(PII_PATTERNS.keys())


@dataclass
class RedactionMapping:
    """Stores the mapping between tokens and original values for re-injection.

    When ``reversible`` is False (permanent-redaction / no-reinject mode) the
    token→original map is never populated, so no PII plaintext is retained past
    the redaction pass and ``reinject`` is a no-op. Same-request dedup still
    works via ``_dedup`` (cleared by ``seal``).
    """

    _forward: dict[str, str] = field(default_factory=dict)   # token -> original (reinject)
    _counters: dict[str, int] = field(default_factory=dict)  # pii_type -> count
    _dedup: dict[str, str] = field(default_factory=dict)     # original -> token (same-request dedup)
    reversible: bool = True

    def add(self, pii_type: str, original: str) -> str:
        existing = self._dedup.get(original)
        if existing is not None:
            return existing
        count = self._counters.get(pii_type, 0) + 1
        self._counters[pii_type] = count
        token = f"[{pii_type.upper()}_{count}]"
        self._dedup[original] = token
        if self.reversible:
            self._forward[token] = original
        return token

    def reinject(self, text: str) -> str:
        if not self.reversible:
            return text
        for token, original in self._forward.items():
            text = text.replace(token, original)
        return text

    def seal(self) -> None:
        """Drop retained plaintext when redaction is permanent (no reinject)."""
        if not self.reversible:
            self._dedup.clear()

    @property
    def redacted_count(self) -> int:
        return sum(self._counters.values())


class PIIRedactor:
    """Policy-driven PII redaction engine.

    If the resolved policy has pii_redaction_enabled=False (and the env default
    is not set), all methods are no-ops with zero overhead. See
    ``effective_policy`` for how the AXON_PII_REDACTION_DEFAULT env flag makes a
    deploy safe-by-default without changing per-policy behavior.
    """

    def effective_policy(self, policy: ResolvedPolicy) -> ResolvedPolicy:
        """Apply the env default when a policy doesn't explicitly enable redaction.

        Explicit policy always wins (an org that turned redaction on/off keeps
        its choice, including its ``pii_reinject`` setting). Only when a policy
        leaves redaction off AND the env default is set do we turn it on with the
        env-configured type set. Returns the policy unchanged otherwise, so the
        Ostiari embed (which doesn't set the env flag) is never double-redacted.
        """
        if policy.pii_redaction_enabled or not env_default_enabled():
            return policy
        from src.gateway.models import ResolvedPolicy as _RP
        return _RP(
            rate_limit_rpm=policy.rate_limit_rpm,
            budget_limit=policy.budget_limit,
            allowed_models=policy.allowed_models,
            max_tokens_per_request=policy.max_tokens_per_request,
            allowed_providers=policy.allowed_providers,
            pii_redaction_enabled=True,
            pii_redact_types=policy.pii_redact_types or env_default_types(),
            pii_reinject=policy.pii_reinject,
        )

    def _redact_str(
        self, text: str, active_types: list[str], mapping: RedactionMapping
    ) -> str:
        """Redact one string, replacing right-to-left to keep indices stable."""
        matches: list[tuple[int, int, str, str]] = []
        for pii_type in active_types:
            pattern = PII_PATTERNS.get(pii_type)
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                matches.append((match.start(), match.end(), pii_type, match.group()))
        matches.sort(key=lambda m: m[0], reverse=True)
        for start, end, pii_type, original in matches:
            token = mapping.add(pii_type, original)
            text = text[:start] + token + text[end:]
        return text

    def _redact_content(self, content, active_types: list[str], mapping: RedactionMapping):
        """Redact a message ``content`` of any shape: str, or a list of parts.

        Multimodal / tool messages carry content as a list of parts (e.g.
        ``{"type": "text", "text": "..."}`` for OpenAI or ``{"text": "..."}`` for
        Bedrock). Previously non-str content passed through UNREDACTED, leaking
        PII in the text parts. We now redact the ``text``/``content`` string
        field of each dict part and leave non-text parts (images, etc.) intact.
        """
        if isinstance(content, str):
            return self._redact_str(content, active_types, mapping)
        if isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict):
                    key = "text" if isinstance(part.get("text"), str) else (
                        "content" if isinstance(part.get("content"), str) else None)
                    if key is not None:
                        new_parts.append(
                            {**part, key: self._redact_str(part[key], active_types, mapping)})
                        continue
                new_parts.append(part)
            return new_parts
        return content

    def _active_types(self, policy: ResolvedPolicy) -> list[str]:
        return policy.pii_redact_types or []

    def redact(self, text: str, policy: ResolvedPolicy) -> tuple[str, RedactionMapping]:
        """Redact PII from text based on policy. Returns (redacted_text, mapping)."""
        policy = self.effective_policy(policy)
        mapping = RedactionMapping(reversible=policy.pii_reinject)
        if not policy.pii_redaction_enabled:
            return text, mapping
        active_types = self._active_types(policy)
        if not active_types:
            return text, mapping
        text = self._redact_str(text, active_types, mapping)
        mapping.seal()
        return text, mapping

    def redact_messages(
        self, messages: list[dict], policy: ResolvedPolicy
    ) -> tuple[list[dict], RedactionMapping]:
        """Redact PII across all message contents. Returns (redacted_messages, mapping)."""
        policy = self.effective_policy(policy)
        mapping = RedactionMapping(reversible=policy.pii_reinject)
        if not policy.pii_redaction_enabled:
            return messages, mapping
        active_types = self._active_types(policy)
        if not active_types:
            return messages, mapping

        redacted = []
        for msg in messages:
            if "content" not in msg:
                redacted.append(msg)
                continue
            redacted.append(
                {**msg, "content": self._redact_content(msg["content"], active_types, mapping)})
        mapping.seal()
        return redacted, mapping

    def reinject_response(self, text: str, mapping: RedactionMapping) -> str:
        """Re-inject original PII values into the LLM response (no-op if permanent)."""
        return mapping.reinject(text)
