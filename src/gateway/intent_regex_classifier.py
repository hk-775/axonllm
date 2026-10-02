"""Opt-in action-first regex candidate; the published baseline stays unchanged.

Rule scores express precedence, not calibrated probabilities. This classifier
does not execute instructions or determine whether a request is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from src.gateway.models import ClassificationResult
from src.gateway.task_classifier import TaskClassifier

_QUOTES = re.compile(
    r"```[\s\S]*?```|`[^`\n]+`|"
    r'"[^"]*"|(?<!\w)\'[^\'\n]*\'(?!\w)'
)
_CORRECTION = re.compile(
    r"\bactually\s*,\s*(?:just\s+)?|\bactually\s+(?:just|please)\s+|"
    r"\b(?:instead|rather)\s*,?\s*(?:please\s+|your\s+task\s+is\s+to\s+)|"
    r"\bwhat\s+i\s+(?:really\s+)?(?:want|need)\s*(?::|is\b)"
)
_NEGATION = re.compile(
    r"\b(?:do\s+not|don't|don't\s+need\s+to|no\s+need\s+to|"
    r"please\s+avoid|without)\b"
    r"(?:(?![.!?;]|\b(?:but|instead|just|rather)\b).){0,180}"
)
_SOFTWARE = (
    r"(?:code|program|function|method|api|endpoint|sql|regex|regular\s+expression|"
    r"database\s+query|unit\s+test|class|module|implementation|"
    r"python|javascript|typescript|rust|bash|shell|terraform|css|html)"
)
_LITERARY = (
    r"(?:story|poem|poetry|dialogue|conversation|narrative|fiction|"
    r"article|essay|newsletter|blog\s+post|slogan|tagline|speech|"
    r"scene|monologue|screenplay|lyrics|feature|"
    r"(?:radio|television|tv|psa|podcast|conversation)\s+(?:psa\s+)?script)"
)


@dataclass(frozen=True)
class _Rule:
    task: str
    name: str
    weight: int
    pattern: re.Pattern[str]


def _rule(task: str, name: str, weight: int, expression: str) -> _Rule:
    return _Rule(task, name, weight, re.compile(expression))


_RULES = (
    _rule("summarization", "requested_summary", 12,
          r"\b(?:summari[sz]e|condense|recap|tldr|tl;dr|"
          r"(?:concise|brief|short)\s+summary|(?:give|provide|write|make)\b"
          r"\s+(?:(?:me|us)\s+)?(?:(?:a|the)\s+)?"
          r"(?:(?:concise|brief|short|detailed)\s+)?summary)\b"),
    _rule("summarization", "source_main_points", 11,
          r"\b(?:gist|main\s+(?:points?|takeaways?)|key\s+(?:points?|takeaways?)|"
          r"core\s+ideas?|most\s+important\s+outcomes|"
          r"short\s+version|executive\s+summary)\b"),
    _rule("creative_writing", "requested_creative_artifact", 11,
          rf"\b(?:write|draft|compose|create|craft|tell|retell|capture)\b"
          rf"[^.!?\n]{{0,100}}\b{_LITERARY}\b"),
    _rule("creative_writing", "imagined_creation", 10,
          r"\b(?:invent|imagine)\b|\bpropose\b[^.!?\n]{0,55}"
          r"\b(?:original|new|fictional)\b|\b(?:retell|describe)\b"
          r"[^.!?\n]{0,100}\b(?:compelling|engaging|colorful|vivid)\b"),
    _rule("creative_writing", "performance_words", 10,
          r"\bwhat\s+(?:could|would|might)\b[^.!?\n]{0,55}\bsay\b"
          r"[^.!?\n]{0,55}\b(?:inspire|persuade|entertain|encourage)\b|"
          r"\b(?:radio|public\s+service\s+announcement)\b"
          r"[^.!?\n]{0,55}\bscript\b"),
    _rule("coding", "software_change", 10,
          rf"\b(?:write|create|build|debug|refactor|fix|modify|adjust|change|"
          rf"implement|inspect|analy[sz]e|check|review|optimi[sz]e)\b"
          rf"[^.!?\n]{{0,70}}\b{_SOFTWARE}\b"),
    _rule("coding", "software_explanation", 10,
          rf"\b(?:explain|understand|break\s+down|how)\b[^.!?\n]{{0,90}}"
          rf"\b{_SOFTWARE}\b|\b(?:regex|regular\s+expression)\b"
          rf"[^.!?\n]{{0,45}}\b(?:works?|matches|validates|pattern)\b|"
          rf"\bhow\b[^.!?\n]{{0,100}}\b(?:sql|programmatically)\b"),
    _rule("coding", "http_operation", 10,
          r"\b(?:modify|adjust|change|filter|fix)\b[^.!?\n]{0,80}"
          r"\b(?:get|post|put|patch|delete)\s+/"),
    _rule("coding", "software_failure", 10,
          r"\b(?:script|code|program|api|server|function)\b"
          r"[\s\S]{0,180}\b(?:wrong|bugs?|errors?|fails?|unexpected|crash)\b|"
          r"\b(?:errors?|unexpected|wrong)\b[^.!?\n]{0,100}"
          r"\b(?:script|code|program|api)\b"),
    _rule("math", "calculation_request", 10,
          r"\b(?:calculate|compute|solve|derive|prove|evaluate)\b"
          r"[^.!?\n]{0,85}\b(?:total|sum|price|cost|tax|size|time|distance|"
          r"equation|integral|derivative|theorem|probability|odds|"
          r"expected\s+value|variance|mean|median|rate|percentage)\b|"
          r"\b(?:what|find|estimate)\b[^.!?\n]{0,65}"
          r"\b(?:probability|odds|chances|grand\s+total|total\s+(?:price|"
          r"charge|cost|size|time|number|storage|travel\s+time))\b"),
    _rule("math", "quantified_question", 10,
          r"\A(?=[\s\S]*(?:\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\b))"
          r"(?=[\s\S]*\b(?:how\s+(?:many|much)|what(?:'s|\s+is)\s+(?:my|the)\s+"
          r"(?:grand\s+)?total|(?:at\s+)?what\s+time|estimate\s+the\s+total)\b)"),
    _rule("math", "probability_question", 10,
          r"\b(?:probability|odds|chances)\b[^.!?\n]{0,60}"
          r"\b(?:that|of|their|the|a)\b"),
    _rule("reasoning", "explicit_analysis", 9,
          r"\b(?:analy[sz]e|reason\s+through|deduce|infer|assess|evaluate|debate|"
          r"weigh|justify|prioriti[sz]e|compare)\b"),
    _rule("reasoning", "decision_tradeoffs", 12,
          r"\b(?:pros\s+and\s+cons|advantages\s+and\s+disadvantages|"
          r"risks\s+and\s+benefits|security\s+and\s+privacy\s+considerations|"
          r"trade[- ]?offs|arguments\s+for\s+and\s+against)\b|"
          r"\b(?:should\s+we|which\s+(?:option|approach))\b"
          r"[^.!?\n]{0,100}\b(?:or|choose|proceed|prefer|best)\b"),
    _rule("reasoning", "causal_interpretation", 9,
          r"\b(?:how|what|why|could)\b[^.!?\n]{0,100}"
          r"\b(?:affect|affected|influence|impact|explain\s+this\s+difference|"
          r"evolved\s+differently|played\s+a\s+role|interpret|implies|"
          r"contributing\s+factors|implications)\b"),
    _rule("general", "factual_information", 6,
          r"\b(?:list|tell\s+me)\b[^.!?\n]{0,70}"
          r"\b(?:milestones|versions|facts|origins|definitions)\b|"
          r"\b(?:explain|describe)\b[^.!?\n]{0,55}"
          r"\b(?:basics|usual|steps|history|meaning)\b|"
          r"\b(?:who|when|where)\s+(?:is|was|were|did|does)\b"),
)


def requested_text(prompt: str) -> str:
    """Mask quotations and negation, then honor an explicit correction.

    This is a lexical approximation. Nested quotes, indirect requests, and
    multiple equally important tasks remain limitations.
    """
    text = unicodedata.normalize("NFKC", prompt).casefold().translate(
        str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
    )
    text = _QUOTES.sub(" ", text)
    corrections = list(_CORRECTION.finditer(text))
    if corrections:
        text = text[corrections[-1].end():]
    return _NEGATION.sub(" ", text)


class IntentRegexClassifier:
    """Action-first rules with the immutable original classifier as fallback."""

    def __init__(self) -> None:
        self._fallback = TaskClassifier()

    def classify(self, prompt: str) -> ClassificationResult:
        """Return a deterministic route and the names of the matching rules."""
        text = requested_text(prompt)
        matches = [(rule, rule.pattern.search(text)) for rule in _RULES]
        matches = [(rule, match) for rule, match in matches if match is not None]
        if not matches:
            result = self._fallback.classify(text)
            return ClassificationResult(
                task_type=result.task_type,
                confidence=result.confidence,
                matched_keywords=["baseline_fallback", *result.matched_keywords],
            )
        # Each rule counts once. Repeating a topic cannot accumulate votes.
        best, _ = max(matches, key=lambda pair: (pair[0].weight, pair[1].start()))
        competitors = [r.weight for r, _ in matches if r.task != best.task]
        return ClassificationResult(
            task_type=best.task,
            confidence=best.weight / (best.weight + max(competitors, default=0)),
            matched_keywords=[r.name for r, _ in matches if r.task == best.task],
        )
