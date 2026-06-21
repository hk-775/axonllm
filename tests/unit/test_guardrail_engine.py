"""Unit tests for GuardrailEngine."""

import pytest

from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailResult,
    GuardrailRule,
    TokenUsage,
)


@pytest.fixture
def engine():
    return GuardrailEngine()


@pytest.fixture
def simple_request():
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hello world"}],
        model="gpt-4",
    )


@pytest.fixture
def simple_response():
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": "Hi there"}}],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="gpt-4",
        provider="openai",
    )


class TestEvaluateRequestNoRules:
    def test_no_rules_passes(self, engine, simple_request):
        result = engine.evaluate_request(simple_request, [])
        assert result.passed is True
        assert result.violated_rules == []
        assert result.message is None


class TestEvaluateRequestKeywordBlock:
    def test_keyword_block_matches(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Tell me about forbidden topic"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="no-forbidden",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-forbidden" in result.violated_rules
        assert "no-forbidden" in result.message

    def test_keyword_block_case_insensitive(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "FORBIDDEN content here"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="no-forbidden",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False

    def test_keyword_block_no_match_passes(self, engine, simple_request):
        rules = [
            GuardrailRule(
                name="no-forbidden",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(simple_request, rules)
        assert result.passed is True
        assert result.violated_rules == []


class TestEvaluateRequestRegexMatch:
    def test_regex_match_matches(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "My SSN is 123-45-6789"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="no-ssn",
                rule_type="regex_match",
                pattern=r"\d{3}-\d{2}-\d{4}",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-ssn" in result.violated_rules

    def test_regex_no_match_passes(self, engine, simple_request):
        rules = [
            GuardrailRule(
                name="no-ssn",
                rule_type="regex_match",
                pattern=r"\d{3}-\d{2}-\d{4}",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(simple_request, rules)
        assert result.passed is True


class TestEvaluateResponse:
    def test_response_keyword_block(self, engine):
        response = ChatCompletionResponse(
            id="resp-1",
            choices=[{"message": {"role": "assistant", "content": "Here is some secret info"}}],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            model="gpt-4",
            provider="openai",
        )
        rules = [
            GuardrailRule(
                name="no-secrets",
                rule_type="keyword_block",
                pattern="secret",
                action="block",
                applies_to="response",
            )
        ]
        result = engine.evaluate_response(response, rules)
        assert result.passed is False
        assert "no-secrets" in result.violated_rules
        assert "Response blocked" in result.message

    def test_response_passes_clean_content(self, engine, simple_response):
        rules = [
            GuardrailRule(
                name="no-secrets",
                rule_type="keyword_block",
                pattern="secret",
                action="block",
                applies_to="response",
            )
        ]
        result = engine.evaluate_response(simple_response, rules)
        assert result.passed is True


class TestAppliesToFiltering:
    def test_request_only_rule_not_applied_to_response(self, engine):
        response = ChatCompletionResponse(
            id="resp-1",
            choices=[{"message": {"role": "assistant", "content": "forbidden content"}}],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
            model="gpt-4",
            provider="openai",
        )
        rules = [
            GuardrailRule(
                name="request-only",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_response(response, rules)
        assert result.passed is True
        assert result.violated_rules == []

    def test_response_only_rule_not_applied_to_request(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "forbidden content"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="response-only",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="response",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is True
        assert result.violated_rules == []

    def test_both_rule_applies_to_request(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "forbidden content"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="both-rule",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="both",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False

    def test_both_rule_applies_to_response(self, engine):
        response = ChatCompletionResponse(
            id="resp-1",
            choices=[{"message": {"role": "assistant", "content": "forbidden content"}}],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
            model="gpt-4",
            provider="openai",
        )
        rules = [
            GuardrailRule(
                name="both-rule",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="both",
            )
        ]
        result = engine.evaluate_response(response, rules)
        assert result.passed is False


class TestWarnAction:
    def test_warn_does_not_block(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "forbidden content"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="warn-rule",
                rule_type="keyword_block",
                pattern="forbidden",
                action="warn",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is True
        assert "warn-rule" in result.violated_rules
        assert result.message is None

    def test_redact_does_not_block(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "forbidden content"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="redact-rule",
                rule_type="keyword_block",
                pattern="forbidden",
                action="redact",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is True
        assert "redact-rule" in result.violated_rules


class TestMultipleViolations:
    def test_multiple_blocking_violations(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "forbidden and secret stuff"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="no-forbidden",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            ),
            GuardrailRule(
                name="no-secret",
                rule_type="keyword_block",
                pattern="secret",
                action="block",
                applies_to="request",
            ),
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-forbidden" in result.violated_rules
        assert "no-secret" in result.violated_rules
        assert "no-forbidden" in result.message
        assert "no-secret" in result.message

    def test_mixed_block_and_warn(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "forbidden and secret stuff"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="block-rule",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            ),
            GuardrailRule(
                name="warn-rule",
                rule_type="keyword_block",
                pattern="secret",
                action="warn",
                applies_to="request",
            ),
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "block-rule" in result.violated_rules
        assert "warn-rule" in result.violated_rules
        # Message only mentions blocking rules
        assert "block-rule" in result.message

    def test_content_category_treated_as_keyword(self, engine):
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "violence in movies"}],
            model="gpt-4",
        )
        rules = [
            GuardrailRule(
                name="no-violence",
                rule_type="content_category",
                pattern="violence",
                action="block",
                applies_to="request",
            )
        ]
        result = engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-violence" in result.violated_rules
