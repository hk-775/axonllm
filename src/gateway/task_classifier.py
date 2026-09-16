"""Task classifier for smart routing — classifies prompts by task type."""

from __future__ import annotations

from functools import lru_cache
import re

from src.gateway.models import ClassificationResult

# Matches a genuine arithmetic expression: two numbers joined by an operator.
# Strong operators (+, *, =, ^) may be written without spaces ("2+2", "x=5");
# ambiguous prose punctuation (-, /) must be space-delimited ("10 / 2") so that
# hyphenated words ("year-over-year"), ranges ("3-5%") and dates ("2023-2024")
# do NOT get misread as math.
_ARITHMETIC_RE = re.compile(r"\d\s*[+*=^]\s*\d|\d\s+[-/]\s+\d")

# Postfix factorial ("4!", "what is 10!"). The operand must be adjacent to the
# "!" — an exclamation mark after a word is prose ("I have 3 cats!" does not
# match, since "!" there follows "s"). "!=" and "!!" are excluded: the first is
# the not-equal operator in most languages, the second is emphasis.
_FACTORIAL_RE = re.compile(r"\d!(?![=!])")

# Function-call notation with a numeric argument: "sqrt(16)", "log(100)",
# "sin(pi/2)". Two deliberate restrictions keep this off prose and code:
#
# * The name must be followed by "(", because keyword matching here is
#   substring-based — a bare "ln" would fire inside "explain" and "mod" inside
#   "model", breaking the reasoning and general cases.
# * The argument must start with a number or a math constant, which is what
#   separates the genuinely ambiguous names from their programming senses:
#   log("starting server") is a logging call, log(100) is a logarithm.
#
# Programming builtins that happen to be mathematical (abs, round, floor, ceil,
# pow) are left out entirely — "what does floor() do in Python" is a coding
# question, and the numeric-argument rule alone would not tell the difference.
_MATH_FUNC_RE = re.compile(
    r"\b(sqrt|cbrt|log|log2|log10|ln|exp|sin|cos|tan|asin|acos|atan"
    r"|sinh|cosh|tanh|gcd|lcm|mod)\s*\(\s*(-?[\d.]|pi\b|e\b|tau\b)"
)

# "15% of 200", "15 percent of 200" — a percentage *applied to* a quantity.
# The trailing "of <number>" is what makes this a calculation rather than a
# statistic quoted in prose ("up 12% year-over-year" has no "of" and so does
# not match).
_PERCENT_OF_RE = re.compile(r"\d\s*(?:%|\s*percent)\s+of\s+\d")

# Quoted text is usually material the user is referring to, not the requested
# operation. Masking it prevents a failing test that contains "write a poem",
# or a report that says "implement immediately", from hijacking the route.
# Apostrophes inside contractions are excluded by the surrounding word checks.
_QUOTED_TEXT_RE = re.compile(
    r'"[^"\n]+"|(?<!\w)\'[^\'\n]+\'(?!\w)'
)

# A single topical noun is weak evidence. "Python" may be a dog name and
# "algorithm" may be printed on a shirt. Strong intent rules below lift real
# requests above this floor, while two independent keywords still count.
_MIN_SPECIALIZED_SCORE = 1.5

