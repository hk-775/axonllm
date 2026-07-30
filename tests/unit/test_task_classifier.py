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

    def test_write_a_function_is_coding_not_creative(self, classifier):
        # Regression: "write a ..." + a coding signal must classify as coding,
        # not creative_writing (the "write a" heuristic used to blindly boost
        # creative_writing even for code prompts).
        for prompt in (
            "write a python function to reverse a linked list",
            "write a bash script to tail logs",
            "create a REST API endpoint in typescript",
        ):
            assert classifier.classify(prompt).task_type == "coding", prompt

    def test_write_a_poem_still_creative(self, classifier):
        # The coding routing must NOT hijack genuinely creative "write a" prompts.
        for prompt in ("write a poem about the sea", "write a short story about a robot"):
            assert classifier.classify(prompt).task_type == "creative_writing", prompt


class TestClassifyMath:
    def test_math_prompt(self, classifier):
        result = classifier.classify("Calculate the integral of x^2")
        assert result.task_type == "math"

    def test_math_with_operators(self, classifier):
        result = classifier.classify("Solve this equation: 2x + 3 = 7")
        assert result.task_type == "math"

    def test_math_division_with_spaces(self, classifier):
        result = classifier.classify("compute 10 / 2 please")
        assert result.task_type == "math"

    def test_prose_with_numbers_not_math(self, classifier):
        # Numbers + prose punctuation (%, hyphens, dates) must not trigger math.
        result = classifier.classify(
            "Summarize this report: Q3 revenue grew to $4.2B, up 12% "
            "year-over-year, with 3-5% margins."
        )
        assert result.task_type == "summarization"

    def test_summarization_with_date_range_not_math(self, classifier):
        result = classifier.classify(
            "Please summarize the following article about the 2023-2024 season."
        )
        assert result.task_type == "summarization"


class TestClassifyMathNotation:
    """Math written in notation rather than as an infix binary expression.

    The arithmetic heuristic requires number-operator-number, so postfix,
    function-call and percentage forms scored zero across the board and the
    prompt fell through to "general" with confidence 0.0 — below the routing
    confidence threshold, so smart routing used its default model instead of the
    math leader. Each of these is asserted with the confidence bound as well as
    the type, because the type alone is not enough to route on.
    """

    # The originally reported case: 4! reached no math signal at all.
    @pytest.mark.parametrize(
        "prompt",
        ["what is 4!", "4!", "what is 10!", "compute 8!", "0! equals what"],
    )
    def test_postfix_factorial_is_math(self, classifier, prompt):
        result = classifier.classify(prompt)
        assert result.task_type == "math", prompt
        assert result.confidence >= 0.3, prompt

    @pytest.mark.parametrize(
        "prompt",
        ["sqrt(16)", "log(100)", "sin(pi/2)", "what is log10(1000)", "gcd(12, 18)"],
    )
    def test_math_function_notation_is_math(self, classifier, prompt):
        result = classifier.classify(prompt)
        assert result.task_type == "math", prompt
        assert result.confidence >= 0.3, prompt

    @pytest.mark.parametrize(
        "prompt",
        [
            "what is the square root of 144",
            "what is the cube root of 27",
            "compute the logarithm of 1000",
            "multiply 12 by 13",
        ],
    )
    def test_spelled_out_operations_are_math(self, classifier, prompt):
        result = classifier.classify(prompt)
        assert result.task_type == "math", prompt
        assert result.confidence >= 0.3, prompt

    @pytest.mark.parametrize(
        "prompt", ["what is 15% of 200", "what is 15 percent of 200"]
    )
    def test_percentage_of_a_quantity_is_math(self, classifier, prompt):
        result = classifier.classify(prompt)
        assert result.task_type == "math", prompt
        assert result.confidence >= 0.3, prompt

    def test_notation_and_words_agree(self, classifier):
        # "5!" and "5 factorial" are the same question; before the fix only the
        # spelled-out form routed to math, which is the tell that the gap was in
        # notation handling rather than in the keyword list.
        assert classifier.classify("5 factorial").task_type == "math"
        assert classifier.classify("what is 5!").task_type == "math"


class TestMathNotationFalsePositives:
    """Prose that merely looks like notation must not become math.

    These are the cases that rule out simply loosening the arithmetic regex.
    """

    def test_exclamation_after_a_word_is_not_factorial(self, classifier):
        # The operand must be adjacent to "!" — prose emphasis follows a letter.
        assert classifier.classify("I have 3 cats!").task_type != "math"
        assert classifier.classify("Amazing, we hit 100% coverage!").task_type != "math"

    def test_not_equal_operator_is_not_factorial(self, classifier):
        # "!=" is not-equal in most languages, and "!!" is emphasis.
        assert classifier.classify("if a != b: pass").task_type != "math"
        assert classifier.classify("check that 1 != 2 in the test").task_type != "math"
        assert classifier.classify("we shipped 3!! releases").task_type != "math"

    def test_logging_call_is_not_a_logarithm(self, classifier):
        # log(...) is ambiguous; the numeric-argument rule is what separates the
        # logarithm from the far more common logging call.
        result = classifier.classify("add a log(msg) call to the function")
        assert result.task_type == "coding"

    def test_percentage_in_prose_is_not_math(self, classifier):
        # No "of <number>", so this is a statistic being quoted, not a
        # calculation being requested.
        result = classifier.classify(
            "Summarize this report: Q3 revenue grew to $4.2B, up 12% "
            "year-over-year, with 3-5% margins."
        )
        assert result.task_type == "summarization"

    def test_math_keyword_does_not_hijack_other_types(self, classifier):
        # A single notation boost must not outweigh a genuine keyword score for
        # another type. These are the realistic near-misses.
        for prompt, expected in (
            ("write a python function to compute n! recursively", "coding"),
            ("implement sqrt(x) using newton's method in rust", "coding"),
            ("write a poem about the number 4!", "creative_writing"),
            ("summarize the paper on log(n) complexity", "summarization"),
            ("explain why 0! = 1", "reasoning"),
        ):
            assert classifier.classify(prompt).task_type == expected, prompt


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
