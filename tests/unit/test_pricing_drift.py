"""Tests for the pricing-coverage audit and its page.

The audit is what makes a $0.00 bill visible, so the properties that matter are
(a) it agrees with what CostTracker can actually price, and (b) its rename
suggestions are conservative — a bad suggestion ends in an operator pasting the
wrong rate, which bills silently and looks deliberate.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.pricing_drift import (
    _family,
    _suggest_rename,
    audit_pricing,
    build_yaml_skeleton,
    format_startup_notice,
    render_drift_page,
)
from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ModelConfig, ProviderModelMapping, TokenPricing


# ── Helpers ──────────────────────────────────────────────────────────


def _registry(*models: ModelConfig) -> ModelRegistry:
    registry = ModelRegistry()
    registry.models = {m.name: m for m in models}
    return registry


def _model(name: str, *providers: tuple[str, str]) -> ModelConfig:
    return ModelConfig(
        name=name,
        description=name,
        providers=[ProviderModelMapping(provider=p, model_id=m) for p, m in providers],
    )


# ── Coverage accounting ──────────────────────────────────────────────


class TestCoverageAccounting:
    def test_fully_priced_registry_reports_no_drift(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        report = audit_pricing(registry, pricing)

        assert report.has_drift is False
        assert report.total_mappings == 1
        assert report.priced_mappings == 1
        assert report.coverage_pct == 100.0
        assert report.unpriced == []
        assert report.orphans == []

    def test_missing_entry_is_reported_unpriced(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        report = audit_pricing(registry, {})

        assert report.has_drift is True
        assert report.priced_mappings == 0
        assert report.coverage_pct == 0.0
        assert [(u.provider, u.model_id) for u in report.unpriced] == [("openai", "gpt-4")]

    def test_lookup_uses_provider_side_model_id_not_gateway_name(self):
        """The gateway name is not the key — pricing it does not price the model.

        This is the drift that is easiest to introduce by hand: pricing.yaml
        indexed by the friendly name looks right and prices nothing, because
        CostTracker bills on the id sent to the provider.
        """
        registry = _registry(_model("claude-sonnet", ("bedrock", "us.anthropic.claude-v2")))
        pricing = {"bedrock": {"claude-sonnet": TokenPricing(0.003, 0.015)}}

        report = audit_pricing(registry, pricing)

        assert len(report.unpriced) == 1
        assert report.unpriced[0].model_id == "us.anthropic.claude-v2"
        # And the friendly name is flagged as reaching nothing.
        assert [(o.provider, o.model_id) for o in report.orphans] == [
            ("bedrock", "claude-sonnet")
        ]

    def test_inline_pricing_in_models_yaml_counts_as_priced(self):
        model = ModelConfig(
            name="custom",
            description="custom",
            providers=[
                ProviderModelMapping(
                    provider="openai",
                    model_id="gpt-4",
                    pricing=TokenPricing(0.01, 0.02),
                )
            ],
        )

        report = audit_pricing(_registry(model), {})

        assert report.unpriced == []
        assert report.priced_mappings == 1

    def test_partially_priced_model_is_not_fully_unpriced(self):
        registry = _registry(
            _model("claude", ("anthropic", "claude-x"), ("bedrock", "bedrock-claude-x"))
        )
        pricing = {"anthropic": {"claude-x": TokenPricing(0.003, 0.015)}}

        report = audit_pricing(registry, pricing)

        assert len(report.unpriced) == 1
        assert report.models_fully_unpriced == []
        assert report.priced_mappings == 1
        assert report.total_mappings == 2

    def test_model_with_no_priced_provider_is_fully_unpriced(self):
        registry = _registry(
            _model("claude", ("anthropic", "claude-x"), ("bedrock", "bedrock-claude-x"))
        )

        report = audit_pricing(registry, {})

        assert report.models_fully_unpriced == ["claude"]

    def test_provider_absent_from_pricing_is_called_out_separately(self):
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4")),
            _model("grok", ("xai", "grok-3")),
        )
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        report = audit_pricing(registry, pricing)

        assert report.providers_missing_section == ["xai"]

    def test_empty_provider_section_counts_as_missing(self):
        """A present-but-empty block prices nothing, so it is the same failure.

        The distinction matters when pricing.yaml is edited by hand: an operator
        who adds `xai:` and stops has done nothing, and the page should say so
        rather than list the mappings with no explanation of why.
        """
        registry = _registry(_model("grok", ("xai", "grok-3")))

        report = audit_pricing(registry, {"xai": {}})

        assert report.providers_missing_section == ["xai"]

    def test_provider_with_some_prices_is_not_missing_a_section(self):
        registry = _registry(_model("gpt", ("openai", "gpt-4"), ("openai", "gpt-9")))
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        report = audit_pricing(registry, pricing)

        assert report.providers_missing_section == []
        assert len(report.unpriced) == 1

    def test_empty_registry_reports_full_coverage(self):
        report = audit_pricing(_registry(), {})

        assert report.has_drift is False
        assert report.coverage_pct == 100.0
        assert report.total_mappings == 0

    def test_orphan_alone_is_drift_but_not_a_billing_gap(self):
        """A leftover price is drift; it is not a mis-billing.

        The distinction is what lets the startup banner clear once the operator
        has priced everything. Treating an unused entry as equally urgent gives a
        warning that survives its own fix, which is the failure mode this whole
        report exists to avoid.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {
            "openai": {
                "gpt-4": TokenPricing(0.03, 0.06),
                "gpt-3.5-turbo": TokenPricing(0.0015, 0.002),
            }
        }

        report = audit_pricing(registry, pricing)

        assert report.unpriced == []
        assert report.has_drift is True
        assert report.has_billing_gap is False
        assert [o.model_id for o in report.orphans] == ["gpt-3.5-turbo"]


