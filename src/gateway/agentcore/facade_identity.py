"""Bounded credential identity forwarded by the IAM-authenticated facade."""

from __future__ import annotations

import base64
import json
from typing import Any

from src.gateway.models import AuthMethod, RequestContext


FACADE_IDENTITY_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-identity-token"
FACADE_IDENTITY_SCHEME = "AxonFacadeV1"
_FACADE_IDENTITY_SCHEMA = "axonllm.facade-credential/v1"
_MAX_ENCODED_IDENTITY_BYTES = 4096
_SUPPORTED_AUTH_METHODS = frozenset(
    {
        AuthMethod.API_KEY,
        AuthMethod.OIDC_JWT,
        AuthMethod.SSO,
    }
)


class FacadeIdentityError(ValueError):
    """The forwarded facade credential identity is malformed."""


def _required_identity_value(
    value: Any,
    name: str,
    *,
    max_length: int = 512,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise FacadeIdentityError(f"{name} is invalid")
    return value


def encode_facade_identity(context: RequestContext) -> str:
    """Encode credential identity without forwarding server-held authority."""
    if context.auth_method not in _SUPPORTED_AUTH_METHODS:
        raise FacadeIdentityError("auth_method is invalid")
    issuer = _required_identity_value(context.issuer, "issuer")
    subject = _required_identity_value(
        context.subject or context.api_key_id,
        "subject",
    )
    tenant_id = _required_identity_value(
        context.tenant_id,
        "tenant_id",
        max_length=128,
    )
    project_id = _required_identity_value(
        context.project_id,
        "project_id",
        max_length=128,
    )
    credential_id = context.api_key_id
    if credential_id is not None:
        credential_id = _required_identity_value(
            credential_id,
            "credential_id",
            max_length=256,
        )

    payload: dict[str, str] = {
        "schema": _FACADE_IDENTITY_SCHEMA,
        "auth_method": context.auth_method.value,
        "issuer": issuer,
        "subject": subject,
        "tenant_id": tenant_id,
        "project_id": project_id,
    }
    if credential_id is not None:
        payload["credential_id"] = credential_id
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).rstrip(b"=")
    if len(encoded) > _MAX_ENCODED_IDENTITY_BYTES:
        raise FacadeIdentityError("facade identity is too large")
    return f"{FACADE_IDENTITY_SCHEME} {encoded.decode('ascii')}"


def decode_facade_identity(value: str) -> RequestContext:
    """Decode one facade credential identity into non-authoritative hints."""
    scheme, separator, encoded = value.partition(" ")
    if (
        separator != " "
        or scheme != FACADE_IDENTITY_SCHEME
        or not encoded
        or len(encoded) > _MAX_ENCODED_IDENTITY_BYTES
        or any(character.isspace() for character in encoded)
    ):
        raise FacadeIdentityError("facade identity is invalid")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise FacadeIdentityError("facade identity is invalid") from exc

    required = {
        "schema",
        "auth_method",
        "issuer",
        "subject",
        "tenant_id",
        "project_id",
    }
    optional = {"credential_id"}
    if (
        type(payload) is not dict
        or set(payload).difference(required | optional)
        or not required.issubset(payload)
        or payload.get("schema") != _FACADE_IDENTITY_SCHEMA
    ):
        raise FacadeIdentityError("facade identity is invalid")
    try:
        auth_method = AuthMethod(payload["auth_method"])
    except (TypeError, ValueError) as exc:
        raise FacadeIdentityError("facade identity is invalid") from exc
    if auth_method not in _SUPPORTED_AUTH_METHODS:
        raise FacadeIdentityError("facade identity is invalid")

    issuer = _required_identity_value(payload["issuer"], "issuer")
    subject = _required_identity_value(payload["subject"], "subject")
    tenant_id = _required_identity_value(
        payload["tenant_id"],
        "tenant_id",
        max_length=128,
    )
    project_id = _required_identity_value(
        payload["project_id"],
        "project_id",
        max_length=128,
    )
    credential_id = payload.get("credential_id")
    if credential_id is not None:
        credential_id = _required_identity_value(
            credential_id,
            "credential_id",
            max_length=256,
        )
    if auth_method is AuthMethod.API_KEY:
        if credential_id is None or credential_id != subject:
            raise FacadeIdentityError("facade identity is invalid")
    elif credential_id is not None:
        raise FacadeIdentityError("facade identity is invalid")

    return RequestContext(
        user_id=subject,
        project_id=project_id,
        roles=[],
        scopes=[],
        auth_method=auth_method,
        tenant_id=tenant_id,
        api_key_id=credential_id,
        issuer=issuer,
        subject=subject,
    )
