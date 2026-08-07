"""Strict payload parsing for the AgentCore invocation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.gateway.request_validator import RequestValidator

from .errors import AgentCoreAdapterError


class InvocationAction(Enum):
    CHAT = "chat"
    LIST_MODELS = "list_models"
    HEALTH = "health"


CHAT_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "stream",
        "tools",
        "tool_choice",
    }
)
AUTHORITY_FIELDS = frozenset(
    {
        "user_id",
        "project_id",
        "tenant",
        "tenant_id",
        "roles",
        "scopes",
    }
)


@dataclass(frozen=True)
class ParsedInvocation:
    action: InvocationAction
    request_data: dict[str, Any] | None = None


def _invalid_payload(message: str) -> AgentCoreAdapterError:
    return AgentCoreAdapterError(400, "invalid_payload", message)


def parse_invocation_payload(
    payload: Any,
    *,
    validator: RequestValidator | None = None,
) -> ParsedInvocation:
    """Validate an action-specific JSON object without coercing field types."""
    if type(payload) is not dict:
        raise _invalid_payload("Invocation payload must be a JSON object.")
    if any(not isinstance(key, str) for key in payload):
        raise _invalid_payload("Invocation payload keys must be strings.")

    supplied_authority = sorted(AUTHORITY_FIELDS.intersection(payload))
    if supplied_authority:
        raise AgentCoreAdapterError(
            400,
            "untrusted_identity_fields",
            "Identity and authorization fields are not accepted in payloads.",
        )

    raw_action = payload.get("action", InvocationAction.CHAT.value)
    if not isinstance(raw_action, str):
        raise _invalid_payload("Field 'action' must be a string.")
    try:
        action = InvocationAction(raw_action)
    except ValueError as exc:
        raise _invalid_payload("Field 'action' is not supported.") from exc

    allowed_fields = {"action"}
    if action is InvocationAction.CHAT:
        allowed_fields.update(CHAT_FIELDS)
    unexpected = sorted(set(payload).difference(allowed_fields))
    if unexpected:
        raise _invalid_payload("Invocation payload contains unsupported fields: " + ", ".join(unexpected) + ".")

    if action is not InvocationAction.CHAT:
        return ParsedInvocation(action=action)

    request_data = {field_name: payload[field_name] for field_name in CHAT_FIELDS if field_name in payload}
    request_data.setdefault("stream", False)
    errors = (validator or RequestValidator()).validate_payload(
        request_data,
        allow_empty_model=False,
        check_model=False,
    )
    if errors:
        raise _invalid_payload(errors[0].message)
    return ParsedInvocation(
        action=action,
        request_data=request_data,
    )
