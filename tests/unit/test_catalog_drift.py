"""Tests for the catalogue-coverage audit and its page.

Three properties matter, in descending order of consequence.

The first is that the audit's notion of "declared" matches the serving code's.
``list_models`` matches a usage record by logical name *or* by any provider-side
``model_id``, so an audit that only knew logical names would report routine
traffic as undeclared — and undeclared traffic is the one finding on this page
that is a governance issue rather than a documentation chore. A false positive
there is worse than no page at all, because it trains an operator to dismiss the
only alarm worth reading.

The second is that "described" means what the dashboard can actually describe:
the catalog is keyed by provider then provider-side id, so matching on the
logical name would count coverage the model picker does not have.

The third is that a malformed catalog does not raise. It is YAML an operator
edits by hand, and a report that dies on a typo is a report nobody runs.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.catalog_drift import (
    audit_catalog,
    build_catalog_skeleton,
    render_catalog_drift_page,
)
from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ModelConfig, ProviderModelMapping


# ── Helpers ──────────────────────────────────────────────────────────


def _registry(*models: ModelConfig) -> ModelRegistry:
    registry = ModelRegistry()
    registry.models = {m.name: m for m in models}
    return registry


def _model(
    name: str, *providers: tuple[str, str], capabilities: list[str] | None = None
) -> ModelConfig:
    return ModelConfig(
        name=name,
        description=name,
        providers=[ProviderModelMapping(provider=p, model_id=m) for p, m in providers],
        capabilities=capabilities or [],
    )


def _catalog(**providers: list[dict]) -> dict:
    return {p: {"models": entries} for p, entries in providers.items()}


@dataclass(frozen=True)
class _Usage:
    """The two fields the audit reads, per the _UsageLike protocol.

    Deliberately not a UsageRecord: the audit declares a structural dependency on
    exactly two attributes, and a test that constructed full records would pass
    even if the audit had quietly started reading a third.
    """

    model: str
    provider: str


# ── The declared set ─────────────────────────────────────────────────


class TestWhatCountsAsDeclared:
    """The join that has to agree with ``list_models``, or the page cries wolf."""

    def test_traffic_naming_the_provider_side_id_is_not_undeclared(self):
        """The false positive that would matter most.

        Records carry whatever the caller asked for, which is routinely the
        provider-side id rather than the logical name. Flagging those would put
        real traffic under a "shadow AI" heading on day one.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {}, [_Usage("gpt-4-turbo", "openai")])

        assert report.undeclared == []
        assert report.has_undeclared_traffic is False

    def test_traffic_naming_the_logical_name_is_not_undeclared(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {}, [_Usage("gpt-4", "openai")])

        assert report.undeclared == []

    def test_a_known_model_on_an_unmapped_provider_is_undeclared(self):
        """The likelier misconfiguration than a wholly unknown model.

        Matching on name alone would pass this: the name is declared, the route
        that served it is not. Something reached xai without a mapping saying it
        could.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {}, [_Usage("gpt-4", "xai")])

        assert [(u.model, u.provider) for u in report.undeclared] == [("gpt-4", "xai")]
        assert report.has_undeclared_traffic is True

    def test_an_unknown_model_is_undeclared(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {}, [_Usage("mystery-v2", "openai")])

        assert [u.model for u in report.undeclared] == ["mystery-v2"]

    def test_undeclared_requests_are_counted_not_just_listed(self):
        """One row per path, with a volume. Three requests on an undeclared route
        is a different conversation from one."""
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        usage = [_Usage("mystery-v2", "xai")] * 3 + [_Usage("other", "xai")]

        report = audit_catalog(registry, {}, usage)

        assert {(u.model, u.requests) for u in report.undeclared} == {
            ("mystery-v2", 3),
            ("other", 1),
        }


# ── Coverage accounting ──────────────────────────────────────────────


class TestCoverageAccounting:
    def test_a_fully_described_registry_reports_no_drift(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(openai=[{"model_id": "gpt-4-turbo", "name": "GPT-4 Turbo"}])

        report = audit_catalog(registry, catalog)

        assert report.has_drift is False
        assert report.total_mappings == 1
        assert report.described_mappings == 1
        assert report.coverage_pct == 100.0
        assert report.undescribed == []
        assert report.unroutable == []

    def test_a_missing_entry_is_reported_undescribed(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {})

        assert report.has_drift is True
        assert report.described_mappings == 0
        assert report.coverage_pct == 0.0
        assert [e.model_id for e in report.undescribed] == ["gpt-4-turbo"]

    def test_the_catalog_is_matched_on_provider_side_id_not_logical_name(self):
        """A catalog keyed by the logical name describes nothing.

        /admin/catalog looks up provider then model_id; counting a logical-name
        match as coverage would report metadata the model picker cannot find.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(openai=[{"model_id": "gpt-4", "name": "GPT-4"}])

        report = audit_catalog(registry, catalog)

        assert report.described_mappings == 0
        assert [e.model_id for e in report.undescribed] == ["gpt-4-turbo"]
        # And the entry that did not match is itself unreachable.
        assert [e.model_id for e in report.unroutable] == ["gpt-4"]

    def test_the_right_provider_section_is_consulted(self):
        """An id present under the wrong provider is not coverage."""
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(azure=[{"model_id": "gpt-4-turbo", "name": "GPT-4"}])

        report = audit_catalog(registry, catalog)

        assert report.described_mappings == 0
        assert report.providers_missing_section == ["openai"]

    def test_each_mapping_counts_separately_for_a_multi_provider_model(self):
        """Coverage is per mapping, not per model: one model described on one of
        two providers is half-routable, and rounding it up to "described" hides
        the failover path that has no metadata."""
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4-turbo"), ("azure", "gpt-4-turbo"))
        )
        catalog = _catalog(openai=[{"model_id": "gpt-4-turbo", "name": "GPT-4"}])

        report = audit_catalog(registry, catalog)

        assert report.total_models == 1
        assert report.total_mappings == 2
        assert report.described_mappings == 1
        assert report.coverage_pct == 50.0

    def test_a_missing_provider_section_is_flagged_apart_from_the_mapping(self):
        """The fix differs — a whole provider block versus one model — and the
        page says which."""
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4-turbo")),
            _model("llama", ("groq", "llama-3-70b")),
        )
        catalog = _catalog(openai=[{"model_id": "other", "name": "Other"}])

        report = audit_catalog(registry, catalog)

        assert report.providers_missing_section == ["groq"]
        by_provider = {e.provider: e.provider_section_missing for e in report.undescribed}
        assert by_provider == {"openai": False, "groq": True}

    def test_an_empty_registry_is_full_coverage_not_a_zero_division(self):
        report = audit_catalog(_registry(), {})

        assert report.coverage_pct == 100.0
        assert report.has_drift is False

    def test_models_declaring_no_capabilities_are_named(self):
        """The field /admin/models returns, where empty reads as "no" rather than
        "unknown"."""
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4-turbo"), capabilities=["chat"]),
            _model("llama", ("groq", "llama-3-70b")),
        )

        report = audit_catalog(registry, {})

        assert report.models_without_capabilities == ["llama"]


