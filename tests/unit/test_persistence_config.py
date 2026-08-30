"""Regression tests for task #4 — persistence table config must match the CDK.

The bug: persistence.py defaulted to a table name ("llm-router-state", PK/SK)
that the CDK never provisioned (it made three differently-named tables with
lowercase pk/sk), and nothing passed a table name to the code. Every write hit
AccessDenied/ResourceNotFound and was silently swallowed.

These tests pin the contract that keeps app and infra in sync:
- the table name is read from AXON_DYNAMODB_TABLE
- the fallback default matches the CDK's provisioned table name (axonllm-state)
- health_status() reports config without touching AWS when disabled
"""

from __future__ import annotations

import asyncio

import pytest

from src.gateway.persistence import DynamoPersistence

# Must equal infra/stack.py -> dynamodb.Table(table_name=...). If either side
# changes, this test fails and forces the other to be updated in lockstep.
CDK_TABLE_NAME = "axonllm-state"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AXON_DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("AXON_DYNAMODB_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AXON_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)


class TestTableNameResolution:
    def test_default_matches_cdk_table_name(self):
        assert DynamoPersistence(region="us-east-1")._table_name == CDK_TABLE_NAME

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("AXON_DYNAMODB_TABLE", "custom-table")
        assert DynamoPersistence(region="us-east-1")._table_name == "custom-table"

    def test_explicit_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("AXON_DYNAMODB_TABLE", "env-table")
        assert DynamoPersistence(table_name="arg-table")._table_name == "arg-table"


class TestDevelopmentEndpoint:
    def test_local_endpoint_is_available_only_in_development(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
        monkeypatch.setenv(
            "AXON_DYNAMODB_ENDPOINT_URL",
            "http://dynamodb-local:8000/",
        )

        persistence = DynamoPersistence(region="us-east-1")

        assert persistence._dynamodb_client_options() == {
            "region_name": "us-east-1",
            "endpoint_url": "http://dynamodb-local:8000",
        }

    def test_production_rejects_endpoint_override(self, monkeypatch):
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "production")
        monkeypatch.setenv(
            "AXON_DYNAMODB_ENDPOINT_URL",
            "http://127.0.0.1:8000",
        )

        with pytest.raises(RuntimeError, match="forbidden in production"):
            DynamoPersistence(region="us-east-1")

    @pytest.mark.parametrize(
        "endpoint",
        [
            "file:///tmp/dynamodb",
            "http://user:password@localhost:8000",
            "http://localhost:8000/path",
            "http://localhost:8000?tenant=a",
        ],
    )
    def test_malformed_endpoint_is_rejected(
        self,
        monkeypatch,
        endpoint,
    ):
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
        monkeypatch.setenv("AXON_DYNAMODB_ENDPOINT_URL", endpoint)

        with pytest.raises(ValueError, match="http\\(s\\) origin"):
            DynamoPersistence(region="us-east-1")


class TestHealthStatus:
    def test_disabled_reports_without_touching_aws(self):
        p = DynamoPersistence(region="us-east-1")  # disabled by default
        status = asyncio.run(p.health_status())
        assert status["enabled"] is False
        assert status["reachable"] is None  # no AWS call attempted
        assert status["table"] == CDK_TABLE_NAME
        assert status["last_write_error"] is None

    def test_write_failure_is_recorded(self):
        p = DynamoPersistence(region="us-east-1")
        assert p.last_write_error is None
        p._record_write_failure("usage record", "req-123")
        assert p.last_write_error == "usage record req-123"


class TestProjectRoundTrip:
    """A Project must survive serialize -> deserialize with every field intact.

    A field the serializer forgets is not an error anywhere: the write succeeds,
    the read returns the dataclass default, and the setting quietly reverts on
    the next restart. For a cache flag that means a project silently stops
    caching; for a threshold it means it silently starts matching at a different
    one than the operator set.
    """

    def _round_trip(self, project):
        item = DynamoPersistence.serialize_project(project)
        return DynamoPersistence.deserialize_project(item)

    def test_tenant_id_preserves_the_legacy_positional_constructor(self):
        from src.gateway.models import Project

        project = Project("p1", "P1", 125.0, 0.8, ["claude-sonnet"])

        assert project.tenant_id is None
        assert project.budget_limit == 125.0
        assert project.alert_threshold == 0.8
        assert project.allowed_models == ["claude-sonnet"]

    def test_the_cache_flags_survive(self):
        from src.gateway.models import Project

        project = Project(
            project_id="p1", name="P1",
            cache_enabled=True, cache_ttl_seconds=900,
            semantic_cache_enabled=True, semantic_cache_threshold=0.97,
            prompt_caching_enabled=True,
        )
        back = self._round_trip(project)
        assert back.cache_enabled is True
        assert back.cache_ttl_seconds == 900
        assert back.semantic_cache_enabled is True
        assert back.semantic_cache_threshold == 0.97
        assert back.prompt_caching_enabled is True

    def test_an_unset_threshold_stays_none_rather_than_becoming_zero(self):
        """0.0 would mean "match everything" — the one value that must not appear
        by accident."""
        from src.gateway.models import Project

        back = self._round_trip(Project(project_id="p1", name="P1"))
        assert back.semantic_cache_threshold is None
        assert back.semantic_cache_enabled is False

    def test_a_decimal_threshold_from_dynamo_becomes_a_float(self):
        """DynamoDB returns numbers as Decimal. A Decimal compares correctly but
        does not serialize to JSON, so the admin endpoint would 500 on a project
        loaded from the table while working on one created in-process.
        """
        from decimal import Decimal

        item = DynamoPersistence.serialize_project(
            __import__("src.gateway.models", fromlist=["Project"]).Project(
                project_id="p1", name="P1", semantic_cache_threshold=0.95,
            )
        )
        item["semantic_cache_threshold"] = Decimal("0.95")
        back = DynamoPersistence.deserialize_project(item)
        assert isinstance(back.semantic_cache_threshold, float)
        assert back.semantic_cache_threshold == 0.95

    def test_tenant_project_uses_compound_ownership_key(self):
        from src.gateway.models import Project

        project = Project(
            project_id="shared",
            tenant_id="tenant-a",
            name="Tenant A",
        )

        item = DynamoPersistence.serialize_project(project)
        back = DynamoPersistence.deserialize_project(item)

        assert item["PK"] == "TENANT#tenant-a"
        assert item["SK"] == "PROJECT#shared"
        assert item["tenant_id"] == "tenant-a"
        assert back.tenant_id == "tenant-a"
