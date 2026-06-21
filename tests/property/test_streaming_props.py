# Feature: litellm-service, Property 6: Streaming chunks translate to valid SSE format
# Validates: Requirements 3.1, 3.2, 3.4
"""Property-based test: Streaming chunks translate to valid SSE format.

For any provider that supports native streaming and for any stream chunk from
that provider, translating the chunk SHALL produce a StreamChunk with a valid id,
choices with delta content, and model name. The final chunk SHALL have
is_final=True.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.adapters.azure_adapter import AzureOpenAIAdapter
from src.gateway.adapters.vertex_adapter import VertexAIAdapter
from src.gateway.adapters.cohere_adapter import CohereAdapter
from src.gateway.models import StreamChunk


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_safe_chars = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"
)
safe_text = st.text(_safe_chars, min_size=1, max_size=40).map(str.strip).filter(
    lambda s: len(s) > 0
)

_chunk_id_strategy = st.text(
    st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-_"),
    min_size=1,
    max_size=30,
).filter(lambda s: len(s.strip()) > 0)

_model_name_strategy = st.text(
    st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-_."),
    min_size=1,
    max_size=40,
).filter(lambda s: len(s.strip()) > 0)


# ---------------------------------------------------------------------------
# OpenAI / Azure streaming chunk strategies
# ---------------------------------------------------------------------------

@st.composite
def openai_intermediate_chunk(draw):
    """OpenAI intermediate streaming chunk: finish_reason=None."""
    return {
        "id": draw(_chunk_id_strategy),
        "choices": [
            {
                "index": 0,
                "delta": {"content": draw(safe_text)},
                "finish_reason": None,
            }
        ],
        "model": draw(_model_name_strategy),
    }


@st.composite
def openai_final_chunk(draw):
    """OpenAI final streaming chunk: finish_reason='stop'."""
    return {
        "id": draw(_chunk_id_strategy),
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
        "model": draw(_model_name_strategy),
    }


# ---------------------------------------------------------------------------
# Anthropic / Bedrock streaming chunk strategies
# ---------------------------------------------------------------------------

@st.composite
def anthropic_intermediate_chunk(draw):
    """Anthropic intermediate chunk: content_block_delta with delta.text."""
    return {
        "type": "content_block_delta",
        "delta": {"text": draw(safe_text)},
        "id": draw(_chunk_id_strategy),
        "model": draw(_model_name_strategy),
    }


@st.composite
def anthropic_final_chunk(draw):
    """Anthropic final chunk: message_stop."""
    return {
        "type": "message_stop",
        "id": draw(_chunk_id_strategy),
        "model": draw(_model_name_strategy),
    }


# ---------------------------------------------------------------------------
# Vertex AI streaming chunk strategies
# ---------------------------------------------------------------------------

@st.composite
def vertex_intermediate_chunk(draw):
    """Vertex AI intermediate chunk: finishReason=None."""
    return {
        "id": draw(_chunk_id_strategy),
        "candidates": [
            {
                "content": {
                    "parts": [{"text": draw(safe_text)}],
                },
                "finishReason": None,
            }
        ],
        "model": draw(_model_name_strategy),
    }


@st.composite
def vertex_final_chunk(draw):
    """Vertex AI final chunk: finishReason='STOP'."""
    return {
        "id": draw(_chunk_id_strategy),
        "candidates": [
            {
                "content": {
                    "parts": [{"text": ""}],
                },
                "finishReason": "STOP",
            }
        ],
        "model": draw(_model_name_strategy),
    }


# ---------------------------------------------------------------------------
# Cohere streaming chunk strategies
# ---------------------------------------------------------------------------

@st.composite
def cohere_intermediate_chunk(draw):
    """Cohere intermediate chunk: event_type='text-generation'."""
    return {
        "event_type": "text-generation",
        "text": draw(safe_text),
        "id": draw(_chunk_id_strategy),
        "model": draw(_model_name_strategy),
    }


@st.composite
def cohere_final_chunk(draw):
    """Cohere final chunk: event_type='stream-end'."""
    return {
        "event_type": "stream-end",
        "id": draw(_chunk_id_strategy),
        "model": draw(_model_name_strategy),
    }


# ---------------------------------------------------------------------------
# Common assertion helpers
# ---------------------------------------------------------------------------

def _assert_valid_stream_chunk(result: StreamChunk, adapter_name: str) -> None:
    """Assert that a StreamChunk has the required structural fields."""
    assert isinstance(result, StreamChunk), (
        f"{adapter_name}: expected StreamChunk, got {type(result)}"
    )
    assert isinstance(result.id, str), (
        f"{adapter_name}: id must be str, got {type(result.id)}"
    )
    assert isinstance(result.choices, list), (
        f"{adapter_name}: choices must be a list, got {type(result.choices)}"
    )
    assert isinstance(result.model, str), (
        f"{adapter_name}: model must be str, got {type(result.model)}"
    )


def _assert_intermediate(result: StreamChunk, adapter_name: str) -> None:
    """Assert intermediate chunk properties."""
    _assert_valid_stream_chunk(result, adapter_name)
    assert result.is_final is False, (
        f"{adapter_name}: intermediate chunk should have is_final=False, got {result.is_final}"
    )


def _assert_final(result: StreamChunk, adapter_name: str) -> None:
    """Assert final chunk properties."""
    _assert_valid_stream_chunk(result, adapter_name)
    assert result.is_final is True, (
        f"{adapter_name}: final chunk should have is_final=True, got {result.is_final}"
    )


# ---------------------------------------------------------------------------
# Property tests — OpenAI
# ---------------------------------------------------------------------------

@given(chunk=openai_intermediate_chunk())
@settings(max_examples=100)
def test_openai_intermediate_chunk_translation(chunk):
    """Property 6 — OpenAI intermediate chunks produce valid StreamChunk with is_final=False.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = OpenAIAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_intermediate(result, "OpenAIAdapter")


