"""Tests for named-entity PII detection layered on the regex patterns.

A fake detector throughout rather than Comprehend: these tests are about the
merge/overlap/fail-open logic in PIIRedactor, and a network call would make them
slow, flaky, and billable. The Comprehend response shape is pinned by
``TestComprehendDetector``, which drives ComprehendEntityDetector with a stub
boto3 client.
"""

import pytest

from src.gateway.models import ResolvedPolicy
from src.gateway.security.pii_ner import (
    NER_TYPE_MAP,
    ComprehendEntityDetector,
    build_entity_detector,
    env_ner_enabled,
    env_ner_types,
)
from src.gateway.security.pii_redactor import PII_PATTERNS, PIIRedactor, RedactionMapping


class FakeDetector:
    """Returns spans for substrings it is told to find."""

    def __init__(self, finds: dict[str, str] | None = None, raises: Exception | None = None):
        # substring -> pii_type
        self._finds = finds or {}
        self._raises = raises
        self.calls: list[tuple[str, list[str]]] = []

    async def detect(self, text, active_types):
        self.calls.append((text, list(active_types)))
        if self._raises is not None:
            raise self._raises
        spans = []
        for needle, pii_type in self._finds.items():
            if pii_type not in active_types:
                continue
            start = text.find(needle)
            if start >= 0:
                spans.append((start, start + len(needle), pii_type))
        return spans


def _policy(**kw):
    base = dict(pii_redaction_enabled=True, pii_redact_types=list(PII_PATTERNS))
    base.update(kw)
    return ResolvedPolicy(**base)


class TestNameRedaction:
    """The gap this module exists to close."""

    @pytest.mark.asyncio
    async def test_a_name_survives_regex_but_not_ner(self):
        text = "I am Alice Smith, email a@b.com."
        # Regex alone: PII_PATTERNS has no name pattern, by design — a name has
        # no distinguishing shape the way an SSN does.
        plain, _ = PIIRedactor().redact_messages(
            [{"role": "user", "content": text}], _policy())
        assert "Alice Smith" in plain[0]["content"]
        assert "[EMAIL_1]" in plain[0]["content"]

        detector = FakeDetector({"Alice Smith": "name"})
        redacted, mapping = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": text}], _policy(pii_ner_enabled=True))
        assert "Alice Smith" not in redacted[0]["content"]
        assert "[NAME_1]" in redacted[0]["content"]
        # The regex layer still runs — NER supplements, it does not replace.
        assert "[EMAIL_1]" in redacted[0]["content"]
        assert mapping.redacted_count == 2

    @pytest.mark.asyncio
    async def test_the_round_trip_restores_both_detectors_values(self):
        text = "Alice Smith at a@b.com in Seattle"
        redactor = PIIRedactor(
            entity_detector=FakeDetector({"Alice Smith": "name", "Seattle": "address"}))
        redacted, mapping = await redactor.redact_messages_async(
            [{"role": "user", "content": text}], _policy(pii_ner_enabled=True))
        assert redactor.reinject_response(redacted[0]["content"], mapping) == text