class TestAuditAgreesWithCostTracker:
    """The audit's verdict has to match what the biller can actually find."""

    @pytest.mark.parametrize(
        "pricing_key,expect_priced",
        [
            ("us.anthropic.claude-v2", True),
            ("claude-sonnet", False),  # gateway name — CostTracker never looks here
        ],
    )
    def test_priced_means_calculate_cost_is_nonzero(self, pricing_key, expect_priced):
        registry = _registry(_model("claude-sonnet", ("bedrock", "us.anthropic.claude-v2")))
        pricing = {"bedrock": {pricing_key: TokenPricing(0.003, 0.015)}}

        report = audit_pricing(registry, pricing)
        cost = CostTracker(pricing).calculate_cost(
            provider="bedrock",
            model="us.anthropic.claude-v2",
            prompt_tokens=1000,
            completion_tokens=1000,
        )

        assert (report.priced_mappings == 1) is expect_priced
        assert (cost > 0) is expect_priced


# ── Rename suggestions ───────────────────────────────────────────────


class TestRenameSuggestions:
    def test_version_bump_within_a_family_is_suggested(self):
        registry = _registry(
            _model("mistral-large", ("bedrock", "mistral.mistral-large-2402-v1:0"))
        )
        pricing = {"bedrock": {"mistral.mistral-large-2407-v1:0": TokenPricing(0.004, 0.012)}}

        report = audit_pricing(registry, pricing)

        assert report.unpriced[0].suggestion == "mistral.mistral-large-2407-v1:0"

    @pytest.mark.parametrize(
        "model_id,orphans",
        [
            # A different Anthropic tier: haiku is ~15x cheaper than opus, so
            # reusing the price would overcharge by an order of magnitude.
            ("claude-haiku-4-5-20251001", ["claude-opus-4-20250514"]),
            # Different OpenAI generations. The trailing number IS the family.
            ("gpt-4.1", ["gpt-4"]),
            ("gpt-5.5-pro", ["gpt-3.5-turbo"]),
            ("o3", ["o1"]),
            # Same parameter count, different modality (vl = vision-language).
            ("qwen.qwen3-vl-235b-a22b", ["qwen.qwen3-235b-a22b-2507-v1:0"]),
            # Sibling tiers under Mantle.
            ("us.anthropic.claude-opus-4-6-v1", ["us.anthropic.claude-sonnet-4-6-v1"]),
        ],
    )
    def test_different_model_is_never_suggested(self, model_id, orphans):
        assert _suggest_rename(model_id, orphans) is None

    def test_no_suggestion_when_the_family_is_ambiguous(self):
        """Two orphans in one family give no basis for choosing between them."""
        candidates = [
            "mistral.mistral-large-2402-v1:0",
            "mistral.mistral-large-2411-v1:0",
        ]

        assert _suggest_rename("mistral.mistral-large-2407-v1:0", candidates) is None

    def test_suggestions_do_not_cross_providers(self):
        """The same id under two providers is legitimate and priced differently."""
        registry = _registry(_model("mistral", ("bedrock", "mistral-large-2402")))
        pricing = {"openai": {"mistral-large-2407": TokenPricing(0.004, 0.012)}}

        report = audit_pricing(registry, pricing)

        assert report.unpriced[0].suggestion is None

    def test_numeric_only_ids_do_not_match_each_other(self):
        """Stripping digits from "2024" leaves nothing to compare."""
        assert _suggest_rename("2024", ["2025"]) is None

    @pytest.mark.parametrize(
        "left,right,same",
        [
            ("mistral.mistral-large-2402-v1:0", "mistral.mistral-large-2407-v1:0", True),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20240307", True),
            ("claude-haiku-4-5-20251001", "claude-opus-4-20250514", False),
            ("gpt-4.1", "gpt-4", False),
        ],
    )
    def test_family_grouping(self, left, right, same):
        assert (_family(left) == _family(right)) is same