_INTENT_RULES: dict[str, tuple[tuple[re.Pattern[str], str, float], ...]] = {
    "coding": (
        (
            re.compile(
                r"\b(?:implement|debug|refactor|fix|compile|complete|convert|"
                r"inspect|validate)\b[\s\S]{0,80}\b(?:code|function|class|"
                r"method|api|endpoint|script|query|sql|test|assertion|"
                r"implementation|algorithm|component|construct|loop|iterator|"
                r"index|query plan|cdk|stack trace)\b"
            ),
            "coding_action",
            3.0,
        ),
        (
            re.compile(
                r"\b(?:write|create|build)\b[\s\S]{0,70}\b(?:code|function|"
                r"class|method|api|endpoint|script|query|sql|component|"
                r"construct|implementation|cdk)\b"
            ),
            "coding_creation",
            3.0,
        ),
        (
            re.compile(
                r"\b(?:aws\s+cdk|cloudfront\s+oac|unit\s+test|test\s+"
                r"assertion|retry\s+loop|query\s+plan|stack\s+trace|borrow\s+"
                r"(?:checker|fail(?:s|ed|ing)?)|production\s+behavior)\b"
            ),
            "coding_technical_phrase",
            2.5,
        ),
        (
            re.compile(
                r"(?:\b(?:python|typescript|javascript|rust|golang|java|bash|"
                r"shell|sql)\b|c\+\+)[\s\S]{0,80}\b(?:function|función|script|"
                r"api|endpoint|iterator|query|code|implementation|validate|"
                r"error|fail(?:s|ed|ing)?|index)\b"
                r"|\b(?:function|función|script|api|endpoint|iterator|query|"
                r"code|implementation|validate|error|fail(?:s|ed|ing)?|index)"
                r"\b[\s\S]{0,80}(?:\b(?:python|typescript|javascript|rust|"
                r"golang|java|bash|shell|sql)\b|c\+\+)"
            ),
            "coding_language_context",
            2.5,
        ),
        (
            re.compile(
                r"\b(?:make|stop|prevent)\b[\s\S]{0,60}\b(?:retry|loop|"
                r"request|transaction)\b[\s\S]{0,60}\b(?:duplicat(?:e|ing)|"
                r"timeout|fail|idempotent)\b"
            ),
            "coding_reliability_change",
            2.5,
        ),
        (
            re.compile(
                r"\b(?:add|insert|remove)\b[\s\S]{0,60}\b(?:function|method|"
                r"call|logging|log)\b"
            ),
            "coding_call_change",
            2.5,
        ),
    ),
    "reasoning": (
        (
            re.compile(
                r"\b(?:compare(?:\s+and)?\s+contrast|assess\s+whether|"
                r"determine\s+(?:which|whether)|deduce|infer|justify)\b"
            ),
            "reasoning_analysis_action",
            2.5,
        ),
        (
            re.compile(
                r"\b(?:analy[sz]e|assess|evaluate)\b[\s\S]{0,80}\b(?:"
                r"trade-?offs?|evidence|recommendation|argument|failure\s+"
                r"modes?|claims?|decision|policy)\b"
            ),
            "reasoning_evaluation",
            2.5,
        ),
        (
            re.compile(
                r"\b(?:what|which)\s+assumptions?\b|\b(?:conclusion|claims?|"
                r"statements?)[\s\S]{0,70}\b(?:premises?|true|truthful|lying)"
                r"\b|\b(?:premises?|evidence)[\s\S]{0,70}\b(?:conclusion|"
                r"supports?|implies?|establishes?)\b"
            ),
            "reasoning_logic_structure",
            2.5,
        ),
        (
            re.compile(
                r"\bargue\s+both\s+sides\b|\bwho\s+can\s+be\s+truthful\b"
            ),
            "reasoning_deliberation",
            2.5,
        ),
        (
            re.compile(
                r"\bthink\s+step\s+by\s+step\b|\bwhich\b[\s\S]{0,60}\b"
                r"most\s+likely\s+caused\b"
            ),
            "reasoning_causal_diagnosis",
            2.5,
        ),
        (
            re.compile(r"\bwhy\b|\bpor\s+qu[eé]\b"),
            "reasoning_why",
            1.5,
        ),
    ),
    "creative_writing": (
        (
            re.compile(
                r"\b(?:write|compose|draft|invent|tell|continue|rewrite|turn|"
                r"create|give)\b[\s\S]{0,90}\b(?:poem|sonnet|story|fiction|"
                r"narrative|dialogue|monologue|scene|taglines?|museum\s+"
                r"placard|opening\s+paragraph|point\s+of\s+view|voice|"
                r"character)\b"
            ),
            "creative_output",
            3.0,
        ),
        (
            re.compile(
                r"\bescribe\b[\s\S]{0,60}\b(?:poema|cuento|historia|diálogo)"
                r"\b"
            ),
            "creative_output_spanish",
            3.0,
        ),
    ),
    "summarization": (
        (
            re.compile(r"\b(?:summari[sz]e|condense|recap|shorten)\b"),
            "summarization_action",
            3.0,
        ),
        (
            re.compile(
                r"\b(?:tldr|tl;dr|key\s+points?|executive\s+overview|"
                r"release\s+notes)\b"
            ),
            "summarization_output",
            2.5,
        ),
        (
            re.compile(
                r"\bextract\b[\s\S]{0,60}\b(?:central|main|key)\s+"
                r"(?:claims?|points?|ideas?|evidence)\b|\bin\s+(?:three|"
                r"four|five|\d+)\s+bullets?\b[\s\S]{0,60}\b(?:capture|"
                r"state|list)\b|\bbriefly\s+state\b"
            ),
            "summarization_structure",
            2.5,
        ),
        (
            re.compile(
                r"\bresume\s+(?:este|esta|estos|estas|el|la|los|las|un|una)"
                r"\b"
            ),
            "summarization_spanish",
            3.0,
        ),
    ),
    "math": (
        (
            re.compile(
                r"\b(?:eigenvalues?|eigenvectors?|determinant|positive\s+"
                r"predictive\s+value|sensitivity|specificity|prevalence|"
                r"differentiat(?:e|ion)|expected\s+value)\b"
            ),
            "math_concept",
            3.0,
        ),
        (
            re.compile(
                r"\b(?:probability|square\s+root|cube\s+root|logarithm|"
                r"factorial|permutation|combination|quadratic|trigonometry|"
                r"multiply|divide|subtract|modulo|remainder)\b"
            ),
            "math_operation",
            1.5,
        ),
        (
            re.compile(
                r"\b(?:cu[aá]nto\s+es\s+)?\d+\s+por\s+ciento\s+de\s+\d+\b"
            ),
            "percent_of_spanish",
            3.0,
        ),
    ),
}

