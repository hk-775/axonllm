"""Check that the model ids in models.yaml still exist at their providers.

``config/models.yaml`` pins a provider-side ``model_id`` per mapping, and
providers retire, rename and alias those ids on their own schedule. Nothing
reconciled the two, and most of the ways that goes wrong are silent:

* **The id is gone.** The provider 404s, the router marks the provider unhealthy
  and fails over. Noisy, and the least dangerous case.
* **The id is an undocumented alias.** The request succeeds and the provider
  serves a *different* model, reporting the substitution in a response field
  nothing reads. ``xai``'s ``grok-3`` did exactly this: it answered 200 while
  resolving to ``grok-4.3``, and because the alias appears on no price list it
  also billed $0.00 while being served by a $1.25/$2.50-per-MTok model.
* **The id exists but has no capacity for this account.** Together returns
  400 ``Unable to access non-serverless model`` for most of its catalogue — a
  provisioning state, not a bad id, and not fixable by renaming.

So a model list is not a guarantee and a 200 is not a confirmation. This module
asks each provider what it currently serves and compares that against what the
registry routes to. It is the availability half of the same problem
:mod:`pricing_drift` covers for rates: two hand-maintained lists that nothing
reconciles.

**Read-only, and list calls only.** This never issues a completion. Listing is
free and idempotent, while probing an id by generating a token costs money per
check and would make loading an admin page a billable event. The tradeoff is
that alias detection — visible only in a completion's response body — has to be
inferred instead: an id absent from the provider's own list is reported as
*unlisted*, which is the observable half of both the retired and the aliased
case, and the page says so rather than guessing which.

The check needs real credentials and reports on the real routing table, so
:func:`should_run` gates it off in demo mode. A demo has no keys to check with,
and rendering "0 problems found" from 0 successful checks reads as a clean bill
of health.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from src.gateway.model_registry import ModelRegistry
from src.gateway.provider_config import ProviderConfig, get_auth_headers

logger = logging.getLogger(__name__)

# Providers whose catalogue is reachable over HTTP with the credentials the
# gateway already holds, mapped to the path that lists it.
#
# Deliberately not exhaustive. ``bedrock`` and ``bedrock-mantle`` are absent
# because their catalogue comes from boto3 and IAM rather than a bearer token,
# and a half-working AWS branch here would report "unknown" for the providers
# carrying most production traffic — worse than declining to check them.
# ``vertex_ai`` and ``azure_openai`` are absent because their model ids are
# deployment and publisher paths, so listing proves nothing about whether a
# mapping resolves. Everything left out is named on the page as unchecked, so
# the coverage claim stays honest.
_LIST_PATHS: dict[str, str] = {
    "openai": "/v1/models",
    "anthropic": "/v1/models",
    "xai": "/v1/language-models",
    "groq": "/v1/models",
    "together": "/v1/models",
    "fireworks": "/v1/models",
    "ai21": "/v1/models",
    "google_ai": "/v1beta/models",
    "cohere": "/v1/models",
}

# Response shapes: (container key, or None for a bare top-level list; id field).
_LIST_SHAPES: dict[str, tuple[str | None, str]] = {
    "openai": ("data", "id"),
    "anthropic": ("data", "id"),
    "xai": ("models", "id"),
    "groq": ("data", "id"),
    "together": (None, "id"),
    "fireworks": ("data", "id"),
    "ai21": ("data", "id"),
    "google_ai": ("models", "name"),
    "cohere": ("models", "name"),
}

# Headers a provider's *list* endpoint requires beyond authentication.
#
# Kept separate from ``http_client._PROVIDER_HEADERS`` rather than imported:
# that dict governs completion requests, and coupling the two would mean a
# change made for the inference path silently altering this audit — which is
# the class of unnoticed coupling this module exists to detect.
_LIST_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}


@dataclass(frozen=True)
class UnlistedModel:
    """A registry mapping whose model id the provider does not list.

    Either retired, or an undocumented alias the provider still honours. A list
    call cannot tell those apart — that needs a completion, which this module
    does not make — so both surface here and the page reports the ambiguity
    instead of picking one.
    """

    model: str
    provider: str
    model_id: str
    # A listed id in the same family, when exactly one exists. A hint, not a
    # conclusion; see :func:`_suggest`.
    suggestion: str | None = None


@dataclass(frozen=True)
class ProviderCheckError:
    """A provider whose catalogue could not be read, and why.

    Reported rather than swallowed: "could not check" and "checked, all fine"
    have to look different on the page, or an expired key quietly renders as a
    clean report.
    """

    provider: str
    reason: str
    # True when the credential is absent rather than the call having failed.
    # Distinguished because that is the expected state for a provider the
    # operator has deliberately not configured, not a fault to escalate.
    unconfigured: bool = False


@dataclass
class AvailabilityReport:
    """What the registry routes to, versus what providers currently serve."""

    checked_providers: list[str] = field(default_factory=list)
    total_checked: int = 0
    unlisted: list[UnlistedModel] = field(default_factory=list)
    errors: list[ProviderCheckError] = field(default_factory=list)
    # Providers in the registry this module has no HTTP way to ask (bedrock,
    # vertex_ai, ...), with the mapping count each accounts for.
    unsupported: dict[str, int] = field(default_factory=dict)
    # True when the check did not run at all — demo mode, or explicitly off.
    did_not_run: bool = False

    @property
    def has_findings(self) -> bool:
        """True when at least one mapping is unlisted at its provider."""
        return bool(self.unlisted)

    @property
    def degraded(self) -> bool:
        """True when a *configured* provider could not be checked.

        Separate from :attr:`has_findings` because the message differs: not
        "this id is wrong" but "this answer is incomplete". A missing credential
        does not count — see :attr:`ProviderCheckError.unconfigured`.
        """
        return any(not e.unconfigured for e in self.errors)

    @property
    def unchecked_mappings(self) -> int:
        """Mappings this report says nothing about, either way."""
        return sum(self.unsupported.values())


def should_run(*, load_demo_data: bool, enabled_override: str | None = None) -> bool:
    """Whether to run the live check in this deployment.

    Off in demo mode. The check's whole value is telling an operator that real
    routing is broken, and a demo has no credentials to check with — so it would
    report every provider unconfigured and nothing unlisted, which reads as a
    pass.

    ``AXON_CHECK_MODEL_AVAILABILITY`` overrides in either direction: ``false``
    to opt a production deployment out of the outbound calls entirely (an
    egress-filtered or air-gapped environment, where every provider would report
    a connection error and the result would be pure noise), ``true`` to exercise
    it locally against real keys.
    """
    if enabled_override is not None and enabled_override.strip():
        return enabled_override.strip().lower() in ("1", "true", "yes")
    return not load_demo_data


def _family(model_id: str) -> str:
    """Reduce an id to its family, for suggesting a likely replacement.

    Same intent as :func:`pricing_drift._family`, kept separate because the
    inputs differ: these ids come back from provider APIs carrying vendor
    prefixes (``models/gemini-3.5-flash``, ``accounts/fireworks/models/...``)
    that the registry's pinned ids do not have.
    """
    tail = model_id.rsplit("/", 1)[-1]
    # Drop date stamps (2407, 20250514) and version suffixes (-v1:0, :0).
    stripped = re.sub(r"\d{4,}", "", tail)
    stripped = re.sub(r"[-_.]v\d*(?::\d+)?$", "-v", stripped)
    stripped = re.sub(r":\d+$", "", stripped)
    stripped = re.sub(r"[-_.]{2,}", "-", stripped).strip("-_.")
    return stripped.lower()


def _suggest(model_id: str, listed: list[str]) -> str | None:
    """Find the one listed id in the same family, if it is unambiguous.

    Conservative for the same reason the pricing suggestions are: this ends in
    someone editing models.yaml, and a wrong hint points production traffic at
    the wrong model. Two candidates give no basis for choosing, so neither is
    offered — one extra lookup beats a confident wrong answer.
    """
    target = _family(model_id)
    if len(target) < 4:
        return None
    matches = sorted({c for c in listed if _family(c) == target and c != model_id})
    if len(matches) == 1:
        return matches[0]
    return None


def _extract_ids(provider: str, payload: object) -> list[str]:
    """Pull model ids out of a provider's list response."""
    container, id_field = _LIST_SHAPES.get(provider, ("data", "id"))
    if container is None:
        items: object = payload
    elif isinstance(payload, dict):
        items = payload.get(container, [])
    else:
        items = []
    if not isinstance(items, list):
        return []

    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(id_field)
        if isinstance(value, str) and value:
            # Google AI and Cohere return "models/gemini-3.5-flash"; the registry
            # pins the bare id, so compare on the same form.
            ids.append(value.removeprefix("models/"))
        # Some providers list documented aliases alongside the canonical id. An
        # alias that the provider itself publishes is a legitimate mapping
        # target — the dangerous kind is the undocumented one, which by
        # definition will not appear here.
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            ids.extend(a for a in aliases if isinstance(a, str) and a)
    return ids


