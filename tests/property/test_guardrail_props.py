# Feature: litellm-service, Properties 26-27: GuardrailEngine property tests
"""Property-based tests for the GuardrailEngine component.

Properties covered:
  26 – Request guardrail violations return 400
  27 – Response guardrail violations replace the response
"""

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailRule,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Keywords that are safe to embed and search for (no regex special chars)
keyword_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L",), whitelist_characters=""),
    min_size=3,
    max_size=12,
)

rule_name_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=20,
).filter(lambda s: len(s.strip()) > 0)

model_strategy = st.sampled_from(["gpt-4", "claude-3", "gemini-pro", "command-r"])

# Filler text that will NOT accidentally contain the keyword
filler_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L",), whitelist_characters=" ,.!?"),
    min_size=0,
    max_size=60,
)


# ===========================================================================
# Property 26: Request guardrail violations return 400
# Feature: litellm-service, Property 26: Request guardrail violations
# ===========================================================================


@given(
    keyword=keyword_strategy,
    rule_name=rule_name_strategy,
    model=model_strategy,
    prefix=filler_strategy,
    suffix=filler_strategy,
)
@settings(max_examples=100)
async def test_request_guardrail_violations_return_block(keyword, rule_name, model, prefix, suffix):
    """Property 26: Request guardrail violations return 400.

    For any request matching a project's request guardrail rule (with
    action="block"), evaluate_request SHALL return passed=False with the
    violated rule name.

    **Validates: Requirements 10.2, 10.3**
    """
    # Ensure keyword is non-empty after stripping
    assume(len(keyword.strip()) > 0)
    # Ensure the keyword doesn't accidentally appear in filler
    assume(keyword.lower() not in prefix.lower())
    assume(keyword.lower() not in suffix.lower())

    engine = GuardrailEngine()

    # Build a request whose message content contains the keyword
    content = f"{prefix} {keyword} {suffix}"
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": content}],
        model=model,
    )

    rule = GuardrailRule(
        name=rule_name,
        rule_type="keyword_block",
        pattern=keyword,
        action="block",
        applies_to="request",
    )

    result = await engine.evaluate_request(request, [rule])

    # Must fail
    assert result.passed is False, (
        f"Request containing keyword '{keyword}' should be blocked"
    )
    # Violated rules must include the rule name
    assert rule_name in result.violated_rules, (
        f"Rule '{rule_name}' should appear in violated_rules, got {result.violated_rules}"
    )
    # Message must be present for blocking violations
    assert result.message is not None, "Blocking violation must produce a message"
    assert rule_name in result.message, (
        f"Message should mention rule '{rule_name}', got: {result.message}"
    )


# ===========================================================================
# Property 27: Response guardrail violations replace the response
# Feature: litellm-service, Property 27: Response guardrail violations
# ===========================================================================


@given(
    keyword=keyword_strategy,
    rule_name=rule_name_strategy,
    model=model_strategy,
    prefix=filler_strategy,
    suffix=filler_strategy,
    prompt_tokens=st.integers(min_value=1, max_value=500),
    completion_tokens=st.integers(min_value=1, max_value=500),
)
@settings(max_examples=100)
async def test_response_guardrail_violations_replace_response(
    keyword, rule_name, model, prefix, suffix, prompt_tokens, completion_tokens
):
    """Property 27: Response guardrail violations replace the response.

    For any provider response matching a project's response guardrail rule
    (with action="block"), evaluate_response SHALL return passed=False with
    the violated rule name and a message containing "Response blocked".

    **Validates: Requirements 10.4, 10.5**
    """
    assume(len(keyword.strip()) > 0)
    assume(keyword.lower() not in prefix.lower())
    assume(keyword.lower() not in suffix.lower())

    engine = GuardrailEngine()

    # Build a response whose choice content contains the keyword
    content = f"{prefix} {keyword} {suffix}"
    response = ChatCompletionResponse(
        id="resp-test",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=model,
        provider="openai",
    )

    rule = GuardrailRule(
        name=rule_name,
        rule_type="keyword_block",
        pattern=keyword,
        action="block",
        applies_to="response",
    )

    result = await engine.evaluate_response(response, [rule])

    # Must fail
    assert result.passed is False, (
        f"Response containing keyword '{keyword}' should be blocked"
    )
    # Violated rules must include the rule name
    assert rule_name in result.violated_rules, (
        f"Rule '{rule_name}' should appear in violated_rules, got {result.violated_rules}"
    )
    # Message must mention "Response blocked"
    assert result.message is not None, "Blocking violation must produce a message"
    assert "Response blocked" in result.message, (
        f"Message should contain 'Response blocked', got: {result.message}"
    )