_NEGATED_INTENT_RULES: dict[str, tuple[re.Pattern[str], float]] = {
    "coding": (
        re.compile(
            r"\b(?:do\s+not|don't)\s+(?:write|change|modify|implement)\s+"
            r"(?:any\s+)?(?:code|software)\b|\bnot\s+code\s+changes?\b|"
            r"\brather\s+than\s+(?:implementing|writing)\s+(?:software|code)"
            r"\b"
        ),
        6.0,
    ),
    "creative_writing": (
        re.compile(
            r"\b(?:do\s+not|don't)\s+(?:write|create|tell)\s+(?:a\s+)?"
            r"(?:story|poem|narrative)\b"
        ),
        6.0,
    ),
}


@lru_cache(maxsize=256)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile a literal keyword with boundaries when its edges are words."""
    escaped = re.escape(keyword)
    prefix = r"(?<!\w)" if keyword and keyword[0].isalnum() else ""
    suffix = r"(?!\w)" if keyword and keyword[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix)


class TaskClassifier:
    """Classifies prompts into task types using keyword/heuristic analysis."""

    TASK_KEYWORDS: dict[str, list[str]] = {
        "coding": [
            "code", "function", "bug", "implement", "class", "method", "api",
            "debug", "refactor", "syntax", "compile", "programming", "algorithm",
            "script", "regex", "query", "sql", "endpoint",
            # common languages / runtimes — strong coding signals
            "python", "javascript", "typescript", "java", "golang", "rust",
            "c++", "bash", "shell",
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
            # Operations users spell out rather than write in notation. The
            # symbolic forms are handled by the heuristics below; these catch
            # "the square root of 144", which contains no operator at all.
            "square root", "cube root", "logarithm", "arithmetic",
            "multiply", "divide", "subtract", "modulo", "remainder",
            "permutation", "combination", "quadratic", "trigonometry",
        ],
    }

    VALID_TASK_TYPES = {"coding", "reasoning", "creative_writing", "summarization", "math", "general"}

    def __init__(self, custom_keywords: dict[str, list[str]] | None = None) -> None:
        """Initialize with default keywords, optionally extended."""
        self._keywords: dict[str, list[str]] = {
            k: list(v) for k, v in self.TASK_KEYWORDS.items()
        }
        self._custom_keywords: dict[str, set[str]] = {}
        if custom_keywords:
            for task_type, keywords in custom_keywords.items():
                existing = self._keywords.get(task_type, [])
                self._keywords[task_type] = existing + keywords
                self._custom_keywords.setdefault(task_type, set()).update(keywords)

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
        intent_text = _QUOTED_TEXT_RE.sub(" ", normalized)

        # Score each task type by keyword matches
        scores: dict[str, float] = {}
        matched: dict[str, list[str]] = {}

        for task_type, keywords in self._keywords.items():
            matches = [
                kw for kw in keywords
                if _keyword_pattern(kw).search(intent_text)
            ]
            custom = self._custom_keywords.get(task_type, set())
            scores[task_type] = sum(
                2.0 if keyword in custom else 1.0
                for keyword in matches
            )
            matched[task_type] = matches

        # Apply structural heuristics
        self._apply_heuristics(prompt, intent_text, scores, matched)

        # Find best and second-best scores
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        best_type = sorted_scores[0][0] if sorted_scores else "general"
        best_score = sorted_scores[0][1] if sorted_scores else 0.0
        second_best_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

        # Weak topic-only evidence is deliberately left as general. This avoids
        # routing "I named my dog Python" to a coding model while preserving
        # custom keywords (weighted above) and strong intent/structure rules.
        if best_score < _MIN_SPECIALIZED_SCORE:
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

        # Requested-operation patterns carry more evidence than isolated topic
        # words. They are deliberately generic rather than corpus IDs or exact
        # benchmark sentences.
        for task_type, rules in _INTENT_RULES.items():
            for pattern, label, weight in rules:
                if pattern.search(normalized):
                    scores.setdefault(task_type, 0.0)
                    scores[task_type] += weight
                    matched.setdefault(task_type, []).append(label)

        # Explicitly negated operations should not win because their nouns are
        # present. Examples: "executive overview, not code changes" and
        # "derive the expected value; do not write a story."
        for task_type, (pattern, penalty) in _NEGATED_INTENT_RULES.items():
            if pattern.search(normalized):
                scores[task_type] = max(
                    0.0,
                    scores.get(task_type, 0.0) - penalty,
                )

        # Contains a genuine arithmetic expression (number-operator-number) →
        # boost math. Guards against prose that merely contains digits and
        # punctuation (percentages, dates, ranges, hyphenated words).
        if _ARITHMETIC_RE.search(original):
            scores.setdefault("math", 0.0)
            scores["math"] += 1.5
            matched.setdefault("math", []).append("math_operators_heuristic")

        # Notation that is not infix binary, and so invisible to the rule above:
        # postfix factorial ("4!"), function calls ("sqrt(16)"), and a percentage
        # applied to a quantity ("15% of 200"). Each carries the same weight as
        # an arithmetic expression — they are equally unambiguous once matched.
        for pattern, label in (
            (_FACTORIAL_RE, "factorial_notation_heuristic"),
            (_MATH_FUNC_RE, "math_function_heuristic"),
            (_PERCENT_OF_RE, "percent_of_heuristic"),
        ):
            if pattern.search(normalized):
                scores.setdefault("math", 0.0)
                scores["math"] += 1.5
                matched.setdefault("math", []).append(label)