class TestGating:
    """Off by default: NER costs more per request than the model's input tokens."""

    @pytest.mark.asyncio
    async def test_detector_is_not_called_when_policy_leaves_ner_off(self):
        detector = FakeDetector({"Alice Smith": "name"})
        redacted, _ = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith here"}], _policy())
        assert detector.calls == []
        assert "Alice Smith" in redacted[0]["content"]

    @pytest.mark.asyncio
    async def test_no_detector_configured_is_a_no_op_not_an_error(self):
        # A deploy without boto3 gets None from build_entity_detector; a policy
        # that asks for NER anyway must degrade, not crash.
        redacted, _ = await PIIRedactor(entity_detector=None).redact_messages_async(
            [{"role": "user", "content": "Alice Smith at a@b.com"}],
            _policy(pii_ner_enabled=True))
        assert "[EMAIL_1]" in redacted[0]["content"]
        assert "Alice Smith" in redacted[0]["content"]

    @pytest.mark.asyncio
    async def test_redaction_disabled_beats_ner_enabled(self):
        detector = FakeDetector({"Alice Smith": "name"})
        redacted, mapping = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith"}],
            ResolvedPolicy(pii_redaction_enabled=False, pii_ner_enabled=True))
        assert redacted[0]["content"] == "Alice Smith"
        assert mapping.redacted_count == 0

    @pytest.mark.asyncio
    async def test_policy_ner_types_narrow_what_is_requested(self):
        detector = FakeDetector({"Alice Smith": "name", "Seattle": "address"})
        redacted, _ = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith in Seattle"}],
            _policy(pii_ner_enabled=True, pii_ner_types=["name"]))
        assert "[NAME_1]" in redacted[0]["content"]
        # address was not requested, so it stays
        assert "Seattle" in redacted[0]["content"]
        assert detector.calls[0][1] == ["name"]


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_a_detector_error_degrades_to_regex_only(self):
        detector = FakeDetector(raises=RuntimeError("comprehend is down"))
        redacted, mapping = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith at a@b.com"}],
            _policy(pii_ner_enabled=True))
        # The regex layer is unaffected by the NER outage.
        assert "[EMAIL_1]" in redacted[0]["content"]
        assert mapping.redacted_count == 1
        assert "Alice Smith" in redacted[0]["content"]

    @pytest.mark.asyncio
    async def test_the_failure_is_logged(self, caplog):
        detector = FakeDetector(raises=RuntimeError("boom"))
        with caplog.at_level("WARNING"):
            await PIIRedactor(entity_detector=detector).redact_messages_async(
                [{"role": "user", "content": "Alice Smith"}],
                _policy(pii_ner_enabled=True))
        # Fail-open is only defensible if it leaves a trace.
        assert any("regex-only" in r.getMessage() for r in caplog.records)
        assert any("boom" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_mapping_records_why_ner_produced_nothing(self):
        # Without this the caller cannot distinguish a throttled detector from
        # one that found no names: both return a normal result with no NER
        # spans. The admin preview panel reported "available" on a throttled
        # detector because of exactly that.
        detector = FakeDetector(raises=RuntimeError("Throttling: rate exceeded"))
        _, mapping = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith"}],
            _policy(pii_ner_enabled=True))
        assert mapping.ner_error is not None
        assert "Throttling" in mapping.ner_error

    @pytest.mark.asyncio
    async def test_a_clean_run_records_no_error(self):
        # The flag must stay None on success, or every caller reads a
        # degradation that never happened.
        detector = FakeDetector({"Alice Smith": "name"})
        _, mapping = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith"}],
            _policy(pii_ner_enabled=True))
        assert mapping.ner_error is None


class TestOverlapResolution:
    """Two detectors over one string can claim overlapping text."""

    def test_longest_span_wins(self):
        r = PIIRedactor()
        mapping = RedactionMapping()
        # A short span nested inside a longer one at the same start.
        out = r._apply_spans("AAAA BBBB CCCC", [(0, 4, "short"), (0, 9, "long")], mapping)
        assert out == "[LONG_1] CCCC"
        assert mapping.redacted_count == 1

    def test_partial_overlap_keeps_the_earlier_longer_span(self):
        r = PIIRedactor()
        mapping = RedactionMapping()
        out = r._apply_spans("AAAA BBBB CCCC", [(0, 9, "a"), (5, 14, "b")], mapping)
        assert out == "[A_1] CCCC"

    def test_disjoint_spans_all_apply(self):
        r = PIIRedactor()
        mapping = RedactionMapping()
        out = r._apply_spans(
            "AAAA BBBB CCCC", [(0, 4, "a"), (5, 9, "b"), (10, 14, "c")], mapping)
        assert out == "[A_1] [B_1] [C_1]"
        assert mapping.reinject(out) == "AAAA BBBB CCCC"

    def test_unsorted_input_is_handled(self):
        r = PIIRedactor()
        mapping = RedactionMapping()
        # Replacement must go right-to-left regardless of input order, or the
        # second replacement lands at an index the first invalidated.
        out = r._apply_spans("AAAA BBBB CCCC", [(10, 14, "c"), (0, 4, "a")], mapping)
        assert out == "[A_1] BBBB [C_1]"

    @pytest.mark.asyncio
    async def test_an_ner_span_overlapping_a_regex_match_does_not_corrupt(self):
        # The realistic case: Comprehend reports ADDRESS over a street line
        # whose trailing digits also match the phone pattern.
        text = "Ship to 500 Main St 555-234-5678 today"
        detector = FakeDetector({"500 Main St 555-234-5678": "address"})
        redactor = PIIRedactor(entity_detector=detector)
        redacted, mapping = await redactor.redact_messages_async(
            [{"role": "user", "content": text}], _policy(pii_ner_enabled=True))
        out = redacted[0]["content"]
        # Exactly one token, no fragments of the other detector's match left.
        assert out == "Ship to [ADDRESS_1] today"
        assert redactor.reinject_response(out, mapping) == text


