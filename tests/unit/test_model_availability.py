"""Tests for the live provider model-availability check.

The check's value depends entirely on not lying in either direction. A false
finding sends an operator to rename a working mapping; a false all-clear is the
state that let ``grok-3`` route to a different model, billed at $0.00, for as
long as it did.

So the properties under test are: a failed call never produces findings, an
unreadable response never reads as "everything is missing", and no credential
ever reaches a rendered string.
"""

from __future__ import annotations

import httpx
import pytest

from src.gateway.admin.model_availability import (
    _extract_ids,
    _family,
    _fetch_ids,
    _suggest,
    check_model_availability,
    should_run,
)
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ModelConfig, ProviderModelMapping
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


def _config(provider: str, api_key: str = "test-key") -> ProviderConfig:
    return ProviderConfig(
        provider_name=provider,
        base_url=f"https://api.{provider}.test",
        auth_type="api_key",
        credentials={"api_key": api_key} if api_key else {},
    )


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient."""

    def __init__(self, handler):
        self._handler = handler
        self.requests: list[tuple[str, dict, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.requests.append((url, headers or {}, params or {}))
        return self._handler(url, headers or {}, params or {})


def _response(payload, status=200) -> httpx.Response:
    return httpx.Response(status_code=status, json=payload)


def _patch_client(monkeypatch, handler) -> _FakeClient:
    client = _FakeClient(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    return client


# ── Gating ───────────────────────────────────────────────────────────


class TestShouldRun:
    def test_off_in_demo_mode(self):
        assert should_run(load_demo_data=True) is False

    def test_on_in_production(self):
        assert should_run(load_demo_data=False) is True

    def test_override_can_force_it_on_in_demo(self):
        assert should_run(load_demo_data=True, enabled_override="true") is True

    def test_override_can_force_it_off_in_production(self):
        """The escape hatch for an egress-filtered deployment."""
        assert should_run(load_demo_data=False, enabled_override="false") is False

    def test_empty_override_is_not_a_choice(self):
        """An unset-but-present env var must not read as an explicit false."""
        assert should_run(load_demo_data=False, enabled_override="") is True


# ── Response parsing ─────────────────────────────────────────────────


class TestExtractIds:
    def test_openai_shape(self):
        assert _extract_ids("openai", {"data": [{"id": "gpt-4"}]}) == ["gpt-4"]

    def test_together_bare_list_shape(self):
        assert _extract_ids("together", [{"id": "meta-llama/Llama-3.3"}]) == [
            "meta-llama/Llama-3.3"
        ]

    def test_google_prefix_is_stripped_to_match_the_registry(self):
        """models.yaml pins the bare id, so the comparison has to use one form."""
        assert _extract_ids("google_ai", {"models": [{"name": "models/gemini-3-pro"}]}) == [
            "gemini-3-pro"
        ]

    def test_published_aliases_count_as_listed(self):
        ids = _extract_ids("xai", {"models": [{"id": "grok-4.3", "aliases": ["grok-latest"]}]})
        assert ids == ["grok-4.3", "grok-latest"]

    def test_unexpected_shapes_yield_nothing_rather_than_raising(self):
        assert _extract_ids("openai", {"data": "not-a-list"}) == []
        assert _extract_ids("openai", ["bare", "strings"]) == []
        assert _extract_ids("openai", None) == []


# ── Rename suggestions ───────────────────────────────────────────────


class TestSuggestions:
    def test_version_bump_is_recognized(self):
        assert _suggest("claude-opus-4-1", ["claude-opus-4-8", "claude-haiku-4-5"]) is None

    def test_dated_snapshot_bump_is_suggested(self):
        assert (
            _suggest("mistral-large-2402", ["mistral-large-2407", "gpt-4"])
            == "mistral-large-2407"
        )

    def test_ambiguity_yields_no_suggestion(self):
        """Two candidates give no basis for choosing, and a wrong hint reroutes
        production traffic to the wrong model."""
        assert _suggest("mistral-large-2402", ["mistral-large-2407", "mistral-large-2411"]) is None

    def test_vendor_prefix_does_not_defeat_the_match(self):
        assert (
            _suggest("Qwen/Qwen-2402", ["Qwen/Qwen-2407"]) == "Qwen/Qwen-2407"
        )

    def test_family_strips_the_vendor_prefix(self):
        """Provider lists carry prefixes the registry's pinned ids do not."""
        assert _family("accounts/fireworks/models/llama-v3-70b") == "llama-v3-70b"

    def test_parameter_size_stays_in_the_family(self):
        """A 70b and an 8b are different models, not two versions of one.

        Collapsing them would suggest renaming a 70b mapping onto an 8b — a
        confident wrong answer that reroutes production traffic and looks
        deliberate.
        """
        assert _suggest("llama-3-70b", ["llama-3-8b"]) is None


# ── Fetch failure handling ───────────────────────────────────────────


