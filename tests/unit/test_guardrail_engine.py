"""Unit tests for GuardrailEngine."""

import asyncio
import threading

import pytest

import src.gateway.guardrail_engine as guardrails
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
    async def test_no_rules_passes(self, engine, simple_request):
        result = await engine.evaluate_request(simple_request, [])
        assert result.passed is True
        assert result.violated_rules == []
        assert result.message is None


class TestEvaluateRequestKeywordBlock:
    async def test_keyword_block_matches(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-forbidden" in result.violated_rules
        assert "no-forbidden" in result.message

    async def test_keyword_block_case_insensitive(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False

    async def test_keyword_block_no_match_passes(self, engine, simple_request):
        rules = [
            GuardrailRule(
                name="no-forbidden",
                rule_type="keyword_block",
                pattern="forbidden",
                action="block",
                applies_to="request",
            )
        ]
        result = await engine.evaluate_request(simple_request, rules)
        assert result.passed is True
        assert result.violated_rules == []


class TestEvaluateRequestRegexMatch:
    async def test_regex_match_matches(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-ssn" in result.violated_rules

    async def test_regex_no_match_passes(self, engine, simple_request):
        rules = [
            GuardrailRule(
                name="no-ssn",
                rule_type="regex_match",
                pattern=r"\d{3}-\d{2}-\d{4}",
                action="block",
                applies_to="request",
            )
        ]
        result = await engine.evaluate_request(simple_request, rules)
        assert result.passed is True


class TestEvaluateResponse:
    async def test_response_keyword_block(self, engine):
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
        result = await engine.evaluate_response(response, rules)
        assert result.passed is False
        assert "no-secrets" in result.violated_rules
        assert "Response blocked" in result.message

    async def test_response_passes_clean_content(self, engine, simple_response):
        rules = [
            GuardrailRule(
                name="no-secrets",
                rule_type="keyword_block",
                pattern="secret",
                action="block",
                applies_to="response",
            )
        ]
        result = await engine.evaluate_response(simple_response, rules)
        assert result.passed is True


class TestAppliesToFiltering:
    async def test_request_only_rule_not_applied_to_response(self, engine):
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
        result = await engine.evaluate_response(response, rules)
        assert result.passed is True
        assert result.violated_rules == []

    async def test_response_only_rule_not_applied_to_request(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is True
        assert result.violated_rules == []

    async def test_both_rule_applies_to_request(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False

    async def test_both_rule_applies_to_response(self, engine):
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
        result = await engine.evaluate_response(response, rules)
        assert result.passed is False


class TestWarnAction:
    async def test_warn_does_not_block(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is True
        assert "warn-rule" in result.violated_rules
        assert result.message is None

    async def test_redact_does_not_block(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is True
        assert "redact-rule" in result.violated_rules


class TestMultipleViolations:
    async def test_multiple_blocking_violations(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-forbidden" in result.violated_rules
        assert "no-secret" in result.violated_rules
        assert "no-forbidden" in result.message
        assert "no-secret" in result.message

    async def test_mixed_block_and_warn(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "block-rule" in result.violated_rules
        assert "warn-rule" in result.violated_rules
        # Message only mentions blocking rules
        assert "block-rule" in result.message

    async def test_content_category_treated_as_keyword(self, engine):
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
        result = await engine.evaluate_request(request, rules)
        assert result.passed is False
        assert "no-violence" in result.violated_rules


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rule_type", "future_rule"),
        ("action", "allow"),
        ("applies_to", "upstream"),
    ],
)
async def test_unknown_persisted_rule_values_fail_closed(
    engine,
    simple_request,
    field,
    value,
):
    values = {
        "name": "invalid-rule",
        "rule_type": "keyword_block",
        "pattern": "never-matches",
        "action": "warn",
        "applies_to": "request",
    }
    values[field] = value

    result = await engine.evaluate_request(
        simple_request,
        [GuardrailRule(**values)],
    )

    assert result.passed is False
    assert result.violated_rules == ["invalid-rule"]


async def test_malformed_persisted_regex_fails_closed(
    engine,
    simple_request,
):
    result = await engine.evaluate_request(
        simple_request,
        [
            GuardrailRule(
                name="malformed-regex",
                rule_type="regex_match",
                pattern="([",
                action="warn",
                applies_to="request",
            )
        ],
    )

    assert result.passed is False
    assert result.violated_rules == ["malformed-regex"]


async def test_regex_timeout_does_not_block_request_loop(
    monkeypatch,
    simple_request,
):
    release_search = threading.Event()

    class _SlowRegex:
        def search(self, *_args, **_kwargs):
            release_search.wait(timeout=1)
            raise TimeoutError

    monkeypatch.setattr(
        guardrails,
        "compile_guardrail_regex",
        lambda _pattern: _SlowRegex(),
    )
    engine = GuardrailEngine(regex_timeout_seconds=0.001)
    heartbeat = asyncio.create_task(asyncio.sleep(0.005))
    started = asyncio.get_running_loop().time()
    try:
        result = await engine.evaluate_request(
            simple_request,
            [
                GuardrailRule(
                    name="slow-regex",
                    rule_type="regex_match",
                    pattern="slow",
                    action="warn",
                    applies_to="request",
                )
            ],
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert heartbeat.done()
        assert elapsed < 0.25
    finally:
        release_search.set()
    await heartbeat

    assert result.passed is False
    assert result.violated_rules == ["slow-regex"]


async def test_regex_compilation_runs_off_request_loop(
    monkeypatch,
    simple_request,
):
    request_thread = threading.get_ident()
    compile_threads: list[int] = []

    class _CompiledRegex:
        def search(self, *_args, **_kwargs):
            return None

    def compile_regex(_pattern):
        compile_threads.append(threading.get_ident())
        return _CompiledRegex()

    monkeypatch.setattr(
        guardrails,
        "compile_guardrail_regex",
        compile_regex,
    )

    result = await GuardrailEngine().evaluate_request(
        simple_request,
        [
            GuardrailRule(
                name="off-loop-regex",
                rule_type="regex_match",
                pattern="safe",
                action="block",
                applies_to="request",
            )
        ],
    )

    assert result.passed is True
    assert compile_threads
    assert request_thread not in compile_threads


async def test_oversized_regex_pattern_fails_closed(
    monkeypatch,
    engine,
    simple_request,
):
    monkeypatch.setattr(guardrails, "_MAX_REGEX_PATTERN_BYTES", 4)

    result = await engine.evaluate_request(
        simple_request,
        [
            GuardrailRule(
                name="oversized-regex",
                rule_type="regex_match",
                pattern="12345",
                action="warn",
                applies_to="request",
            )
        ],
    )

    assert result.passed is False
    assert result.violated_rules == ["oversized-regex"]
