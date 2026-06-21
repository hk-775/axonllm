"""Unit tests for EnsembleStrategy pure helpers (src/gateway/ensemble.py)."""

import pytest

from src.gateway.ensemble import (
    DEFAULT_FALLBACK_POLICY,
    DEFAULT_QUORUM,
    PER_MEMBER_TIMEOUT_SECONDS,
    EnsembleConfigError,
    EnsembleStrategy,
)
from src.gateway.models import (
    ChatCompletionResponse,
    EnsemblePreset,
    PanelMemberResult,
    TokenUsage,
)


# --- Helpers -----------------------------------------------------------------


def _make_response(content: str, model: str = "m") -> ChatCompletionResponse:
    """Build a minimal OpenAI-compatible response carrying ``content``."""
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model=model,
        provider="test-provider",
    )


def _survivor(model: str, content: str) -> PanelMemberResult:
    """Build a succeeded PanelMemberResult with the given completion content."""
    return PanelMemberResult(
        model=model,
        status="succeeded",
        response=_make_response(content, model=model),
        cost=0.0,
    )


def _preset(**overrides) -> EnsemblePreset:
    """Build a valid preset, allowing field overrides."""
    defaults = dict(
        name="budget",
        panel=["nova-lite", "mistral", "deepseek-r1"],
        judge="claude-sonnet",
        quorum=2,
        fallback_policy="best-single",
        cost_ceiling=0.50,
        ranking_criteria="length",
    )
    defaults.update(overrides)
    return EnsemblePreset(**defaults)


# --- Module constants --------------------------------------------------------


class TestModuleConstants:
    def test_default_quorum(self):
        assert DEFAULT_QUORUM == 1
        assert EnsembleStrategy.DEFAULT_QUORUM == 1

    def test_default_fallback_policy(self):
        assert DEFAULT_FALLBACK_POLICY == "error"
        assert EnsembleStrategy.DEFAULT_FALLBACK_POLICY == "error"

    def test_per_member_timeout(self):
        assert PER_MEMBER_TIMEOUT_SECONDS == 60.0
        assert EnsembleStrategy.PER_MEMBER_TIMEOUT_SECONDS == 60.0


# --- validate_preset ---------------------------------------------------------


class TestValidatePreset:
    def test_accepts_valid_preset(self):
        # Should not raise.
        EnsembleStrategy.validate_preset(_preset())

    def test_accepts_minimal_panel_and_quorum(self):
        EnsembleStrategy.validate_preset(
            _preset(panel=["only-model"], quorum=1, fallback_policy="error")
        )

    def test_accepts_name_length_1(self):
        EnsembleStrategy.validate_preset(_preset(name="x"))

    def test_accepts_name_length_128(self):
        EnsembleStrategy.validate_preset(_preset(name="a" * 128))

    def test_accepts_panel_size_10(self):
        panel = [f"m{i}" for i in range(10)]
        EnsembleStrategy.validate_preset(_preset(panel=panel, quorum=1))

    def test_accepts_quorum_equal_panel_size(self):
        EnsembleStrategy.validate_preset(
            _preset(panel=["a", "b", "c"], quorum=3)
        )

    def test_accepts_both_fallback_policies(self):
        EnsembleStrategy.validate_preset(_preset(fallback_policy="best-single"))
        EnsembleStrategy.validate_preset(_preset(fallback_policy="error"))

    def test_rejects_empty_name(self):
        with pytest.raises(EnsembleConfigError, match="name length"):
            EnsembleStrategy.validate_preset(_preset(name=""))

    def test_rejects_name_length_129(self):
        with pytest.raises(EnsembleConfigError, match="name length"):
            EnsembleStrategy.validate_preset(_preset(name="a" * 129))

    def test_rejects_empty_panel(self):
        with pytest.raises(EnsembleConfigError, match="panel size"):
            EnsembleStrategy.validate_preset(_preset(panel=[], quorum=1))

    def test_rejects_panel_size_11(self):
        panel = [f"m{i}" for i in range(11)]
        with pytest.raises(EnsembleConfigError, match="panel size"):
            EnsembleStrategy.validate_preset(_preset(panel=panel, quorum=1))

    def test_rejects_quorum_below_1(self):
        with pytest.raises(EnsembleConfigError, match="quorum"):
            EnsembleStrategy.validate_preset(_preset(quorum=0))

    def test_rejects_quorum_above_panel_size(self):
        with pytest.raises(EnsembleConfigError, match="quorum"):
            EnsembleStrategy.validate_preset(
                _preset(panel=["a", "b"], quorum=3)
            )

    def test_rejects_bad_fallback_policy(self):
        with pytest.raises(EnsembleConfigError, match="fallback_policy"):
            EnsembleStrategy.validate_preset(_preset(fallback_policy="bogus"))

    def test_error_message_identifies_preset(self):
        with pytest.raises(EnsembleConfigError, match="myname"):
            EnsembleStrategy.validate_preset(
                _preset(name="myname", quorum=99)
            )


# --- evaluate_quorum ---------------------------------------------------------


class TestEvaluateQuorum:
    def test_above_quorum(self):
        assert EnsembleStrategy.evaluate_quorum(3, 2) is True

    def test_exactly_at_quorum(self):
        assert EnsembleStrategy.evaluate_quorum(2, 2) is True

    def test_below_quorum(self):
        assert EnsembleStrategy.evaluate_quorum(1, 2) is False

    def test_zero_survivors(self):
        assert EnsembleStrategy.evaluate_quorum(0, 1) is False


# --- estimate_cost_multiplier ------------------------------------------------