# ── Unroutable catalog entries ───────────────────────────────────────


class TestUnroutableEntries:
    def test_an_entry_no_mapping_reaches_is_unroutable(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(
            openai=[
                {"model_id": "gpt-4-turbo", "name": "GPT-4 Turbo"},
                {"model_id": "gpt-3.5-old", "name": "GPT-3.5 (retired)"},
            ]
        )

        report = audit_catalog(registry, catalog)

        assert report.described_mappings == 1
        assert [(e.provider, e.model_id, e.display_name) for e in report.unroutable] == [
            ("openai", "gpt-3.5-old", "GPT-3.5 (retired)")
        ]
        assert report.has_drift is True


# ── The traffic join ─────────────────────────────────────────────────


class TestTrafficJoin:
    def test_no_usage_is_distinct_from_zero_usage(self):
        """The distinction the counts cannot carry.

        "Nothing has run yet" and "every declared model is dormant" produce the
        same dormant count if you conflate them, and mean opposite things: the
        first is a fresh process, the second is 46 unwatched models.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        unknown = audit_catalog(registry, {}, None)
        assert unknown.observed_models is None
        assert unknown.dormant == []

        measured = audit_catalog(registry, {}, [])
        assert measured.observed_models == 0
        assert [d.model for d in measured.dormant] == ["gpt-4"]

    def test_a_model_with_traffic_is_not_dormant(self):
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4-turbo")),
            _model("llama", ("groq", "llama-3-70b")),
        )

        report = audit_catalog(registry, {}, [_Usage("gpt-4", "openai")])

        assert [d.model for d in report.dormant] == ["llama"]
        assert report.trafficked_models == 1
        assert report.observed_models == 1

    def test_traffic_under_the_provider_side_id_clears_dormancy(self):
        """Same matching rule as the undeclared check, applied in reverse.

        Diverging here would report a model as unused while its own records sit
        in the tracker.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {}, [_Usage("gpt-4-turbo", "openai")])

        assert report.dormant == []
        assert report.trafficked_models == 1

    def test_dormancy_ignores_which_provider_served_the_traffic(self):
        """A model called through an undeclared provider is both undeclared *and*
        in use. Listing it as dormant as well would contradict the row above it.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, {}, [_Usage("gpt-4", "xai")])

        assert report.dormant == []
        assert len(report.undeclared) == 1

    def test_undeclared_traffic_is_escalated_separately_from_drift(self):
        """Both true at once, reported apart.

        A docs chore and a governance finding on one alarm means the alarm still
        fires after the chore is done, which is how people learn to ignore it.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(openai=[{"model_id": "gpt-4-turbo", "name": "GPT-4"}])

        clean = audit_catalog(registry, catalog, [_Usage("gpt-4", "openai")])
        assert clean.has_drift is False
        assert clean.has_undeclared_traffic is False

        shadow = audit_catalog(registry, catalog, [_Usage("x", "xai")])
        assert shadow.has_undeclared_traffic is True
        assert shadow.undescribed == []