class TestMultimodalContent:
    @pytest.mark.asyncio
    async def test_ner_reaches_text_parts_of_list_content(self):
        detector = FakeDetector({"Alice Smith": "name"})
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "I am Alice Smith"},
            {"type": "image", "source": {"bytes": "..."}},
        ]}]
        redacted, _ = await PIIRedactor(entity_detector=detector).redact_messages_async(
            messages, _policy(pii_ner_enabled=True))
        assert "[NAME_1]" in redacted[0]["content"][0]["text"]
        # Non-text parts pass through untouched.
        assert redacted[0]["content"][1] == {"type": "image", "source": {"bytes": "..."}}

    @pytest.mark.asyncio
    async def test_a_message_without_content_is_preserved(self):
        detector = FakeDetector({"Alice": "name"})
        messages = [{"role": "assistant", "tool_calls": [{"id": "x"}]}]
        redacted, _ = await PIIRedactor(entity_detector=detector).redact_messages_async(
            messages, _policy(pii_ner_enabled=True))
        assert redacted[0] == messages[0]


class TestNoReinjectMode:
    @pytest.mark.asyncio
    async def test_permanent_redaction_retains_no_ner_plaintext(self):
        detector = FakeDetector({"Alice Smith": "name"})
        redactor = PIIRedactor(entity_detector=detector)
        redacted, mapping = await redactor.redact_messages_async(
            [{"role": "user", "content": "Alice Smith"}],
            _policy(pii_ner_enabled=True, pii_reinject=False))
        assert "[NAME_1]" in redacted[0]["content"]
        assert mapping._forward == {}
        assert redactor.reinject_response(redacted[0]["content"], mapping) == "[NAME_1]"


class TestEnvDefaults:
    def test_unset_means_off(self, monkeypatch):
        monkeypatch.delenv("AXON_PII_NER_DEFAULT", raising=False)
        assert env_ner_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("AXON_PII_NER_DEFAULT", value)
        assert env_ner_enabled() is True

    def test_types_default_to_the_full_set(self, monkeypatch):
        monkeypatch.delenv("AXON_PII_NER_TYPES", raising=False)
        assert set(env_ner_types()) == set(NER_TYPE_MAP.values())

    def test_types_can_be_narrowed(self, monkeypatch):
        monkeypatch.setenv("AXON_PII_NER_TYPES", "name")
        assert env_ner_types() == ["name"]

    def test_unknown_types_are_dropped_not_raised(self, monkeypatch):
        monkeypatch.setenv("AXON_PII_NER_TYPES", "name,nonsense")
        assert env_ner_types() == ["name"]

    def test_all_unknown_falls_back_to_the_default_set(self, monkeypatch):
        # Rather than an empty list, which would silently disable detection a
        # policy explicitly asked for.
        monkeypatch.setenv("AXON_PII_NER_TYPES", "nonsense")
        assert set(env_ner_types()) == set(NER_TYPE_MAP.values())

    @pytest.mark.asyncio
    async def test_env_default_enables_ner_without_a_policy_field(self, monkeypatch):
        monkeypatch.setenv("AXON_PII_NER_DEFAULT", "true")
        detector = FakeDetector({"Alice Smith": "name"})
        redacted, _ = await PIIRedactor(entity_detector=detector).redact_messages_async(
            [{"role": "user", "content": "Alice Smith"}], _policy())
        assert "[NAME_1]" in redacted[0]["content"]


class _StubComprehend:
    """Stands in for the boto3 comprehend client."""

    def __init__(self, entities):
        self._entities = entities
        self.last_kwargs = None

    def detect_pii_entities(self, **kwargs):
        self.last_kwargs = kwargs
        return {"Entities": self._entities}


