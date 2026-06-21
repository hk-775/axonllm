"""Unit tests for TaskClassifier."""

import pytest

from src.gateway.task_classifier import TaskClassifier
from src.gateway.models import ClassificationResult


@pytest.fixture
def classifier():
    return TaskClassifier()


class TestClassifyCoding:
    def test_coding_prompt(self, classifier):
        result = classifier.classify("Implement a Python function to sort a list and debug the code")
        assert result.task_type == "coding"

    def test_code_blocks_boost(self, classifier):
        result = classifier.classify("Fix this bug:\n```python\ndef foo():\n    pass\n```")
        assert result.task_type == "coding"
        assert result.confidence > 0.5

    def test_debug_keyword(self, classifier):
        result = classifier.classify("Help me debug this code that has a syntax error")
        assert result.task_type == "coding"


class TestClassifyMath:
    def test_math_prompt(self, classifier):
        result = classifier.classify("Calculate the integral of x^2")
        assert result.task_type == "math"

    def test_math_with_operators(self, classifier):
        result = classifier.classify("Solve this equation: 2x + 3 = 7")
        assert result.task_type == "math"


class TestClassifyCreativeWriting:
    def test_creative_prompt(self, classifier):
        result = classifier.classify("Write a short story about a dragon")
        assert result.task_type == "creative_writing"

    def test_write_a_heuristic(self, classifier):
        result = classifier.classify("Write a poem about the ocean")
        assert result.task_type == "creative_writing"


class TestClassifyReasoning:
    def test_reasoning_prompt(self, classifier):
        result = classifier.classify("Explain why the sky is blue")
        assert result.task_type == "reasoning"

    def test_logic_keyword(self, classifier):
        result = classifier.classify("Analyze the logic behind this argument and explain the reason")
        assert result.task_type == "reasoning"


class TestClassifySummarization:
    def test_summarization_prompt(self, classifier):
        result = classifier.classify("Summarize this article about climate change")
        assert result.task_type == "summarization"

    def test_tldr_keyword(self, classifier):
        result = classifier.classify("Give me a tldr of this long document")
        assert result.task_type == "summarization"


class TestClassifyGeneral:
    def test_general_prompt(self, classifier):
        result = classifier.classify("Hello, how are you?")
        assert result.task_type == "general"

    def test_empty_prompt(self, classifier):
        result = classifier.classify("")
        assert result.task_type == "general"
        assert result.confidence == 0.0

    def test_no_keywords_match(self, classifier):
        result = classifier.classify("What time is it in Tokyo?")
        assert result.task_type == "general"


class TestConfidence:
    def test_confidence_in_range(self, classifier):
        result = classifier.classify("Write a Python function to sort a list")
        assert 0.0 <= result.confidence <= 1.0

    def test_general_has_zero_confidence(self, classifier):
        result = classifier.classify("")
        assert result.confidence == 0.0

    def test_strong_match_high_confidence(self, classifier):
        result = classifier.classify(
            "Implement a function, debug the class method, refactor the api code"
        )
        assert result.task_type == "coding"
        assert result.confidence > 0.5


class TestCustomKeywords:
    def test_custom_keywords_extend(self):
        custom = {"coding": ["typescript", "react"]}
        classifier = TaskClassifier(custom_keywords=custom)
        result = classifier.classify("Build a react component in typescript")
        assert result.task_type == "coding"


class TestMatchedKeywords:
    def test_matched_keywords_returned(self, classifier):
        result = classifier.classify("Implement a function and debug the code")
        assert result.task_type == "coding"
        assert "implement" in result.matched_keywords
        assert "function" in result.matched_keywords
        assert "debug" in result.matched_keywords
        assert "code" in result.matched_keywords