class TestFetchFailures:
    @pytest.mark.asyncio
    async def test_missing_credential_is_unconfigured_not_an_error(self):
        """The ordinary state for a provider the operator did not set up."""
        ids, error = await _fetch_ids("openai", _config("openai", api_key=""), 5.0)

        assert ids == []
        assert error is not None
        assert error.unconfigured is True

    @pytest.mark.asyncio
    async def test_non_200_reports_status_only(self, monkeypatch):
        """A provider error body can echo the request, and this string is
        rendered into an admin page."""
        _patch_client(monkeypatch, lambda *a: _response({"error": "nope"}, status=401))

        ids, error = await _fetch_ids("openai", _config("openai"), 5.0)

        assert ids == []
        assert error.reason == "HTTP 401"
        assert error.unconfigured is False

    @pytest.mark.asyncio
    async def test_transport_failure_never_leaks_the_url(self, monkeypatch):
        """httpx embeds the request URL in its exceptions, and for google_ai that
        URL carries the API key as a query parameter."""

        def _raise(*args):
            raise httpx.ConnectError("failed to connect to https://api.test?key=sk-secret")

        _patch_client(monkeypatch, _raise)

        ids, error = await _fetch_ids("openai", _config("openai"), 5.0)

        assert ids == []
        assert "sk-secret" not in error.reason
        assert "ConnectError" in error.reason

    @pytest.mark.asyncio
    async def test_empty_list_is_an_error_not_a_wall_of_findings(self, monkeypatch):
        """A changed response shape is far likelier than a provider serving
        nothing, and flagging every mapping at once makes the page ignorable."""
        _patch_client(monkeypatch, lambda *a: _response({"data": []}))

        ids, error = await _fetch_ids("openai", _config("openai"), 5.0)

        assert ids == []
        assert error.reason == "no model ids in response"

    @pytest.mark.asyncio
    async def test_google_key_goes_in_params_not_the_url(self, monkeypatch):
        """So it cannot end up in a log line or an exception built from the URL."""
        client = _patch_client(monkeypatch, lambda *a: _response({"models": [{"name": "g"}]}))

        await _fetch_ids("google_ai", _config("google_ai", api_key="sk-secret"), 5.0)

        url, headers, params = client.requests[0]
        assert "sk-secret" not in url
        assert params["key"] == "sk-secret"

    @pytest.mark.asyncio
    async def test_anthropic_sends_its_required_version_header(self, monkeypatch):
        """/v1/models 400s without it, which would render as a provider outage."""
        client = _patch_client(monkeypatch, lambda *a: _response({"data": [{"id": "c"}]}))

        await _fetch_ids("anthropic", _config("anthropic"), 5.0)

        _, headers, _ = client.requests[0]
        assert headers["anthropic-version"] == "2023-06-01"


# ── Whole-audit behaviour ────────────────────────────────────────────


class TestCheckModelAvailability:
    @pytest.mark.asyncio
    async def test_listed_mapping_produces_no_finding(self, monkeypatch):
        _patch_client(monkeypatch, lambda *a: _response({"data": [{"id": "gpt-4"}]}))
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        report = await check_model_availability(registry, {"openai": _config("openai")})

        assert report.unlisted == []
        assert report.total_checked == 1
        assert report.checked_providers == ["openai"]

    @pytest.mark.asyncio
    async def test_unlisted_mapping_is_reported_with_a_hint(self, monkeypatch):
        _patch_client(monkeypatch, lambda *a: _response({"data": [{"id": "gpt-4-2407"}]}))
        registry = _registry(_model("gpt-4", ("openai", "gpt-4-2402")))

        report = await check_model_availability(registry, {"openai": _config("openai")})

        assert len(report.unlisted) == 1
        assert report.unlisted[0].model_id == "gpt-4-2402"
        assert report.unlisted[0].suggestion == "gpt-4-2407"
        assert report.has_findings is True

    @pytest.mark.asyncio
    async def test_a_failed_provider_produces_no_false_findings(self, monkeypatch):
        """The property that keeps a network blip from becoming a wall of
        bogus 'retired model' warnings."""
        _patch_client(monkeypatch, lambda *a: _response({}, status=500))
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        report = await check_model_availability(registry, {"openai": _config("openai")})

        assert report.unlisted == []
        assert report.total_checked == 0
        assert report.has_findings is False
        assert report.degraded is True

    @pytest.mark.asyncio
    async def test_unconfigured_provider_is_not_degraded(self, monkeypatch):
        """A provider the operator deliberately did not set up is not a fault."""
        registry = _registry(_model("grok", ("xai", "grok-4.3")))

        report = await check_model_availability(registry, {})

        assert report.degraded is False
        assert report.errors[0].unconfigured is True
        assert report.checked_providers == []

    @pytest.mark.asyncio
    async def test_providers_without_a_catalogue_endpoint_are_counted_unchecked(self):
        """Bedrock lists through boto3, not a bearer token. Saying nothing about
        it has to be visible, or the coverage claim overstates itself."""
        registry = _registry(
            _model("claude", ("bedrock", "anthropic.claude-v2"), ("vertex_ai", "claude@1")),
        )

        report = await check_model_availability(registry, {})

        assert report.unsupported == {"bedrock": 1, "vertex_ai": 1}
        assert report.unchecked_mappings == 2
        assert report.total_checked == 0

    @pytest.mark.asyncio
    async def test_one_provider_failing_does_not_stop_the_others(self, monkeypatch):
        def _handler(url, headers, params):
            if "openai" in url:
                return _response({}, status=500)
            return _response({"models": [{"id": "grok-4.3"}]})

        _patch_client(monkeypatch, _handler)
        registry = _registry(
            _model("gpt-4", ("openai", "gpt-4")),
            _model("grok", ("xai", "grok-4.3")),
        )

        report = await check_model_availability(
            registry, {"openai": _config("openai"), "xai": _config("xai")}
        )

        assert report.checked_providers == ["xai"]
        assert report.total_checked == 1
        assert report.unlisted == []
        assert report.degraded is True

    @pytest.mark.asyncio
    async def test_it_never_issues_a_completion(self, monkeypatch):
        """Listing is free; generating a token is not. Loading an admin page
        must not be a billable event."""
        client = _patch_client(monkeypatch, lambda *a: _response({"data": [{"id": "gpt-4"}]}))
        registry = _registry(_model("gpt-4", ("openai", "gpt-4")))

        await check_model_availability(registry, {"openai": _config("openai")})

        for url, _, _ in client.requests:
            assert "completion" not in url
            assert "/messages" not in url