# ── Malformed input ──────────────────────────────────────────────────


class TestMalformedCatalog:
    """It is hand-edited YAML. A report that raises on a typo is not run."""

    @pytest.mark.parametrize(
        "catalog",
        [
            pytest.param({"openai": None}, id="null-provider-block"),
            pytest.param({"openai": {}}, id="no-models-key"),
            pytest.param({"openai": {"models": None}}, id="null-models-list"),
            pytest.param({"openai": {"models": ["oops"]}}, id="string-instead-of-dict"),
            pytest.param({"openai": {"models": [{"no_id": 1}]}}, id="entry-without-id"),
            pytest.param({"openai": {"models": [{"model_id": ""}]}}, id="empty-id"),
            pytest.param(None, id="whole-catalog-none"),
        ],
    )
    def test_it_does_not_raise(self, catalog):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        report = audit_catalog(registry, catalog, [])

        assert report.described_mappings == 0

    def test_an_entry_with_no_id_does_not_describe_a_mapping_with_no_id(self):
        """Keying a missing model_id on "" would let one malformed entry claim
        coverage for every malformed mapping."""
        registry = _registry(_model("broken", ("openai", "")))

        report = audit_catalog(registry, {"openai": {"models": [{"name": "x"}]}})

        assert report.described_mappings == 0
        assert len(report.undescribed) == 1


# ── The paste-ready skeleton ─────────────────────────────────────────