@given(chunk=openai_final_chunk())
@settings(max_examples=100)
def test_openai_final_chunk_translation(chunk):
    """Property 6 — OpenAI final chunks produce valid StreamChunk with is_final=True.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = OpenAIAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_final(result, "OpenAIAdapter")


# ---------------------------------------------------------------------------
# Property tests — Azure OpenAI
# ---------------------------------------------------------------------------

@given(chunk=openai_intermediate_chunk())
@settings(max_examples=100)
def test_azure_intermediate_chunk_translation(chunk):
    """Property 6 — Azure intermediate chunks produce valid StreamChunk with is_final=False.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = AzureOpenAIAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_intermediate(result, "AzureOpenAIAdapter")


@given(chunk=openai_final_chunk())
@settings(max_examples=100)
def test_azure_final_chunk_translation(chunk):
    """Property 6 — Azure final chunks produce valid StreamChunk with is_final=True.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = AzureOpenAIAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_final(result, "AzureOpenAIAdapter")


# ---------------------------------------------------------------------------
# Property tests — Anthropic
# ---------------------------------------------------------------------------

@given(chunk=anthropic_intermediate_chunk())
@settings(max_examples=100)
def test_anthropic_intermediate_chunk_translation(chunk):
    """Property 6 — Anthropic intermediate chunks produce valid StreamChunk with is_final=False.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = AnthropicAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_intermediate(result, "AnthropicAdapter")


@given(chunk=anthropic_final_chunk())
@settings(max_examples=100)
def test_anthropic_final_chunk_translation(chunk):
    """Property 6 — Anthropic final chunks produce valid StreamChunk with is_final=True.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = AnthropicAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_final(result, "AnthropicAdapter")


# ---------------------------------------------------------------------------
# Property tests — Bedrock
# ---------------------------------------------------------------------------

@given(chunk=anthropic_intermediate_chunk())
@settings(max_examples=100)
def test_bedrock_intermediate_chunk_translation(chunk):
    """Property 6 — Bedrock intermediate chunks produce valid StreamChunk with is_final=False.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = BedrockAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_intermediate(result, "BedrockAdapter")


@given(chunk=anthropic_final_chunk())
@settings(max_examples=100)
def test_bedrock_final_chunk_translation(chunk):
    """Property 6 — Bedrock final chunks produce valid StreamChunk with is_final=True.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = BedrockAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_final(result, "BedrockAdapter")


# ---------------------------------------------------------------------------
# Property tests — Vertex AI
# ---------------------------------------------------------------------------

@given(chunk=vertex_intermediate_chunk())
@settings(max_examples=100)
def test_vertex_intermediate_chunk_translation(chunk):
    """Property 6 — Vertex AI intermediate chunks produce valid StreamChunk with is_final=False.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = VertexAIAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_intermediate(result, "VertexAIAdapter")


