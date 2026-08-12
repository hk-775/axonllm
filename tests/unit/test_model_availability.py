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
    _fetch_bedrock_ids,
    _fetch_ids,
    _fetch_mantle_ids,
    _list_bedrock_ids,
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
        """Transport exception text is never exposed to the admin surface."""

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
    async def test_google_key_uses_header_and_never_the_url(self, monkeypatch):
        """The API key must not enter URL-bearing logs or exceptions."""
        client = _patch_client(monkeypatch, lambda *a: _response({"models": [{"name": "g"}]}))

        await _fetch_ids("google_ai", _config("google_ai", api_key="sk-secret"), 5.0)

        url, headers, params = client.requests[0]
        assert "sk-secret" not in url
        assert headers["x-goog-api-key"] == "sk-secret"
        assert params == {"pageSize": "1000"}

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
        """Vertex ids are deployment paths, so listing proves nothing about
        whether a mapping resolves. Saying nothing about it has to be visible, or
        the coverage claim overstates itself.
        """
        registry = _registry(_model("claude", ("vertex_ai", "claude@1")))

        report = await check_model_availability(registry, {})

        assert report.unsupported == {"vertex_ai": 1}
        assert report.unchecked_mappings == 1
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


# ── AWS catalogues ───────────────────────────────────────────────────
#
# Bedrock and Mantle authenticate through the boto3 credential chain rather than
# a bearer token, so they bypass _fetch_ids entirely and need their own coverage.


class _FakeBedrockClient:
    """Stand-in for boto3's bedrock client, covering both list calls."""

    def __init__(self, foundation, profiles, *, paginator=True):
        self._foundation = foundation
        self._profiles = profiles
        self._paginator = paginator
        self.calls: list[str] = []

    def list_foundation_models(self):
        self.calls.append("list_foundation_models")
        return {"modelSummaries": [{"modelId": m} for m in self._foundation]}

    def get_paginator(self, name):
        self.calls.append(name)
        profiles = self._profiles
        outer = self

        class _Paginator:
            def paginate(self):
                # Two pages, to prove pagination is actually consumed: the
                # account has 63 profiles and a single page caps at 100 today,
                # so a regression here would stay invisible in production until
                # it silently truncated.
                mid = len(profiles) // 2
                for chunk in (profiles[:mid], profiles[mid:]):
                    outer.calls.append("page")
                    yield {
                        "inferenceProfileSummaries": [
                            {"inferenceProfileId": p} for p in chunk
                        ]
                    }

        return _Paginator()


def _patch_boto(monkeypatch, client):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    return client


class TestBedrockListing:
    def test_inference_profiles_are_listed_alongside_foundation_models(self, monkeypatch):
        """The property that keeps 10 of 14 working mappings from being flagged.

        models.yaml pins cross-region profiles (``us.anthropic.…``) for most
        Bedrock mappings, and list_foundation_models does not return them.
        """
        _patch_boto(
            monkeypatch,
            _FakeBedrockClient(
                ["anthropic.claude-sonnet-4-20250514-v1:0"],
                ["us.anthropic.claude-opus-4-6-v1", "us.amazon.nova-pro-v1:0"],
            ),
        )

        ids = _list_bedrock_ids("us-east-1")

        assert "anthropic.claude-sonnet-4-20250514-v1:0" in ids
        assert "us.anthropic.claude-opus-4-6-v1" in ids
        assert "us.amazon.nova-pro-v1:0" in ids

    def test_pagination_is_consumed(self, monkeypatch):
        profiles = [f"us.vendor.model-{i}" for i in range(10)]
        _patch_boto(monkeypatch, _FakeBedrockClient(["base"], profiles))

        ids = _list_bedrock_ids("us-east-1")

        assert set(profiles).issubset(set(ids))

    def test_missing_profile_permission_is_a_failed_read_not_a_short_list(
        self, monkeypatch
    ):
        """A caller can hold ListFoundationModels without ListInferenceProfiles.

        Returning only foundation models there would flag every profile mapping
        as retired, so the read has to fail loudly instead.
        """
        _patch_boto(monkeypatch, _FakeBedrockClient(["base-model"], []))

        with pytest.raises(RuntimeError):
            _list_bedrock_ids("us-east-1")

    @pytest.mark.asyncio
    async def test_that_failure_surfaces_as_an_error_with_no_findings(self, monkeypatch):
        _patch_boto(monkeypatch, _FakeBedrockClient(["base-model"], []))
        registry = _registry(_model("claude", ("bedrock", "us.anthropic.claude-opus-4-6-v1")))

        report = await check_model_availability(registry, {})

        assert report.unlisted == []
        assert report.degraded is True
        assert report.total_checked == 0

    @pytest.mark.asyncio
    async def test_absent_aws_credentials_are_unconfigured_not_degraded(self, monkeypatch):
        from botocore.exceptions import NoCredentialsError

        def _raise(*a, **k):
            raise NoCredentialsError()

        import boto3

        monkeypatch.setattr(boto3, "client", _raise)

        ids, error = await _fetch_bedrock_ids("bedrock", None, 5.0, "us-east-1")

        assert ids == []
        assert error.unconfigured is True

    @pytest.mark.asyncio
    async def test_botocore_errors_report_the_type_only(self, monkeypatch):
        """A botocore message can carry the caller's ARN and the request URL,
        and this string is rendered into an admin page."""
        import boto3

        def _raise(*a, **k):
            raise RuntimeError(
                "User arn:aws:sts::123456789012:assumed-role/secret is not authorized"
            )

        monkeypatch.setattr(boto3, "client", _raise)

        ids, error = await _fetch_bedrock_ids("bedrock", None, 5.0, "us-east-1")

        assert "123456789012" not in error.reason
        assert "RuntimeError" in error.reason


