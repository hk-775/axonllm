"""Streaming utilities for the LLM-Router service."""

from src.gateway.models import ChatCompletionResponse, StreamChunk


def simulate_streaming(response: ChatCompletionResponse) -> list[StreamChunk]:
    """Simulate streaming by breaking a complete response into token-sized chunks.

    Takes a ChatCompletionResponse from a non-streaming provider and breaks the
    response content into word-level chunks, returning a list of StreamChunk
    objects. The last chunk has is_final=True.

    Args:
        response: A complete ChatCompletionResponse.

    Returns:
        A list of StreamChunk objects whose concatenated delta contents equal
        the original response content.
    """
    # Extract the text content from the first choice
    content = ""
    if response.choices:
        choice = response.choices[0]
        message = choice.get("message", {})
        content = message.get("content", "")

    # If content is empty, return a single final chunk with empty content
    if not content:
        return [
            StreamChunk(
                id=response.id,
                choices=[{"index": 0, "delta": {"content": ""}}],
                model=response.model,
                is_final=True,
            )
        ]

    # Split content into word-level tokens, preserving whitespace
    tokens = _split_into_tokens(content)

    chunks: list[StreamChunk] = []
    for i, token in enumerate(tokens):
        is_last = i == len(tokens) - 1
        chunks.append(
            StreamChunk(
                id=response.id,
                choices=[{"index": 0, "delta": {"content": token}}],
                model=response.model,
                is_final=is_last,
            )
        )

    return chunks


def _split_into_tokens(text: str) -> list[str]:
    """Split text into word-level tokens preserving all characters.

    Each word keeps its trailing whitespace so that concatenation of all
    tokens exactly reconstructs the original text.

    For example:
        "Hello world" -> ["Hello ", "world"]
        "  a  b " -> ["  ", "a  ", "b "]
        "x" -> ["x"]
    """
    if not text:
        return []

    tokens: list[str] = []
    current = ""
    in_word = False

    for ch in text:
        if ch.isspace():
            current += ch
            if in_word:
                # We were in a word; the space is trailing whitespace.
                # We'll keep accumulating trailing spaces.
                pass
            # else: leading whitespace, keep accumulating
        else:
            if in_word:
                # Continuing a word
                current += ch
            else:
                # Starting a new word
                if current:
                    # Flush accumulated leading whitespace as part of next token
                    # Actually, attach leading whitespace to the upcoming word
                    current += ch
                else:
                    current = ch
                in_word = True
            continue
        # After processing a space, check if we need to flush
        # We flush when we transition from trailing-space back to a new word,
        # which we handle above. So just continue.

    if current:
        tokens.append(current)

    # Fix: the above logic groups leading whitespace with the following word.
    # But if text is only whitespace, we get one token of all spaces.
    # Let's use a simpler, correct approach.

    # Simpler approach: split on word boundaries, keeping separators
    tokens = _split_preserving_all(text)
    return tokens


def _split_preserving_all(text: str) -> list[str]:
    """Split text into tokens where each token is a word plus any whitespace
    that follows it. Leading whitespace before the first word becomes its own token.

    Concatenation of all tokens exactly equals the original text.
    """
    if not text:
        return []

    tokens: list[str] = []
    i = 0
    n = len(text)

    # Consume leading whitespace as its own token if present
    if text[0].isspace():
        j = 0
        while j < n and text[j].isspace():
            j += 1
        if j == n:
            # Entire string is whitespace
            return [text]
        tokens.append(text[:j])
        i = j

    # Now consume word + trailing whitespace pairs
    while i < n:
        # Consume word characters (non-space)
        j = i
        while j < n and not text[j].isspace():
            j += 1
        # Consume trailing whitespace
        while j < n and text[j].isspace():
            j += 1
        tokens.append(text[i:j])
        i = j

    return tokens