class TestEstimateCostMultiplier:
    @pytest.mark.parametrize("panel_size", [1, 2, 3, 5, 10])
    def test_multiplier_is_n_plus_one(self, panel_size):
        assert EnsembleStrategy.estimate_cost_multiplier(panel_size) == panel_size + 1

    def test_returns_float(self):
        result = EnsembleStrategy.estimate_cost_multiplier(3)
        assert isinstance(result, float)
        assert result == 4.0


# --- rank_survivors ----------------------------------------------------------


class TestRankSurvivors:
    def test_orders_by_length_descending(self):
        survivors = [
            _survivor("short", "ab"),
            _survivor("long", "abcdefgh"),
            _survivor("medium", "abcd"),
        ]
        ranked = EnsembleStrategy.rank_survivors(survivors, criteria="length")
        assert [r.model for r in ranked] == ["long", "medium", "short"]

    def test_ties_broken_by_panel_order(self):
        # All equal length → original order preserved (stable).
        survivors = [
            _survivor("first", "aaa"),
            _survivor("second", "bbb"),
            _survivor("third", "ccc"),
        ]
        ranked = EnsembleStrategy.rank_survivors(survivors, criteria="length")
        assert [r.model for r in ranked] == ["first", "second", "third"]

    def test_ranking_is_deterministic(self):
        survivors = [
            _survivor("a", "xx"),
            _survivor("b", "xxxx"),
            _survivor("c", "xx"),
            _survivor("d", "xxxx"),
        ]
        first = EnsembleStrategy.rank_survivors(survivors, criteria="length")
        second = EnsembleStrategy.rank_survivors(survivors, criteria="length")
        assert [r.model for r in first] == [r.model for r in second]
        # Equal-length pairs keep panel order; longer ones come first.
        assert [r.model for r in first] == ["b", "d", "a", "c"]

    def test_unknown_criteria_preserves_panel_order(self):
        survivors = [
            _survivor("a", "longcontent"),
            _survivor("b", "x"),
            _survivor("c", "medium"),
        ]
        ranked = EnsembleStrategy.rank_survivors(survivors, criteria="bogus")
        assert [r.model for r in ranked] == ["a", "b", "c"]

    def test_empty_survivors(self):
        assert EnsembleStrategy.rank_survivors([], criteria="length") == []

    def test_single_survivor(self):
        survivors = [_survivor("only", "content")]
        ranked = EnsembleStrategy.rank_survivors(survivors, criteria="length")
        assert [r.model for r in ranked] == ["only"]

    def test_missing_response_treated_as_empty(self):
        no_resp = PanelMemberResult(model="empty", status="succeeded", response=None)
        survivors = [no_resp, _survivor("has-content", "abc")]
        ranked = EnsembleStrategy.rank_survivors(survivors, criteria="length")
        # Non-empty content ranks above empty.
        assert [r.model for r in ranked] == ["has-content", "empty"]


# --- build_synthesis_prompt --------------------------------------------------


class TestBuildSynthesisPrompt:
    def test_returns_system_and_user_messages(self):
        survivors = [_survivor("nova-lite", "answer one")]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "What is 2+2?", criteria="length"
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_includes_four_sections(self):
        survivors = [_survivor("nova-lite", "answer one")]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "Original question", criteria="length"
        )
        user_content = messages[1]["content"]
        assert "CONSENSUS" in user_content
        assert "CONTRADICTIONS" in user_content
        assert "GAPS" in user_content
        assert "UNIQUE INSIGHTS" in user_content

    def test_includes_grounding_instruction(self):
        survivors = [_survivor("nova-lite", "answer one")]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "Original question", criteria="length"
        )
        system_content = messages[0]["content"]
        user_content = messages[1]["content"]
        # Grounding: don't introduce unsupported facts / reflect only candidate content.
        assert "Do NOT" in system_content
        assert "not supported" in system_content
        assert "only content present" in user_content

    def test_includes_original_prompt(self):
        survivors = [_survivor("nova-lite", "answer one")]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "UNIQUE_ORIGINAL_PROMPT_MARKER", criteria="length"
        )
        assert "UNIQUE_ORIGINAL_PROMPT_MARKER" in messages[1]["content"]

    def test_labels_each_survivor_with_model_and_content(self):
        survivors = [
            _survivor("nova-lite", "first response text"),
            _survivor("mistral", "second response text"),
        ]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "Original", criteria="length"
        )
        user_content = messages[1]["content"]
        # Both models are labeled and their content included.
        assert "nova-lite" in user_content
        assert "mistral" in user_content
        assert "first response text" in user_content
        assert "second response text" in user_content
        assert "[Response 1" in user_content
        assert "[Response 2" in user_content

    def test_excludes_failed_members(self):
        # Only survivors are passed in; a failed member's content must not appear.
        survivors = [_survivor("nova-lite", "survivor content")]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "Original", criteria="length"
        )
        user_content = messages[1]["content"]
        assert "survivor content" in user_content
        assert "FAILED_MEMBER_CONTENT" not in user_content

    def test_survivors_labeled_in_ranked_order(self):
        # Longer content ranks first → labeled Response 1.
        survivors = [
            _survivor("short-model", "ab"),
            _survivor("long-model", "abcdefghij"),
        ]
        messages = EnsembleStrategy.build_synthesis_prompt(
            survivors, "Original", criteria="length"
        )
        user_content = messages[1]["content"]
        response1_idx = user_content.index("[Response 1")
        response2_idx = user_content.index("[Response 2")
        long_idx = user_content.index("long-model")
        short_idx = user_content.index("short-model")
        # long-model appears in the Response 1 block, short-model in Response 2.
        assert response1_idx < long_idx < response2_idx
        assert response2_idx < short_idx
