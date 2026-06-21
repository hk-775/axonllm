# Feature: litellm-service, Property 30: Structured log entry completeness
# Feature: litellm-service, Property 31: Failure log diagnostic fields
"""Property-based tests for the GatewayLogger component.

Properties covered:
  30 – Structured log entries contain all required fields
  31 – Failure logs contain diagnostic fields
"""

import json
import logging

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from src.gateway.logging import GatewayLogger
from src.gateway.models import RequestLogEntry


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=30,
).filter(lambda s: len(s.strip()) > 0)

model_strategy = st.sampled_from(["gpt-4", "claude-3", "gemini-pro", "command-r"])
provider_strategy = st.sampled_from(["openai", "anthropic", "bedrock", "azure_openai", "vertex_ai", "cohere"])
latency_strategy = st.floats(min_value=0.0, max_value=60000.0, allow_nan=False, allow_infinity=False)
cost_strategy = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
status_code_strategy = st.sampled_from([200, 201, 400, 401, 403, 429, 500, 502, 503, 504])
token_strategy = st.integers(min_value=0, max_value=100000)
timestamp_strategy = st.datetimes()


# ===========================================================================
# Property 30: Structured log entries contain all required fields
# Feature: litellm-service, Property 30: Structured log entry completeness
# ===========================================================================


@given(
    request_id=id_strategy,
    project_id=id_strategy,
    user_id=id_strategy,
    model=model_strategy,
    provider=provider_strategy,
    latency_ms=latency_strategy,
    status_code=status_code_strategy,
    prompt_tokens=token_strategy,
    completion_tokens=token_strategy,
    cost=cost_strategy,
    timestamp=timestamp_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_structured_log_entry_contains_all_required_fields(
    request_id, project_id, user_id, model, provider,
    latency_ms, status_code, prompt_tokens, completion_tokens,
    cost, timestamp, caplog,
):
    """Property 30: Structured log entries contain all required fields.

    For any processed request, the emitted structured log entry SHALL contain:
    request_id, project_id, user_id, model, provider, latency_ms, status_code,
    prompt_tokens, completion_tokens, total_tokens, and cost.

    **Validates: Requirements 14.2**
    """
    total_tokens = prompt_tokens + completion_tokens

    entry = RequestLogEntry(
        request_id=request_id,
        project_id=project_id,
        user_id=user_id,
        model=model,
        provider=provider,
        latency_ms=latency_ms,
        status_code=status_code,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost=cost,
        timestamp=timestamp,
    )

    logger = GatewayLogger()

    with caplog.at_level(logging.DEBUG, logger=f"gateway.project.{project_id}"):
        logger.log_request(entry)

    # Find the JSON log message emitted by log_request
    log_messages = [r.message for r in caplog.records if "request_completed" in r.message]
    assert len(log_messages) >= 1, "Expected at least one request_completed log entry"

    log_data = json.loads(log_messages[-1])

    # Verify all required fields are present
    required_fields = [
        "request_id", "project_id", "user_id", "model", "provider",
        "latency_ms", "status_code", "prompt_tokens", "completion_tokens",
        "total_tokens", "cost",
    ]
    for field_name in required_fields:
        assert field_name in log_data, f"Required field '{field_name}' missing from log entry"

    # Verify field values match the input
    assert log_data["request_id"] == request_id
    assert log_data["project_id"] == project_id
    assert log_data["user_id"] == user_id
    assert log_data["model"] == model
    assert log_data["provider"] == provider
    assert log_data["latency_ms"] == latency_ms
    assert log_data["status_code"] == status_code
    assert log_data["prompt_tokens"] == prompt_tokens
    assert log_data["completion_tokens"] == completion_tokens
    assert log_data["total_tokens"] == total_tokens
    assert log_data["cost"] == cost

    caplog.clear()


# ===========================================================================
# Property 31: Failure logs contain diagnostic fields
# Feature: litellm-service, Property 31: Failure log diagnostic fields
# ===========================================================================

error_type_strategy = st.sampled_from([
    "ConnectionError", "TimeoutError", "RateLimitError",
    "AuthenticationError", "ServerError", "InvalidRequestError",
])
retry_attempt_strategy = st.integers(min_value=0, max_value=10)
message_strategy = st.text(min_size=0, max_size=100)


@given(
    provider=provider_strategy,
    error_type=error_type_strategy,
    status_code=status_code_strategy,
    retry_attempt=retry_attempt_strategy,
    message=message_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_failure_log_contains_diagnostic_fields(
    provider, error_type, status_code, retry_attempt, message, caplog,
):
    """Property 31: Failure logs contain diagnostic fields.

    For any failed provider call, the emitted log entry SHALL contain the
    provider name, error type, HTTP status code, and retry attempt number.

    **Validates: Requirements 14.4**
    """
    logger = GatewayLogger()

    with caplog.at_level(logging.DEBUG, logger="gateway"):
        logger.log_failure(
            provider=provider,
            error_type=error_type,
            status_code=status_code,
            retry_attempt=retry_attempt,
            message=message,
        )

    # Find the JSON log message emitted by log_failure
    log_messages = [r.message for r in caplog.records if "provider_failure" in r.message]
    assert len(log_messages) >= 1, "Expected at least one provider_failure log entry"

    log_data = json.loads(log_messages[-1])

    # Verify all diagnostic fields are present
    diagnostic_fields = ["provider", "error_type", "status_code", "retry_attempt"]
    for field_name in diagnostic_fields:
        assert field_name in log_data, f"Diagnostic field '{field_name}' missing from failure log"

    # Verify field values match the input
    assert log_data["provider"] == provider
    assert log_data["error_type"] == error_type
    assert log_data["status_code"] == status_code
    assert log_data["retry_attempt"] == retry_attempt

    caplog.clear()
