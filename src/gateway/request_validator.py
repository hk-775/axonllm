"""Request validation for incoming chat completion requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import tiktoken

from src.gateway.models import ChatCompletionRequest, ValidationError

if TYPE_CHECKING:
    from src.gateway.model_registry import ModelRegistry

VALID_ROLES = {"system", "user", "assistant", "tool"}


class RequestValidator:
    """Validates incoming chat completion requests before they enter the pipeline.

    Checks message structure, role values, model existence, and token limits.
    """

    def __init__(self, model_registry: ModelRegistry) -> None:
        self.model_registry = model_registry

    def validate(self, request: ChatCompletionRequest) -> list[ValidationError]:
        """Validate the request and return a list of errors (empty if valid)."""
        errors: list[ValidationError] = []

        # 1. Check each message has 'role' and 'content' fields
        for idx, msg in enumerate(request.messages):
            if not isinstance(msg, dict):
                errors.append(ValidationError(
                    field=f"messages[{idx}]",
                    message=f"Message at index {idx} is not a valid object.",
                    severity="error",
                ))
                continue

            if "role" not in msg:
                errors.append(ValidationError(
                    field=f"messages[{idx}].role",
                    message=f"Message at index {idx} is missing required field 'role'.",
                    severity="error",
                ))
            if "content" not in msg:
                errors.append(ValidationError(
                    field=f"messages[{idx}].content",
                    message=f"Message at index {idx} is missing required field 'content'.",
                    severity="error",
                ))

        # Return early if structural errors found
        if errors:
            return errors

        # 2. Check each role is valid
        for idx, msg in enumerate(request.messages):
            role = msg.get("role")
            if role not in VALID_ROLES:
                errors.append(ValidationError(
                    field=f"messages[{idx}].role",
                    message=f"Invalid role '{role}' at index {idx}. Must be one of: {sorted(VALID_ROLES)}.",
                    severity="error",
                ))

        if errors:
            return errors

        # 3. Check model exists in registry
        if request.model not in self.model_registry.models:
            errors.append(ValidationError(
                field="model",
                message=f"Model '{request.model}' not found.",
                severity="error",
            ))
            return errors

        # 4. Check token limit if configured
        model_config = self.model_registry.models.get(request.model)
        if model_config and model_config.max_context_tokens is not None:
            prompt_text = " ".join(
                msg.get("content", "") for msg in request.messages
            )
            try:
                encoding = tiktoken.get_encoding("cl100k_base")
                estimated_tokens = len(encoding.encode(prompt_text))
            except Exception:
                estimated_tokens = len(prompt_text) // 4  # rough fallback

            if estimated_tokens > model_config.max_context_tokens:
                errors.append(ValidationError(
                    field="messages",
                    message=(
                        f"Estimated prompt tokens ({estimated_tokens}) exceed "
                        f"model limit ({model_config.max_context_tokens})."
                    ),
                    severity="error",
                ))

        return errors