@given(chunk=vertex_final_chunk())
@settings(max_examples=100)
def test_vertex_final_chunk_translation(chunk):
    """Property 6 — Vertex AI final chunks produce valid StreamChunk with is_final=True.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = VertexAIAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_final(result, "VertexAIAdapter")


# ---------------------------------------------------------------------------
# Property tests — Cohere
# ---------------------------------------------------------------------------

@given(chunk=cohere_intermediate_chunk())
@settings(max_examples=100)
def test_cohere_intermediate_chunk_translation(chunk):
    """Property 6 — Cohere intermediate chunks produce valid StreamChunk with is_final=False.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = CohereAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_intermediate(result, "CohereAdapter")


@given(chunk=cohere_final_chunk())
@settings(max_examples=100)
def test_cohere_final_chunk_translation(chunk):
    """Property 6 — Cohere final chunks produce valid StreamChunk with is_final=True.

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    adapter = CohereAdapter()
    result = adapter.translate_stream_chunk(chunk)
    _assert_final(result, "CohereAdapter")


# Feature: litellm-service, Property 7: Simulated streaming reconstructs original response
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------
# Property 7 — Simulated streaming reconstructs original response
#
# For any complete ChatCompletionResponse from a non-streaming provider,
# simulating streaming by breaking the response into token-sized chunks and
# concatenating all chunk deltas SHALL produce content equal to the original
# response content.
# ---------------------------------------------------------------------------

from src.gateway.models import ChatCompletionResponse, TokenUsage
from src.gateway.streaming import simulate_streaming


# Strategy: generate random ChatCompletionResponse objects with text content
@st.composite
def chat_completion_response(draw):
    """Generate a random ChatCompletionResponse with text content."""
    response_id = draw(
        st.text(
            st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
            min_size=1,
            max_size=30,
        ).filter(lambda s: len(s.strip()) > 0)
    )
    # Content can include spaces, punctuation, newlines, etc.
    content = draw(
        st.text(
            st.characters(
                whitelist_categories=("L", "N", "P", "Z", "S"),
                blacklist_characters="\x00",
            ),
            min_size=0,
            max_size=200,
        )
    )
    model = draw(_model_name_strategy)
    provider = draw(
        st.sampled_from(["openai", "anthropic", "bedrock", "azure_openai", "vertex_ai", "cohere"])
    )
    prompt_tokens = draw(st.integers(min_value=0, max_value=10000))
    completion_tokens = draw(st.integers(min_value=0, max_value=10000))

    return ChatCompletionResponse(
        id=response_id,
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=model,
        provider=provider,
    )


@given(response=chat_completion_response())
@settings(max_examples=100)
def test_simulated_streaming_reconstruction(response):
    """Property 7 — Simulated streaming reconstructs original response.

    For any complete ChatCompletionResponse, simulating streaming and
    concatenating all chunk deltas SHALL produce content equal to the
    original response content.

    **Validates: Requirements 3.3**
    """
    # Extract original content
    original_content = response.choices[0]["message"]["content"]

    # Simulate streaming
    chunks = simulate_streaming(response)

    # Must produce at least one chunk
    assert len(chunks) >= 1, "simulate_streaming must produce at least one chunk"

    # Last chunk must have is_final=True
    assert chunks[-1].is_final is True, "Last chunk must have is_final=True"

    # All non-last chunks must have is_final=False
    for chunk in chunks[:-1]:
        assert chunk.is_final is False, f"Non-final chunk has is_final=True: {chunk}"

    # All chunks must have the same id and model as the original response
    for chunk in chunks:
        assert chunk.id == response.id, (
            f"Chunk id {chunk.id!r} != response id {response.id!r}"
        )
        assert chunk.model == response.model, (
            f"Chunk model {chunk.model!r} != response model {response.model!r}"
        )

    # Concatenate all chunk delta contents
    reconstructed = "".join(
        chunk.choices[0]["delta"]["content"] for chunk in chunks
    )

    # Reconstructed content must equal original
    assert reconstructed == original_content, (
        f"Reconstructed content does not match original.\n"
        f"Original:      {original_content!r}\n"
        f"Reconstructed: {reconstructed!r}"
    )
