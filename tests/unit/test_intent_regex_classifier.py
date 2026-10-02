"""Behavioral checks for the separately versioned action-first candidate."""

from __future__ import annotations

import pytest

from src.gateway.intent_regex_classifier import IntentRegexClassifier
from src.gateway.task_classifier import TaskClassifier


@pytest.mark.parametrize("quote", ['"{}"', "'{}'", "“{}”", "‘{}’", "`{}`", "```{}```"])
def test_quoted_request_does_not_override_actual_task(quote):
    prompt = (
        f"The source says {quote.format('Write code to calculate an integral')}. "
        "Give me a concise summary of the source."
    )
    assert IntentRegexClassifier().classify(prompt).task_type == "summarization"


@pytest.mark.parametrize(("prompt", "expected"), [
    ("Draft a radio script encouraging neighbors to join a book club.", "creative_writing"),
    ("Write a Python script to archive yesterday's log files.", "coding"),
    ("How does this SQL query select distinct customer names?", "coding"),
    ("Don't write code. Compare the risks and benefits of automated refunds.", "reasoning"),
    ("Don’t summarize anything. Explain the usual rules of volleyball.", "general"),
    ("Could you write a story? Actually, just provide a summary of this excerpt.",
     "summarization"),
    ("Draft a speech that actually reads like an inspiring story.", "creative_writing"),
    ("A box holds 15 folders and each has 20 pages. How many pages altogether?", "math"),
    ("What is the probability that two dice both show six?", "math"),
    ("Who was the first person to walk on the Moon?", "general"),
    ("Explain why the competing forecasts have different implications.", "reasoning"),
])
def test_requested_operation(prompt, expected):
    result = IntentRegexClassifier().classify(prompt)
    assert result.task_type == expected
    assert 0 <= result.confidence <= 1
    assert result.matched_keywords


def test_repeated_topic_words_do_not_outvote_requested_artifact():
    prompt = "Write a poem about " + "code math summary probability " * 50
    assert IntentRegexClassifier().classify(prompt).task_type == "creative_writing"


def test_unknown_and_symbolic_requests_retain_baseline_fallback():
    classifier = IntentRegexClassifier()
    for prompt in ("", "Hello there", "sqrt(81)", "8 * 9"):
        actual = classifier.classify(prompt)
        assert actual.task_type == TaskClassifier().classify(prompt).task_type
        assert "baseline_fallback" in actual.matched_keywords