class TestMantleListing:
    @pytest.mark.asyncio
    async def test_ids_come_back_from_the_openai_shaped_payload(self, monkeypatch):
        import src.gateway.admin.model_availability as mod

        monkeypatch.setattr(
            mod, "_mantle_list_request", lambda region, timeout: ["openai.gpt-5.5"]
        )

        ids, error = await _fetch_mantle_ids("bedrock-mantle", None, 5.0, "us-east-1")

        assert ids == ["openai.gpt-5.5"]
        assert error is None

    @pytest.mark.asyncio
    async def test_no_aws_credentials_is_unconfigured(self, monkeypatch):
        import src.gateway.admin.model_availability as mod

        def _raise(region, timeout):
            raise mod._NoAwsCredentials

        monkeypatch.setattr(mod, "_mantle_list_request", _raise)

        ids, error = await _fetch_mantle_ids("bedrock-mantle", None, 5.0, "us-east-1")

        assert ids == []
        assert error.unconfigured is True

    @pytest.mark.asyncio
    async def test_http_error_reports_the_status_only(self, monkeypatch):
        import urllib.error

        import src.gateway.admin.model_availability as mod

        def _raise(region, timeout):
            raise urllib.error.HTTPError(
                "https://bedrock-mantle.us-east-1.api.aws/v1/models", 403, "Forbidden", {}, None
            )

        monkeypatch.setattr(mod, "_mantle_list_request", _raise)

        ids, error = await _fetch_mantle_ids("bedrock-mantle", None, 5.0, "us-east-1")

        assert error.reason == "HTTP 403"
        assert error.unconfigured is False

    @pytest.mark.asyncio
    async def test_signing_targets_the_endpoint_that_serves_traffic(self, monkeypatch):
        """Checking a different endpoint than mantle_provider routes to would
        make a pass here meaningless."""
        seen: dict[str, object] = {}

        class _Creds:
            def get_frozen_credentials(self):
                return "frozen"

        class _Session:
            def get_credentials(self):
                return _Creds()

        import boto3
        import botocore.auth
        import urllib.request

        monkeypatch.setattr(boto3, "Session", lambda: _Session())
        monkeypatch.setattr(
            botocore.auth,
            "SigV4Auth",
            lambda creds, service, region: type(
                "_A",
                (),
                {"add_auth": lambda self, req: seen.update(service=service, region=region)},
            )(),
        )

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"data": [{"id": "openai.gpt-5.5"}]}'

        def _urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

        from src.gateway.admin.model_availability import _mantle_list_request

        ids = _mantle_list_request("us-west-2", 5.0)

        assert ids == ["openai.gpt-5.5"]
        assert seen["url"] == "https://bedrock-mantle.us-west-2.api.aws/v1/models"
        assert seen["service"] == "bedrock"
        assert seen["region"] == "us-west-2"


class TestAwsProvidersNeedNoProviderConfig:
    @pytest.mark.asyncio
    async def test_aws_providers_are_checked_without_a_providers_yaml_entry(
        self, monkeypatch
    ):
        """They authenticate through the boto3 chain, so an absent config is
        normal — not a reason to skip them or report them unconfigured."""
        import src.gateway.admin.model_availability as mod

        _patch_boto(
            monkeypatch,
            _FakeBedrockClient(["base"], ["us.anthropic.claude-opus-4-6-v1"]),
        )
        monkeypatch.setattr(
            mod, "_mantle_list_request", lambda region, timeout: ["openai.gpt-5.5"]
        )

        registry = _registry(
            _model("claude", ("bedrock", "us.anthropic.claude-opus-4-6-v1")),
            _model("gpt", ("bedrock-mantle", "openai.gpt-5.5")),
        )

        report = await check_model_availability(registry, {})

        assert report.checked_providers == ["bedrock", "bedrock-mantle"]
        assert report.total_checked == 2
        assert report.unlisted == []
        assert report.unsupported == {}

    @pytest.mark.asyncio
    async def test_only_vertex_and_azure_remain_unchecked(self, monkeypatch):
        """The honesty property: whatever this module cannot ask about has to be
        counted, so partial coverage never reads as full."""
        registry = _registry(
            _model("claude", ("vertex_ai", "claude@1"), ("azure_openai", "my-deployment")),
        )

        report = await check_model_availability(registry, {})

        assert report.unsupported == {"azure_openai": 1, "vertex_ai": 1}
        assert report.unchecked_mappings == 2

    @pytest.mark.asyncio
    async def test_the_configured_region_is_used(self, monkeypatch):
        """A gateway pointed at eu-west-1 must not be audited against us-east-1,
        where the catalogue differs."""
        import src.gateway.admin.model_availability as mod

        seen: list[str] = []
        monkeypatch.setattr(
            mod,
            "_mantle_list_request",
            lambda region, timeout: (seen.append(region), ["openai.gpt-5.5"])[1],
        )
        registry = _registry(_model("gpt", ("bedrock-mantle", "openai.gpt-5.5")))

        await check_model_availability(registry, {}, bedrock_region="eu-west-1")

        assert seen == ["eu-west-1"]
