# Feature: litellm-service, Property 4: Request normalization preserves all supported parameters
# Validates: Requirements 2.2, 4.5, 4.6
"""Property-based test: Request normalization preserves all supported parameters.

For any valid ChatCompletionRequest with supported parameters (temperature,
max_tokens, top_p, stop, system message) and for any ProviderAdapter,
translating the request SHALL produce a provider-native dict containing the
provider's equivalent for each supported parameter. Unsupported parameters
SHALL be omitted and a warning SHALL be included.
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.openai_style import _is_openai_reasoning_model
from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.adapters.azure_adapter import AzureOpenAIAdapter
from src.gateway.adapters.vertex_adapter import VertexAIAdapter
from src.gateway.adapters.cohere_adapter import CohereAdapter
from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_safe_chars = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _"
)
safe_text = st.text(_safe_chars, min_size=1, max_size=30).map(str.strip).filter(
    lambda s: len(s) > 0
)

user_message_strategy = st.fixed_dictionaries({
    "role": st.just("user"),
    "content": safe_text,
})

assistant_message_strategy = st.fixed_dictionaries({
    "role": st.just("assistant"),
    "content": safe_text,
})

# Build a messages list: at least one user message, optionally interleaved with assistant
messages_strategy = st.lists(
    st.one_of(user_message_strategy, assistant_message_strategy),
    min_size=1,
    max_size=5,
).filter(lambda msgs: any(m["role"] == "user" for m in msgs))

stop_sequences_strategy = st.lists(safe_text, min_size=1, max_size=3, unique=True)


@st.composite
def chat_completion_request_strategy(draw):
    """Generate a valid ChatCompletionRequest with random optional parameters."""
    messages = draw(messages_strategy)
    model = draw(safe_text)
    temperature = draw(st.one_of(st.none(), st.floats(min_value=0.0, max_value=2.0, allow_nan=False)))
    max_tokens = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=4096)))
    top_p = draw(st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False)))
    stop = draw(st.one_of(st.none(), stop_sequences_strategy))
    stream = draw(st.booleans())
    system = draw(st.one_of(st.none(), safe_text))

    return ChatCompletionRequest(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        stop=stop,
        stream=stream,
        system=system,
    )


# ---------------------------------------------------------------------------
# All adapters to test
# ---------------------------------------------------------------------------

ALL_ADAPTERS = [
    OpenAIAdapter(),
    AnthropicAdapter(),
    BedrockAdapter(),
    AzureOpenAIAdapter(),
    VertexAIAdapter(),
    CohereAdapter(),
]


# ---------------------------------------------------------------------------
# Verification helpers per adapter family
# ---------------------------------------------------------------------------


def _verify_openai_family(payload: dict, request: ChatCompletionRequest) -> None:
    """Verify OpenAI / Azure OpenAI translated payload preserves parameters."""
    is_reasoning = _is_openai_reasoning_model(request.model)

    # System message should be prepended as first message with role "system"
    if request.system:
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][0]["content"] == request.system

    if request.temperature is not None and not is_reasoning:
        assert payload["temperature"] == request.temperature
    else:
        assert "temperature" not in payload

    if request.max_tokens is not None:
        parameter = (
            "max_completion_tokens" if is_reasoning else "max_tokens"
        )
        assert payload[parameter] == request.max_tokens
    else:
        assert "max_tokens" not in payload
        assert "max_completion_tokens" not in payload

    if request.top_p is not None and not is_reasoning:
        assert payload["top_p"] == request.top_p
    else:
        assert "top_p" not in payload

    if request.stop is not None:
        assert payload["stop"] == request.stop
    else:
        assert "stop" not in payload

    if request.stream:
        assert payload.get("stream") is True
    else:
        assert "stream" not in payload


def _verify_anthropic_family(payload: dict, request: ChatCompletionRequest) -> None:
    """Verify Anthropic / Bedrock translated payload preserves parameters."""
    # System should be a separate top-level field (not in messages)
    if request.system:
        assert payload["system"] == request.system

    # Messages should NOT contain system role entries
    for msg in payload["messages"]:
        assert msg["role"] != "system"

    # max_tokens is always present (defaults to 4096 if not set)
    assert "max_tokens" in payload
    if request.max_tokens is not None:
        assert payload["max_tokens"] == request.max_tokens
    else:
        assert payload["max_tokens"] == 4096  # default

    if request.temperature is not None:
        assert payload["temperature"] == request.temperature
    else:
        assert "temperature" not in payload

    if request.top_p is not None:
        assert payload["top_p"] == request.top_p
    else:
        assert "top_p" not in payload

    # stop -> stop_sequences
    if request.stop is not None:
        assert payload["stop_sequences"] == request.stop
        assert "stop" not in payload
    else:
        assert "stop_sequences" not in payload

    if request.stream:
        assert payload.get("stream") is True


def _verify_vertex(payload: dict, request: ChatCompletionRequest) -> None:
    """Verify Vertex AI translated payload preserves parameters."""
    # System message -> systemInstruction
    if request.system:
        assert "systemInstruction" in payload
        parts = payload["systemInstruction"]["parts"]
        assert any(p["text"] == request.system for p in parts)

    # Contents should not contain system role
    for content in payload.get("contents", []):
        assert content["role"] in ("user", "model")

    gen_config = payload.get("generationConfig", {})

    if request.temperature is not None:
        assert gen_config["temperature"] == request.temperature
    else:
        assert "temperature" not in gen_config

    if request.max_tokens is not None:
        assert gen_config["maxOutputTokens"] == request.max_tokens
    else:
        assert "maxOutputTokens" not in gen_config

    if request.top_p is not None:
        assert gen_config["topP"] == request.top_p
    else:
        assert "topP" not in gen_config

    if request.stop is not None:
        assert gen_config["stopSequences"] == request.stop
    else:
        assert "stopSequences" not in gen_config

    # Vertex selects streaming through the endpoint, not a body parameter.
    assert "stream" not in payload
    assert "_warnings" not in payload


def _verify_cohere(payload: dict, request: ChatCompletionRequest) -> None:
    """Verify Cohere translated payload preserves parameters."""
    # System message -> preamble
    if request.system:
        assert payload["preamble"] == request.system

    if request.temperature is not None:
        assert payload["temperature"] == request.temperature
    else:
        assert "temperature" not in payload

    if request.max_tokens is not None:
        assert payload["max_tokens"] == request.max_tokens
    else:
        assert "max_tokens" not in payload

    # top_p -> p
    if request.top_p is not None:
        assert payload["p"] == request.top_p
        assert "top_p" not in payload
    else:
        assert "p" not in payload

    # stop -> stop_sequences
    if request.stop is not None:
        assert payload["stop_sequences"] == request.stop
        assert "stop" not in payload
    else:
        assert "stop_sequences" not in payload

    if request.stream:
        assert payload["stream"] is True
    else:
        assert "stream" not in payload


# Map adapter class to its verification function
_VERIFIERS = {
    OpenAIAdapter: _verify_openai_family,
    AzureOpenAIAdapter: _verify_openai_family,
    AnthropicAdapter: _verify_anthropic_family,
    BedrockAdapter: _verify_anthropic_family,
    VertexAIAdapter: _verify_vertex,
    CohereAdapter: _verify_cohere,
}


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@given(request=chat_completion_request_strategy())
@settings(max_examples=100)
def test_request_normalization_preserves_parameters(request):
    """Property 4: Request normalization preserves all supported parameters.

    For any valid ChatCompletionRequest with supported parameters (temperature,
    max_tokens, top_p, stop, system message) and for any ProviderAdapter,
    translating the request SHALL produce a provider-native dict containing the
    provider's equivalent for each supported parameter. Unsupported parameters
    SHALL be omitted and a warning SHALL be included.

    **Validates: Requirements 2.2, 4.5, 4.6**
    """
    loop = asyncio.new_event_loop()
    try:
        for adapter in ALL_ADAPTERS:
            payload = loop.run_until_complete(adapter.translate_request(request))

            # Vertex carries the model in the endpoint URL. Other adapters put
            # it in the body.
            if isinstance(adapter, VertexAIAdapter):
                assert "model" not in payload
            else:
                assert payload.get("model") == request.model, (
                    f"{type(adapter).__name__}: model mismatch - "
                    f"expected '{request.model}', got '{payload.get('model')}'"
                )

            # Run adapter-specific verification
            verifier = _VERIFIERS[type(adapter)]
            try:
                verifier(payload, request)
            except AssertionError as exc:
                raise AssertionError(
                    f"{type(adapter).__name__}: {exc}"
                ) from exc
    finally:
        loop.close()


# Feature: litellm-service, Property 5: Response normalization produces complete OpenAI-compatible format
# Validates: Requirements 2.3
"""
Property 5: For any valid provider response dict and for any ProviderAdapter,
translating the response SHALL produce a ChatCompletionResponse containing
choices, usage (prompt_tokens, completion_tokens, total_tokens), and model metadata.
"""

# ---------------------------------------------------------------------------
# Hypothesis strategies for provider response dicts
# ---------------------------------------------------------------------------

_response_id_strategy = st.text(
    st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-_"),
    min_size=1,
    max_size=30,
).filter(lambda s: len(s.strip()) > 0)

_model_name_strategy = st.text(
    st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-_."),
    min_size=1,
    max_size=40,
).filter(lambda s: len(s.strip()) > 0)

_token_count_strategy = st.integers(min_value=0, max_value=100_000)


@st.composite
def openai_response_strategy(draw):
    """Generate a valid OpenAI / Azure OpenAI provider response dict."""
    return {
        "id": draw(_response_id_strategy),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": draw(safe_text)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": draw(_token_count_strategy),
            "completion_tokens": draw(_token_count_strategy),
        },
        "model": draw(_model_name_strategy),
    }


@st.composite
def anthropic_response_strategy(draw):
    """Generate a valid Anthropic provider response dict."""
    return {
        "id": draw(_response_id_strategy),
        "content": [{"type": "text", "text": draw(safe_text)}],
        "model": draw(_model_name_strategy),
        "stop_reason": "stop",
        "usage": {
            "input_tokens": draw(_token_count_strategy),
            "output_tokens": draw(_token_count_strategy),
        },
    }


@st.composite
def bedrock_response_strategy(draw):
    """Generate a valid Bedrock provider response dict.

    Bedrock may use either camelCase (inputTokens/outputTokens) or
    snake_case (input_tokens/output_tokens) for usage keys.
    """
    use_camel = draw(st.booleans())
    prompt_tokens = draw(_token_count_strategy)
    completion_tokens = draw(_token_count_strategy)

    if use_camel:
        usage = {"inputTokens": prompt_tokens, "outputTokens": completion_tokens}
    else:
        usage = {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}

    return {
        "id": draw(_response_id_strategy),
        "content": [{"type": "text", "text": draw(safe_text)}],
        "model": draw(_model_name_strategy),
        "stop_reason": "stop",
        "usage": usage,
        "_expected_prompt_tokens": prompt_tokens,
        "_expected_completion_tokens": completion_tokens,
    }


@st.composite
def vertex_response_strategy(draw):
    """Generate a valid Vertex AI provider response dict."""
    return {
        "id": draw(_response_id_strategy),
        "candidates": [
            {
                "content": {
                    "parts": [{"text": draw(safe_text)}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": draw(_token_count_strategy),
            "candidatesTokenCount": draw(_token_count_strategy),
        },
        "model": draw(_model_name_strategy),
    }


@st.composite
def cohere_response_strategy(draw):
    """Generate a valid Cohere provider response dict."""
    return {
        "id": draw(_response_id_strategy),
        "text": draw(safe_text),
        "model": draw(_model_name_strategy),
        "finish_reason": "stop",
        "meta": {
            "tokens": {
                "input_tokens": draw(_token_count_strategy),
                "output_tokens": draw(_token_count_strategy),
            }
        },
    }


# ---------------------------------------------------------------------------
# Adapter ↔ strategy ↔ expected provider name mapping
# ---------------------------------------------------------------------------

_ADAPTER_RESPONSE_CONFIGS = [
    (OpenAIAdapter(), openai_response_strategy(), "openai"),
    (AzureOpenAIAdapter(), openai_response_strategy(), "azure_openai"),
    (AnthropicAdapter(), anthropic_response_strategy(), "anthropic"),
    (BedrockAdapter(), bedrock_response_strategy(), "bedrock"),
    (VertexAIAdapter(), vertex_response_strategy(), "vertex_ai"),
    (CohereAdapter(), cohere_response_strategy(), "cohere"),
]


# ---------------------------------------------------------------------------
# Common assertion helper
# ---------------------------------------------------------------------------


def _assert_complete_response(
    response: "ChatCompletionResponse",
    expected_provider: str,
) -> None:
    """Assert that a ChatCompletionResponse has all required fields."""
    # id must be a non-None string
    assert response.id is not None, "id must not be None"
    assert isinstance(response.id, str), f"id must be str, got {type(response.id)}"

    # choices must be a list
    assert isinstance(response.choices, list), (
        f"choices must be a list, got {type(response.choices)}"
    )

    # usage fields
    assert response.usage.prompt_tokens >= 0, (
        f"prompt_tokens must be >= 0, got {response.usage.prompt_tokens}"
    )
    assert response.usage.completion_tokens >= 0, (
        f"completion_tokens must be >= 0, got {response.usage.completion_tokens}"
    )
    assert response.usage.total_tokens >= 0, (
        f"total_tokens must be >= 0, got {response.usage.total_tokens}"
    )
    assert response.usage.total_tokens == response.usage.prompt_tokens + response.usage.completion_tokens, (
        f"total_tokens ({response.usage.total_tokens}) != "
        f"prompt_tokens ({response.usage.prompt_tokens}) + "
        f"completion_tokens ({response.usage.completion_tokens})"
    )

    # model must be a string
    assert isinstance(response.model, str), f"model must be str, got {type(response.model)}"

    # provider must match the adapter's provider name
    assert response.provider == expected_provider, (
        f"provider mismatch: expected '{expected_provider}', got '{response.provider}'"
    )


# ---------------------------------------------------------------------------
# Property tests — one per adapter for clear failure reporting
# ---------------------------------------------------------------------------


@given(resp=openai_response_strategy())
@settings(max_examples=100)
def test_response_normalization_completeness_openai(resp):
    """**Validates: Requirements 2.3** — OpenAI adapter."""
    adapter = OpenAIAdapter()
    result = adapter.translate_response(resp)
    _assert_complete_response(result, "openai")


@given(resp=openai_response_strategy())
@settings(max_examples=100)
def test_response_normalization_completeness_azure(resp):
    """**Validates: Requirements 2.3** — Azure OpenAI adapter."""
    adapter = AzureOpenAIAdapter()
    result = adapter.translate_response(resp)
    _assert_complete_response(result, "azure_openai")


@given(resp=anthropic_response_strategy())
@settings(max_examples=100)
def test_response_normalization_completeness_anthropic(resp):
    """**Validates: Requirements 2.3** — Anthropic adapter."""
    adapter = AnthropicAdapter()
    result = adapter.translate_response(resp)
    _assert_complete_response(result, "anthropic")


@given(resp=bedrock_response_strategy())
@settings(max_examples=100)
def test_response_normalization_completeness_bedrock(resp):
    """**Validates: Requirements 2.3** — Bedrock adapter."""
    adapter = BedrockAdapter()
    result = adapter.translate_response(resp)
    _assert_complete_response(result, "bedrock")
    # Also verify the expected token counts are preserved
    expected_prompt = resp["_expected_prompt_tokens"]
    expected_completion = resp["_expected_completion_tokens"]
    assert result.usage.prompt_tokens == expected_prompt, (
        f"prompt_tokens mismatch: expected {expected_prompt}, got {result.usage.prompt_tokens}"
    )
    assert result.usage.completion_tokens == expected_completion, (
        f"completion_tokens mismatch: expected {expected_completion}, got {result.usage.completion_tokens}"
    )


@given(resp=vertex_response_strategy())
@settings(max_examples=100)
def test_response_normalization_completeness_vertex(resp):
    """**Validates: Requirements 2.3** — Vertex AI adapter."""
    adapter = VertexAIAdapter()
    result = adapter.translate_response(resp)
    _assert_complete_response(result, "vertex_ai")


@given(resp=cohere_response_strategy())
@settings(max_examples=100)
def test_response_normalization_completeness_cohere(resp):
    """**Validates: Requirements 2.3** — Cohere adapter."""
    adapter = CohereAdapter()
    result = adapter.translate_response(resp)
    _assert_complete_response(result, "cohere")


# ---------------------------------------------------------------------------
# Combined property test (all adapters in one test)
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=100)
def test_response_normalization_completeness(data):
    """Property 5: Response normalization produces complete OpenAI-compatible format.

    For any valid provider response dict and for any ProviderAdapter,
    translating the response SHALL produce a ChatCompletionResponse containing
    choices, usage (prompt_tokens, completion_tokens, total_tokens), and model
    metadata.

    **Validates: Requirements 2.3**
    """
    for adapter, strategy, expected_provider in _ADAPTER_RESPONSE_CONFIGS:
        resp = data.draw(strategy, label=f"{expected_provider}_response")
        result = adapter.translate_response(resp)
        try:
            _assert_complete_response(result, expected_provider)
        except AssertionError as exc:
            raise AssertionError(
                f"{type(adapter).__name__}: {exc}"
            ) from exc
