"""Task classifier for smart routing — classifies prompts by task type."""

from __future__ import annotations

import re

from src.gateway.models import ClassificationResult

# Matches a genuine arithmetic expression: two numbers joined by an operator.
# Strong operators (+, *, =, ^) may be written without spaces ("2+2", "x=5");
# ambiguous prose punctuation (-, /) must be space-delimited ("10 / 2") so that
# hyphenated words ("year-over-year"), ranges ("3-5%") and dates ("2023-2024")
# do NOT get misread as math.
_ARITHMETIC_RE = re.compile(r"\d\s*[+*=^]\s*\d|\d\s+[-/]\s+\d")


class TaskClassifier:
    """Classifies prompts into task types using keyword/heuristic analysis."""

    TASK_KEYWORDS: dict[str, list[str]] = {
        "coding": [
            "code", "function", "bug", "implement", "class", "method", "api",
            "debug", "refactor", "syntax", "compile", "programming", "algorithm",
            "```",
        ],
        "reasoning": [
            "why", "explain", "reason", "logic", "analyze", "think", "deduce",
            "argument", "because", "therefore", "proof",
        ],
        "creative_writing": [
            "write", "story", "poem", "creative", "fiction", "narrative",
            "character", "dialogue", "essay", "blog",
        ],
        "summarization": [
            "summarize", "summary", "tldr", "brief", "condense", "key points",
            "overview", "recap",
        ],
        "math": [
            "calculate", "equation", "solve", "math", "formula", "integral",
            "derivative", "probability", "statistics", "algebra", "factorial",
        ],
    }

    VALID_TASK_TYPES = {"coding", "reasoning", "creative_writing", "summarization", "math", "general"}

    def __init__(self, custom_keywords: dict[str, list[str]] | None = None) -> None:
        """Initialize with default keywords, optionally extended."""
        self._keywords: dict[str, list[str]] = {
            k: list(v) for k, v in self.TASK_KEYWORDS.items()
        }
        if custom_keywords:
            for task_type, keywords in custom_keywords.items():
                existing = self._keywords.get(task_type, [])
                self._keywords[task_type] = existing + keywords

    def classify(self, prompt: str) -> ClassificationResult:
        """Classify a prompt into a task type with confidence score.

        Algorithm:
        1. Normalize prompt to lowercase
        2. For each task type, count keyword matches
        3. Apply structural heuristics (code blocks, "Write a", math operators)
        4. Return highest-scoring type, or "general" if no matches
        5. Confidence = best_score / (best_score + second_best_score + epsilon)
        """
        normalized = prompt.lower()

        # Score each task type by keyword matches
        scores: dict[str, float] = {}
        matched: dict[str, list[str]] = {}

        for task_type, keywords in self._keywords.items():
            matches = [kw for kw in keywords if kw in normalized]
            scores[task_type] = len(matches)
            matched[task_type] = matches

        # Apply structural heuristics
        self._apply_heuristics(prompt, normalized, scores, matched)

        # Find best and second-best scores
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        best_type = sorted_scores[0][0] if sorted_scores else "general"
        best_score = sorted_scores[0][1] if sorted_scores else 0.0
        second_best_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

        # If no keywords matched at all, return "general"
        if best_score == 0.0:
            return ClassificationResult(
                task_type="general",
                confidence=0.0,
                matched_keywords=[],
            )

        # Compute confidence
        epsilon = 1e-6
        confidence = best_score / (best_score + second_best_score + epsilon)
        # Clamp to [0.0, 1.0]
        confidence = max(0.0, min(1.0, confidence))

        return ClassificationResult(
            task_type=best_type,
            confidence=confidence,
            matched_keywords=matched.get(best_type, []),
        )

    def _apply_heuristics(
        self,
        original: str,
        normalized: str,
        scores: dict[str, float],
        matched: dict[str, list[str]],
    ) -> None:
        """Apply structural heuristics to boost scores."""
        # Triple backticks → boost coding
        if "```" in original:
            scores.setdefault("coding", 0.0)
            scores["coding"] += 2.0
            if "```" not in matched.get("coding", []):
                matched.setdefault("coding", []).append("```")

        # Starts with "Write a" or "Create a" → boost creative_writing
        stripped = original.strip()
        if stripped.lower().startswith("write a") or stripped.lower().startswith("create a"):
            scores.setdefault("creative_writing", 0.0)
            scores["creative_writing"] += 2.0
            matched.setdefault("creative_writing", []).append("write_a_heuristic")

        # Contains a genuine arithmetic expression (number-operator-number) →
        # boost math. Guards against prose that merely contains digits and
        # punctuation (percentages, dates, ranges, hyphenated words).
        if _ARITHMETIC_RE.search(original):
            scores.setdefault("math", 0.0)
            scores["math"] += 1.5
            matched.setdefault("math", []).append("math_operators_heuristic")
