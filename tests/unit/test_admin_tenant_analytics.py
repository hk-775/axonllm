"""Tenant isolation for admin analytics built from the shared usage list."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src.gateway.admin import routes as admin_routes
from src.gateway.admin.routes import AdminAPI
from src.gateway.cost_tracker import CostTracker
from src.gateway.efficiency_analyzer import EfficiencyAnalyzer
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ModelConfig, ProviderModelMapping, UsageRecord


def _record(tenant_id: str, cost: float) -> UsageRecord:
    return UsageRecord(
        request_id=f"request-{tenant_id}",
        project_id="shared-project",
        user_id="shared-user",
        provider="openai",
        model="gpt-4",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost=cost,
        timestamp=datetime.now(timezone.utc),
        tenant_id=tenant_id,
    )


def _request(tenant_id: str):
    return SimpleNamespace(
        state=SimpleNamespace(
            context=SimpleNamespace(tenant_id=tenant_id)
        ),
        query_params={},
        path_params={},
    )


def _api() -> AdminAPI:
    tracker = CostTracker(pricing_config={})
    tracker._records = [
        _record("tenant-a", 0.10),
        _record("tenant-b", 9.00),
    ]
    registry = ModelRegistry()
    registry.models["gpt-4"] = ModelConfig(
        name="gpt-4",
        description="Test model",
        providers=[
            ProviderModelMapping(
                provider="openai",
                model_id="gpt-4",
            )
        ],
    )
    return AdminAPI(
        cost_tracker=tracker,
        health_tracker=ProviderHealthTracker(),
        model_registry=registry,
        efficiency_analyzer=EfficiencyAnalyzer(tracker),
    )


def _json(response):
    return json.loads(response.body)


def test_model_trace_and_efficiency_views_filter_before_aggregation() -> None:
    api = _api()
    request = _request("tenant-a")

    models = _json(asyncio.run(api.list_models(request)))
    traces = _json(asyncio.run(api.traces(request)))
    efficiency = _json(asyncio.run(api.efficiency_overview(request)))

    assert models[0]["total_requests"] == 1
    assert models[0]["total_cost"] == 0.10
    assert traces["total"] == 1
    assert [trace["cost"] for trace in traces["traces"]] == [0.10]
    assert efficiency["total_users_analyzed"] == 1
    assert efficiency["total_cost"] == 0.10


def test_catalog_drift_receives_only_the_callers_tenant(
    monkeypatch,
) -> None:
    api = _api()
    captured: list[UsageRecord] = []

    def _audit(_registry, _catalog, records):
        captured.extend(records)
        return object()

    monkeypatch.setattr(admin_routes, "audit_catalog", _audit)
    monkeypatch.setattr(
        admin_routes,
        "render_catalog_drift_page",
        lambda *_args, **_kwargs: "ok",
    )

    response = asyncio.run(api.catalog_drift(_request("tenant-a")))

    assert response.status_code == 200
    assert [record.tenant_id for record in captured] == ["tenant-a"]
