"""Strict type, resource, and context checks for chat requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gateway.models import ChatCompletionRequest
from src.gateway.request_validator import RequestValidator


def _payload(**overrides):
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return payload


def _fields(errors) -> set[str]:
    return {error.field for error in errors}


@pytest.mark.parametrize(
    ("update", "field"),
    [
        ({"model": 7}, "model"),
        ({"messages": "hello"}, "messages"),
        ({"messages": [{"role": 1, "content": "x"}]}, "messages[0].role"),
        ({"messages": [{"role": "user", "content": 1}]}, "messages[0].content"),
        ({"temperature": True}, "temperature"),
        ({"top_p": "0.5"}, "top_p"),
        ({"max_tokens": True}, "max_tokens"),
        ({"stream": 1}, "stream"),
        ({"system": ["instructions"]}, "system"),
        ({"stop": [1]}, "stop[0]"),
        ({"tools": {}}, "tools"),
        ({"context": []}, "context"),
        ({"context": {"smart_routing": "true"}}, "context.smart_routing"),
        ({"provider": 1}, "provider"),
    ],
)
def test_type_confusion_is_rejected(update, field):
    errors = RequestValidator().validate_payload(_payload(**update))
    assert field in _fields(errors)


@pytest.mark.parametrize("temperature", [-0.01, 2.01, float("inf")])
def test_temperature_range_is_enforced(temperature):
    errors = RequestValidator().validate_payload(_payload(temperature=temperature))
    assert "temperature" in _fields(errors)


@pytest.mark.parametrize("top_p", [-0.01, 1.01, float("nan")])
def test_top_p_range_is_enforced(top_p):
    errors = RequestValidator().validate_payload(_payload(top_p=top_p))
    assert "top_p" in _fields(errors)


@pytest.mark.parametrize("max_tokens", [0, -1, 1.5])
def test_max_tokens_must_be_a_positive_integer(max_tokens):
    errors = RequestValidator().validate_payload(_payload(max_tokens=max_tokens))
    assert "max_tokens" in _fields(errors)


def test_max_tokens_has_a_configurable_hard_ceiling():
    validator = RequestValidator(max_requested_output_tokens=100)
    errors = validator.validate_payload(_payload(max_tokens=101))
    assert "max_tokens" in _fields(errors)


def test_message_count_and_individual_content_are_bounded():
    validator = RequestValidator(
        max_messages=2,
        max_message_content_bytes=8,
    )
    count_errors = validator.validate_payload(
        _payload(
            messages=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
            ]
        )
    )
    size_errors = validator.validate_payload(_payload(messages=[{"role": "user", "content": "123456789"}]))
    assert "messages" in _fields(count_errors)
    assert "messages[0].content" in _fields(size_errors)


def test_total_message_content_is_bounded():
    validator = RequestValidator(
        max_message_content_bytes=16,
        max_total_message_content_bytes=10,
    )
    errors = validator.validate_payload(
        _payload(
            messages=[
                {"role": "user", "content": "123456"},
                {"role": "assistant", "content": "123456"},
            ]
        )
    )
    assert "messages" in _fields(errors)


def _tool(description: str = "lookup"):
    return {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
            },
        },
    }


def test_tool_count_and_schema_size_are_bounded():
    count_validator = RequestValidator(max_tools=1)
    schema_validator = RequestValidator(max_tool_schema_bytes=100)

    count_errors = count_validator.validate_payload(_payload(tools=[_tool(), _tool()]))
    schema_errors = schema_validator.validate_payload(_payload(tools=[_tool(description="x" * 200)]))
    assert "tools" in _fields(count_errors)
    assert "tools[0]" in _fields(schema_errors)


def test_stop_count_and_size_are_bounded():
    validator = RequestValidator(
        max_stop_sequences=2,
        max_stop_sequence_bytes=4,
    )
    count_errors = validator.validate_payload(_payload(stop=["a", "b", "c"]))
    size_errors = validator.validate_payload(_payload(stop=["12345"]))
    assert "stop" in _fields(count_errors)
    assert "stop[0]" in _fields(size_errors)


def test_context_limit_includes_requested_output_tokens():
    model = SimpleNamespace(max_context_tokens=None)
    registry = SimpleNamespace(models={"m": model})
    validator = RequestValidator(registry)
    request = ChatCompletionRequest(
        model="m",
        messages=[{"role": "user", "content": "short"}],
    )
    prompt_tokens = validator._estimate_prompt_tokens(request)
    model.max_context_tokens = prompt_tokens + 5

    assert validator.validate(request) == []

    request.max_tokens = 6
    errors = validator.validate(request)
    assert "messages" in _fields(errors)
    assert "requested output tokens (6)" in errors[0].message


def test_auto_routing_still_receives_shape_and_resource_validation():
    validator = RequestValidator(max_message_content_bytes=8)
    errors = validator.validate_payload(
        _payload(
            model="",
            messages=[{"role": "user", "content": "x" * 9}],
        ),
        allow_empty_model=True,
    )
    assert "model" not in _fields(errors)
    assert "messages[0].content" in _fields(errors)


def test_valid_multimodal_tool_request_passes_shape_validation():
    errors = RequestValidator().validate_payload(
        _payload(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look this up"},
                        {"type": "image_url", "image_url": {"url": "https://example.test/x"}},
                    ],
                }
            ],
            temperature=0.5,
            top_p=0.9,
            max_tokens=256,
            stream=False,
            stop=["DONE"],
            tools=[_tool()],
            tool_choice={
                "type": "function",
                "function": {"name": "lookup"},
            },
        )
    )
    assert errors == []
