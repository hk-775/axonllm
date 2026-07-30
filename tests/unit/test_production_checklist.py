"""Tests for the production-readiness checklist and its page.

Every check on the checklist covers a state the gateway runs in without
complaint, so the properties that matter are about *honesty*:

* a check that could not run reports UNKNOWN, never PASS — collapsing those is
  how an expired credential renders as a green checklist;
* the whole page is off in demo mode, where every check would fail correctly and
  none of it would mean anything;
* nothing here can block a boot or a request.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.model_availability import (
    AvailabilityReport,
    ProviderCheckError,
    UnlistedModel,
)
from src.gateway.admin.pricing_drift import audit_pricing
from src.gateway.admin.production_checklist import (
    Status,
    check_auth_mode,
    check_demo_data,
    check_model_ids,
    check_persistence,
    check_pricing,
    check_provider_credentials,
    format_startup_notice,
    render_checklist_page,
    run_checklist,
    should_run,
)
from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.config import AppConfig
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ModelConfig, ProviderModelMapping, TokenPricing
from src.gateway.provider_config import ProviderConfig


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


def _config(provider: str) -> ProviderConfig:
    return ProviderConfig(
        provider_name=provider,
        base_url=f"https://api.{provider}.test",
        auth_type="api_key",
        credentials={"api_key": "test-key"},
    )


def _stub_availability(monkeypatch, report: AvailabilityReport) -> None:
    """Make the live provider check return *report* without any outbound call.

    Used for the all-clear cases. Disabling the check instead would leave
    ``model_ids`` at UNKNOWN — correctly, since a check that did not run has not
    passed — so a fully-passing checklist is only reachable by having the check
    actually succeed.
    """

    async def _fake(*args, **kwargs):
        return report

    monkeypatch.setattr(
        "src.gateway.admin.production_checklist.check_model_availability", _fake
    )


class _FakePersistence:
    """Stands in for DynamoPersistence, whose writes fail silently by design."""

    def __init__(self, enabled=True, reachable=True, last_write_error=None, raises=False):
        self.enabled = enabled
        self._reachable = reachable
        self._last_write_error = last_write_error
        self._raises = raises

    async def health_status(self) -> dict:
        if self._raises:
            raise RuntimeError("boom")
        return {
            "enabled": self.enabled,
            "table": "axonllm-state",
            "reachable": self._reachable,
            "last_write_error": self._last_write_error,
        }


# ── Demo gating ──────────────────────────────────────────────────────


class TestDemoGating:
    """The checklist is a production instrument, and says so in demo mode."""

    def test_demo_mode_does_not_run(self):
        assert should_run(AppConfig(load_demo_data=True)) is False

    def test_production_runs(self):
        assert should_run(AppConfig(load_demo_data=False)) is True

    @pytest.mark.asyncio
    async def test_demo_report_is_marked_not_run_rather_than_passing(self):
        """The distinction the whole gate rests on.

        An empty report and a passing report render identically unless the
        did-not-run case is explicit, and "no checks ran" must not look like
        "everything is fine".
        """
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=True),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={},
            provider_configs={},
            environ={},
        )

        assert report.did_not_run is True
        assert report.checks == []

    @pytest.mark.asyncio
    async def test_demo_page_explains_itself(self):
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=True),
            model_registry=_registry(),
            pricing_config={},
            provider_configs={},
            environ={},
        )
        html = render_checklist_page(report)

        assert "demo mode" in html
        assert "banner fail" not in html
        assert "checks pass" not in html

    @pytest.mark.asyncio
    async def test_demo_mode_makes_no_outbound_calls(self, monkeypatch):
        """A demo must not reach out to providers, credentials or not."""

        async def _boom(*args, **kwargs):
            raise AssertionError("demo mode should not call providers")

        monkeypatch.setattr(
            "src.gateway.admin.production_checklist.check_model_availability", _boom
        )

        report = await run_checklist(
            app_config=AppConfig(load_demo_data=True),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={},
            provider_configs={"openai": _config("openai")},
            environ={"AXON_CHECK_MODEL_AVAILABILITY": "true"},
        )

        assert report.did_not_run is True


# ── Auth mode ────────────────────────────────────────────────────────


class TestAuthMode:
    def test_enforce_passes(self):
        result = check_auth_mode(AppConfig(auth_mode="ENFORCE"))
        assert result.status is Status.PASS

    def test_log_only_fails(self):
        """The one unambiguous production failure: served, and logged as denied."""
        result = check_auth_mode(AppConfig(auth_mode="LOG_ONLY"))

        assert result.status is Status.FAIL
        assert result.blocking is True
        assert "LOG_ONLY" in result.summary


# ── Demo data ────────────────────────────────────────────────────────


class TestDemoDataCheck:
    def test_explicit_false_passes(self):
        result = check_demo_data(
            AppConfig(load_demo_data=False), {"AXON_LOAD_DEMO_DATA": "false"}
        )
        assert result.status is Status.PASS

    def test_unset_warns_because_the_entrypoint_defaults_it_on(self):
        """serve_dashboard.py — the Dockerfile CMD — defaults this to true.

        So "unset" is not the same as "off": the same image started the ordinary
        way would seed fabricated spend into a real dashboard.
        """
        result = check_demo_data(AppConfig(load_demo_data=False), {})

        assert result.status is Status.WARN
        assert "unset" in result.summary


# ── Pricing ──────────────────────────────────────────────────────────


class TestPricingCheck:
    def test_full_coverage_passes(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}

        result = check_pricing(audit_pricing(registry, pricing))

        assert result.status is Status.PASS
        assert result.detail == ""

    def test_model_with_no_priced_provider_fails(self):
        """$0.00 outright, so a budget cap on that model can never trigger."""
        registry = _registry(_model("grok", ("xai", "grok-3")))

        result = check_pricing(audit_pricing(registry, {}))

        assert result.status is Status.FAIL
        assert "$0.00" in result.detail
        assert ("xai / grok-3", "grok") in result.rows

    def test_partially_priced_model_only_warns(self):
        """One priced provider still bills, so this is survivable, not broken."""
        registry = _registry(_model("claude", ("anthropic", "c-1"), ("bedrock", "c-2")))
        pricing = {"anthropic": {"c-1": TokenPricing(0.003, 0.015)}}

        result = check_pricing(audit_pricing(registry, pricing))

        assert result.status is Status.WARN
        assert result.blocking is False

    def test_orphans_alone_are_not_a_warning(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        pricing = {
            "openai": {
                "gpt-4": TokenPricing(0.03, 0.06),
                "gpt-3.5-turbo": TokenPricing(0.0015, 0.002),
            }
        }

        result = check_pricing(audit_pricing(registry, pricing))

        assert result.status is Status.PASS
        assert "unused" in result.summary


# ── Model ids ────────────────────────────────────────────────────────


class TestModelIdCheck:
    def test_absent_report_is_unknown_not_pass(self):
        """A check that did not run has not passed."""
        result = check_model_ids(None)

        assert result.status is Status.UNKNOWN
        assert "AXON_CHECK_MODEL_AVAILABILITY" in result.fix

    def test_no_provider_readable_is_unknown(self):
        """Nothing was verified, so nothing may be claimed.

        This is the expired-credential case: without UNKNOWN it renders as a
        clean report built from zero successful checks.
        """
        report = AvailabilityReport(
            errors=[ProviderCheckError("openai", "HTTP 401")],
        )

        result = check_model_ids(report)

        assert result.status is Status.UNKNOWN
        assert ("openai", "HTTP 401") in result.rows

    def test_all_listed_passes_and_states_its_coverage(self):
        report = AvailabilityReport(
            checked_providers=["openai"],
            total_checked=2,
        )

        result = check_model_ids(report)

        assert result.status is Status.PASS
        assert "Checked 2 mappings" in result.summary

    def test_unlisted_id_warns_rather_than_fails(self):
        """A list call cannot prove breakage — an alias still serves traffic.

        Reporting this as FAIL would repeat the original error of treating the
        provider's model list as authoritative.
        """
        report = AvailabilityReport(
            checked_providers=["xai"],
            total_checked=1,
            unlisted=[UnlistedModel("grok", "xai", "grok-3", suggestion="grok-4.3")],
        )

        result = check_model_ids(report)

        assert result.status is Status.WARN
        assert result.blocking is False
        assert "alias" in result.detail
        assert result.rows == [("xai / grok-3", "grok — closest listed: grok-4.3")]

    def test_pass_names_what_it_could_not_check(self):
        """A partial check must not imply full coverage."""
        report = AvailabilityReport(
            checked_providers=["openai"],
            total_checked=1,
            unsupported={"bedrock": 17},
        )

        result = check_model_ids(report)

        assert result.status is Status.PASS
        assert "17 mappings on bedrock not checked" in result.summary


# ── Credentials ──────────────────────────────────────────────────────


class TestCredentialCheck:
    def test_all_configured_passes(self):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        result = check_provider_credentials(registry, {"openai": _config("openai")})

        assert result.status is Status.PASS

    def test_model_with_no_usable_provider_fails(self):
        """Cannot serve a request, and nothing says so until one arrives."""
        registry = _registry(_model("grok", ("xai", "grok-4.3")))

        result = check_provider_credentials(registry, {})

        assert result.status is Status.FAIL
        assert "grok" in result.detail

    def test_alternative_provider_downgrades_to_warn(self):
        registry = _registry(_model("claude", ("anthropic", "c-1"), ("xai", "grok-4.3")))

        result = check_provider_credentials(registry, {"anthropic": _config("anthropic")})

        assert result.status is Status.WARN
        assert result.rows == [("xai", "1 mapping")]

    def test_bedrock_counts_as_credentialled(self):
        """Bedrock authenticates through the boto3 chain, not providers.yaml.

        Its absence from provider_configs is normal, so treating it as missing
        would fail the check for every deployment routing to Bedrock — which is
        most of them.
        """
        registry = _registry(_model("claude", ("bedrock", "anthropic.claude-v2")))

        result = check_provider_credentials(registry, {})

        assert result.status is Status.PASS


# ── Persistence ──────────────────────────────────────────────────────


class TestPersistenceCheck:
    @pytest.mark.asyncio
    async def test_reachable_passes(self):
        result = await check_persistence(_FakePersistence())
        assert result.status is Status.PASS

    @pytest.mark.asyncio
    async def test_enabled_but_unreachable_fails(self):
        """Writes are dropped right now, with no request-visible symptom."""
        result = await check_persistence(_FakePersistence(reachable=False))

        assert result.status is Status.FAIL
        assert "dropped" in result.detail

    @pytest.mark.asyncio
    async def test_disabled_warns(self):
        result = await check_persistence(_FakePersistence(enabled=False))

        assert result.status is Status.WARN
        assert "in-memory" in result.summary

    @pytest.mark.asyncio
    async def test_missing_persistence_warns(self):
        result = await check_persistence(None)
        assert result.status is Status.WARN

    @pytest.mark.asyncio
    async def test_dropped_write_warns_even_when_reachable(self):
        result = await check_persistence(
            _FakePersistence(last_write_error="cost record abc")
        )

        assert result.status is Status.WARN
        assert "cost record abc" in result.detail

    @pytest.mark.asyncio
    async def test_probe_failure_is_unknown_not_pass(self):
        result = await check_persistence(_FakePersistence(raises=True))
        assert result.status is Status.UNKNOWN


# ── Runner ───────────────────────────────────────────────────────────


class TestRunner:
    @pytest.mark.asyncio
    async def test_clean_production_config_is_ready(self, monkeypatch):
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))
        _stub_availability(
            monkeypatch, AvailabilityReport(checked_providers=["openai"], total_checked=1)
        )

        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False, auth_mode="ENFORCE"),
            model_registry=registry,
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(),
            environ={"AXON_LOAD_DEMO_DATA": "false"},
        )

        assert report.ready is True
        assert report.failures == []
        assert report.count(Status.PASS) == len(report.checks)

    @pytest.mark.asyncio
    async def test_skipping_the_live_check_is_not_a_pass(self):
        """Opting out of the outbound calls leaves the answer unknown, not clean.

        The whole checklist would be worthless if turning a check off counted as
        passing it.
        """
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False, auth_mode="ENFORCE"),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(),
            environ={"AXON_LOAD_DEMO_DATA": "false", "AXON_CHECK_MODEL_AVAILABILITY": "false"},
        )

        assert report.ready is True  # not blocking...
        assert report.count(Status.UNKNOWN) == 1  # ...but not claimed as verified

    @pytest.mark.asyncio
    async def test_failures_sort_first(self):
        """The page should open on what needs attention."""
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False, auth_mode="LOG_ONLY"),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(),
            environ={"AXON_CHECK_MODEL_AVAILABILITY": "false"},
        )

        assert report.checks[0].status is Status.FAIL
        assert report.ready is False

    @pytest.mark.asyncio
    async def test_warnings_do_not_block_readiness(self):
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False, auth_mode="ENFORCE"),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(enabled=False),
            environ={"AXON_CHECK_MODEL_AVAILABILITY": "false"},
        )

        assert report.warnings
        assert report.ready is True

    @pytest.mark.asyncio
    async def test_availability_override_disables_outbound_calls(self, monkeypatch):
        """The escape hatch for an egress-filtered deployment."""

        async def _boom(*args, **kwargs):
            raise AssertionError("should not call providers when disabled")

        monkeypatch.setattr(
            "src.gateway.admin.production_checklist.check_model_availability", _boom
        )

        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            environ={"AXON_CHECK_MODEL_AVAILABILITY": "false"},
        )

        by_key = {c.key: c for c in report.checks}
        assert by_key["model_ids"].status is Status.UNKNOWN


# ── Startup notice ───────────────────────────────────────────────────


class TestStartupNotice:
    @pytest.mark.asyncio
    async def test_clean_deployment_prints_nothing(self, monkeypatch):
        """A banner on every healthy boot is one nobody reads."""
        _stub_availability(
            monkeypatch, AvailabilityReport(checked_providers=["openai"], total_checked=1)
        )
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(),
            environ={"AXON_LOAD_DEMO_DATA": "false"},
        )

        assert format_startup_notice(report, "http://x") is None

    @pytest.mark.asyncio
    async def test_warnings_alone_stay_quiet(self):
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(enabled=False),
            environ={"AXON_CHECK_MODEL_AVAILABILITY": "false"},
        )

        assert report.warnings
        assert format_startup_notice(report, "http://x") is None

    @pytest.mark.asyncio
    async def test_failure_names_the_check_and_the_page(self):
        report = await run_checklist(
            app_config=AppConfig(load_demo_data=False, auth_mode="LOG_ONLY"),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(),
            environ={"AXON_CHECK_MODEL_AVAILABILITY": "false"},
        )

        notice = format_startup_notice(report, "http://localhost:8000/admin/production-checklist")

        assert notice is not None
        assert "authentication is enforced" in notice.lower()
        assert "/admin/production-checklist" in notice

    def test_demo_report_prints_nothing(self):
        from src.gateway.admin.production_checklist import ChecklistReport

        assert format_startup_notice(ChecklistReport(did_not_run=True), "http://x") is None


# ── Page rendering ───────────────────────────────────────────────────


class TestPageRendering:
    async def _report(self, monkeypatch, **kwargs):
        _stub_availability(
            monkeypatch, AvailabilityReport(checked_providers=["openai"], total_checked=1)
        )
        defaults = dict(
            app_config=AppConfig(load_demo_data=False),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            pricing_config={"openai": {"gpt-4": TokenPricing(0.03, 0.06)}},
            provider_configs={"openai": _config("openai")},
            persistence=_FakePersistence(),
            environ={"AXON_LOAD_DEMO_DATA": "false"},
        )
        defaults.update(kwargs)
        return await run_checklist(**defaults)

    @pytest.mark.asyncio
    async def test_clean_page_is_green(self, monkeypatch):
        html = render_checklist_page(await self._report(monkeypatch))

        assert "banner ok" in html
        assert "All checks pass" in html
        assert "banner fail" not in html

    @pytest.mark.asyncio
    async def test_failing_page_says_nothing_is_enforced(self, monkeypatch):
        """An operator has to know the gateway keeps serving regardless."""
        html = render_checklist_page(
            await self._report(
                monkeypatch,
                app_config=AppConfig(load_demo_data=False, auth_mode="LOG_ONLY"),
            )
        )

        assert "banner fail" in html
        assert "does not block" in html

    @pytest.mark.asyncio
    async def test_model_ids_are_escaped(self, monkeypatch):
        """Model ids come from a config file, which is not necessarily trusted."""
        html = render_checklist_page(
            await self._report(
                monkeypatch,
                model_registry=_registry(_model("evil", ("openai", "<script>alert(1)</script>"))),
                pricing_config={},
            )
        )

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    @pytest.mark.asyncio
    async def test_page_links_to_the_pricing_detail(self, monkeypatch):
        html = render_checklist_page(await self._report(monkeypatch))
        assert "/admin/pricing-drift" in html


class TestChecklistRoute:
    @pytest.fixture
    def api(self):
        return AdminAPI(
            cost_tracker=CostTracker({"openai": {"gpt-4": TokenPricing(0.03, 0.06)}}),
            health_tracker=ProviderHealthTracker(),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            app_config=AppConfig(load_demo_data=False, auth_mode="LOG_ONLY"),
            provider_configs={"openai": _config("openai")},
        )

    @pytest.fixture
    def client(self, api, monkeypatch):
        # No outbound calls from a unit test.
        monkeypatch.setenv("AXON_CHECK_MODEL_AVAILABILITY", "false")
        return TestClient(Starlette(routes=create_admin_routes(api)))

    def test_route_serves_html(self, client):
        resp = client.get("/admin/production-checklist")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "Production Readiness" in resp.text

    def test_route_reports_the_running_auth_mode(self, client):
        assert "LOG_ONLY" in client.get("/admin/production-checklist").text

    def test_route_rebuilds_per_request(self, api, client):
        """A fix has to show up on reload, without a restart."""
        assert "banner fail" in client.get("/admin/production-checklist").text

        api._app_config = AppConfig(load_demo_data=False, auth_mode="ENFORCE")

        assert "banner fail" not in client.get("/admin/production-checklist").text

    def test_demo_deployment_gets_the_not_applicable_page(self, monkeypatch):
        monkeypatch.setenv("AXON_CHECK_MODEL_AVAILABILITY", "false")
        api = AdminAPI(
            cost_tracker=CostTracker({}),
            health_tracker=ProviderHealthTracker(),
            model_registry=_registry(_model("gpt-4", ("openai", "gpt-4"))),
            app_config=AppConfig(load_demo_data=True),
        )
        client = TestClient(Starlette(routes=create_admin_routes(api)))

        resp = client.get("/admin/production-checklist")

        assert resp.status_code == 200
        assert "demo mode" in resp.text

    def test_default_app_config_assumes_the_safe_direction(self):
        """A caller that passes nothing must not get a false FAIL on auth.

        AppConfig's defaults are fail-closed (ENFORCE, no demo data), which is
        what the running gateway would have if it set nothing either.
        """
        api = AdminAPI(
            cost_tracker=CostTracker({}),
            health_tracker=ProviderHealthTracker(),
            model_registry=_registry(),
        )

        assert api._app_config.auth_mode == "ENFORCE"
        assert api._app_config.load_demo_data is False