class TestSkeleton:
    def test_nothing_undescribed_yields_no_skeleton(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(openai=[{"model_id": "gpt-4-turbo", "name": "GPT-4"}])

        assert build_catalog_skeleton(audit_catalog(registry, catalog)) == ""

    def test_capabilities_are_left_as_a_todo_not_guessed(self):
        """The conservative half of this page.

        An invented ``vision`` sends a request the provider rejects; an invented
        omission hides a capability the model has. Both then look like settled
        facts wherever they are read.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        skeleton = build_catalog_skeleton(audit_catalog(registry, {}))

        assert "capabilities: []" in skeleton
        assert "TODO" in skeleton
        assert "vision" not in skeleton.replace("chat/vision/tools/streaming", "")

    def test_it_parses_as_yaml_and_nests_under_the_provider(self):
        yaml = pytest.importorskip("yaml")
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4-turbo")),
            _model("llama", ("groq", "llama-3-70b")),
        )

        parsed = yaml.safe_load(build_catalog_skeleton(audit_catalog(registry, {})))

        assert set(parsed["providers"]) == {"openai", "groq"}
        assert parsed["providers"]["openai"]["models"][0]["model_id"] == "gpt-4-turbo"

    def test_a_missing_provider_section_gets_the_block_scaffolding(self):
        """A models list under a provider key that does not exist is not
        pasteable on its own."""
        registry = _registry(_model("llama", ("groq", "llama-3-70b")))

        skeleton = build_catalog_skeleton(audit_catalog(registry, {}))

        assert "display_name:" in skeleton
        assert "auth_type:" in skeleton

    def test_a_present_provider_section_gets_only_the_models(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(openai=[{"model_id": "other", "name": "Other"}])

        skeleton = build_catalog_skeleton(audit_catalog(registry, catalog))

        assert "display_name:" not in skeleton
        assert "model_id: gpt-4-turbo" in skeleton

    def test_a_shared_model_id_is_emitted_once_per_provider(self):
        """Two logical models can map to one provider id. Emitting it twice makes
        a duplicate YAML key, which silently drops one on load."""
        registry = _registry(
            _model("fast", ("openai", "gpt-4-turbo")),
            _model("cheap", ("openai", "gpt-4-turbo")),
        )

        skeleton = build_catalog_skeleton(audit_catalog(registry, {}))

        assert skeleton.count("model_id: gpt-4-turbo") == 1


# ── The page ─────────────────────────────────────────────────────────


class TestPage:
    def test_full_coverage_renders_the_ok_banner(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        catalog = _catalog(openai=[{"model_id": "gpt-4-turbo", "name": "GPT-4"}])

        html = render_catalog_drift_page(
            audit_catalog(registry, catalog), "config/models.yaml", "config/catalog.yaml"
        )

        assert "banner ok" in html
        assert "describes every routed model" in html

    def test_undeclared_traffic_outranks_the_metadata_gap_in_the_headline(self):
        """Both findings are present; the headline is the governance one.

        A page that led with "39 mappings have no metadata" while a request was
        served by an undeclared route buries the item that is not a chore.
        """
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}, [_Usage("mystery", "xai")]),
            "config/models.yaml",
            "config/catalog.yaml",
        )

        assert "banner fail" in html
        assert "banner warn" not in html
        assert "undeclared" in html
        # Both tables still render — the headline ranks, it does not hide.
        assert "Routed but undescribed" in html

    def test_a_metadata_gap_alone_is_a_warning_not_a_failure(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}, [_Usage("gpt-4", "openai")]),
            "config/models.yaml",
            "config/catalog.yaml",
        )

        assert "banner warn" in html
        assert "banner fail" not in html

    def test_the_page_is_named_the_same_thing_everywhere(self):
        """The ribbon, the <title>, and the dashboard's sidebar label.

        Three files name this page and nothing joins them, so the standalone copy
        said "Model Inventory" while the sidebar said "Catalogue" — the same page
        reading as two.
        """
        import pathlib

        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        html = render_catalog_drift_page(
            audit_catalog(registry, {}), "config/models.yaml", "config/catalog.yaml"
        )

        assert '<span class="title">Catalogue Coverage</span>' in html
        assert "<title>AxonLLM — Catalogue Coverage</title>" in html

        shell = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src/gateway/admin/static/index.html"
        ).read_text(encoding="utf-8")
        assert 'title="Catalogue Coverage"' in shell
        assert "label: 'Catalogue'" in shell

    def test_both_config_paths_are_named_on_the_page(self):
        """The drift is fixed by editing one of two files and a finding does not
        say which, so the page names both."""
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}), "config/models.yaml", "config/catalog.yaml"
        )

        assert "config/models.yaml" in html
        assert "config/catalog.yaml" in html

    def test_the_traffic_sections_are_omitted_when_no_usage_was_supplied(self):
        """Rather than rendering an empty "0 dormant", which asserts a
        measurement nobody took."""
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}, None), "config/models.yaml", "config/catalog.yaml"
        )

        assert "Declared but dormant" not in html
        assert "No usage was supplied" in html
        # And the counter reads as unmeasured, not as zero.
        assert "<b>&mdash;</b>" in html or "<b>—</b>" in html

    def test_model_names_are_escaped(self):
        """Names come from a YAML file, which is a text field.

        This page is served under the admin auth boundary, so the threat is a
        pasted config rather than a visitor — still injection, still stored.
        """
        registry = _registry(_model("<script>alert(1)</script>", ("openai", "x")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}), "config/models.yaml", "config/catalog.yaml"
        )

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_the_config_paths_are_escaped(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}), "<b>models</b>", "<i>catalog</i>"
        )

        assert "<b>models</b>" not in html
        assert "<i>catalog</i>" not in html
        assert "&lt;b&gt;models&lt;/b&gt;" in html

    def test_undeclared_traffic_values_are_escaped(self):
        """The one table fed from request data rather than from config."""
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))

        html = render_catalog_drift_page(
            audit_catalog(registry, {}, [_Usage("<img src=x onerror=1>", "xai")]),
            "config/models.yaml",
            "config/catalog.yaml",
        )

        assert "<img src=x" not in html
        assert "&lt;img" in html


# ── The route ────────────────────────────────────────────────────────


class TestCatalogDriftRoute:
    @pytest.fixture
    def api(self):
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4-turbo")),
            _model("grok", ("xai", "grok-3")),
        )
        return AdminAPI(
            cost_tracker=CostTracker({}),
            health_tracker=ProviderHealthTracker(),
            model_registry=registry,
            catalog=_catalog(openai=[{"model_id": "gpt-4-turbo", "name": "GPT-4"}]),
            config_path="config/models.yaml",
            catalog_path="config/catalog.yaml",
        )

    @pytest.fixture
    def client(self, api):
        return TestClient(Starlette(routes=create_admin_routes(api)))

    def test_the_route_serves_html(self, client):
        resp = client.get("/admin/catalog-drift")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "grok-3" in resp.text

    def test_it_reads_the_catalog_the_dashboard_serves(self, api, client):
        """One dict, two readers. /admin/catalog and this page cannot disagree
        about what is described, because there is nothing to keep in sync."""
        assert "grok-3" in client.get("/admin/catalog-drift").text

        api._catalog["xai"] = {"models": [{"model_id": "grok-3", "name": "Grok 3"}]}

        html = client.get("/admin/catalog-drift").text
        assert "banner ok" in html
        assert "Routed but undescribed" not in html

    def test_it_joins_against_the_records_the_cost_tracker_holds(self, api, client):
        """Same live-read property on the traffic half.

        Records arrive continuously, so a cached report would report a model as
        dormant while it is being called. A fresh tracker holds ``[]``, which the
        route passes through as measured-and-empty — both models genuinely
        dormant — and one record has to move exactly one of them.
        """
        html = client.get("/admin/catalog-drift").text
        assert "2 of 2 models have served no recorded request" in html

        api.cost_tracker._records.append(
            _record(provider="openai", model="gpt-4-turbo")
        )

        html = client.get("/admin/catalog-drift").text
        assert "1 of 2 models have served no recorded request" in html
        # gpt-4 has traffic under its provider-side id; grok is the idle one.
        assert "<td>grok</td>" in html
        assert "<td>gpt-4</td>" not in html

    def test_the_route_passes_records_rather_than_none(self, client):
        """The route always has a records list, so the traffic halves always run.

        Passing None would render the "no usage was supplied" caveat forever,
        which reads as a broken page rather than as a quiet gateway.
        """
        assert "No usage was supplied" not in client.get("/admin/catalog-drift").text

    def test_the_route_is_registered(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-turbo")))
        api = AdminAPI(
            cost_tracker=CostTracker({}),
            health_tracker=ProviderHealthTracker(),
            model_registry=registry,
        )
        paths = [r.path for r in create_admin_routes(api)]

        assert "/admin/catalog-drift" in paths

    def test_embed_mode_drops_the_ribbon(self, client):
        """The dashboard frames this page in an iframe; two toolbars is the tell
        that it was bolted on."""
        assert '<div class="toolbar">' in client.get("/admin/catalog-drift").text
        embedded = client.get("/admin/catalog-drift?embed=1").text
        assert '<div class="toolbar">' not in embedded
        assert '<div class="wrap">' in embedded


def _record(*, provider: str, model: str):
    """A minimal real UsageRecord, for the route tests that go through the tracker.

    The audit only needs two fields, but the tracker holds real records, so the
    route-level tests use one — that is the seam where a field rename would
    actually break.
    """
    from datetime import datetime

    from src.gateway.models import UsageRecord

    return UsageRecord(
        request_id="r1",
        project_id="p1",
        user_id="u1",
        provider=provider,
        model=model,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost=0.0,
        timestamp=datetime(2026, 1, 1),
    )
