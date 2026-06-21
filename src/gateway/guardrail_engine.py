"""Guardrail engine for evaluating requests and responses against configurable rules."""

import re

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailResult,
    GuardrailRule,
)


class GuardrailEngine:
    """Evaluates requests and responses against project guardrail rules."""

    def evaluate_request(
        self, request: ChatCompletionRequest, rules: list[GuardrailRule]
    ) -> GuardrailResult:
        """Evaluate request against guardrail rules.

        Extracts text from all messages in the request and checks against
        rules where applies_to is "request" or "both".

        Returns GuardrailResult with pass/fail, violated rule names, and message.
        """
        applicable_rules = [
            r for r in rules if r.applies_to in ("request", "both")
        ]
        text = self._extract_request_text(request)
        return self._evaluate(text, applicable_rules, context="Request")

    def evaluate_response(
        self, response: ChatCompletionResponse, rules: list[GuardrailRule]
    ) -> GuardrailResult:
        """Evaluate response against guardrail rules.

        Extracts text from choices[*].message.content and checks against
        rules where applies_to is "response" or "both".

        Returns GuardrailResult with pass/fail, violated rule names, and message.
        """
        applicable_rules = [
            r for r in rules if r.applies_to in ("response", "both")
        ]
        text = self._extract_response_text(response)
        return self._evaluate(text, applicable_rules, context="Response")

    def _extract_request_text(self, request: ChatCompletionRequest) -> str:
        """Extract all text content from request messages."""
        parts: list[str] = []
        for msg in request.messages:
            content = msg.get("content", "")
            if content:
                parts.append(str(content))
        return " ".join(parts)

    def _extract_response_text(self, response: ChatCompletionResponse) -> str:
        """Extract text content from response choices."""
        parts: list[str] = []
        for choice in response.choices:
            message = choice.get("message", {})
            content = message.get("content", "")
            if content:
                parts.append(str(content))
        return " ".join(parts)

    def _evaluate(
        self, text: str, rules: list[GuardrailRule], context: str
    ) -> GuardrailResult:
        """Evaluate text against a list of rules.

        Only rules with action="block" cause passed=False.
        "warn" and "redact" rules add to violated_rules but don't fail.
        """
        violated_rules: list[str] = []
        has_blocking_violation = False

        for rule in rules:
            if self._matches(text, rule):
                violated_rules.append(rule.name)
                if rule.action == "block":
                    has_blocking_violation = True

        if not violated_rules:
            return GuardrailResult(passed=True, violated_rules=[], message=None)

        passed = not has_blocking_violation
        message: str | None = None
        if not passed:
            blocking_names = [
                r.name for r in rules if r.name in violated_rules and r.action == "block"
            ]
            message = f"{context} blocked by guardrail rules: {', '.join(blocking_names)}"

        return GuardrailResult(
            passed=passed, violated_rules=violated_rules, message=message
        )

    def _matches(self, text: str, rule: GuardrailRule) -> bool:
        """Check if text matches a guardrail rule's pattern."""
        if not rule.pattern:
            return False

        if rule.rule_type == "keyword_block":
            return rule.pattern.lower() in text.lower()
        elif rule.rule_type == "regex_match":
            return re.search(rule.pattern, text) is not None
        elif rule.rule_type == "content_category":
            # Treat as keyword_block for now (simple implementation)
            return rule.pattern.lower() in text.lower()
        return False