class TestComprehendDetector:
    """Pins the response-shape handling, without touching the network."""

    @pytest.mark.asyncio
    async def test_maps_entity_types_and_offsets(self):
        text = "I am Alice Smith from Seattle"
        client = _StubComprehend([
            {"Type": "NAME", "Score": 0.99, "BeginOffset": 5, "EndOffset": 16},
            {"Type": "ADDRESS", "Score": 0.99, "BeginOffset": 22, "EndOffset": 29},
        ])
        spans = await ComprehendEntityDetector(client=client).detect(
            text, ["name", "address"])
        assert spans == [(5, 16, "name"), (22, 29, "address")]
        # Offsets must index the string as given, or the token lands wrong.
        assert text[5:16] == "Alice Smith"
        assert text[22:29] == "Seattle"

    @pytest.mark.asyncio
    async def test_types_the_regexes_already_cover_are_ignored(self):
        # Comprehend also returns EMAIL/SSN. Paying to re-find what PII_PATTERNS
        # finds for free would add duplicate spans, not coverage.
        client = _StubComprehend([
            {"Type": "EMAIL", "Score": 1.0, "BeginOffset": 0, "EndOffset": 5},
        ])
        assert await ComprehendEntityDetector(client=client).detect("a@b.c", ["name"]) == []

    @pytest.mark.asyncio
    async def test_low_confidence_is_dropped(self):
        client = _StubComprehend([
            {"Type": "NAME", "Score": 0.5, "BeginOffset": 0, "EndOffset": 5},
        ])
        assert await ComprehendEntityDetector(client=client).detect("Alice", ["name"]) == []

    @pytest.mark.asyncio
    async def test_unrequested_types_are_dropped(self):
        client = _StubComprehend([
            {"Type": "ADDRESS", "Score": 1.0, "BeginOffset": 0, "EndOffset": 7},
        ])
        assert await ComprehendEntityDetector(client=client).detect(
            "Seattle", ["name"]) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity", [
        {"Type": "NAME", "Score": 1.0, "BeginOffset": -1, "EndOffset": 5},
        {"Type": "NAME", "Score": 1.0, "BeginOffset": 0, "EndOffset": 999},
        {"Type": "NAME", "Score": 1.0, "BeginOffset": 5, "EndOffset": 5},
        {"Type": "NAME", "Score": 1.0, "BeginOffset": None, "EndOffset": 5},
        {"Type": "NAME", "Score": 1.0},
    ])
    async def test_malformed_offsets_are_skipped(self, entity):
        # A span past the end would slice silently and put a token in the wrong
        # place — worse than skipping the detection.
        client = _StubComprehend([entity])
        assert await ComprehendEntityDetector(client=client).detect(
            "Alice", ["name"]) == []

    @pytest.mark.asyncio
    async def test_empty_and_typeless_input_short_circuits(self):
        client = _StubComprehend([])
        d = ComprehendEntityDetector(client=client)
        assert await d.detect("   ", ["name"]) == []
        assert await d.detect("Alice Smith", []) == []
        # No call was made, so no charge was incurred.
        assert client.last_kwargs is None

    @pytest.mark.asyncio
    async def test_oversized_text_is_truncated_not_rejected(self):
        client = _StubComprehend([])
        text = "a" * 200_000
        await ComprehendEntityDetector(client=client).detect(text, ["name"])
        sent = client.last_kwargs["Text"]
        assert len(sent.encode("utf-8")) <= 95_000

    @pytest.mark.asyncio
    async def test_multibyte_truncation_does_not_corrupt(self):
        # Cutting on a byte boundary can split a multi-byte character; it must
        # be dropped rather than producing invalid UTF-8.
        client = _StubComprehend([])
        text = "é" * 100_000
        await ComprehendEntityDetector(client=client).detect(text, ["name"])
        assert isinstance(client.last_kwargs["Text"], str)

    @pytest.mark.asyncio
    async def test_a_client_error_propagates(self):
        class Boom:
            def detect_pii_entities(self, **kw):
                raise RuntimeError("throttled")

        # Raising rather than returning [] is deliberate: [] is
        # indistinguishable from "no names here", and the caller must be able to
        # tell an outage from a clean result.
        with pytest.raises(RuntimeError):
            await ComprehendEntityDetector(client=Boom()).detect("Alice", ["name"])


class TestBuildDetector:
    def test_returns_a_detector_when_boto3_is_available(self):
        # boto3 is a hard dependency of the gateway, so this is the real path.
        assert build_entity_detector() is not None

    def test_returns_none_when_boto3_is_missing(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("no boto3")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # None disables NER, which is correct for a deploy that cannot call
        # Comprehend: regex redaction keeps working.
        assert build_entity_detector() is None