# ── YAML skeleton ────────────────────────────────────────────────────


class TestYamlSkeleton:
    def test_skeleton_is_valid_yaml_shaped_like_the_pricing_file(self):
        import yaml

        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4")),
            _model("grok", ("xai", "grok-3")),
        )
        report = audit_pricing(registry, {})

        parsed = yaml.safe_load(build_yaml_skeleton(report))

        assert set(parsed["providers"]) == {"openai", "xai"}
        assert parsed["providers"]["openai"]["gpt-4"]["prompt_token_cost"] == 0.0
        assert parsed["providers"]["xai"]["grok-3"]["completion_token_cost"] == 0.0

    def test_zero_placeholders_remain_visibly_unpriced(self, tmp_path):
        """A pasted TODO must not turn a red finding green before it is priced."""
        from src.gateway.config_loader import load_pricing_config

        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4")),
            _model("grok", ("xai", "grok-3")),
        )
        path = tmp_path / "pricing.yaml"
        path.write_text(build_yaml_skeleton(audit_pricing(registry, {})), encoding="utf-8")

        report = audit_pricing(registry, load_pricing_config(str(path)))

        assert len(report.unpriced) == 2
        assert report.priced_mappings == 0

    def test_non_finite_or_negative_rates_are_not_priced(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        for pricing in (
            TokenPricing(float("nan"), 0.01),
            TokenPricing(-0.01, 0.01),
            TokenPricing(0.0, 0.0),
        ):
            report = audit_pricing(
                registry,
                {"openai": {"gpt-4": pricing}},
            )
            assert report.priced_mappings == 0
            assert len(report.unpriced) == 1

    def test_duplicate_model_ids_appear_once(self):
        """Two gateway models can share a provider id; YAML keys cannot repeat."""
        registry = _registry(
            _model("fast", ("openai", "gpt-4")),
            _model("slow", ("openai", "gpt-4")),
        )

        skeleton = build_yaml_skeleton(audit_pricing(registry, {}))

        assert skeleton.count("    gpt-4:") == 1

    def test_no_skeleton_when_nothing_is_unpriced(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        assert build_yaml_skeleton(audit_pricing(registry, pricing)) == ""


# ── Startup notice ───────────────────────────────────────────────────


class TestStartupNotice:
    def test_clean_config_prints_nothing(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        report = audit_pricing(registry, pricing)

        assert format_startup_notice(report, "http://x/admin/pricing-drift") is None

    def test_notice_names_the_url_and_the_count(self):
        registry = _registry(_model("grok", ("xai", "grok-3")))

        notice = format_startup_notice(
            audit_pricing(registry, {}), "http://localhost:8000/admin/pricing-drift"
        )

        assert notice is not None
        assert "1 of 1" in notice
        assert "http://localhost:8000/admin/pricing-drift" in notice
        assert "xai" in notice
        # No blank-line artifacts from conditional sections.
        assert "\n\n\n" not in notice

    def test_notice_clears_once_every_mapping_is_priced(self):
        """Leftover entries must not keep the banner alive after the fix.

        A warning that still fires when the operator has priced everything it
        asked for reads as broken, and the next one gets ignored. Regression
        guard: gating on ``has_drift`` here printed "PRICING GAP: 0 of 48
        mappings have no price" forever.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {
            "openai": {
                "gpt-4": TokenPricing(0.03, 0.06),
                "gpt-3.5-turbo": TokenPricing(0.0015, 0.002),
            }
        }

        report = audit_pricing(registry, pricing)

        assert report.has_drift is True  # the orphan is still reported...
        assert format_startup_notice(report, "http://x") is None  # ...but quietly

    def test_notice_never_claims_zero_mappings_are_unpriced(self):
        """The count in the banner is the reason the banner exists."""
        registry = _registry(_model("grok", ("xai", "grok-3")))

        notice = format_startup_notice(audit_pricing(registry, {}), "http://x")

        assert notice is not None
        assert "0 of" not in notice


# ── Page rendering ───────────────────────────────────────────────────


class TestPageRendering:
    def test_drift_page_lists_every_unpriced_mapping(self):
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4")),
            _model("grok", ("xai", "grok-3")),
        )

        html = render_drift_page(audit_pricing(registry, {}), "config/pricing.yaml")

        assert "2 of 2 provider mappings have no price" in html
        assert "gpt-4" in html
        assert "grok-3" in html
        assert "config/pricing.yaml" in html
        assert "TODO per 1K tokens" in html

    def test_clean_page_says_so_without_a_warning(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        html = render_drift_page(audit_pricing(registry, pricing), "config/pricing.yaml")

        assert "banner ok" in html
        assert "banner warn" not in html
        assert "have no price" not in html

    def test_production_page_says_unpriced_mappings_are_blocked(self):
        registry = _registry(_model("grok", ("xai", "grok-3")))

        html = render_drift_page(
            audit_pricing(registry, {}),
            "config/pricing.yaml",
            unpriced_mappings_blocked=True,
        )

        assert "Production routing excludes these mappings" in html
        assert "configured model is unavailable" in html
        assert "requests routed to these are recorded" not in html

    def test_fully_priced_with_leftovers_is_the_healthy_banner(self):
        """Everything billing correctly is not a warning, even with stale rows."""
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {
            "openai": {
                "gpt-4": TokenPricing(0.03, 0.06),
                "gpt-3.5-turbo": TokenPricing(0.0015, 0.002),
            }
        }

        html = render_drift_page(audit_pricing(registry, pricing), "config/pricing.yaml")

        assert "banner ok" in html
        assert "banner warn" not in html
        assert "0 of 1" not in html
        # The leftover is still listed, just not escalated.
        assert "safe to delete" in html
        assert "gpt-3.5-turbo" in html

    def test_model_ids_are_html_escaped(self):
        """Model ids come from a config file, which is not necessarily trusted.

        An operator pasting a catalog entry should not be able to inject markup
        into an admin page.
        """
        registry = _registry(_model("evil", ("openai", "<script>alert(1)</script>")))

        html = render_drift_page(audit_pricing(registry, {}), "config/pricing.yaml")

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_pricing_path_is_escaped(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        html = render_drift_page(audit_pricing(registry, {}), "<b>path</b>")

        assert "<b>path</b>" not in html
        assert "&lt;b&gt;path&lt;/b&gt;" in html


class TestDriftRoute:
    @pytest.fixture
    def api(self):
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4")),
            _model("grok", ("xai", "grok-3")),
        )
        return AdminAPI(
            cost_tracker=CostTracker({"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}),
            health_tracker=ProviderHealthTracker(),
            model_registry=registry,
            pricing_path="config/pricing.yaml",
        )

    @pytest.fixture
    def client(self, api):
        return TestClient(Starlette(routes=create_admin_routes(api)))

    def test_route_serves_html(self, client):
        resp = client.get("/admin/pricing-drift")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "1 of 2 provider mappings have no price" in resp.text
        assert "grok-3" in resp.text

    def test_route_reads_the_table_the_cost_tracker_bills_from(self, api, client):
        """The report is rebuilt per request off the live table, not cached.

        Which means it also cannot disagree with the biller: there is one dict,
        and both read it.
        """
        assert "1 of 2 provider mappings have no price" in client.get(
            "/admin/pricing-drift"
        ).text

        api.cost_tracker.pricing_config["xai"] = {"grok-3": TokenPricing(0.003, 0.015)}

        resp = client.get("/admin/pricing-drift")
        assert "have no price" not in resp.text
        assert "banner ok" in resp.text


class TestShippedPricingCoverage:
    """Assertions about the real config/pricing.yaml, not a fixture.

    Every test above builds its own registry and table, which is right for the
    audit logic but says nothing about the files that ship. These pin the
    coverage of the actual configs, because an unpriced mapping is invisible at
    runtime: CostTracker returns 0.00 on a miss rather than raising, so the
    request succeeds, the bill reads zero, and project spend, budget blocks and
    quota alerts all under-count.
    """

    @pytest.fixture
    def report(self):
        from src.gateway.config_loader import load_pricing_config

        registry = ModelRegistry()
        registry.load("config/models.yaml")
        return audit_pricing(registry, load_pricing_config("config/pricing.yaml"))

    def test_every_referenced_provider_has_a_pricing_section(self, report):
        """A provider with no section at all prices *none* of its models.

        This is how fireworks came to bill nothing on both its mappings: the
        section was omitted on the belief that Fireworks published no per-model
        rates, which was wrong. One missing block is a wider hole than one
        missing key, so it gets its own assertion.
        """
        assert report.providers_missing_section == []

    def test_the_fireworks_rates_are_the_published_ones(self):
        """Pinned to the numbers on fireworks.ai/models, per 1k tokens.

        Not a tautology against the YAML: these two mappings shipped unpriced,
        and the failure mode is silent. If a future edit drops the section or a
        rate, this says so instead of the bill quietly returning to $0.00.
        """
        from src.gateway.config_loader import load_pricing_config

        fireworks = load_pricing_config("config/pricing.yaml")["fireworks"]

        deepseek = fireworks["accounts/fireworks/models/deepseek-v4-pro"]
        assert (deepseek.prompt_token_cost, deepseek.completion_token_cost) == (
            0.00174,
            0.00348,
        )

        oss = fireworks["accounts/fireworks/models/gpt-oss-120b"]
        assert (oss.prompt_token_cost, oss.completion_token_cost) == (0.00015, 0.0006)

    def test_a_fireworks_request_no_longer_bills_zero(self):
        """The consequence, asserted through the biller rather than the table.

        A rate that loads but is not reachable by the key CostTracker looks up
        is the same outcome as no rate at all -- the provider-side model_id from
        models.yaml is the key, not the gateway's friendly name.
        """
        from src.gateway.config_loader import load_pricing_config

        tracker = CostTracker(load_pricing_config("config/pricing.yaml"))

        for model_id in (
            "accounts/fireworks/models/deepseek-v4-pro",
            "accounts/fireworks/models/gpt-oss-120b",
        ):
            cost = tracker.calculate_cost("fireworks", model_id, 3000, 800)
            assert cost > 0, f"{model_id} still bills $0.00"

    def test_the_remaining_gaps_are_the_documented_ones(self, report):
        """Not a coverage floor but an exact set, so a *new* unpriced mapping
        fails even though the count happens to stay the same.

        These five Bedrock Mantle GPT-5.x SKUs have no verified commercial
        us-east-1 rate: 5.5 and 5.6-sol are absent from the AWS Price List in
        every region, while 5.4, 5.6-luna, and 5.6-terra are listed only in
        us-gov-east-1 / us-gov-west-1 at GovCloud rates. Filling any of them
        would replace a visible gap with an invisible wrong bill. Production
        must keep these mappings unavailable until an operator supplies a
        verified contract rate.
        """
        assert {(u.provider, u.model_id) for u in report.unpriced} == {
            ("bedrock-mantle", "openai.gpt-5.4"),
            ("bedrock-mantle", "openai.gpt-5.5"),
            ("bedrock-mantle", "openai.gpt-5.6-luna"),
            ("bedrock-mantle", "openai.gpt-5.6-sol"),
            ("bedrock-mantle", "openai.gpt-5.6-terra"),
        }