async def _fetch_ids(
    provider: str,
    config: ProviderConfig,
    timeout: float,
) -> tuple[list[str], ProviderCheckError | None]:
    """Fetch one provider's model ids. Never raises."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a declared dependency
        return [], ProviderCheckError(provider, "httpx not installed")

    url = f"{config.base_url.rstrip('/')}{_LIST_PATHS[provider]}"
    params: dict[str, str] = {}

    try:
        headers = get_auth_headers(config)
    except Exception:
        # ProviderError(401) when the credential is absent — the ordinary state
        # for a provider the operator has not configured.
        return [], ProviderCheckError(provider, "no credentials configured", unconfigured=True)

    # Google AI authenticates by query parameter rather than by header. Passed
    # via params rather than interpolated into the URL so the key cannot end up
    # in a log line or an exception message built from it.
    if provider == "google_ai":
        key = config.credentials.get("api_key", "")
        if not key:
            return [], ProviderCheckError(
                provider, "no credentials configured", unconfigured=True
            )
        params = {"key": key, "pageSize": "1000"}

    headers = {**headers, **_LIST_HEADERS.get(provider, {}), "accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers, params=params)
    except Exception as exc:
        # Type name only. An httpx exception's message embeds the request URL,
        # which for google_ai carries the API key as a query parameter.
        return [], ProviderCheckError(provider, f"request failed ({type(exc).__name__})")

    if response.status_code != 200:
        # Status only, for the same reason — a provider error body can echo the
        # request, and this string is rendered into an admin page.
        return [], ProviderCheckError(provider, f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except Exception:
        return [], ProviderCheckError(provider, "unparseable response")

    ids = _extract_ids(provider, payload)
    if not ids:
        # An empty list is far more likely a changed response shape than a
        # provider serving no models, and treating it as "everything is missing"
        # would flag every mapping at once — a false alarm big enough to make
        # the whole page ignorable.
        return [], ProviderCheckError(provider, "no model ids in response")
    return ids, None


async def check_model_availability(
    model_registry: ModelRegistry,
    provider_configs: dict[str, ProviderConfig],
    *,
    timeout: float = 10.0,
) -> AvailabilityReport:
    """Compare every registry mapping against its provider's live model list.

    One list call per provider, issued concurrently, each independently
    fallible: a provider that errors contributes an entry to
    :attr:`AvailabilityReport.errors` and no findings, so a network problem
    cannot manufacture a wall of false "retired model" warnings.
    """
    report = AvailabilityReport()

    # provider -> [(gateway model name, provider-side model id)]
    wanted: dict[str, list[tuple[str, str]]] = {}
    for model_name in sorted(model_registry.models):
        for mapping in model_registry.models[model_name].providers:
            wanted.setdefault(mapping.provider, []).append((model_name, mapping.model_id))

    checkable: list[str] = []
    for provider in sorted(wanted):
        if provider not in _LIST_PATHS:
            # No HTTP catalogue endpoint wired for this provider.
            report.unsupported[provider] = len(wanted[provider])
        elif provider not in provider_configs:
            # load_provider_configs drops providers with no credentials, so an
            # absent config means unconfigured rather than unsupported.
            report.errors.append(
                ProviderCheckError(provider, "no credentials configured", unconfigured=True)
            )
        else:
            checkable.append(provider)

    results = await asyncio.gather(
        *(_fetch_ids(p, provider_configs[p], timeout) for p in checkable)
    )

    for provider, (listed, error) in zip(checkable, results):
        if error is not None:
            report.errors.append(error)
            continue

        report.checked_providers.append(provider)
        listed_set = set(listed)
        for model_name, model_id in wanted[provider]:
            report.total_checked += 1
            if model_id in listed_set:
                continue
            report.unlisted.append(
                UnlistedModel(
                    model=model_name,
                    provider=provider,
                    model_id=model_id,
                    suggestion=_suggest(model_id, listed),
                )
            )

    return report
